"""The dashboard main window: the eight-tab QTabWidget + a score-preview pane and
the global action bar.

The three job tabs (High Score / All Jobs / Tracker) are real `JobsTab`s wired to
the data and registry; Auto-apply mirrors the batch apply queue; Stats / Resume
Data / Apply Answers / Settings are filled in later phases. Long-running actions
(scrape, apply, tailor) run on a worker thread via `qt.workers.run_async`. The
score preview rides in a vertical splitter and is shown only on the job tabs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from PySide6 import QtCore, QtGui, QtWidgets

import apply_queue
import ats_accounts
import chrome
import jobsdata
import osopen
import settings
from csv_io import read_csv_gz, reconcile_is_seen, write_csv_gz_atomic
from jobsdata import (
    ALL_COLUMNS,
    APPDATA,
    HIGH_SCORE_COLUMNS,
    TRACKER_COLUMNS,
    drop_blocklisted,
    filter_high_unseen,
    gdrive_root_dir,
    load_files,
    load_followup_days,
    load_hidden_columns,
    load_local_blocklist,
    load_min_score,
)
from qt import theme, workers
from qt.answers_tab import AnswersEditor
from qt.apply_panel import ApplyPanel
from qt.apply_queue_panel import ApplyQueuePanel
from qt.chrome import ChipBar, IdentityStrip
from qt.detail_card import JobDetailCard
from qt.jobs_tab import JobsTab
from qt.manual_add_dialog import ManualAddDialog
from qt.resume_data_tab import ResumeDataEditor
from qt.settings_tab import SettingsForm
from qt.stats_tab import StatsTab, _human_age
from qt.vm_panel import VMPanel
from resume_trash import recycle_resume_folder
from seen_db import SeenRegistry

TAB_TITLES = [
    "High Score (Unseen)",
    "All Jobs",
    "Tracker",
    "Auto-apply",
    "Stats",
    "Resume Data",
    "Apply Answers",
    "Settings",
]

# Tabs where a selected row has an analysis worth previewing.
PREVIEW_TABS = {"High Score (Unseen)", "All Jobs", "Tracker"}

# Tailoring is parallel (all selected at once). Above this many, warn first — a big
# fan-out means that many simultaneous Gemini calls + pdflatex processes (API limits /
# local load). Below it, just go.
PARALLEL_WARN_THRESHOLD = 5

# Cap on concurrently-running tailor jobs. Uncapped (a 14-job batch = 14
# threads, each making several Gemini calls at once) the batch stampedes the
# per-minute quota: every thread 429s together, retries together, and the
# unlucky tail exhausts its retries and fails. Four keeps the pipeline busy
# while staying under free-tier RPM limits; the rest of the batch queues.
MAX_PARALLEL_TAILORS = 4


def _tailor_pool_size(n_jobs: int) -> int:
    """Worker-thread count for a tailor batch of `n_jobs`."""
    return max(1, min(n_jobs, MAX_PARALLEL_TAILORS))


def _console_python(exe: str | None = None) -> str:
    """The console Python to run child scripts with.

    The dashboard launches under ``pythonw.exe`` (no console window), whose
    ``sys.executable`` is ``pythonw.exe``. A child spawned with that has no real
    stdout, so its output — and any error — vanishes. Swap to the sibling
    ``python.exe`` so the scraper/scorer have capturable stdio.
    """
    exe = exe or sys.executable or "python"
    if exe.lower().endswith("pythonw.exe"):
        cand = exe[: -len("pythonw.exe")] + "python.exe"
        if os.path.exists(cand):
            return cand
    return exe


def _no_window_flag() -> int:
    """CREATE_NO_WINDOW on Windows (don't flash a console for the captured child);
    0 everywhere else."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


class MainWindow(QtWidgets.QMainWindow):
    """Top-level window. `csv_paths` are the scored run files to load."""

    # Tailoring progress, streamed from the worker + thread-pool threads to the UI
    # status bar. A Qt signal so a cross-thread emit is queued onto the UI thread
    # (the ThreadPoolExecutor workers are plain threads — direct widget calls from
    # them would be unsafe).
    tailor_progress = QtCore.Signal(str)
    # One per-job outcome dict ({"id", "label", "dir", "error"}) the moment that
    # job finishes, queued onto the UI thread. The registry is written per job so
    # an interrupted batch (crash, power loss) keeps every result already done —
    # the July 8 batch crash lost all 12 finished resumes because bookkeeping
    # only happened after the WHOLE batch completed.
    tailor_job_done = QtCore.Signal(object)

    def __init__(self, csv_paths: list[Path] | None = None, registry=None,
                 parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.csv_paths: list[Path] = list(csv_paths or [])
        self.registry = registry if registry is not None else SeenRegistry()
        self.setWindowTitle("INployed")
        self.setMinimumSize(1000, 660)

        self.min_score = load_min_score()
        self.followup_days = load_followup_days()
        self.hidden_columns = load_hidden_columns()
        self.df = pd.DataFrame()
        self.df_high = pd.DataFrame()
        self.id_to_path: dict[str, Path] = {}
        self._row_by_id: dict[str, int] = {}
        self._url_by_id: dict[str, str] = {}
        self._tracked: dict[str, dict] = {}
        # Undo stack for mark-seen: each entry is the list of ids a single
        # mark-seen action newly added, so undo reverts exactly that action.
        self._seen_undo: list[list[str]] = []
        self._ui_scale_pct = jobsdata.load_ui_scale_pct()
        self._restart_requested = False  # app.main() relaunches when this is set
        # Async-load state: the first load (and every background refresh) runs off
        # the UI thread so a slow Google Drive mount can't freeze the window.
        # `_loading` guards against overlapping workers; `_reload_pending` remembers
        # a request that arrived mid-load so it runs once the current one finishes.
        self._loading = False
        self._reload_pending = False
        # All source-CSV rewrites (mark-seen / delete) run on this FIFO single-flight
        # background queue so the UI never freezes on a ~27MB gz rewrite and two
        # writes can never interleave on the same files. Registry (SQLite) writes
        # stay on the UI thread — the connection is thread-affine and they're fast.
        self._writes = workers.SerialTaskQueue(self)
        self.tailor_progress.connect(self._set_status)
        self.tailor_job_done.connect(self._on_tailor_job_done)

        self._build()
        self._setup_fs_watcher()
        self._apply_preview_visibility()
        # Data is loaded via start()/reload_data_async AFTER the window is shown, so
        # construction never blocks on the (possibly Drive-backed) source files.

    # ---- construction --------------------------------------------------------

    def _make_jobs_tab(self, key: str, columns) -> JobsTab:
        return JobsTab(
            key, columns,
            on_open_url=self._open_url,
            on_set_status=self._set_status_for,
            on_block=self._block_company,
            on_selection=self._show_preview,
            on_delete=self._delete_jobs,
            on_edit=self._edit_manual_job,
            on_generate_cover=self._generate_cover_for,
            cover_state=self._cover_state,
            on_queue_apply=self._queue_for_auto_apply,
            hidden_columns=self.hidden_columns,
            save_hidden=self._save_hidden,
        )

    def _build_empty_hint(self) -> QtWidgets.QWidget:
        """First-run hint shown on the High Score tab when no jobs are loaded yet."""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.addStretch(1)
        title = QtWidgets.QLabel("No jobs yet")
        title.setProperty("heading", True)
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)
        msg = QtWidgets.QLabel(
            "Get started in three steps: set your keys and folders in Settings, find "
            "new jobs to fetch and score them, then add your résumé data so jobs "
            "are matched to you.")
        msg.setWordWrap(True)
        msg.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        msg.setProperty("muted", True)
        v.addWidget(msg)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        b_settings = QtWidgets.QPushButton("Open Settings")
        b_settings.clicked.connect(lambda: self._show_tab("Settings"))
        row.addWidget(b_settings)
        b_scrape = QtWidgets.QPushButton("Find new jobs")
        b_scrape.setProperty("accent", True)
        b_scrape.clicked.connect(self._run_scraper_dialog)
        row.addWidget(b_scrape)
        b_resume = QtWidgets.QPushButton("Set up Resume Data")
        b_resume.clicked.connect(lambda: self._show_tab("Resume Data"))
        row.addWidget(b_resume)
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)
        return w

    def _show_tab(self, title: str) -> None:
        page = self._tab_widgets.get(title)
        if page is not None:
            self.tabs.setCurrentWidget(page)

    # ---- interface scaling (bottom scale bar) --------------------------------

    def _build_scale_bar(self) -> QtWidgets.QWidget:
        """The persistent 'Interface size' control (part of the bottom action bar):
        -/+ buttons (10% steps) and a slider (50-200%). All drive `_apply_scale`,
        which re-scales the live UI immediately."""
        bar = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(QtWidgets.QLabel("Interface size"))
        minus = QtWidgets.QPushButton("-")
        minus.setFixedWidth(26)
        minus.setToolTip("Smaller (-10%)")
        minus.clicked.connect(lambda: self._nudge_scale(-10))
        h.addWidget(minus)
        self._scale_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._scale_slider.setMinimum(75)
        self._scale_slider.setMaximum(150)
        self._scale_slider.setSingleStep(10)
        self._scale_slider.setPageStep(10)
        self._scale_slider.setFixedWidth(140)
        self._scale_slider.setValue(self._ui_scale_pct)
        self._scale_slider.valueChanged.connect(self._on_scale_slider)
        h.addWidget(self._scale_slider)
        plus = QtWidgets.QPushButton("+")
        plus.setFixedWidth(26)
        plus.setToolTip("Larger (+10%)")
        plus.clicked.connect(lambda: self._nudge_scale(10))
        h.addWidget(plus)
        self._scale_readout = QtWidgets.QLabel(f"{self._ui_scale_pct}%")
        self._scale_readout.setMinimumWidth(38)
        h.addWidget(self._scale_readout)
        # A short debounce so dragging the slider stays smooth (apply once it settles).
        self._scale_debounce = QtCore.QTimer(self)
        self._scale_debounce.setSingleShot(True)
        self._scale_debounce.setInterval(60)
        self._scale_debounce.timeout.connect(lambda: self._apply_scale(self._scale_slider.value()))
        return bar

    def _on_scale_slider(self, value: int) -> None:
        # Snap to 10% steps, show the live %, and apply after a brief settle.
        snapped = max(75, min(150, round(value / 10) * 10))
        if snapped != value:
            self._scale_slider.blockSignals(True)
            self._scale_slider.setValue(snapped)
            self._scale_slider.blockSignals(False)
        self._scale_readout.setText(f"{snapped}%")
        self._scale_debounce.start()

    def _nudge_scale(self, delta: int) -> None:
        self._apply_scale(self._ui_scale_pct + delta)

    def _apply_scale(self, pct: int) -> None:
        """Clamp to [50, 200], re-scale the live UI (font only — fast), sync the bar,
        and persist the choice via jobsdata."""
        pct = max(75, min(150, int(pct)))
        self._ui_scale_pct = pct
        theme.set_scale(QtWidgets.QApplication.instance(), pct / 100.0)
        if hasattr(self, "_scale_slider"):
            self._scale_slider.blockSignals(True)
            self._scale_slider.setValue(pct)
            self._scale_slider.blockSignals(False)
            self._scale_readout.setText(f"{pct}%")
        try:
            jobsdata.save_ui_scale_pct(pct)
        except OSError:
            pass  # a failed persist must never break live scaling

    def _restart_app(self) -> None:
        """Close and reopen the dashboard. We only flag the intent and close the
        window here; `app.main()` relaunches a fresh process once this one has fully
        exited, so the single-instance lock is released before the new one starts."""
        resp = QtWidgets.QMessageBox.question(
            self, "Restart INployed", "Close and reopen the dashboard now?")
        if resp != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._restart_requested = True
        self.close()

    def _setup_zoom_shortcuts(self) -> None:
        """Ctrl++ / Ctrl+- step the interface size by 10%; Ctrl+0 resets to 100%.
        They drive the same `_apply_scale` as the bottom bar. Several +/- spellings
        are bound because the zoom-in key needs Shift on many layouts."""
        def shortcut(seq, slot):
            QtGui.QShortcut(QtGui.QKeySequence(seq), self, activated=slot)

        for seq in ("Ctrl++", "Ctrl+="):           # zoom in (= shares the + key)
            shortcut(seq, lambda: self._nudge_scale(10))
        shortcut("Ctrl+-", lambda: self._nudge_scale(-10))   # zoom out
        shortcut("Ctrl+0", lambda: self._apply_scale(100))   # reset

    def _build(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        vbox = QtWidgets.QVBoxLayout(central)
        vbox.setContentsMargins(8, 8, 8, 8)

        # Identity strip: wordmark + freshness pill + jobs/unseen/tracked counts.
        self.identity_strip = IdentityStrip()
        vbox.addWidget(self.identity_strip)
        self._last_run_label = ""  # freshness text shared with the status bar

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)

        self.high_tab = self._make_jobs_tab("high", HIGH_SCORE_COLUMNS)
        self.all_tab = self._make_jobs_tab("all", ALL_COLUMNS)
        self.tracker_tab = self._make_jobs_tab("tracker", TRACKER_COLUMNS)
        self._setup_tracker_toolbar()
        # "Add job by hand" lives on the discovery tabs (High Score / All Jobs):
        # a job added there is scored + tailored exactly like a scraped one.
        self.high_tab.add_toolbar_button("Add job by hand", self._add_manual_job_dialog)
        self.all_tab.add_toolbar_button("Add job by hand", self._add_manual_job_dialog)
        self.stats_tab = StatsTab()
        self.settings_tab = SettingsForm(on_saved=self._on_settings_saved,
                                         vm_panel_factory=self._make_vm_panel)
        self.resume_data_tab = ResumeDataEditor()
        self.answers_tab = AnswersEditor()
        # The Auto-apply tab mirrors the batch apply queue (SP3). Its mutations
        # ride the background write queue via _submit_queue_write; the queue
        # path is resolved by apply_queue at call time (APPLY_QUEUE_PATH-aware).
        self.apply_queue_panel = ApplyQueuePanel(
            submit_write=self._submit_queue_write,
            on_set_password=self._set_ats_password,
            on_mark_applied=self._apply_queue_mark_applied,
            on_mark_seen=self._apply_queue_mark_seen,
            on_answer_now=lambda: self._show_tab("Apply Answers"))
        self._tab_widgets: dict[str, QtWidgets.QWidget] = {}
        pages = {"High Score (Unseen)": self.high_tab, "All Jobs": self.all_tab,
                 "Tracker": self.tracker_tab, "Auto-apply": self.apply_queue_panel,
                 "Stats": self.stats_tab,
                 "Resume Data": self.resume_data_tab, "Apply Answers": self.answers_tab,
                 "Settings": self.settings_tab}
        for title in TAB_TITLES:
            page = pages.get(title) or QtWidgets.QWidget()
            self._tab_widgets[title] = page
            self.tabs.addTab(page, title)

        self.high_tab.set_empty_widget(self._build_empty_hint())

        # The job detail card replaces the old ScorePreview — same `self.preview`
        # attribute so the splitter wiring + _apply_preview_visibility stand.
        # The card OWNS the Tailor/Apply buttons now; alias them so every
        # existing enable/repolish path keeps the same object identities.
        self.preview = JobDetailCard(
            on_open=self._open_url,
            on_tailor=self._tailor_selected,
            on_apply=self._apply_selected,
            on_open_resume=self._open_resume_folder,
            on_followed_up=self._tracker_followed_up)
        self.btn_tailor = self.preview.tailor_btn
        self.btn_apply = self.preview.apply_btn
        self.btn_apply.setEnabled(False)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.splitter.addWidget(self.tabs)
        self.splitter.addWidget(self.preview)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([640, 300])  # the detail card needs ~300px @100%

        # The Apply panel rides to the RIGHT of the tabs+preview column; it opens
        # (and the preview hides) when the user clicks Apply, and closes back to the
        # preview via its own ✕. Hidden until then.
        self._apply_panel_open = False
        self._apply_panel_job: dict = {}
        self.apply_panel = ApplyPanel(on_close=self._close_apply_panel,
                                      on_applied=self._mark_applied_from_panel)
        self.apply_panel.hide()
        self.hsplit = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.hsplit.addWidget(self.splitter)
        self.hsplit.addWidget(self.apply_panel)
        self.hsplit.setStretchFactor(0, 1)
        self.hsplit.setStretchFactor(1, 0)
        vbox.addWidget(self.hsplit, 1)
        # Connect only now that self.preview exists (addTab above fires currentChanged).
        self.tabs.currentChanged.connect(lambda _i: self._on_tab_changed())

        vbox.addLayout(self._build_action_bar())
        self._setup_zoom_shortcuts()  # Ctrl +/-/0 mirror the bottom scale bar
        # The status bar is just the transient message line now (the interface-size
        # control moved up into the single bottom action bar).
        self.setStatusBar(QtWidgets.QStatusBar())

    def _build_action_bar(self) -> QtWidgets.QHBoxLayout:
        bar = QtWidgets.QHBoxLayout()
        tip = QtWidgets.QLabel("Ctrl/Shift-click for multiple · Ctrl+A selects all · "
                               "double-click opens · right-click for status (incl. applied) / block")
        tip.setProperty("muted", True)
        bar.addWidget(tip)
        bar.addStretch(1)

        def button(text, slot, accent=False):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            if accent:
                b.setProperty("accent", True)
            bar.addWidget(b)
            return b

        # Tailor/Apply live on the job detail card now (Phase 3c) — the bar keeps
        # the selection-utility actions, with Find new jobs as its primary.
        button("Mark seen (selected)", self._mark_seen_selected)
        self.btn_undo_seen = button("Undo seen", self._undo_seen)
        button("Resume folder", self._open_resume_folder)
        button("Check setup", self._check_setup)
        self.btn_queue_apply = button("Queue auto-apply", self._queue_apply_selected)
        self.btn_queue_apply.setToolTip(
            "Add the selected job(s) to the auto-apply queue (untailored ones are "
            "tailored first) — see the Auto-apply tab")
        button("Find new jobs", self._run_scraper_dialog, accent=True)

        # One bottom panel: the interface-size control and a Restart button ride in
        # the same bar as the actions (they used to be a separate status-bar strip).
        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        sep.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        bar.addWidget(sep)
        bar.addWidget(self._build_scale_bar())
        self.btn_restart = button("Restart", self._restart_app)
        self.btn_restart.setProperty("tier", "tertiary")
        self.btn_restart.setToolTip("Close and reopen the dashboard")

        self._action_bar = bar
        self._update_seen_buttons()
        return bar

    def _setup_tracker_toolbar(self) -> None:
        """Tracker-only controls added to that tab's filter bar."""
        self.tracker_due_only = QtWidgets.QCheckBox("Follow-up due only")
        self.tracker_due_only.stateChanged.connect(lambda _s: self._refresh_tracker())
        # It's a filter, so it lives in the Filters popup (and counts toward the badge).
        self.tracker_tab.add_filter_row(
            self.tracker_due_only, is_active=self.tracker_due_only.isChecked)
        # Pipeline chip bar (Phase 3d): exclusive status chips below the filter
        # bar. "Follow-up due" PROXIES the (test-coupled) popup checkbox above.
        self._tracker_status_filter = "all"
        self.tracker_chips = ChipBar(
            [("all", "All", None),
             ("applied", "Applied", theme.SEMANTICS["accent"]["base"]),
             ("interviewing", "Interviewing", theme.SEMANTICS["warning"]["base"]),
             ("offer", "Offer", theme.SEMANTICS["success"]["base"]),
             ("rejected", "Rejected", theme.SEMANTICS["danger"]["base"]),
             ("due", "Follow-up due", theme.SEMANTICS["followup"]["base"])],
            on_change=self._on_tracker_chip)
        self.tracker_chips.set_checked("all")
        self.tracker_tab.layout().insertWidget(1, self.tracker_chips)
        # Set status lives on the right-click menu (it was redundant as a button here).
        self.tracker_tab.add_toolbar_button("Mark followed up", self._tracker_followed_up)
        self.tracker_tab.add_toolbar_button("Interview prep", self._tracker_prep)
        self.tracker_tab.add_toolbar_button("Remove", self._tracker_remove)
        self.tracker_tab.add_toolbar_button("Export tracker…", self._export_tracker)
        self.tracker_tab.add_toolbar_button("Import tracker…", self._import_tracker)

    # ---- data ----------------------------------------------------------------

    def start(self) -> None:
        """Kick off the first data load. Call this AFTER showing the window so it
        paints immediately; the load itself then runs off the UI thread."""
        self._reconcile_orphaned_tailors()
        self.reload_data_async()

    def _reconcile_orphaned_tailors(self) -> int:
        """Adopt tailor runs whose UI-thread finalize was lost.

        A queue-chained tailor writes its résumé folder on a worker thread and
        only records the résumé (registry) + flips the entry tailoring -> queued
        with its artifact paths back on the UI thread, in `_finish_tailor` /
        `_finish_queue_tailor`. If the window closes (or otherwise exits) before
        that `finished` signal is delivered, the folder is complete on disk but
        the entry is stranded: either at "tailoring", or — if the user hit
        Re-queue on it — "queued" yet still artifact-less (claimable with NO
        résumé to upload). In both cases the résumé is unrecorded, so the job
        never tints blue.

        On launch this heals every such entry: a "tailoring" or "queued" entry
        with an EMPTY folder artifact whose canonical folder is complete on disk
        gets exactly what the lost callback would have done — record_resume +
        set_artifacts (which also flips "tailoring" -> "queued"). Healthy
        entries (folder already linked) and terminal ones are left untouched.
        Best-effort: a bad/locked queue or half-written folder is skipped."""
        from resume_tailor import output
        try:
            data = apply_queue.load()
        except Exception:  # noqa: BLE001 - a bad/locked queue must not break startup
            return 0
        healed = 0
        for e in data.get("jobs", []):
            if not isinstance(e, dict) or e.get("status") not in ("tailoring", "queued"):
                continue
            if (e.get("artifacts") or {}).get("folder"):
                continue   # already linked to a folder — healthy, leave it
            jid = str(e.get("job_posting_id") or "")
            company = str(e.get("company") or "")
            title = str(e.get("title") or "")
            if not jid or not (company or title):
                continue
            try:
                folder = output.base_dir(company, title)
                complete = (folder.is_dir()
                            and (folder / output.resume_filename()).exists()
                            and (folder / "apply.md").exists())
            except OSError:
                complete = False
            if not complete:
                continue   # still tailoring, or genuinely never finished — leave it
            try:
                self.registry.record_resume(jid, str(folder))
            except Exception:  # noqa: BLE001 - bookkeeping only (mirrors _finish_tailor)
                pass
            arts = self._queue_artifacts(folder)
            self._submit_queue_write(
                lambda jid=jid, arts=arts: apply_queue.set_artifacts(jid, arts))
            healed += 1
        if healed:
            self._set_status(
                f"Recovered {healed} tailored job(s) that didn't finish saving "
                "last session — now queued for auto-apply.")
            panel = getattr(self, "apply_queue_panel", None)
            if panel is not None:
                panel.refresh()
        return healed

    def _load_frames(self):
        """The blocking half of a reload, safe to run OFF the UI thread: read and
        merge the source files and drop blocklisted rows. Touches neither Qt nor the
        SQLite registry (both thread-affine) — those wait for _apply_frames."""
        df, id_to_path = load_files(self.csv_paths)
        df = drop_blocklisted(df, load_local_blocklist(self.csv_paths))
        return df, id_to_path

    def _apply_frames(self, loaded) -> None:
        """The UI-thread half of a reload: overlay the seen registry, install the
        frame, and refresh every derived view."""
        df, id_to_path = loaded
        self.id_to_path = id_to_path
        if not df.empty:
            if "is_seen" not in df.columns:
                df["is_seen"] = "no"
            df, _ = reconcile_is_seen(df, self.registry)
        self.df = df
        self._apply_df_views()
        self._set_status(self._summary_line())

    def _summary_line(self) -> str:
        """The persistent status-bar summary: counts + discovery freshness."""
        total = 0 if self.df.empty else len(self.df)
        parts = [f"{total:,} jobs", f"{len(self.df_high)} unseen ≥ {self.min_score}"]
        if self._last_run_label:
            parts.append(f"last discovery run {self._last_run_label}")
        return " · ".join(parts)

    def _update_identity_counts(self) -> None:
        strip = getattr(self, "identity_strip", None)
        if strip is None:
            return
        total = 0 if self.df.empty else len(self.df)
        strip.set_counts(total, len(self.df_high), len(self._tracked))

    def _apply_df_views(self) -> None:
        """Refresh everything derived from the in-memory `self.df` — row/url maps,
        the high-score view, both job tabs, tracker/stats, the Apply button, and the
        fs watcher. Zero disk I/O: the optimistic mark-seen/delete paths mutate
        `self.df` and call this for an instant repaint while the CSV rewrite runs
        on the background write queue."""
        df = self.df
        self._row_by_id = ({jid: i for i, jid in enumerate(df["job_posting_id"])}
                           if not df.empty else {})
        self._url_by_id = (dict(zip(df["job_posting_id"].astype(str), df["url"].astype(str)))
                           if not df.empty and "url" in df.columns else {})
        self.df_high = filter_high_unseen(df, self.min_score)
        resume_ids = self._resume_ids()
        failed_ids = self._tailor_failure_ids()
        self.high_tab.set_source_df(self.df_high, resume_ids, failed_ids)
        self.all_tab.set_source_df(df, resume_ids, failed_ids)
        self._refresh_tracker()
        self._refresh_stats()
        self._refresh_apply_button()  # a freshly tailored job may now be apply-ready
        # Sources/folder may have changed (a local scrape appended paths) — keep the
        # auto-refresh watcher pointed at the current files. No-op before setup.
        if getattr(self, "_fs_watcher", None) is not None:
            self._rearm_watcher()

    def reload_data(self) -> None:
        """Synchronous load + apply. Kept for tests and for the post-action
        refreshes (scrape / manual-add / tailor) that already run after a worker
        finishes; startup and the background watcher/poll use reload_data_async."""
        self._apply_frames(self._load_frames())

    def reload_data_async(self) -> None:
        """Load off the UI thread, then apply on it — so a cold/slow source mount
        keeps the window responsive instead of freezing it. Overlapping calls
        coalesce into a single trailing reload."""
        if self._loading:
            self._reload_pending = True
            return
        # Nothing on disk to read yet → apply synchronously (instant, empty) rather
        # than spin up a worker; this also keeps the test suite thread-free.
        if not any(Path(p).exists() for p in self.csv_paths):
            self._apply_frames(self._load_frames())
            return
        self._loading = True
        self._reload_pending = False
        self._set_status("Loading jobs …")
        workers.run_async(self, self._load_frames,
                          on_done=self._on_frames_loaded,
                          on_error=self._on_load_error)

    def _on_frames_loaded(self, loaded) -> None:
        self._loading = False
        self._apply_frames(loaded)
        if self._reload_pending:
            self.reload_data_async()

    def _on_load_error(self, exc: BaseException) -> None:
        # A load failure must never kill the window; surface it and stay usable.
        self._loading = False
        self._set_status(f"Could not load jobs: {exc}")
        if self._reload_pending:
            self.reload_data_async()

    def _refresh_tracker(self) -> None:
        rows = self.registry.status_rows()
        self._tracked = {r["job_posting_id"]: r for r in rows}
        rpaths = self._resume_ids()   # tracker ✓ also follows on-disk existence
        today = date.today()
        recs: list[dict] = []
        for r in rows:
            jid = r["job_posting_id"]
            row = self._row_for(jid)
            days = ""
            days_n = None
            if r.get("applied_date"):
                try:
                    days_n = (today - date.fromisoformat(r["applied_date"])).days
                    days = str(days_n)
                except ValueError:
                    pass
            follow = ""
            if r.get("followed_up_at"):
                follow = "done"
            elif (r["status"] == "applied" and days_n is not None
                  and days_n >= self.followup_days):
                follow = "DUE"
            recs.append({
                "job_posting_id": jid,
                "status": r["status"],
                "status_date": r.get("status_date") or "",
                "applied_date": r.get("applied_date") or "",
                "days": days,
                "follow_up": follow,
                "score": self._cell(row, "score"),
                "deep_score": self._cell(row, "deep_score"),
                "job_title": r.get("job_title") or self._cell(row, "job_title"),
                "company_name": r.get("company") or self._cell(row, "company_name"),
                "url": r.get("url") or self._cell(row, "url"),
                "resume": "✓" if jid in rpaths else "",
            })
        # Pipeline chip counts come from the UNFILTERED recs (each chip shows
        # its full bucket size, whatever is currently selected).
        counts = Counter(r["status"] for r in recs)
        chip_counts = {"all": len(recs),
                       "due": sum(1 for r in recs if r["follow_up"] == "DUE")}
        for key in ("applied", "interviewing", "offer", "rejected"):
            chip_counts[key] = counts.get(key, 0)
        status_f = getattr(self, "_tracker_status_filter", "all")
        if status_f != "all":
            recs = [r for r in recs if r["status"] == status_f]
        if getattr(self, "tracker_due_only", None) is not None and self.tracker_due_only.isChecked():
            recs = [r for r in recs if r["follow_up"] == "DUE"]
        chips = getattr(self, "tracker_chips", None)
        if chips is not None:
            chips.set_counts(chip_counts)
            self._sync_tracker_chip_selection()
        cols = [c for c, _ in TRACKER_COLUMNS] + ["job_posting_id"]
        tdf = pd.DataFrame(recs) if recs else pd.DataFrame(columns=cols)
        self.tracker_tab.set_source_df(tdf, self._resume_ids())
        self._update_identity_counts()

    def _on_tracker_chip(self, key: str) -> None:
        """A pipeline chip was clicked. Status chips drive `_tracker_status_filter`;
        the "Follow-up due" chip proxies the (test-coupled) `tracker_due_only`
        checkbox in the Filters popup — the checkbox stays the single source of
        truth for the due-only filter."""
        self._tracker_status_filter = (
            key if key in ("applied", "interviewing", "offer", "rejected") else "all")
        want_due = key == "due"
        if self.tracker_due_only.isChecked() != want_due:
            self.tracker_due_only.setChecked(want_due)  # fires _refresh_tracker
        else:
            self._refresh_tracker()

    def _sync_tracker_chip_selection(self) -> None:
        """Mirror the live filter state back onto the chips (e.g. the user
        toggled the due-only checkbox directly in the Filters popup)."""
        chips = getattr(self, "tracker_chips", None)
        if chips is None:
            return
        if self.tracker_due_only.isChecked():
            key = "due"
        else:
            status_f = getattr(self, "_tracker_status_filter", "all")
            key = status_f if status_f != "all" else "all"
        chips.set_checked(key)   # silent — never re-fires _on_tracker_chip

    def _resume_ids(self) -> frozenset:
        # Only ids whose tailored folder still EXISTS on disk are tinted blue, so a
        # folder deleted by hand drops its tint on the next reload (jobsdata keeps
        # the registry row — the tint returns if the folder comes back).
        try:
            return frozenset(jobsdata.live_resume_ids(self.registry.resume_paths()))
        except Exception:  # noqa: BLE001 - cosmetic; never break the view
            return frozenset()

    def _tailor_failure_ids(self) -> frozenset:
        # Jobs whose most recent tailor run failed — tinted red ("re-run me")
        # until a later run succeeds (record_resume clears the flag).
        try:
            return frozenset(self.registry.tailor_failure_ids())
        except Exception:  # noqa: BLE001 - cosmetic; never break the view
            return frozenset()

    # ---- auto-refresh on file change -----------------------------------------

    def _setup_fs_watcher(self) -> None:
        """Refresh the dashboard automatically when its source CSVs change — there
        is no manual Refresh button. A QFileSystemWatcher reacts instantly when the
        OS emits file events (Drive mirror mode, a local scrape); a slower mtime
        poll is the fallback for setups that emit none (Drive streaming mode), so
        the dashboard always catches up on its own."""
        self._fs_watcher = QtCore.QFileSystemWatcher(self)
        self._reload_timer = QtCore.QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(1500)  # debounce a burst of sync writes
        self._reload_timer.timeout.connect(self._auto_reload)
        self._fs_watcher.fileChanged.connect(self._on_fs_change)
        self._fs_watcher.directoryChanged.connect(self._on_fs_change)
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(15000)  # fallback when no file events arrive
        self._poll_timer.timeout.connect(self._poll_for_changes)
        self._poll_timer.start()
        self._rearm_watcher()

    def _rearm_watcher(self) -> None:
        """Re-point the watcher at the current files + folder and re-snapshot their
        signature. Needed after every load because an atomic replace (how Drive/
        score writes land) drops the old path from the watch list."""
        w = self._fs_watcher
        if w.files():
            w.removePaths(w.files())
        if w.directories():
            w.removePaths(w.directories())
        paths = [str(p) for p in self.csv_paths if Path(p).exists()]
        root = gdrive_root_dir(self.csv_paths)
        if root and root.exists():
            paths.append(str(root))
        if paths:
            w.addPaths(paths)
        self._source_sig = self._current_sig()

    def _current_sig(self) -> tuple:
        """A cheap (path, mtime, size) signature of the source files, so the poll
        fallback reloads only when something actually changed on disk."""
        sig = []
        for p in self.csv_paths:
            try:
                st = os.stat(p)
                sig.append((str(p), st.st_mtime_ns, st.st_size))
            except OSError:
                continue
        return tuple(sig)

    def _poll_for_changes(self) -> None:
        if not self._writes.is_idle():
            return  # our own background rewrite is in flight (see _on_fs_change)
        if self._current_sig() != self._source_sig:
            self.reload_data_async()  # re-snapshots the signature via _rearm_watcher

    def _on_fs_change(self, _path: str) -> None:
        # While one of OUR background rewrites is in flight, ignore fs events: a
        # >1.5s gap between its per-file replaces would otherwise fire the debounce
        # mid-write and reload half-old data (e.g. resurrect just-deleted rows) —
        # and the write-done re-snapshot would then keep that stale view. A real
        # Drive sync landing in this window is caught by the next 15s poll.
        if not self._writes.is_idle():
            return
        self._reload_timer.start()  # coalesce a flurry of events into one reload

    def _auto_reload(self) -> None:
        self.reload_data_async()

    # ---- row helpers ---------------------------------------------------------

    def _row_for(self, jid: str):
        i = self._row_by_id.get(jid)
        if i is None or self.df.empty:
            return None
        try:
            return self.df.iloc[i]
        except (IndexError, KeyError):
            return None

    @staticmethod
    def _cell(row, col: str) -> str:
        if row is None:
            return ""
        v = row.get(col, "")
        return "" if pd.isna(v) else str(v)

    def _job_payload(self, jid: str) -> dict | None:
        row = self._row_for(jid)
        if row is None:
            return None
        return {
            "job_posting_id": jid,
            "company_name": self._cell(row, "company_name"),
            "job_title": self._cell(row, "job_title"),
            "job_description_formatted": self._cell(row, "job_description_formatted"),
            "job_description": self._cell(row, "job_description"),
            "job_summary": self._cell(row, "job_summary"),
            "url": self._cell(row, "url"),
        }

    def _active_jobs_tab(self) -> JobsTab | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, JobsTab) else None

    def _selected_ids(self) -> list[str]:
        tab = self._active_jobs_tab()
        return tab.selected_ids() if tab else []

    # ---- preview -------------------------------------------------------------

    def _on_tab_changed(self) -> None:
        self._apply_preview_visibility()
        # Re-render the detail card from the NEW tab's selection so its variant
        # (discovery vs tracker) always matches the tab it is shown under; this
        # also recomputes the Apply button state (_show_preview does both).
        tab = self._active_jobs_tab()
        ids = tab.selected_ids() if tab is not None else []
        self._show_preview(ids[0] if ids else "")
        # vm_enabled may have changed in Settings — re-evaluate the resume.md push button.
        self.resume_data_tab._refresh_push_state()

    def _apply_preview_visibility(self) -> None:
        title = self.tabs.tabText(self.tabs.currentIndex())
        show = title in PREVIEW_TABS and not getattr(self, "_apply_panel_open", False)
        self.preview.setVisible(show)
        self._preview_shown = show

    def _show_preview(self, jid: str) -> None:
        self._update_apply_button(jid)
        if not jid:
            self.preview.set_empty()
            return
        fields = jobsdata.job_detail_fields(self._row_for(jid), self._tracked.get(jid))
        tracker = (self._tracker_card_info(jid)
                   if self.tabs.currentWidget() is self.tracker_tab else None)
        self.preview.set_fields(fields, jid=jid, tracker=tracker)

    def _tracker_card_info(self, jid: str) -> dict | None:
        """The tracker-variant card data for one tracked job: status, days since
        applying, follow-up state, and a synthesized NEXT STEP line."""
        r = self._tracked.get(jid)
        if r is None:
            return None
        status = str(r.get("status") or "")
        days_n = None
        if r.get("applied_date"):
            try:
                days_n = (date.today() - date.fromisoformat(r["applied_date"])).days
            except ValueError:
                pass
        follow = ""
        if r.get("followed_up_at"):
            follow = "done"
        elif status == "applied" and days_n is not None and days_n >= self.followup_days:
            follow = "DUE"
        if follow == "DUE":
            next_step = (f"No reply in {days_n} day(s) — send a short follow-up "
                         "note, then Mark followed up.")
        elif follow == "done":
            next_step = "Followed up — awaiting a reply."
        elif status == "interviewing":
            next_step = "Interview ahead — generate an Interview prep sheet."
        elif status == "offer":
            next_step = "Offer open — respond and update the status."
        elif status == "rejected":
            next_step = "Rejected — no action needed."
        elif status == "applied" and days_n is not None:
            wait = max(0, self.followup_days - days_n)
            next_step = (f"Applied {days_n} day(s) ago — follow up in {wait} "
                         "day(s) if there is no reply.")
        else:
            next_step = ""
        return {"status": status, "applied_date": r.get("applied_date") or "",
                "days": "" if days_n is None else str(days_n),
                "follow_up": follow, "next_step": next_step}

    # ---- apply readiness (button enable + green) -----------------------------

    def _apply_ready(self, jid: str) -> tuple[bool, Path | None]:
        """A job is ready to apply to when its tailored folder holds BOTH the
        résumé PDF and apply.md on disk. Returns (ready, folder)."""
        if not jid:
            return False, None
        try:
            path = self.registry.resume_path(jid)
        except Exception:  # noqa: BLE001 - cosmetic; never break the view
            return False, None
        if not path:
            return False, None
        from resume_tailor import output
        folder = Path(str(path))
        try:
            ok = (folder.is_dir() and (folder / output.resume_filename()).exists()
                  and (folder / "apply.md").exists())
        except OSError:
            ok = False
        return (True, folder) if ok else (False, None)

    def _update_apply_button(self, jid: str) -> None:
        btn = getattr(self, "btn_apply", None)
        if btn is None:
            return
        ready, _ = self._apply_ready(jid)
        btn.setEnabled(ready)
        if btn.property("applyReady") != ready:
            btn.setProperty("applyReady", ready)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _refresh_apply_button(self) -> None:
        """Recompute the Apply button's state for the focused job of the active tab."""
        tab = self._active_jobs_tab()
        ids = tab.selected_ids() if tab is not None else []
        self._update_apply_button(ids[0] if ids else "")

    # ---- background source-CSV writes (the queue in self._writes) -------------

    def _enqueue_write(self, fn, *, description: str, on_done=None) -> None:
        """Run a source-CSV rewrite on the background write queue.

        On completion (UI thread) the self-write feedback loop is muted — the
        rewrite fires the QFileSystemWatcher and shifts the mtime signature, which
        would otherwise trigger a pointless full reload of data we already show.
        On error, disk is truth: warn and resync with a full reload."""
        def done(result) -> None:
            self._suppress_self_write_events()
            if on_done is not None:
                on_done(result)

        def error(exc: BaseException) -> None:
            self._on_write_error(description, exc)

        self._writes.submit(fn, on_done=done, on_error=error)

    def _suppress_self_write_events(self) -> None:
        """Mute the fs-watcher/poll reactions to our OWN just-finished rewrite:
        cancel the debounced reload it scheduled, re-add the watched paths the
        atomic replace dropped, and re-snapshot the mtime signature so the 15s
        poll stays quiet. (A real Drive sync landing inside this exact window is
        picked up by the NEXT poll — accepted tradeoff.)"""
        if getattr(self, "_reload_timer", None) is not None:
            self._reload_timer.stop()
        if getattr(self, "_fs_watcher", None) is not None:
            self._rearm_watcher()   # also re-snapshots _source_sig

    def _on_write_error(self, description: str, exc: BaseException) -> None:
        """A background write failed — the in-memory (optimistic) state may now
        disagree with the files. Disk is truth: tell the user and reload."""
        if getattr(self, "_reload_timer", None) is not None:
            self._reload_timer.stop()
        QtWidgets.QMessageBox.warning(
            self, "Background write failed",
            f"Could not update the job files ({description}): {exc}\n\n"
            "Reloading the dashboard from disk.")
        self.reload_data()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 - Qt override
        """Flush pending work before the window goes away.

        Two things can be mid-flight at close: a résumé tailoring run (on a
        worker thread — its finalize records the résumé + flips the queue entry
        to "queued", and closing before that `finished` signal is delivered
        strands it) and queued background CSV/queue writes. Wait for the tailor
        first (its finalize enqueues writes), then drain the write queue."""
        if self._scrape_in_flight():
            resp = QtWidgets.QMessageBox.question(
                self, "Job search in progress",
                "A job search is still running. Closing the dashboard now "
                "disconnects it: any jobs it collects won't be scored or shown "
                "until you accept the recovery prompt on the next launch.\n\n"
                "Close anyway?",
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Cancel)
            if resp != QtWidgets.QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if self._tailor_in_flight():
            resp = QtWidgets.QMessageBox.question(
                self, "Tailoring in progress",
                "A résumé tailoring run is still finishing.\n\n"
                "Wait for it to save before closing? If you close now it is "
                "recovered automatically on the next launch.",
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No
                | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Yes)
            if resp == QtWidgets.QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if resp == QtWidgets.QMessageBox.StandardButton.Yes:
                self._set_status("Finishing résumé tailoring …")
                if not self._await_tailor():
                    QtWidgets.QMessageBox.warning(
                        self, "Tailoring still running",
                        "The tailoring run didn't finish in time — it will be "
                        "recovered on the next launch.")
        q = getattr(self, "_writes", None)
        if q is not None and not q.is_idle():
            self._set_status("Finishing background writes …")
            if not q.drain(timeout_ms=30000):
                QtWidgets.QMessageBox.warning(
                    self, "Writes still pending",
                    f"{q.pending_count()} background write(s) did not finish — the "
                    "files on disk may be missing your last mark-seen/delete.")
        super().closeEvent(event)

    def _scrape_in_flight(self) -> bool:
        """True only when a scrape/score run is genuinely still executing — the
        `_scraping` flag AND a live background thread, same gating rationale as
        `_tailor_in_flight` (the flag alone can linger set)."""
        if not getattr(self, "_scraping", False):
            return False
        for thread, _worker in list(getattr(self, "_bg_threads", []) or []):
            try:
                if thread.isRunning():
                    return True
            except RuntimeError:   # the C++ QThread is already gone
                pass
        return False

    def _tailor_in_flight(self) -> bool:
        """True only when a tailor run is genuinely still executing — the
        `_tailoring` flag AND a live background thread. The flag alone can linger
        set (e.g. if a launch stub never spawns a real worker), so closeEvent
        gates its "wait for tailoring?" prompt on this to avoid blocking on a
        run that isn't actually happening."""
        if not getattr(self, "_tailoring", False):
            return False
        for thread, _worker in list(getattr(self, "_bg_threads", []) or []):
            try:
                if thread.isRunning():
                    return True
            except RuntimeError:   # the C++ QThread is already gone
                pass
        return False

    def _await_tailor(self, timeout_ms: int = 120000) -> bool:
        """Pump the UI event loop until an in-flight tailor run's finalize has
        executed (it clears `_tailoring` in `_finish_tailor`) or the timeout
        elapses. The tailor runs on its own QThread and progresses on its own;
        pumping here only delivers its queued `finished` signal so
        `_finish_tailor` / `_finish_queue_tailor` run on the UI thread BEFORE we
        tear it down. Returns True once no tailor is in flight."""
        app = QtWidgets.QApplication.instance()
        deadline = time.monotonic() + timeout_ms / 1000.0
        while getattr(self, "_tailoring", False):
            if time.monotonic() > deadline:
                return False
            if app is None:
                break
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
        return not getattr(self, "_tailoring", False)

    # ---- mark seen (with undo / redo) ----------------------------------------

    def _write_is_seen(self, ids: list[str], value: str, paths=None) -> None:
        """Set is_seen=`value` for `ids` in whichever source CSV(s) hold them.
        Runs on the write queue's worker thread — `paths` is snapshotted on the
        UI thread at enqueue time so this never reads the mutable id_to_path map."""
        idset = set(ids)
        if paths is None:
            paths = {self.id_to_path[i] for i in ids if i in self.id_to_path}
        for path in paths:
            try:
                df = read_csv_gz(path)
                df["job_posting_id"] = df["job_posting_id"].astype(str)
                mask = df["job_posting_id"].isin(idset)
                if mask.any():
                    df.loc[mask, "is_seen"] = value
                    write_csv_gz_atomic(df, path)
            except (OSError, ValueError):
                pass

    def _apply_seen_locally(self, ids: list[str], value: str) -> None:
        """Optimistic seen-flip: update the in-memory frame + views instantly, then
        queue the CSV rewrite in the background (the freeze used to live there)."""
        if not self.df.empty and "is_seen" in self.df.columns:
            idset = {str(i) for i in ids}
            mask = self.df["job_posting_id"].astype(str).isin(idset)
            self.df.loc[mask, "is_seen"] = value
        self._apply_df_views()
        paths = {self.id_to_path[i] for i in ids if i in self.id_to_path}
        ids = list(ids)
        self._enqueue_write(lambda: self._write_is_seen(ids, value, paths),
                            description=f"is_seen={value} on {len(ids)} job(s)")

    def _mark_ids_seen(self, ids: list[str], *, record_undo: bool = True) -> None:
        if not ids:
            return
        already = self.registry.all_ids()
        new_ids = [i for i in ids if i not in already]  # only the ones this click adds
        self.registry.mark(ids)   # registry write stays on the UI thread (fast, thread-affine)
        if record_undo and new_ids:
            self._seen_undo.append(new_ids)
            self._update_seen_buttons()
        self._apply_seen_locally(ids, "yes")

    def _mark_seen_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select one or more rows to mark seen.")
            return
        self._mark_ids_seen(ids)
        self._set_status(f"Marked {len(ids)} job(s) as seen.")

    def _undo_seen(self) -> None:
        if not self._seen_undo:
            self._set_status("Nothing to undo.")
            return
        ids = self._seen_undo.pop()
        self.registry.unmark(ids)
        self._update_seen_buttons()
        self._apply_seen_locally(ids, "no")
        self._set_status(f"Undid 'seen' on {len(ids)} job(s).")

    def _update_seen_buttons(self) -> None:
        self.btn_undo_seen.setEnabled(bool(self._seen_undo))

    # ---- context-menu callbacks ----------------------------------------------

    def _open_url(self, jid: str) -> None:
        url = self._url_by_id.get(jid) or self._cell(self._row_for(jid), "url")
        if url:
            chrome.open_in_chrome(url)

    def _set_status_for(self, ids: list[str], status: str) -> None:
        for jid in ids:
            row = self._row_for(jid)
            self.registry.set_status(jid, status, company=self._cell(row, "company_name"),
                                     job_title=self._cell(row, "job_title"),
                                     url=self._cell(row, "url"))
        if status == "applied":
            # Applying to a job means you've triaged it — also mark it seen (this
            # is what the old 'Mark applied' button did) and reload via that path.
            self._mark_ids_seen(ids)
        else:
            self._refresh_tracker()
        self._set_status(f"Set {len(ids)} job(s) to '{status}'.")

    def _block_company(self, company: str) -> None:
        try:
            jobsdata.append_to_blocklist(self.csv_paths, company)
        except OSError as exc:
            self._set_status(f"Could not block {company}: {exc}")
            return
        self.reload_data()
        self._set_status(f"Blocked {company} — hidden now and skipped on the next job search.")

    def _save_hidden(self, key: str, hidden: list[str]) -> None:
        self.hidden_columns[key] = list(hidden)
        jobsdata.save_hidden_columns(self.hidden_columns)

    def _open_resume_folder(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select a row to open its resume folder.")
            return
        path = self.registry.resume_path(ids[0])
        if not path or not Path(path).exists():
            self._set_status("No tailored resume recorded — use 'Tailor resume' first.")
            return
        try:
            osopen.open_path(path)
        except OSError as e:
            self._set_status(f"Could not open {path}: {e}")

    # ---- run scraper (spend-guarded) -----------------------------------------

    # -u: the children write to a pipe, which Python block-buffers — without it
    # scrape.log stays empty for the whole run and a healthy scrape looks dead.
    @staticmethod
    def scraper_cmd(bounded: bool) -> list[str]:
        cmd = [_console_python(), "-u", "scraper.py"]
        if bounded:
            cmd += ["--max-keywords", "1", "--limit", "5"]
        return cmd

    @staticmethod
    def scorer_cmd() -> list[str]:
        return [_console_python(), "-u", "score_jobs.py"]

    @staticmethod
    def _scrape_log_path() -> Path:
        try:
            APPDATA.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return APPDATA / "scrape.log"

    def _confirm_scrape(self) -> str | None:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Find new jobs")
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setText("Finding new jobs collects fresh postings through the discovery service — this "
                    "spends real money / API credits.\n\n- Small test run: 1 keyword, 5 postings/search "
                    "(cheap check).\n- Full run: your full search config (normal daily cost).\n\nIt then "
                    "scores the new jobs and refreshes the dashboard.")
        small = box.addButton("Small test run", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        full = box.addButton("Full run", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is small:
            return "bounded"
        if clicked is full:
            return "full"
        return None

    def _run_scraper_dialog(self) -> None:
        if getattr(self, "_scraping", False):
            self._set_status("A job search is already running.")
            return
        choice = self._confirm_scrape()
        if not choice:
            return
        self._scraping = True
        self._set_status(f"Finding new jobs … progress in {self._scrape_log_path()}")
        workers.run_async(self, lambda: self._scrape_work(choice == "bounded"),
                          on_done=self._after_scrape, on_error=self._after_scrape_error)

    def offer_unscored_recovery(self) -> None:
        """Offer to score run CSVs an interrupted scrape left behind.

        The scrape pipeline (scraper -> scorer -> refresh) runs inside this
        process, so closing the dashboard mid-run orphans it: the collected
        `<label>/<run>.csv` survives on disk but was never scored, and unscored
        CSVs are invisible to the dashboard. Called from app.main() once at
        startup (never from __init__ — a modal there would hang headless tests).
        """
        if getattr(self, "_scraping", False):
            return
        try:
            pending = jobsdata.unscored_run_csvs()
        except OSError:
            return
        if not pending:
            return
        names = "\n".join(f"  •  {p.parent.name}/{p.name}" for p in pending)
        resp = QtWidgets.QMessageBox.question(
            self, "Unscored job-search results found",
            "A previous job search was interrupted before its results were "
            "scored, so they never appeared in the dashboard:\n\n"
            f"{names}\n\n"
            "Score them now? This only runs the scoring step (Gemini) — it does "
            "not collect new jobs and costs no discovery credits.",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.Yes)
        if resp != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._scraping = True
        self._set_status(
            f"Scoring recovered job-search results … progress in {self._scrape_log_path()}")
        workers.run_async(self, self._score_only_work,
                          on_done=self._after_scrape, on_error=self._after_scrape_error)

    def _scrape_env(self) -> dict:
        """Environment for the local scrape subprocess: a copy of ours, plus a pointer to
        the synced Drive master so the scraper also excludes — and never re-bills — jobs
        the VM already collected. The local repo master is only a small stub of recent
        local runs, so without this a local 'Find new jobs' run re-pulls (and re-scores)
        postings the VM already has. Set on the CHILD's env only, not our own process, so
        the post-scrape VM-push set stays lean — it carries what THIS host collected, not
        the Drive master pulled down from the VM."""
        env = os.environ.copy()
        root = gdrive_root_dir(self.csv_paths)
        if root is not None:
            master = Path(root) / "linkedin_jobs_master.csv.gz"
            if master.exists():
                # scraper.EXTRA_MASTER_ENV — the synced Drive master to also exclude from.
                env["LINKEDIN_EXTRA_MASTER"] = str(master)
        return env

    def _scrape_work(self, bounded: bool):
        """Run scraper.py then score_jobs.py, streaming their output to scrape.log.

        Output is captured (the dashboard runs under pythonw with no console) so a
        failure surfaces the real error instead of a dead 'check the console'.
        """
        log_path = self._scrape_log_path()
        env = self._scrape_env()
        before = self._outbox_snapshot()
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            self._run_pipeline((self.scraper_cmd(bounded), self.scorer_cmd()),
                               log, log_path, env=env)
            # Both steps succeeded: push the freshly-collected ids to the VM (if
            # configured) so its next scheduled run doesn't re-collect — and re-bill —
            # what this run just pulled. Best-effort: never fail the scrape over a sync.
            self._push_seen_ids_to_vm(log)
            self._push_outbox_to_vm(log, before)
        return True

    def _score_only_work(self):
        """Recovery worker: run ONLY score_jobs.py (it picks up the newest unscored
        run CSV itself). Appends to scrape.log — the earlier, interrupted run's
        output is the context for what is being recovered, so keep it."""
        log_path = self._scrape_log_path()
        before = self._outbox_snapshot()
        with open(log_path, "a", encoding="utf-8", errors="replace") as log:
            self._run_pipeline((self.scorer_cmd(),), log, log_path)
            # The recovered run's ids/rows never made it to the VM either — they
            # ride the same post-scrape sync as a normal run, or the recovery
            # stays local-only.
            self._push_seen_ids_to_vm(log)
            self._push_outbox_to_vm(log, before)
        return True

    def _run_pipeline(self, cmds, log, log_path, env=None) -> None:
        """Run each command with the repo root as cwd, streaming combined
        stdout+stderr to `log`; raise RuntimeError carrying the output tail when
        one exits non-zero (the dashboard runs under pythonw — the log is the
        only console there is)."""
        repo = Path(__file__).resolve().parents[2]
        for cmd in cmds:
            log.write(f"\n=== {' '.join(cmd)} ===\n")
            log.flush()
            proc = subprocess.Popen(
                cmd, cwd=str(repo), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=_no_window_flag(), env=env)
            captured: list[str] = []
            if proc.stdout is not None:
                for line in proc.stdout:
                    captured.append(line)
                    log.write(line)
                    log.flush()
            rc = proc.wait()
            if rc != 0:
                tail = "".join(captured).strip().splitlines()[-15:]
                raise RuntimeError(
                    f"{Path(cmd[-1]).name} failed (exit {rc}).\n\n"
                    + ("\n".join(tail) if tail else "(no output captured)")
                    + f"\n\nFull log: {log_path}")

    @staticmethod
    def _push_seen_ids_to_vm(log) -> None:
        """Best-effort post-scrape sync: write this host's exclude-id set and scp it
        to the VM when one is configured. Any failure (no VM, gcloud error, file error)
        is logged to scrape.log and swallowed — the scrape result is unaffected."""
        try:
            repo = Path(__file__).resolve().parents[2]
            if str(repo) not in sys.path:
                sys.path.insert(0, str(repo))
            import scraper
            import vm_sync
            target = vm_sync.VMTarget.from_env()
            if not target.configured():
                log.write("\n=== VM seen-id sync: no VM configured, skipped ===\n")
                log.flush()
                return
            path = scraper.write_external_exclude_ids()
            log.write(f"\n=== VM seen-id sync: pushing {path.name} to VM ===\n")
            log.flush()
            res = vm_sync.sync_exclude_ids_to_vm(target, path)
            if res is not None and res.returncode == 0:
                log.write("VM seen-id sync: OK\n")
            else:
                rc = getattr(res, "returncode", "n/a")
                err = (getattr(res, "stderr", "") or getattr(res, "stdout", "")).strip()
                log.write(f"VM seen-id sync: FAILED (exit {rc}) {err}\n")
            log.flush()
        except Exception as e:  # noqa: BLE001 - sync is best-effort, never fail the scrape
            try:
                log.write(f"\n=== VM seen-id sync: error ({e}) — scrape unaffected ===\n")
                log.flush()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _outbox_snapshot() -> dict:
        """Pre-scrape {run-file: mtime} so the post-scrape hook can tell which run
        files this scrape produced/rewrote. {} on any failure (hook degrades to
        push-retries-only)."""
        try:
            repo = Path(__file__).resolve().parents[2]
            if str(repo / "local") not in sys.path:
                sys.path.insert(0, str(repo / "local"))
            import outbox
            return outbox.snapshot_run_files()
        except Exception:  # noqa: BLE001 - best-effort
            return {}

    def _push_outbox_to_vm(self, log, before: dict) -> None:
        """Best-effort post-scrape data sync: queue this run's new master rows (+ the
        run-stats file) in the outbox and push every pending outbox file to the VM's
        ~/incoming/. Also sweeps the local master for rows the Drive master lacks
        (outbox.unsynced_master_ids) so rows collected outside this hook — a CLI
        snapshot recovery, a push that never got retried — are queued too instead of
        staying stranded on this PC. Failures are logged to scrape.log and swallowed —
        a sync problem never fails a scrape. Push still runs when the run added
        nothing, so files queued by earlier failed pushes retry here."""
        try:
            repo = Path(__file__).resolve().parents[2]
            if str(repo / "local") not in sys.path:
                sys.path.insert(0, str(repo / "local"))
            import outbox
            import vm_sync
            ids = outbox.new_run_ids(before)
            root = gdrive_root_dir(self.csv_paths)
            if root is not None:
                have = set(ids)
                for jid in outbox.unsynced_master_ids(
                        Path(root) / "linkedin_jobs_master.csv.gz"):
                    if jid not in have:
                        ids.append(jid)
                        have.add(jid)
            if ids:
                path = outbox.write_rows_outbox(ids)
                log.write(f"\n=== outbox: queued {len(ids)} row id(s) -> "
                          f"{getattr(path, 'name', None)} ===\n")
            else:
                log.write("\n=== outbox: no new rows this run ===\n")
            outbox.write_stats_outbox()
            target = vm_sync.VMTarget.from_env()
            pushed, kept = outbox.push_outbox(target, log=log)
            log.write(f"outbox push done: {pushed} pushed, {kept} kept\n")
            log.flush()
        except Exception as e:  # noqa: BLE001 - sync is best-effort
            try:
                log.write(f"\n=== outbox sync: error ({e}) — scrape unaffected ===\n")
                log.flush()
            except Exception:  # noqa: BLE001
                pass

    def _after_scrape(self, _result) -> None:
        self._scraping = False
        # A local scrape writes to the repo dir, not the synced Drive folder this
        # window was opened against — fold the new scored run file(s) into the
        # sources so the freshly scraped jobs actually appear.
        for p in jobsdata.local_run_files():
            if p not in self.csv_paths:
                self.csv_paths.append(p)
        self.reload_data()
        self._set_status("Job search + score complete — dashboard refreshed.")

    def _after_scrape_error(self, exc) -> None:
        self._scraping = False
        msg = str(exc)
        self._set_status(f"Find new jobs failed — {msg.splitlines()[0] if msg else exc}")
        QtWidgets.QMessageBox.critical(self, "Find new jobs", f"The run failed.\n\n{msg}")

    # ---- add a job by hand (no scraper) --------------------------------------

    def _add_manual_job_dialog(self) -> None:
        """Open the manual-entry form, then run parse->score->tailor->append off-thread.

        Reuses the exact scoring (score_jobs) + tailoring (resume_tailor) pipelines a
        scraped job goes through; only the input differs (a paste/URL, not Bright
        Data). The heavy work runs on a worker thread so the window never freezes."""
        if getattr(self, "_manual_adding", False):
            self._set_status("A manual add is already running.")
            return
        dlg = ManualAddDialog(self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        do_tailor = bool(vals.get("do_tailor"))
        cfg = settings.load()
        # The cover-letter prompt only makes sense when we're tailoring; a "just score"
        # add never tailors, so never ask.
        cover = False
        if do_tailor:
            cover = QtWidgets.QMessageBox.question(
                self, "Cover letter", "Also generate a cover letter for this job?"
            ) == QtWidgets.QMessageBox.StandardButton.Yes
        opts = {"cover_letter": cover, "ats_report": bool(cfg.get("tailor_ats_report", True)),
                "prep_sheet": bool(cfg.get("tailor_prep_sheet", False)),
                "tone": cfg.get("resume_tone", "professional")}
        self._manual_adding = True
        self._apply_auth_env()
        self._set_status("Adding job — scoring + tailoring …" if do_tailor
                         else "Adding job — scoring …")
        workers.run_async(self, lambda: self._manual_add_work(vals, opts, do_tailor),
                          on_done=self._finish_manual_add, on_error=self._finish_manual_add_error)

    def _manual_add_work(self, vals: dict, opts: dict, do_tailor: bool = True) -> dict:
        """Worker body: the toolkit-agnostic manual_add pipeline. The LLM/scraper
        seams default to the real implementations (mockable in tests)."""
        import manual_add
        res = manual_add.add_manual_job(
            jd_text=vals.get("jd_text", ""), url=vals.get("url", ""),
            company=vals.get("company", ""), title=vals.get("title", ""),
            do_tailor=do_tailor, tailor_opts=opts, on_status=self.tailor_progress.emit)
        res["requested_tailor"] = do_tailor  # so the finish status can distinguish skip vs fail
        # Queue + push the new master row to the VM (best-effort — never fail the add).
        try:
            repo = Path(__file__).resolve().parents[2]
            if str(repo / "local") not in sys.path:
                sys.path.insert(0, str(repo / "local"))
            import outbox
            import vm_sync
            jid = str((res.get("record") or {}).get("job_posting_id", "")).strip()
            with open(self._scrape_log_path(), "a", encoding="utf-8",
                      errors="replace") as log:
                log.write("\n=== manual add: outbox sync ===\n")
                if jid:
                    outbox.write_rows_outbox([jid])
                outbox.push_outbox(vm_sync.VMTarget.from_env(), log=log)
        except Exception:  # noqa: BLE001 - sync is best-effort
            pass
        return res

    def _finish_manual_add(self, result: dict) -> None:
        self._manual_adding = False
        result = result or {}
        rec = result.get("record") or {}
        if result.get("resume_dir") and rec.get("job_posting_id"):
            try:
                self.registry.record_resume(rec["job_posting_id"], str(result["resume_dir"]))
            except Exception:  # noqa: BLE001 - bookkeeping only
                pass
        # Fold the manual scored gz into the sources so the new job appears now and
        # survives a restart — same bridge a local scrape gets.
        for p in jobsdata.local_run_files():
            if p not in self.csv_paths:
                self.csv_paths.append(p)
        self.reload_data()
        title = rec.get("job_title", "job")
        company = rec.get("company_name", "")
        score = rec.get("score", "")
        if result.get("resume_dir"):
            tailored = "tailored"
        elif result.get("requested_tailor"):
            tailored = "added (tailor later)"     # tailoring was attempted but failed
        else:
            tailored = "scored (not tailored)"    # "just score" — never tailored
        self._set_status(f"Manual job {tailored}: {title} @ {company} (score {score}).")

    def _finish_manual_add_error(self, exc) -> None:
        self._manual_adding = False
        msg = str(exc)
        self._set_status(f"Add job failed — {msg.splitlines()[0] if msg else exc}")
        QtWidgets.QMessageBox.warning(self, "Add a job by hand", f"Could not add the job.\n\n{msg}")

    # ---- delete / edit job entries -------------------------------------------

    def _delete_jobs(self, ids) -> None:
        """Permanently remove the selected job(s) from the dataset (confirm first).
        Any tailored-résumé folder goes to the Recycle Bin (recoverable)."""
        ids = [str(i) for i in (ids or []) if str(i).strip()]
        if not ids:
            return
        if QtWidgets.QMessageBox.question(
                self, "Delete job(s)?",
                f"Permanently remove {len(ids)} job(s) from your dataset? This can't be "
                "undone. Any tailored-résumé folder is moved to the Recycle Bin."
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        # Snapshot the résumé folders BEFORE the delete — the registry rows are
        # cleared below, and the folder must still be findable afterwards.
        folders = {}
        for jid in ids:
            try:
                folders[jid] = self.registry.resume_path(jid)
            except Exception:  # noqa: BLE001 - bookkeeping only
                folders[jid] = None
        # Registry cleanup stays on the UI thread (SQLite is thread-affine, and
        # it's fast) so the tracker view is already correct in the repaint below.
        for jid in ids:
            try:
                self.registry.clear_status(jid)       # drop any tracker status too
                self.registry.clear_resume_path(jid)  # résumé link is stale either way
                self.registry.clear_tailor_failure(jid)  # deleted job needs no re-run flag
            except Exception:  # noqa: BLE001 - bookkeeping only
                pass
        # Optimistic: drop the rows from the in-memory frame and repaint now; the
        # multi-file CSV rewrite (the part that used to freeze the UI for seconds)
        # runs on the background write queue.
        if not self.df.empty:
            self.df = self.df[~self.df["job_posting_id"].astype(str).isin(set(ids))]
            self.df = self.df.reset_index(drop=True)
        for jid in ids:
            self.id_to_path.pop(jid, None)
        self._apply_df_views()
        self._set_status(f"Deleting {len(ids)} job(s) in background …")

        def work():
            n = jobsdata.delete_jobs(ids)
            trash_failed = []
            for jid in ids:
                try:
                    # Best-effort: refuses (False) anything outside the output root;
                    # a locked folder (open in Explorer) raises and is reported below.
                    recycle_resume_folder(folders.get(jid))
                except OSError:  # includes send2trash's TrashPermissionError
                    trash_failed.append(jid)
            return n, trash_failed

        self._enqueue_write(work, description=f"delete {len(ids)} job(s)",
                            on_done=self._finish_delete)

    def _finish_delete(self, result) -> None:
        n, trash_failed = result
        msg = f"Deleted {n} job(s)."
        if trash_failed:
            msg += (f" Couldn't move {len(trash_failed)} résumé folder(s) to the "
                    "Recycle Bin (folder in use?) — remove them by hand.")
        self._set_status(msg)

    def _edit_manual_job(self, jid) -> None:
        """Field-fix a manually-added job (URL/title/company/JD) via the manual-add
        form in edit mode. Does not re-score/re-tailor — preserves the job's id and
        score so its tracker status and résumé link survive the edit."""
        jid = str(jid or "")
        if not jid:
            return
        row = jobsdata.master_row(jid) or {}
        initial = {
            "url": str(row.get("url", "") or ""),
            "title": str(row.get("job_title", "") or ""),
            "company": str(row.get("company_name", "") or ""),
            "jd_text": str(row.get("job_description_formatted", "")
                           or row.get("job_summary", "") or ""),
        }
        dlg = ManualAddDialog(self, edit_mode=True, initial=initial)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        record = dict(row)
        record["job_posting_id"] = jid            # identity is stable across an edit
        record["url"] = vals["url"]
        record["job_title"] = vals["title"]
        record["company_name"] = vals["company"]
        if vals["jd_text"]:
            record["job_description_formatted"] = vals["jd_text"]
            record["job_summary"] = vals["jd_text"][:1000]
        record.setdefault("source", "manual")
        record.setdefault("run_label", "manual")
        jobsdata.update_manual_job(record, old_id=jid)
        self.reload_data()
        self._set_status(f"Updated job: {vals['title']} @ {vals['company']}.")

    # ---- apply (open posting for review; never submits) -----------------------

    def _apply_selected(self) -> None:
        if getattr(self, "_applying", False):
            return
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select a job to open its application.")
            return
        jid = ids[0]
        payload = self._job_payload(jid)
        self._applying = True
        self._set_status("Opening application …")
        workers.run_async(self, lambda: self._apply_work(jid, payload),
                          on_done=self._finish_apply_ok, on_error=self._finish_apply_error)

    def _apply_work(self, jid: str, payload: dict | None):
        from resume_tailor import apply as apply_mod
        folder = apply_mod.resolve_generated_dir(job_id=jid, job=payload)
        ctx = apply_mod.build_apply_context(folder)
        url = ctx.get("apply_url", "")
        if url:
            try:
                chrome.open_in_chrome(url)
            except Exception:  # noqa: BLE001
                pass
        return ctx

    def _finish_apply_ok(self, ctx: dict) -> None:
        self._applying = False
        job = ctx.get("job") or {}
        self._apply_panel_job = job  # for the panel's "I applied to this job" button
        resume_pdf = ctx.get("resume_pdf", "")
        # Open the right-side Apply panel (copyable paths + the apply sheet) and hide
        # the bottom score preview while it's up — the ✕ on the panel restores it.
        self.apply_panel.show_application(ctx)
        self._apply_panel_open = True
        self.preview.setVisible(False)
        self._preview_shown = False
        self.apply_panel.show()
        sizes = self.hsplit.sizes()
        if len(sizes) >= 2 and sizes[-1] < 50:  # first open — carve out room for the panel
            total = sum(sizes) or 1000
            self.hsplit.setSizes([max(420, total - 380), 380])
        if resume_pdf:
            QtWidgets.QApplication.clipboard().setText(resume_pdf)
        self._set_status(f"Apply sheet ready for {job.get('company', '?')} — "
                         f"{job.get('title', '?')}. Paste it into Claude-in-Chrome; "
                         f"review before submitting.")

    def _close_apply_panel(self) -> None:
        self._apply_panel_open = False
        self.apply_panel.hide()
        self._apply_preview_visibility()  # restores the score preview on a job tab

    def _mark_applied_from_panel(self) -> None:
        """The panel's "I applied to this job" button: confirm, record the job as
        applied in the tracker (using the panel's stored marker identity, so it works
        even when the row isn't in the loaded data), mark it seen, and close the panel
        — so the one button doubles as "added to tracker" and "exit"."""
        job = getattr(self, "_apply_panel_job", None) or {}
        jid = str(job.get("job_posting_id") or "").strip()
        if not jid:
            self._set_status("Couldn't identify this job to add to the tracker.")
            return
        title, company = job.get("title", "this job"), job.get("company", "?")
        if QtWidgets.QMessageBox.question(
                self, "Mark as applied?",
                f"Add '{title}' @ {company} to your application tracker as applied?"
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.registry.set_status(jid, "applied", company=company,
                                 job_title=title, url=job.get("url", ""))
        self._mark_ids_seen([jid])   # applied implies seen (matches the right-click path)
        self._close_apply_panel()
        self._set_status(f"Marked applied — added {company} to the tracker.")

    def _finish_apply_error(self, exc) -> None:
        self._applying = False
        msg = str(exc)
        self._set_status(msg.splitlines()[0] if msg else "Apply failed")
        QtWidgets.QMessageBox.information(
            self, "Apply", f"{msg}\n\nUse 'Tailor resume' on this job, then try Apply again.")

    # ---- tailor --------------------------------------------------------------

    def _confirm_large_tailor(self, n: int) -> bool:
        """Warn before fanning out a big parallel batch (separate method so it's
        trivially testable). Returns True to proceed."""
        return QtWidgets.QMessageBox.question(
            self, "Tailor many resumes at once?",
            f"About to tailor {n} resumes ({MAX_PARALLEL_TAILORS} at a time), each "
            f"making its own Gemini calls and launching pdflatex. If the API rate-limits, "
            f"jobs wait it out and retry; any job that still fails turns red in the "
            f"Unseen tab for a re-run. Continue?"
        ) == QtWidgets.QMessageBox.StandardButton.Yes

    def _tailor_selected(self) -> None:
        if getattr(self, "_tailoring", False):
            return
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select one or more jobs to tailor a resume for.")
            return
        jobs = [j for j in (self._job_payload(i) for i in ids) if j]
        if not jobs:
            self._set_status("Could not find job data for the selection.")
            return
        if len(jobs) > PARALLEL_WARN_THRESHOLD and not self._confirm_large_tailor(len(jobs)):
            return
        cfg = settings.load()
        cover = QtWidgets.QMessageBox.question(
            self, "Cover letter",
            f"Also generate a cover letter for the selected {len(jobs)} job(s)?"
        ) == QtWidgets.QMessageBox.StandardButton.Yes
        opts = {"cover_letter": cover, "ats_report": bool(cfg.get("tailor_ats_report", True)),
                "prep_sheet": bool(cfg.get("tailor_prep_sheet", False)),
                "tone": cfg.get("resume_tone", "professional")}
        self._start_tailor(jobs, opts)

    def _start_tailor(self, jobs: list[dict], opts: dict, on_finished=None) -> bool:
        """Launch the tailor worker — the ONE shared path both the Tailor button
        (`_tailor_selected`) and auto-apply queueing (`_queue_for_auto_apply`)
        come through, so queue-chaining never duplicates the worker plumbing.

        Returns False WITHOUT launching when a run is already in flight (the
        `_tailoring` guard). `on_finished(results, exc)` — exactly one of the
        two is None — fires on the UI thread AFTER the standard `_finish_tailor`
        / `_finish_tailor_error` handling, so a chained step already sees the
        registry's resume paths recorded."""
        if getattr(self, "_tailoring", False):
            return False
        self._tailoring = True
        self._tailor_recorded = set()   # job ids bookkept by _on_tailor_job_done
        self.btn_tailor.setEnabled(False)
        self._apply_auth_env()
        plural = "resume" if len(jobs) == 1 else "resumes in parallel"
        self._set_status(f"Tailoring {len(jobs)} {plural} …")

        def done(results) -> None:
            self._finish_tailor(results)
            if on_finished is not None:
                on_finished(results, None)

        def error(exc: BaseException) -> None:
            self._finish_tailor_error(exc)
            if on_finished is not None:
                on_finished(None, exc)

        try:
            workers.run_async(self, lambda: self._tailor_work(jobs, opts),
                              on_done=done, on_error=error)
        except Exception as exc:  # noqa: BLE001 - launch (thread spawn) failed; clear
            self._tailoring = False  # the re-entry guard so Tailor isn't dead-locked
            self.btn_tailor.setEnabled(True)  # (same shape as _generate_cover_for's)
            if on_finished is not None:  # park queue-chained "tailoring" entries as
                on_finished(None, RuntimeError(  # failed — orphans are unclaimable
                    f"tailor launch failed: {exc}"))
            raise
        return True

    def _tailor_work(self, jobs: list[dict], opts: dict) -> list[dict]:
        """Tailor every selected job CONCURRENTLY (all at once) on a thread pool, and
        return a per-job outcome list. No registry/SQLite writes happen here — those
        are done back on the UI thread in `_finish_tailor` (the registry connection is
        thread-affine and concurrent writes would contend). Per-job exceptions are
        captured so one failure never sinks the rest of the batch."""
        from resume_tailor import assets, llm
        from resume_tailor import tailor as tailor_resume

        # Pre-warm the shared lru_caches once so N threads don't each re-parse the YAML
        # / re-extract the example PDF (and so there's no cold-cache race). Best-effort:
        # tailor() will surface any real loading error per job.
        for warm in (assets.load_master, assets.atoms_by_id, assets.blocks,
                     assets.template_head, assets.example_text):
            try:
                warm()
            except Exception:  # noqa: BLE001 - pre-warm only
                pass
        llm.reset_usage()  # once for the whole batch; jobs pass reset_usage=False

        n = len(jobs)
        done_lock = threading.Lock()
        done = 0

        def report(label: str, msg: str) -> None:
            # Cross-thread-safe: queued onto the UI thread by the Qt signal.
            self.tailor_progress.emit(f"Tailoring ({done}/{n} done): {label} — {msg}")

        def one(job: dict) -> dict:
            nonlocal done
            label = f'{job.get("job_title") or "Role"} @ {job.get("company_name") or "?"}'
            try:
                out = tailor_resume(job, cover_letter=opts["cover_letter"],
                                    ats_report=opts["ats_report"], prep_sheet=opts["prep_sheet"],
                                    tone=opts["tone"], reset_usage=False,
                                    on_status=lambda m, lbl=label: report(lbl, m))
                result = {"id": job.get("job_posting_id"), "label": label,
                          "dir": out, "error": None}
            except Exception as exc:  # noqa: BLE001 - capture per-job; report in the summary
                result = {"id": job.get("job_posting_id"), "label": label,
                          "dir": None, "error": str(exc)}
            with done_lock:
                done += 1
            # Queued to the UI thread: the registry records this job NOW, so an
            # interrupted batch keeps everything already finished.
            self.tailor_job_done.emit(result)
            self.tailor_progress.emit(f"Tailoring ({done}/{n} done): {label} finished")
            return result

        with ThreadPoolExecutor(max_workers=_tailor_pool_size(n)) as pool:
            return list(pool.map(one, jobs))

    def _record_tailor_result(self, result: dict) -> bool:
        """Write ONE job's outcome to the registry (UI thread only): success
        records the resume folder (clearing any old red flag), failure records
        the red 'tailor failed — re-run' flag the unseen tab shows. True when
        the write landed."""
        jid = (result or {}).get("id")
        if not jid:
            return False
        try:
            if result.get("dir"):
                self.registry.record_resume(jid, str(result["dir"]))
            else:
                self.registry.record_tailor_failure(
                    jid, str(result.get("error") or "unknown error"))
        except Exception:  # noqa: BLE001 - bookkeeping only; the view heals on reload
            return False
        return True

    def _on_tailor_job_done(self, result: dict) -> None:
        """Bookkeep ONE finished tailor job (queued here, the UI thread, from a
        pool thread the moment the job ends). Incremental on purpose — a batch
        interrupted at job 12 of 14 must keep those 12 results; the July 8 crash
        lost all of them because bookkeeping waited for the whole batch."""
        if self._record_tailor_result(result):
            rec = getattr(self, "_tailor_recorded", None)
            if rec is not None:
                rec.add(result["id"])

    def _finish_tailor(self, results: list[dict]) -> None:
        self._tailoring = False
        self.btn_tailor.setEnabled(True)
        results = results or []
        oks = [r for r in results if r.get("dir")]
        fails = [r for r in results if not r.get("dir")]
        # Normally every result was already bookkept per job (the queued
        # _on_tailor_job_done deliveries precede this callback — same FIFO
        # event queue). Catch up on any that weren't, e.g. a registry hiccup
        # mid-batch or a test driving this callback directly.
        recorded = getattr(self, "_tailor_recorded", set())
        for r in results:
            if r.get("id") and r["id"] not in recorded:
                self._record_tailor_result(r)
        total = len(results)
        if fails:
            lines = "\n".join(f"  - {r['label']}: {r['error']}" for r in fails)
            QtWidgets.QMessageBox.warning(
                self, "Tailor resume",
                f"Tailored {len(oks)} of {total} resume(s).\n\nFailed:\n{lines}")
            self._set_status(f"Tailored {len(oks)} of {total}; {len(fails)} failed (see dialog).")
        elif oks:
            self._set_status(f"Resume(s) ready ({len(oks)}).")
        last = oks[-1]["dir"] if oks else None
        # Only open the file manager when the user opted in — off by default so a
        # multi-job batch doesn't spawn a window per résumé (Settings → Dashboard).
        if last and settings.load().get("tailor_open_folder", False):
            try:
                osopen.open_path(last)
            except OSError:
                pass
        self.reload_data()

    def _finish_tailor_error(self, exc) -> None:
        self._tailoring = False
        self.btn_tailor.setEnabled(True)
        QtWidgets.QMessageBox.warning(self, "Tailor resume", f"Tailoring failed: {exc}")
        self._set_status(f"Tailor failed: {exc}")

    # ---- batch auto-apply queueing (SP3) ---------------------------------------

    def _submit_queue_write(self, fn, on_done=None, on_error=None) -> None:
        """Run an apply-queue mutation on the background write queue.

        Deliberately NOT `_enqueue_write` — its self-write suppression exists
        for the source-CSV watcher, while the queue file has its own watcher
        inside ApplyQueuePanel that SHOULD see these writes land. Late-binds
        `self._writes` so tests that swap in an inline runner drive it too."""
        self._writes.submit(
            fn, on_done=on_done,
            on_error=on_error or (lambda exc: self._set_status(
                f"Apply-queue write failed: {exc}")))

    def _queue_artifacts(self, folder) -> dict:
        """The artifact paths a queue entry carries for a tailored folder.
        Optional files not on disk map to "" so the agent never chases a path
        that doesn't exist."""
        from resume_tailor import output
        folder = Path(folder)

        def existing(name: str) -> str:
            p = folder / name
            try:
                return str(p) if p.exists() else ""
            except OSError:
                return ""

        return {
            "folder": str(folder),
            "resume_pdf": existing(output.resume_filename()),
            "cover_letter_pdf": existing(output.cover_filename()),
            "cover_letter_txt": existing(output.cover_txt_filename()),
            "apply_md": existing("apply.md"),
        }

    def _queue_entry_for(self, jid: str, batch_id: str, status: str) -> dict | None:
        """A fresh apply-queue entry for one job, from the loaded frame — with
        the tracker-row / master-CSV fallback for ids the frames don't carry
        (tracker-only jobs; the same fallback _generate_cover_for uses).
        Returns None when no apply URL can be resolved ANYWHERE: an entry with
        an empty apply_url would send the SP4 agent nowhere and burn one of
        the job's attempts, so it must never reach the queue."""
        row = self._row_for(jid)
        company = self._cell(row, "company_name")
        title = self._cell(row, "job_title")
        url = self._url_by_id.get(jid) or self._cell(row, "url")
        easy_raw = self._cell(row, "is_easy_apply")
        if not (company and title and url):
            tracked = self._tracked.get(jid) or {}
            company = company or str(tracked.get("company") or "")
            title = title or str(tracked.get("job_title") or "")
            url = url or str(tracked.get("url") or "")
        if not (company and title and url):
            master = jobsdata.master_row(jid) or {}
            company = company or str(master.get("company_name") or "")
            title = title or str(master.get("job_title") or "")
            url = url or str(master.get("url") or "")
            easy_raw = easy_raw or str(master.get("is_easy_apply") or "")
        if not url.strip():
            return None
        easy = easy_raw.strip().lower() in ("true", "1", "yes")
        return apply_queue.new_entry(
            jid,
            company=company,
            title=title,
            apply_url=url,
            is_easy_apply=easy,
            batch_id=batch_id,
            status=status)

    def _queue_apply_selected(self) -> None:
        """The action-bar 'Queue auto-apply' button: queue the selection."""
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select one or more jobs to queue for auto-apply.")
            return
        self._queue_for_auto_apply(ids)

    def _queue_for_auto_apply(self, ids) -> None:
        """'Queue for auto-apply' (context menu + action bar): skip jobs already
        applied to, enforce the batch cap, enqueue apply-ready jobs with their
        artifact paths, and offer ONE tailor-then-queue run for the rest (cover
        letter included — that Gemini spend is consented by the Yes click)."""
        seen: set[str] = set()
        ids = [s for s in (str(i).strip() for i in (ids or []))
               if s and not (s in seen or seen.add(s))]
        if not ids:
            self._set_status("Select one or more jobs to queue for auto-apply.")
            return
        try:
            applied = {str(r.get("job_posting_id")) for r in self.registry.status_rows()
                       if r.get("status") == "applied"}
        except Exception:  # noqa: BLE001 - registry hiccup: skip nothing, queue on
            applied = set()
        notes: list[str] = []
        skipped = [i for i in ids if i in applied]
        if skipped:
            notes.append(f"skipped {len(skipped)} already-applied")
        remaining = [i for i in ids if i not in applied]
        if not remaining:
            self._set_status(f"Nothing to queue — skipped {len(skipped)} "
                             "already-applied job(s).")
            return
        cfg = settings.load()
        try:
            cap = int(cfg.get("auto_apply_batch_cap", 10) or 10)
        except (TypeError, ValueError):
            cap = 10
        if len(remaining) > cap:
            notes.append(f"capped at {cap} (auto_apply_batch_cap)")
            remaining = remaining[:cap]

        ready: list[tuple[str, Path]] = []
        not_ready: list[str] = []
        for jid in remaining:
            ok, folder = self._apply_ready(jid)
            if ok and folder is not None:
                ready.append((jid, folder))
            else:
                not_ready.append(jid)

        batch_id = datetime.now().strftime("batch-%Y%m%d-%H%M%S")
        queued_n = 0
        no_data = 0
        for jid, folder in ready:
            entry = self._queue_entry_for(jid, batch_id, "queued")
            if entry is None:      # no apply URL anywhere — refuse (see _queue_entry_for)
                no_data += 1
                continue
            entry["artifacts"].update(self._queue_artifacts(folder))
            self._submit_queue_write(lambda e=entry: apply_queue.enqueue(e))
            queued_n += 1
        if no_data:
            notes.append(f"{no_data} without job data — not queued")

        started_tailor = False
        if not_ready:
            if getattr(self, "_tailoring", False):
                notes.append(f"{len(not_ready)} not tailored — skipped while a "
                             "tailor run is in flight")
            elif QtWidgets.QMessageBox.question(
                    self, "Queue for auto-apply",
                    f"{len(not_ready)} job(s) aren't tailored yet. Tailor now "
                    "(cover letter included — spends Gemini credit) and queue "
                    "when done?") == QtWidgets.QMessageBox.StandardButton.Yes:
                jobs = [j for j in (self._job_payload(i) for i in not_ready) if j]
                # A job whose entry can't resolve an apply URL can never be
                # queued after tailoring either — drop it BEFORE spending
                # Gemini credit on it, and count it with the data-less ones.
                entries: list[dict] = []
                with_data: list[dict] = []
                for j in jobs:
                    entry = self._queue_entry_for(
                        str(j["job_posting_id"]), batch_id, "tailoring")
                    if entry is not None:
                        entries.append(entry)
                        with_data.append(j)
                jobs = with_data
                missing = len(not_ready) - len(jobs)
                if missing:
                    notes.append(f"{missing} without job data — not queued")
                if jobs:
                    # Enqueue as "tailoring" FIRST so the panel shows them the
                    # moment the worker starts; _finish_queue_tailor flips each
                    # to "queued" (set_artifacts) or parks it "failed".
                    for entry in entries:
                        self._submit_queue_write(lambda e=entry: apply_queue.enqueue(e))
                    opts = {"cover_letter": True,
                            "ats_report": bool(cfg.get("tailor_ats_report", True)),
                            "prep_sheet": bool(cfg.get("tailor_prep_sheet", False)),
                            "tone": cfg.get("resume_tone", "professional")}
                    self._queue_tailor_pending = [str(j["job_posting_id"])
                                                  for j in jobs]
                    started_tailor = self._start_tailor(
                        jobs, opts, on_finished=self._finish_queue_tailor)
                    if not started_tailor:  # raced the guard: park them honestly
                        self._finish_queue_tailor(None, RuntimeError(
                            "a tailor run was already in flight"))
            else:
                notes.append(f"{len(not_ready)} not tailored — left out")

        if not started_tailor:  # otherwise _start_tailor owns the status line
            parts = ([f"Queued {queued_n} job(s) for auto-apply"] if queued_n else [])
            parts += notes
            self._set_status((" · ".join(parts) + ".") if parts
                             else "Nothing queued for auto-apply.")

    def _finish_queue_tailor(self, results, exc=None) -> None:
        """After a queue-chained tailor run (fires on the UI thread, AFTER the
        standard _finish_tailor handling): success flips each entry
        tailoring -> queued with its artifact paths (apply_queue.set_artifacts);
        failure parks it `failed` with the reason in its notes."""
        pending = [str(i) for i in getattr(self, "_queue_tailor_pending", []) or []]
        self._queue_tailor_pending = []

        def park_failed(jid: str, note: str) -> None:
            self._submit_queue_write(
                lambda: apply_queue.finish(jid, "failed", notes=note))

        if exc is not None:
            for jid in pending:
                park_failed(jid, f"tailor failed: {exc}")
            return
        by_id = {str(r.get("id") or ""): r for r in (results or [])}
        for jid in pending:
            r = by_id.get(jid)
            if r is None:
                park_failed(jid, "tailor returned no result for this job")
            elif r.get("dir"):
                arts = self._queue_artifacts(r["dir"])
                self._submit_queue_write(
                    lambda jid=jid, arts=arts: apply_queue.set_artifacts(jid, arts))
            else:
                park_failed(jid, f"tailor failed: {r.get('error') or 'unknown error'}")

    def _apply_queue_mark_applied(self, entry: dict) -> None:
        """Auto-apply panel action: record a queued job as applied in the tracker,
        mark it seen (applied implies seen — matches _mark_applied_from_panel), and
        drop it from the queue. Queue write rides _submit_queue_write."""
        jid = str(entry.get("job_posting_id") or "").strip()
        if not jid:
            return
        self.registry.set_status(jid, "applied",
                                 company=entry.get("company", ""),
                                 job_title=entry.get("title", ""),
                                 url=entry.get("apply_url", ""))
        self._mark_ids_seen([jid])
        self._submit_queue_write(
            lambda: apply_queue.remove(jid),
            on_done=lambda _r: self.apply_queue_panel.refresh())
        self._refresh_tracker()

    def _apply_queue_mark_seen(self, entry: dict) -> None:
        """Auto-apply panel action: mark a queued job seen (no status → it stays
        under All Jobs) and drop it from the queue. The job may already be gone."""
        jid = str(entry.get("job_posting_id") or "").strip()
        if not jid:
            return
        self._mark_ids_seen([jid])

        def _remove() -> None:
            try:
                apply_queue.remove(jid)
            except apply_queue.UnknownJobError:
                pass

        self._submit_queue_write(
            _remove, on_done=lambda _r: self.apply_queue_panel.refresh())

    def _set_ats_password(self) -> None:
        """Store the ONE master ATS password (typed twice, password-echo) in the
        Windows Credential Manager via ats_accounts. The value goes straight
        from the dialog into keyring — never to disk, logs, or the queue."""
        echo = QtWidgets.QLineEdit.EchoMode.Password
        first, ok = QtWidgets.QInputDialog.getText(
            self, "Master ATS password", "New master password:", echo)
        if not ok:
            return
        second, ok = QtWidgets.QInputDialog.getText(
            self, "Master ATS password", "Repeat to confirm:", echo)
        if not ok:
            return
        if not first.strip() or first != second:
            QtWidgets.QMessageBox.warning(
                self, "Master ATS password",
                "The two entries were blank or didn't match — nothing was stored.")
            return
        try:
            stored = ats_accounts.set_master_password(first)
        except Exception as exc:  # noqa: BLE001 - keyring backend failure
            QtWidgets.QMessageBox.warning(
                self, "Master ATS password",
                f"Could not store the password: {exc}")
            return
        if not stored:
            QtWidgets.QMessageBox.warning(
                self, "Master ATS password",
                "Could not store the password (is the keyring package installed?).")
            return
        self._set_status("Master ATS password stored in Windows Credential Manager.")
        panel = getattr(self, "apply_queue_panel", None)
        if panel is not None:
            panel.refresh_password_state()

    # ---- cover letter for an already-tailored job (right-click) ---------------

    def _cover_state(self, jid: str) -> str | None:
        """Drives the right-click menu item: None when the job isn't tailored
        (no folder / resume PDF / apply.md — same readiness as Apply), else
        "exists"/"missing" by whether the cover-letter PDF is on disk."""
        ready, folder = self._apply_ready(jid)
        if not ready or folder is None:
            return None
        from resume_tailor import output
        try:
            return "exists" if (folder / output.cover_filename()).exists() else "missing"
        except OSError:
            return None

    def _generate_cover_for(self, jid: str) -> None:
        if getattr(self, "_covering", False):
            return
        state = self._cover_state(jid)
        if state is None:
            self._set_status("Tailor this job first — the cover letter reuses its "
                             "tailored résumé bullets.")
            return
        if state == "exists" and QtWidgets.QMessageBox.question(
                self, "Regenerate cover letter?",
                "A cover letter already exists for this job. Regenerate and "
                "replace it?") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        payload = self._job_payload(jid)
        if payload is None:
            # Row not in the loaded frames (e.g. tracker-only) — the master CSV
            # still carries the JD (same fallback the edit dialog uses).
            row = jobsdata.master_row(jid) or {}
            if row:
                payload = {
                    "job_posting_id": jid,
                    "company_name": str(row.get("company_name", "") or ""),
                    "job_title": str(row.get("job_title", "") or ""),
                    "job_description_formatted": str(row.get("job_description_formatted", "") or ""),
                    "job_description": str(row.get("job_description", "") or ""),
                    "job_summary": str(row.get("job_summary", "") or ""),
                    "url": str(row.get("url", "") or ""),
                }
        if not payload:
            self._set_status("Job description not available — cannot generate a "
                             "cover letter.")
            return
        _, folder = self._apply_ready(jid)
        tone = settings.load().get("resume_tone", "professional")
        self._covering = True
        self._apply_auth_env()
        self._set_status(f"Generating cover letter for {payload['company_name']} — "
                         f"{payload['job_title']} …")
        try:
            workers.run_async(self, lambda: self._cover_work(payload, folder, tone),
                              on_done=self._finish_cover, on_error=self._finish_cover_error)
        except Exception:  # noqa: BLE001 - launch (thread spawn) failed; clear the
            self._covering = False  # re-entry guard so the menu item isn't dead-locked
            raise

    def _cover_work(self, job: dict, folder, tone: str):
        from resume_tailor.run import generate_cover_letter
        # Re-check on the worker: the folder may have been deleted between the
        # menu click and this thread starting.
        if not folder or not Path(folder).is_dir():
            raise RuntimeError("The tailored folder no longer exists — re-tailor "
                               "this job first.")
        return generate_cover_letter(job, Path(folder), tone=tone,
                                     on_status=self.tailor_progress.emit)

    def _finish_cover(self, path) -> None:
        self._covering = False
        self._set_status(f"Cover letter ready → {path}")

    def _finish_cover_error(self, exc) -> None:
        self._covering = False
        QtWidgets.QMessageBox.warning(self, "Cover letter",
                                      f"Cover letter failed: {exc}")
        self._set_status(f"Cover letter failed: {exc}")

    # ---- check setup ---------------------------------------------------------

    def _check_setup(self) -> None:
        from resume_tailor import master_validate
        try:
            result = master_validate.check_setup()
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Check setup", f"Could not run checks: {exc}")
            return
        problems: list[str] = []
        for label, errs in (("Resume data", result.get("master", [])),
                            ("Apply answers", result.get("answers", []))):
            problems.extend(f"[{label}] {e}" for e in errs)
        try:
            cfg = jobsdata._load_cfg()
            stored = settings.load()
            # Match the runtime resolvers' env > file precedence
            # (config.tailor_provider() / score_jobs.load_scoring_config()): an
            # exported RESUME_TAILOR_PROVIDER / SCORE_PROVIDER wins at run time, so
            # Check-setup must honour it too or its warnings won't match what runs.
            tailor_provider = str(
                os.environ.get("RESUME_TAILOR_PROVIDER")
                or cfg.get("tailor_provider") or "gemini").strip().lower()
            if tailor_provider != "claude":  # gemini engine warnings only apply on gemini
                auth = cfg.get("gemini_auth", "vertex")
                project = stored.get("GOOGLE_CLOUD_PROJECT", "") or os.environ.get(
                    "GOOGLE_CLOUD_PROJECT", "")
                has_key = settings.secret_status().get(
                    "RESUME_TAILOR_GEMINI_API_KEY", False) or bool(
                        os.environ.get("RESUME_TAILOR_GEMINI_API_KEY"))
                problems.extend(f"[Engine] {w}" for w in
                                jobsdata._engine_credential_warnings(auth, project, has_key))
            scoring_provider = str(
                os.environ.get("SCORE_PROVIDER")
                or stored.get("provider") or "gemini").strip().lower()
            cli_found = shutil.which("claude") is not None
            problems.extend(f"[Engine] {w}" for w in jobsdata._claude_cli_warnings(
                tailor_provider, scoring_provider, cli_found))
        except Exception:  # noqa: BLE001
            pass
        if not problems:
            QtWidgets.QMessageBox.information(
                self, "Check setup", "All good — no problems found.")
            self._set_status("Setup check passed.")
        else:
            QtWidgets.QMessageBox.critical(
                self, "Check setup", "Problems found:\n\n- " + "\n- ".join(problems))
            self._set_status(f"Setup check: {len(problems)} problem(s) — see the list.")

    # ---- tracker extras ------------------------------------------------------

    def _tracker_followed_up(self) -> None:
        ids = self.tracker_tab.selected_ids()
        if not ids:
            self._set_status("Select tracker rows to mark followed up.")
            return
        self.registry.mark_followed_up(ids)
        self._refresh_tracker()
        self._set_status(f"Marked follow-up done on {len(ids)} job(s).")

    def _tracker_remove(self) -> None:
        ids = self.tracker_tab.selected_ids()
        if not ids:
            self._set_status("Select tracker rows to remove.")
            return
        if QtWidgets.QMessageBox.question(
                self, "Remove from tracker?",
                f"Remove {len(ids)} job(s) from the application tracker?"
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        for jid in ids:
            self.registry.clear_status(jid)
        self._refresh_tracker()
        self._set_status(f"Removed {len(ids)} job(s) from the tracker.")

    def _export_tracker(self) -> None:
        """Save a backup of the whole tracker DB (seen + statuses + résumé links)."""
        default = APPDATA / f"inployed-tracker-{date.today():%Y%m%d}.db"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export tracker", str(default), "SQLite database (*.db)")
        if not path:
            return
        try:
            dest = self.registry.export_to(Path(path))
        except Exception as e:  # noqa: BLE001 - surface any backup failure to the user
            self._set_status(f"Export failed: {e}")
            return
        self._set_status(f"Tracker exported → {dest}")

    def _import_tracker(self) -> None:
        """Merge a previously exported tracker backup into the current one."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import tracker", str(APPDATA),
            "SQLite database (*.db);;All files (*)")
        if not path:
            return
        if QtWidgets.QMessageBox.question(
                self, "Import tracker?",
                "Merge this backup into your current tracker? Existing entries are "
                "kept (a more recent status wins) and nothing is deleted."
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            counts = self.registry.import_from(Path(path))
        except Exception as e:  # noqa: BLE001 - surface any restore failure to the user
            self._set_status(f"Import failed: {e}")
            return
        self._refresh_tracker()
        QtWidgets.QMessageBox.information(
            self, "Tracker imported",
            f"Merged {counts['status']} tracked application(s), {counts['seen']} "
            f"seen id(s), and {counts['resume_paths']} résumé link(s).")

    def _tracker_prep(self) -> None:
        if getattr(self, "_prepping", False):
            return
        ids = self.tracker_tab.selected_ids() or self._selected_ids()
        if not ids:
            self._set_status("Select a job to generate an interview prep sheet for.")
            return
        job = self._job_payload(ids[0])
        if job is None:
            self._set_status("Job description not available — cannot build a prep sheet.")
            return
        resume_dir = self.registry.resume_path(ids[0])
        self._prepping = True
        self._apply_auth_env()
        self._set_status(f"Generating interview prep for {job['company_name']} — "
                         f"{job['job_title']} …")
        workers.run_async(self, lambda: self._prep_work(job, resume_dir),
                          on_done=self._finish_prep, on_error=self._finish_prep_error)

    def _prep_work(self, job: dict, resume_dir):
        from resume_tailor.prep import generate_prep_sheet
        out_dir = Path(resume_dir) if resume_dir and Path(resume_dir).exists() else None
        return generate_prep_sheet(job, out_dir)

    def _finish_prep(self, path) -> None:
        self._prepping = False
        self._set_status(f"Interview prep ready → {path}")
        try:
            osopen.open_path(path)
        except OSError:
            pass

    def _finish_prep_error(self, exc) -> None:
        self._prepping = False
        self._set_status(f"Interview prep FAILED — {exc}")

    # ---- stats + calibration -------------------------------------------------

    def _refresh_stats(self) -> None:
        stats_df = None
        root = gdrive_root_dir(self.csv_paths)
        path = (root / "run_stats.csv") if root else None
        if path and path.exists():
            try:
                stats_df = pd.read_csv(path)
            except (OSError, ValueError, pd.errors.ParserError):
                stats_df = None
        summary = "run_stats.csv not synced yet — metrics appear after the next VM run."
        table_df = pd.DataFrame()
        newest = None
        if stats_df is not None and not stats_df.empty:
            table_df = stats_df.iloc[::-1].reset_index(drop=True)  # newest first
            summary = self._stats_summary(stats_df)
            try:
                newest = datetime.fromisoformat(
                    str(stats_df.iloc[-1].get("timestamp", "")).strip())
            except ValueError:
                newest = None
        self.stats_tab.set_stats(table_df, summary, self._calibration_text())
        threshold = int(settings.load().get("stale_after_hours", 36) or 36)
        state, age = jobsdata.run_staleness(newest, datetime.now(), threshold)
        self.stats_tab.set_freshness(state, age)
        # Mirror the freshness onto the identity strip + status-bar summary.
        self._last_run_label = ("never" if age == float("inf")
                                else _human_age(age))
        label = ("Fresh — last run " if state == "fresh"
                 else "Stale — last run ") + self._last_run_label
        strip = getattr(self, "identity_strip", None)
        if strip is not None:
            strip.set_freshness(state, label)

    @staticmethod
    def _stats_summary(stats_df: pd.DataFrame) -> str:
        last = stats_df.iloc[-1]
        recent = stats_df.tail(7)
        empty = pd.Series(0, index=recent.index)
        tok = (pd.to_numeric(recent.get("prompt_tokens", empty), errors="coerce").fillna(0)
               + pd.to_numeric(recent.get("output_tokens", empty), errors="coerce").fillna(0))
        rows_in = pd.to_numeric(recent.get("rows_in", empty), errors="coerce").fillna(0)
        return (f"{len(stats_df)} run(s) logged · last: {last.get('timestamp', '?')} — "
                f"{last.get('rows_in', 0)} new, {last.get('llm_scored', 0)} scored · "
                f"7-run avg: {rows_in.mean():.0f} new, {tok.mean():,.0f} tokens/run")

    def _calibration_text(self) -> str:
        rows = self.registry.status_rows()
        if not rows:
            return ("Calibration: no labels yet — right-click a job -> Set status -> applied to "
                    "start building the applied-vs-recommendation dataset (target ~100 labels).")
        by_reco: Counter[str] = Counter()
        for r in rows:
            reco = self._cell(self._row_for(r["job_posting_id"]), "recommendation").strip().lower()
            by_reco[reco if reco in ("apply", "consider", "skip") else "unscored"] += 1
        parts = " · ".join(f"{k}: {v}" for k, v in by_reco.most_common())
        n = len(rows)
        note = " — enough to start tuning" if n >= 100 else f" (target ~100, at {n})"
        return f"Calibration: {n} labeled application(s){note} · by model reco — {parts}"

    # ---- settings ------------------------------------------------------------

    def _make_vm_panel(self, parent) -> VMPanel:
        """Build the VM operations panel mounted inside the Settings VM section."""
        return VMPanel(parent=parent)

    def _on_settings_saved(self) -> None:
        """Re-read the values the dashboard caches from config and refresh."""
        self.min_score = load_min_score()
        self.followup_days = load_followup_days()
        self.resume_data_tab._refresh_push_state()  # vm_enabled may have changed
        self.reload_data()

    # ---- engine env ----------------------------------------------------------

    def _apply_auth_env(self) -> None:
        """Seed the env var the in-process tailor reads at call time."""
        os.environ["RESUME_TAILOR_GEMINI_AUTH"] = jobsdata._load_cfg().get("gemini_auth", "vertex")

    # ---- misc ----------------------------------------------------------------

    def _set_status(self, msg: str) -> None:
        self.statusBar().showMessage(msg)

    def tab_count(self) -> int:
        return self.tabs.count()

    def tab_titles(self) -> list[str]:
        return [self.tabs.tabText(i) for i in range(self.tabs.count())]
