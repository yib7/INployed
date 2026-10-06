"""The dashboard main window: the eight-tab QTabWidget + a score-preview pane and
the global action bar.

The three job tabs (High Score / All Jobs / Tracker) are real `JobsTab`s wired to
the data and registry; Auto-apply mirrors the batch apply queue; Stats / Resume
Data / Apply Answers / Settings are filled in later phases. Long-running actions
(scrape, apply, tailor) run on a worker thread via `qt.workers.run_async`. The
score preview rides in a vertical splitter and is shown only on the job tabs; it
grows to ~half that splitter while the card's description pane is open and hands
the height back when it closes (`_on_description_toggled`).

`MainWindow` keeps the build, the scale bar, data loading, the banners, the file
watchers, the preview and the close. Its actions live in four bases, one module
each: `_PipelineActions` (`mw_pipeline`), `_TailorActions` (`mw_tailor`),
`_QueueActions` (`mw_queue`) and `_TrackerActions` (`mw_tracker`). A test that
patches a name one of those methods reads patches the module that holds it.
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections import Counter, namedtuple
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from PySide6 import QtCore, QtGui, QtWidgets

import apply_queue
import errmsg
import jobsdata
import settings
import setup_check
from csv_io import reconcile_is_seen
from jobsdata import (
    ALL_COLUMNS,
    HIGH_SCORE_COLUMNS,
    TRACKER_COLUMNS,
    drop_blocklisted,
    filter_high_unseen_with_count,
    find_google_drive_app,
    gdrive_root_dir,
    load_files,
    load_followup_days,
    load_hidden_columns,
    load_local_blocklist,
    load_min_score,
    load_repost_window_days,
)
from qt import theme, workers
from qt.answers_tab import AnswersEditor
from qt.apply_panel import ApplyPanel
from qt.apply_queue_panel import ApplyQueuePanel
from qt.chat_dialog import JobChatDialog
from qt.chrome import ChipBar, IdentityStrip
from qt.detail_card import JobDetailCard
from qt.jobs_tab import JobsTab
from qt.resume_data_tab import ResumeDataEditor
from qt.settings_tab import SettingsForm
from qt.stats_tab import StatsTab
from qt.vm_panel import VMPanel
from qt.widgets import ElidedLabel
from qt.plaintext import Label, literal
from qt.mw_pipeline import _PipelineActions
from qt.mw_queue import _QueueActions
from qt.mw_tailor import _TailorActions
from qt.mw_tracker import _TrackerActions
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

# The off-thread reload payload: the merged job frame + resume-path map, plus the
# run_stats frame and staleness threshold read alongside them so no UI-thread
# repaint ever touches the Drive-synced stats file.
# `problems` carries the (path, reason) pairs for sources that exist but could
# not be read, so the empty state can say "this file is unreadable" instead of
# "no jobs yet". Defaulted so the four-field construction older tests use, and
# the bare (df, id_to_path) tuple _apply_frames also accepts, both still work.
# `offline` carries (path, copy mtime or None, rows) for each Drive source whose
# folder was gone (Google Drive not running), shown from its saved copy.
LoadedFrames = namedtuple("LoadedFrames", "df id_to_path stats stale_hours problems offline",
                          defaults=((), ()))

# How long a cached resume-folder disk probe stays valid (resume
# folders can live under the Drive root, so per-selection stats must not hit the
# filesystem on every click). Cleared outright on every _apply_frames.
_DISK_CACHE_TTL_S = 20.0
# Hard cap on cached probes. Entries are per-key, and some keys are
# per-job, so an all-day triage session grew the dict without bound between
# reloads. Comfortably above the handful of live keys a session actually reuses.
_DISK_CACHE_MAX = 512


class MainWindow(QtWidgets.QMainWindow, _PipelineActions, _TailorActions, _QueueActions,
                 _TrackerActions):
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
        # The window closes only the seen.db it opened (closeEvent).
        self._owns_registry = registry is None
        self.setWindowTitle("INployed")
        self.setMinimumSize(1000, 660)

        self.min_score = load_min_score()
        self.repost_window_days = load_repost_window_days()
        self.followup_days = load_followup_days()
        self.hidden_columns = load_hidden_columns()
        self.df = pd.DataFrame()
        self.df_high = pd.DataFrame()
        self._reposts_hidden = 0
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
        # Open per-job chat windows, keyed by job id. They are PARENTED to this
        # window (so they die with it) and remove themselves from here on close —
        # a stale entry would be a Python wrapper over a deleted C++ object.
        self._chat_dialogs: dict[str, "JobChatDialog"] = {}
        # Detail-pane growth while the card's description is open: the state the
        # last descriptionToggled reported, and the splitter sizes to hand back
        # when it closes (None = nothing to restore).
        self._preview_desc_open = False
        self._preview_prev_sizes: list[int] | None = None
        self._preview_shown = False
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
            on_ask_ai=self._ask_ai_for,
            hidden_columns=self.hidden_columns,
            save_hidden=self._save_hidden,
        )

    # The two states the empty panel can be in. "Nothing yet" is the first run;
    # "unreadable" is a source file that exists and could not be parsed, which
    # used to render identically to the first run and so told a user with a
    # half-synced 37 MB master that they had no jobs.
    EMPTY_FIRST_RUN = ("No jobs yet",
                       "Three steps: set your keys and folders in Settings, fetch "
                       "and score new jobs, then add your résumé data so jobs get "
                       "matched to you.")
    EMPTY_UNREADABLE_TITLE = "Your job file could not be read"
    EMPTY_OFFLINE_TITLE = "Google Drive is not running"

    def _build_empty_hint(self) -> QtWidgets.QWidget:
        """First-run hint shown on the High Score tab when no jobs are loaded yet."""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.addStretch(1)
        title = Label(self.EMPTY_FIRST_RUN[0])
        title.setProperty("heading", True)
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)
        msg = Label(self.EMPTY_FIRST_RUN[1])
        msg.setWordWrap(True)
        msg.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        msg.setProperty("muted", True)
        v.addWidget(msg)
        self._empty_title, self._empty_msg = title, msg
        self._refresh_empty_hint()
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

    def unreadable_sources_message(self) -> str:
        """What the empty panel says when every source that exists failed to parse.

        Names the file and the parser's own reason, because "check your Drive" is
        no use without knowing WHICH file is broken. Kept a plain string
        so the wording is testable without building a window."""
        problems = tuple(getattr(self, "_load_problems", ()) or ())
        if not problems:
            return ""
        lines = [f"{Path(p).name} ({reason})" for p, reason in problems[:3]]
        if len(problems) > 3:
            lines.append(f"and {len(problems) - 3} more")
        return (
            f"{len(problems)} job file(s) exist but could not be read, so nothing "
            "is showing. This is usually a sync that has not finished or a file "
            "that was cut short, not lost data. Wait for Google Drive to finish, "
            "then press Refresh; if it persists, delete the file below and let the "
            "next run re-sync it.\n\n" + "\n".join(lines))

    def offline_message(self) -> str:
        """What the Drive banner says while a Drive source's folder is gone.

        Names the file by folder and file name only (the full path can carry the
        user's name into screenshots), says how old the copy on screen is, and
        says the dashboard recovers by itself once Drive runs."""
        offline = tuple(getattr(self, "_offline", ()) or ())
        if not offline:
            return ""
        lines = []
        for path, saved_at, rows in offline:
            name = str(Path(Path(path).parent.name, Path(path).name))
            if saved_at is None:
                lines.append(f"{name} is missing and its jobs are hidden.")
            else:
                dt = datetime.fromtimestamp(saved_at)
                when = f"{dt:%b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M %p}"
                lines.append(f"{name} is missing, so this shows its saved copy from "
                             f"{when} ({rows:,} jobs).")
        return ("Google Drive for desktop is not running. " + " ".join(lines)
                + " Start Google Drive and the dashboard switches back by itself.")

    def _refresh_offline_banner(self) -> None:
        banner = getattr(self, "offline_banner", None)
        if banner is None:
            return
        text = self.offline_message()
        self.offline_label.setText(text)
        self.offline_start_btn.setVisible(bool(text) and find_google_drive_app() is not None)
        if not text:
            self.offline_start_btn.setEnabled(True)  # ready for the next outage
        banner.setVisible(bool(text))

    def _start_google_drive(self) -> None:
        """Launch Google Drive for desktop; the 15s source poll reloads once its
        drive mounts."""
        exe = find_google_drive_app()
        if exe is None:
            return
        try:
            os.startfile(str(exe))  # detached; Windows only, like the app itself
        except OSError as exc:
            self._set_status(f"Could not start Google Drive: {errmsg.for_user(exc)}")
            return
        self.offline_start_btn.setEnabled(False)
        self._set_status("Starting Google Drive. The jobs come back once it finishes loading.")

    def _refresh_empty_hint(self) -> None:
        """Swap the empty panel between its first-run and its unreadable wording."""
        title = getattr(self, "_empty_title", None)
        msg = getattr(self, "_empty_msg", None)
        if title is None or msg is None:
            return
        problem_text = self.unreadable_sources_message()
        offline_text = self.offline_message()
        if problem_text:
            title.setText(self.EMPTY_UNREADABLE_TITLE)
            msg.setText(problem_text)
        elif offline_text:
            title.setText(self.EMPTY_OFFLINE_TITLE)
            msg.setText(offline_text)
        else:
            title.setText(self.EMPTY_FIRST_RUN[0])
            msg.setText(self.EMPTY_FIRST_RUN[1])

    def _answer_now(self, prefill: dict | None = None) -> bool:
        """A parked job's Answer now: Add answer on the Apply Answers tab,
        prefilled with the parked question. Accepting it writes that answer
        alone to the store (`AnswersEditor.answer_now`), so other unsaved edits
        on the tab are neither saved nor in its way. True once the answer is on
        disk (the Auto-apply tab then offers Re-queue). With no question kept,
        the tab opens."""
        self._show_tab("Apply Answers")
        if not prefill:
            return False
        return bool(self.answers_tab.answer_now(prefill))

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
        h.addWidget(Label("Interface size"))
        minus = QtWidgets.QPushButton("-")
        minus.setProperty("compact", True)  # zero side padding so the glyph fits
        minus.setFixedWidth(26)
        minus.setToolTip("Smaller (-10%)")
        minus.clicked.connect(lambda: self._nudge_scale(-10))
        h.addWidget(minus)
        self._scale_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._scale_slider.setAccessibleName("Interface size")
        self._scale_slider.setMinimum(75)
        self._scale_slider.setMaximum(150)
        self._scale_slider.setSingleStep(10)
        self._scale_slider.setPageStep(10)
        self._scale_slider.setFixedWidth(140)
        self._scale_slider.setValue(self._ui_scale_pct)
        self._scale_slider.valueChanged.connect(self._on_scale_slider)
        h.addWidget(self._scale_slider)
        plus = QtWidgets.QPushButton("+")
        plus.setProperty("compact", True)  # zero side padding so the glyph fits
        plus.setFixedWidth(26)
        plus.setToolTip("Larger (+10%)")
        plus.clicked.connect(lambda: self._nudge_scale(10))
        h.addWidget(plus)
        self._scale_readout = Label(f"{self._ui_scale_pct}%")
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
        """Clamp to [theme.MIN_SCALE, theme.MAX_SCALE] (75..150%), re-scale the
        live UI (font only — fast), sync the bar, and persist via jobsdata."""
        pct = max(75, min(150, int(pct)))
        previous = self._ui_scale_pct
        self._ui_scale_pct = pct
        theme.set_scale(QtWidgets.QApplication.instance(), pct / 100.0)
        self._rescale_geometry(pct / previous if previous else 1.0)
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

    def _rescale_geometry(self, factor: float) -> None:
        """Re-scale the pixel geometry that was sized off the interface scale.

        `theme.set_scale` re-scales what rides on the font — type, row heights,
        every painted metric — but three things are pixel values applied ONCE,
        at construction, from the scale that was current then: each table's
        column widths, and the height this window's vertical splitter hands the
        detail card. Both were already scale-aware at startup (see
        `JobsTab._set_column_widths` and the `setSizes` below `self.splitter`),
        so the bug only appeared on the LIVE path — drag the interface-size
        slider and the type grows inside geometry that doesn't: at 150% the High
        Score header clipped to "cor" / "licants" and the detail card kept its
        100% height, which cut the last STRENGTHS line through the middle. A
        restart then fixed it, which is why it survived this long.

        Multiplicative, so a column the user dragged wider stays proportionally
        wider. Best-effort: a geometry hiccup must never break the scale change
        itself, which has already been applied by the time this runs.
        """
        if factor <= 0 or abs(factor - 1.0) < 1e-9:
            return
        for tab in (self.high_tab, self.all_tab, self.tracker_tab):
            try:
                tab.rescale_columns(factor)
            except Exception:  # noqa: BLE001 - cosmetic; never break rescaling
                pass
        panel = getattr(self, "apply_queue_panel", None)
        if panel is not None:
            try:
                panel.rescale_columns(factor)
            except Exception:  # noqa: BLE001
                pass
        sizes = self.splitter.sizes()
        if len(sizes) == 2 and sizes[1] > 0:
            total = sizes[0] + sizes[1]
            detail = min(round(sizes[1] * factor), max(0, total - 120))
            self.splitter.setSizes([total - detail, detail])

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

        # Shown while a Drive source's folder is gone (Google Drive not running):
        # which file, how old the copy on screen is, and a button to start Drive.
        self.offline_banner = QtWidgets.QFrame()
        self.offline_banner.setProperty("callout", "warning")
        ob = QtWidgets.QHBoxLayout(self.offline_banner)
        self.offline_label = Label("")
        self.offline_label.setWordWrap(True)
        ob.addWidget(self.offline_label, 1)
        self.offline_start_btn = QtWidgets.QPushButton("Start Google Drive")
        self.offline_start_btn.clicked.connect(self._start_google_drive)
        ob.addWidget(self.offline_start_btn, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        vbox.addWidget(self.offline_banner)
        self.offline_banner.setVisible(False)

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
        # The Auto-apply tab mirrors the batch apply queue. Its mutations
        # ride the background write queue via _submit_queue_write; the queue
        # path is resolved by apply_queue at call time (APPLY_QUEUE_PATH-aware).
        self.apply_queue_panel = ApplyQueuePanel(
            submit_write=self._submit_queue_write,
            on_set_password=self._set_ats_password,
            on_mark_applied=self._apply_queue_mark_applied,
            on_mark_seen=self._apply_queue_mark_seen,
            on_answer_now=self._answer_now)
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
        self._page_policies = {page: page.sizePolicy() for page in self._tab_widgets.values()}

        self.high_tab.set_empty_widget(self._build_empty_hint())

        # The detail card is bound to `self.preview` because the splitter wiring and
        # _apply_preview_visibility both key off that attribute name.
        # The card OWNS the Tailor/Apply buttons; the aliases below give every
        # enable/repolish path a stable object identity to work with.
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
        # The detail card needs ~300px @100%, and every line inside it scales with
        # the interface size — at 150% the unscaled 300 clipped the last strength
        # bullet in half. Scale the initial split the same way the card's contents
        # scale (a drag still overrides it; only the starting point is set here).
        _s = theme._current_scale
        self.splitter.setSizes([round(640 * _s), round(300 * _s)])
        # Opening the card's description grows this pane to ~half the splitter and
        # closing it hands the height back (see _on_description_toggled). The card
        # never reaches up into its parent; it just says which state it is in.
        self.preview.descriptionToggled.connect(self._on_description_toggled)
        self.splitter.splitterMoved.connect(self._on_preview_splitter_moved)

        # The Apply panel rides to the RIGHT of the tabs+preview column; it opens
        # (and the preview hides) when the user clicks Apply, and closes back to the
        # preview via its own ✕. Hidden until then.
        self._apply_panel_open = False
        self._apply_panel_job: dict = {}
        self.apply_panel = ApplyPanel(on_close=self._close_apply_panel,
                                      on_applied=self._mark_applied_from_panel,
                                      on_ask_ai=self._ask_ai_from_panel)
        self.apply_panel.hide()
        self.hsplit = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.hsplit.addWidget(self.splitter)
        self.hsplit.addWidget(self.apply_panel)
        self.hsplit.setStretchFactor(0, 1)
        self.hsplit.setStretchFactor(1, 0)
        vbox.addWidget(self.hsplit, 1)
        # Connect only now that self.preview exists (addTab above fires currentChanged).
        self.tabs.currentChanged.connect(lambda _i: self._on_tab_changed())
        self._size_tabs_by_current_page()

        vbox.addLayout(self._build_action_bar())
        self._setup_zoom_shortcuts()  # Ctrl +/-/0 mirror the bottom scale bar
        # The status bar is just the transient message line now (the interface-size
        # control moved up into the single bottom action bar).
        self.setStatusBar(QtWidgets.QStatusBar())

    def _build_action_bar(self) -> QtWidgets.QHBoxLayout:
        bar = QtWidgets.QHBoxLayout()
        tip = ElidedLabel("Ctrl/Shift-click for multiple · Ctrl+A selects all · "
                          "double-click opens · right-click for status (incl. applied) / block")
        tip.setProperty("muted", True)
        # The hint is the one thing in this bar that gives ground when the window
        # is narrow (ElidedLabel = Ignored policy, no minimum), so the controls to
        # its right keep their full width instead of every element shrinking
        # together. It ELIDES rather than clipping: at 1100px the plain QLabel
        # rendered a bare "Ct".
        # It also carries the bar's only stretch, so it shows in full on a wide
        # window and simply gives ground as the window narrows.
        bar.addWidget(tip, 1)

        def button(text, slot, accent=False):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            if accent:
                b.setProperty("accent", True)
            bar.addWidget(b)
            return b

        # Tailor/Apply live on the job detail card, not here — the bar keeps
        # the selection-utility actions, with Find new jobs as its primary.
        button("Mark seen (selected)", self._mark_seen_selected)
        self.btn_undo_seen = button("Undo seen", self._undo_seen)
        button("Resume folder", self._open_resume_folder)
        button("Check setup", self._check_setup)
        self.btn_queue_apply = button("Queue auto-apply", self._queue_apply_selected)
        self.btn_queue_apply.setToolTip(
            "Add the selected job(s) to the auto-apply queue (untailored ones are "
            "tailored first); see the Auto-apply tab")
        button("Find new jobs", self._run_scraper_dialog, accent=True)

        # One bottom panel: the interface-size control and a Restart button ride in
        # the same bar as the actions, so the window has a single control strip.
        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        sep.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        bar.addWidget(sep)
        scale_bar = self._build_scale_bar()
        # Never squeeze the scale control: at a 1024-wide window the label used
        # to clip to "Inter" and the readout to "110".
        scale_bar.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed,
                                QtWidgets.QSizePolicy.Policy.Preferred)
        bar.addWidget(scale_bar)
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
        # Pipeline chip bar: exclusive status chips below the filter
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
                continue   # still tailoring, or never finished — leave it
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
                "last session; now queued for auto-apply.")
            panel = getattr(self, "apply_queue_panel", None)
            if panel is not None:
                panel.refresh()
        return healed

    def _load_frames(self):
        """The blocking half of a reload, safe to run OFF the UI thread: read and
        merge the source files, drop blocklisted rows, and read run_stats.csv +
        the staleness threshold (a synchronous Drive read here would run on
        every UI-thread repaint). Touches neither Qt nor the SQLite registry
        (both thread-affine) — those wait for _apply_frames."""
        problems: list[tuple[Path, str]] = []
        offline: list[tuple[Path, float | None, int]] = []
        df, id_to_path = load_files(self.csv_paths, problems=problems, offline=offline)
        df = drop_blocklisted(df, load_local_blocklist(self.csv_paths))
        stats_df = None
        root = gdrive_root_dir(self.csv_paths)
        stats_path = (root / "run_stats.csv") if root else None
        if stats_path and stats_path.exists():
            try:
                stats_df = pd.read_csv(stats_path)
            except (OSError, ValueError, pd.errors.ParserError):
                stats_df = None
        stale_hours = int(settings.load().get("stale_after_hours", 36) or 36)
        return LoadedFrames(df, id_to_path, stats_df, stale_hours, tuple(problems),
                            tuple(offline))

    def _apply_frames(self, loaded) -> None:
        """The UI-thread half of a reload: overlay the seen registry, install the
        frame (+ the off-thread stats read), and refresh every derived view."""
        if isinstance(loaded, LoadedFrames):
            df, id_to_path = loaded.df, loaded.id_to_path
            self._stats_df = loaded.stats
            self._stale_hours = loaded.stale_hours
            self._load_problems = tuple(loaded.problems or ())
            self._offline = tuple(loaded.offline or ())
        else:  # bare (df, id_to_path) — older callers/tests
            df, id_to_path = loaded
            self._load_problems = ()
            self._offline = ()
        self._refresh_empty_hint()
        self._refresh_offline_banner()
        self._disk_cache = {}   # resume-folder stats may be stale
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
        if self._reposts_hidden > 0:
            noun = "repost" if self._reposts_hidden == 1 else "reposts"
            parts.append(f"{self._reposts_hidden} {noun} hidden")
        if self._last_run_label:
            parts.append(f"last discovery run {self._last_run_label}")
        # A partial failure never reaches the empty panel (the frame is not
        # empty), so the status bar is the only place it can be said at all.
        n_bad = len(getattr(self, "_load_problems", ()) or ())
        if n_bad:
            parts.append(f"{n_bad} source file(s) unreadable")
        if getattr(self, "_offline", ()):
            parts.append("Google Drive offline")
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
        # Repost suppression runs here on the UI thread. On a 30k-row frame the
        # key computation takes ~30 ms with pyarrow and ~90 ms on the pandas-only
        # fallback; the whole High Score filter lands at ~60 ms and ~100 ms.
        # pyarrow stays optional; that cost per
        # refresh is the accepted trade for one fewer hard dependency.
        self.df_high, self._reposts_hidden = filter_high_unseen_with_count(
            df, self.min_score, marked_at=self.registry.marked_at_all(),
            window_days=self.repost_window_days)
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
        self._set_status(f"Could not load jobs: {errmsg.for_user(exc)}")
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

    def _disk_cached(self, key, compute):
        """Serve `compute()` through the short-TTL disk-probe cache:
        resume folders can live under the Drive root, so per-selection existence
        checks must not stat the filesystem on every click. `_apply_frames`
        clears the cache outright, so a reload always re-probes."""
        cache = getattr(self, "_disk_cache", None)
        if cache is None:
            cache = self._disk_cache = {}
        hit = cache.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[1] < _DISK_CACHE_TTL_S:
            return hit[0]
        value = compute()
        cache[key] = (value, now)
        # Bound the cache. Keys include per-job entries, so a long
        # triage session over a tens-of-thousands-row master added one entry per
        # job clicked and only ever shrank on a full reload. Every entry past the
        # TTL is dead weight by definition — drop those first, and if the cache is
        # still over the cap, drop the oldest.
        if len(cache) > _DISK_CACHE_MAX:
            for k in [k for k, v in cache.items() if now - v[1] >= _DISK_CACHE_TTL_S]:
                del cache[k]
            if len(cache) > _DISK_CACHE_MAX:
                for k in sorted(cache, key=lambda k: cache[k][1])[:len(cache) - _DISK_CACHE_MAX]:
                    del cache[k]
        return value

    def _resume_ids(self) -> frozenset:
        # Only ids whose tailored folder still EXISTS on disk are tinted blue, so a
        # folder deleted by hand drops its tint on the next reload (jobsdata keeps
        # the registry row — the tint returns if the folder comes back).
        def probe() -> frozenset:
            try:
                return frozenset(jobsdata.live_resume_ids(self.registry.resume_paths()))
            except Exception:  # noqa: BLE001 - cosmetic; never break the view
                return frozenset()
        return self._disk_cached("resume_ids", probe)

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

    def _size_tabs_by_current_page(self) -> None:
        """A QTabWidget is as tall at minimum as its tallest page, so a tall
        Auto-apply page (the Jev notice, a waiting pause) took its height out of
        the job detail card under every other tab. A hidden page drops out of
        the minimum; the page on show keeps its own."""
        current = self.tabs.currentWidget()
        ignored = QtWidgets.QSizePolicy.Policy.Ignored
        for page, policy in self._page_policies.items():
            if page is current:
                page.setSizePolicy(policy)
            else:
                page.setSizePolicy(ignored, ignored)

    def _on_tab_changed(self) -> None:
        self._size_tabs_by_current_page()
        self._apply_preview_visibility()
        # Re-render the detail card from the NEW tab's selection so its variant
        # (discovery vs tracker) always matches the tab it is shown under; this
        # also recomputes the Apply button state (_show_preview does both).
        tab = self._active_jobs_tab()
        ids = tab.selected_ids() if tab is not None else []
        self._show_preview(ids[0] if ids else "")
        # The resume.md push-button state is driven by the settings-saved path
        # (_on_settings_saved → refresh_push_state), not by tab switches — the old
        # per-switch refresh did settings.load() + VMTarget.from_env() on every
        # tab change.

    def _apply_preview_visibility(self) -> None:
        title = self.tabs.tabText(self.tabs.currentIndex())
        show = title in PREVIEW_TABS and not getattr(self, "_apply_panel_open", False)
        self.preview.setVisible(show)
        self._preview_shown = show

    def _on_preview_splitter_moved(self, *_args) -> None:
        """A drag of the outer divider is the user's newest word on how tall the
        detail pane should be, so it retires the pre-expand memory: collapsing
        the description then leaves their size alone instead of yanking it back.
        Only real drags land here — setSizes() and window resizes do not emit
        splitterMoved, so this never discards a size the window itself set."""
        self._preview_prev_sizes = None

    def _on_description_toggled(self, expanded: bool) -> None:
        """Grow the detail pane to ~half the splitter while the card's
        description is open, and give the height back when it closes.

        The signal fires from INSIDE JobDetailCard.set_fields/set_empty, i.e.
        mid selection-update, so this only ever touches the splitter — calling
        back into the card would recurse. Repeats of the state already recorded
        are dropped, which is what makes a sticky expand across job selections
        (and a discovery↔Tracker switch) a no-op here."""
        expanded = bool(expanded)
        if expanded == self._preview_desc_open:
            return
        self._preview_desc_open = expanded
        if not expanded:
            # Restore even while the pane is hidden: a tab switch to Settings
            # hides it and THEN clears the card, so the collapse routinely
            # arrives with the preview already gone. A hidden pane reports 0
            # but setSizes still lands on the splitter's sizer, and that is what
            # comes back when the pane is re-shown. Dropping the restore here
            # instead would leave the pane stuck at half for the whole session.
            prev, self._preview_prev_sizes = self._preview_prev_sizes, None
            if prev:
                self.splitter.setSizes(prev)
            return
        if not self._preview_shown:
            # The Apply panel is open (or this isn't a preview tab), so the pane
            # is hidden and reports 0 height: growing it would fight
            # _apply_preview_visibility, and recording [n, 0] would restore a
            # zero-height pane on the way out.
            self._preview_prev_sizes = None
            return
        # A pane that was just un-hidden still reports its hidden [n, 0] until
        # the next layout pass — and _on_tab_changed re-shows it and re-renders
        # the card in the SAME turn, so that stale read is the common case here.
        # refresh() recomputes the splitter's ranges in place; unlike
        # processEvents it delivers no events, so nothing can re-enter.
        self.splitter.refresh()
        sizes = self.splitter.sizes()
        if len(sizes) != 2:
            return
        self._preview_prev_sizes = list(sizes)
        target = sum(sizes) // 2
        if sizes[1] >= target:
            return          # already at least half — never shrink what the user grew
        self.splitter.setSizes([sum(sizes) - target, target])

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
            next_step = (f"No reply in {days_n} day(s): send a short follow-up "
                         "note, then Mark followed up.")
        elif follow == "done":
            next_step = "Followed up; awaiting a reply."
        elif status == "interviewing":
            next_step = "Interview ahead: generate an Interview prep sheet."
        elif status == "offer":
            next_step = "Offer open: respond and update the status."
        elif status == "rejected":
            next_step = "Rejected: no action needed."
        elif status == "applied" and days_n is not None:
            wait = max(0, self.followup_days - days_n)
            next_step = (f"Applied {days_n} day(s) ago: follow up in {wait} "
                         "day(s) if there is no reply.")
        else:
            next_step = ""
        return {"status": status, "applied_date": r.get("applied_date") or "",
                "days": "" if days_n is None else str(days_n),
                "follow_up": follow, "next_step": next_step}

    # ---- apply readiness (button enable + green) -----------------------------

    def _apply_ready(self, jid: str) -> tuple[bool, Path | None]:
        """A job is ready to apply to when its tailored folder holds BOTH the
        résumé PDF and apply.md on disk. Returns (ready, folder). Served through
        the short-TTL disk cache — selection changes must not stat Drive."""
        if not jid:
            return False, None
        return self._disk_cached(("apply_ready", jid),
                                 lambda: self._apply_ready_probe(jid))

    def _apply_ready_probe(self, jid: str) -> tuple[bool, Path | None]:
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
            literal(f"Could not update the job files ({description}): {errmsg.for_user(exc)}\n\n"
            "Reloading the dashboard from disk."))
        self.reload_data_async()

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
                        "The tailoring run didn't finish in time; it will be "
                        "recovered on the next launch.")
        q = getattr(self, "_writes", None)
        if q is not None and not q.is_idle():
            self._set_status("Finishing background writes …")
            if not q.drain(timeout_ms=30000):
                QtWidgets.QMessageBox.warning(
                    self, "Writes still pending",
                    literal(f"{q.pending_count()} background write(s) did not finish; the "
                    "files on disk may be missing your last mark-seen/delete."))
        if self._owns_registry:
            # close() checkpoints the WAL; a connection left to the garbage
            # collector raised a ResourceWarning at exit.
            self._owns_registry = False
            try:
                self.registry.close()
            except sqlite3.Error:
                pass
        super().closeEvent(event)

    def _scrape_in_flight(self) -> bool:
        """True only when a scrape/score run is still executing — the
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
        """True only when a tailor run is still executing — the
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

    # ---- check setup ---------------------------------------------------------

    def _check_setup(self) -> None:
        # One probe at a time. The job-data half runs on a worker thread and can
        # sit on a 15-second timeout, so an impatient double-click would queue a
        # second thread and pop a second modal behind the first.
        if getattr(self, "_setup_check_running", False):
            self._set_status("Setup check already running...")
            return
        try:
            problems = setup_check.local_problems()
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Check setup", literal(f"Could not run checks: {errmsg.for_user(exc)}"))
            return
        # Everything above is local file reads. The job-data check is a network
        # call and the claude CLI version check starts a subprocess, so both go
        # to a worker thread. A blocking probe here would freeze the window,
        # which is exactly the startup bug this dashboard already had.
        self._set_status("Checking setup...")
        self._setup_check_running = True
        workers.run_async(
            self, setup_check.worker_problems,
            on_done=lambda extra: self._show_setup_result(problems + list(extra or [])),
            on_error=lambda _exc: self._show_setup_result(problems))

    def _show_setup_result(self, problems: list[str]) -> None:
        # Cleared before the modal, not after: the dialog blocks until dismissed,
        # and a flag still set behind it would refuse the next click.
        self._setup_check_running = False
        if not problems:
            # The credential checks above read the FILE (settings.load /
            # secret_status), while the tailor and the scorer read this process's
            # os.environ — a snapshot of .env taken once at launch (local/app.py).
            # So right after a Settings save this can truthfully report a key as
            # present while the engine is still using the old one. Rather than
            # make the check lie in the other direction, say which reading it is.
            QtWidgets.QMessageBox.information(
                self, "Check setup",
                "All good: no problems found.\n\nThis reads your saved settings "
                "files. If you changed a key, model or path in Settings since "
                "launching, restart the dashboard for it to actually be used.")
            self._set_status("Setup check passed.")
        else:
            # Same launch-snapshot caveat as the all-good branch, and it matters
            # MORE here: scraper.API_TOKEN is bound once at import, so right after
            # a rotation this reports the OLD token as dead and looks like the new
            # one failed. The message a mid-rotation user sees is the one that
            # needs the hint, so it gets it too.
            QtWidgets.QMessageBox.critical(
                self, "Check setup", literal("Problems found:\n\n- " + "\n- ".join(problems)
                + "\n\nThese are checked against the settings this dashboard loaded "
                "at launch. If you just changed a key or path, restart the "
                "dashboard and check again before chasing one of these."))
            self._set_status(f"Setup check: {len(problems)} problem(s); see the list.")

    # ---- settings ------------------------------------------------------------

    def _make_vm_panel(self, parent) -> VMPanel:
        """Build the VM operations panel mounted inside the Settings VM section."""
        return VMPanel(parent=parent)

    def _on_settings_saved(self) -> None:
        """Re-read the values the dashboard caches from config and refresh."""
        self.min_score = load_min_score()
        self.repost_window_days = load_repost_window_days()
        self.followup_days = load_followup_days()
        self.resume_data_tab.refresh_push_state()  # vm_enabled may have changed
        self.answers_tab.refresh_test_answers_state()  # the judge mode or key may have changed
        self.apply_queue_panel.refresh_jev_state()  # Start follows the Jev switch
        self.reload_data_async()

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
