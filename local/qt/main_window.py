"""The dashboard main window: the seven-tab QTabWidget + a score-preview pane and
the global action bar.

The three job tabs (High Score / All Jobs / Tracker) are real `JobsTab`s wired to
the data and registry; Stats / Resume Data / Apply Answers / Settings are filled in
later phases. Long-running actions (scrape, apply, tailor) run on a worker thread
via `qt.workers.run_async`. The score preview rides in a vertical splitter and is
shown only on the job tabs.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from PySide6 import QtCore, QtWidgets

import chrome
import jobsdata
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
from qt.jobs_tab import JobsTab
from qt.resume_data_tab import ResumeDataEditor
from qt.settings_tab import SettingsForm
from qt.stats_tab import StatsTab
from qt.vm_panel import VMPanel
from qt.widgets import ScorePreview
from seen_db import SeenRegistry

TAB_TITLES = [
    "High Score (Unseen)",
    "All Jobs",
    "Tracker",
    "Stats",
    "Resume Data",
    "Apply Answers",
    "Settings",
]

# Tabs where a selected row has an analysis worth previewing.
PREVIEW_TABS = {"High Score (Unseen)", "All Jobs", "Tracker"}

# Tailoring is parallel (all selected at once). Above this many, warn first — a big
# fan-out means that many simultaneous Gemini calls + pdflatex processes (API limits /
# local load). Below it, just go. See .autopilot/DECISIONS.md (cycle 11, SP3).
PARALLEL_WARN_THRESHOLD = 5


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
        self.id_to_path: dict[str, Path] = {}
        self._row_by_id: dict[str, int] = {}
        self._url_by_id: dict[str, str] = {}
        self._tracked: dict[str, dict] = {}
        # Undo stack for mark-seen: each entry is the list of ids a single
        # mark-seen action newly added, so undo reverts exactly that action.
        self._seen_undo: list[list[str]] = []
        self._ui_scale_pct = jobsdata.load_ui_scale_pct()
        self.tailor_progress.connect(self._set_status)

        self._build()
        self.reload_data()
        self._setup_fs_watcher()
        self._apply_preview_visibility()

    # ---- construction --------------------------------------------------------

    def _make_jobs_tab(self, key: str, columns) -> JobsTab:
        return JobsTab(
            key, columns,
            on_open_url=self._open_url,
            on_set_status=self._set_status_for,
            on_block=self._block_company,
            on_selection=self._show_preview,
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
            "Get started in three steps: set your keys and folders in Settings, run "
            "the scraper to fetch and score jobs, then add your résumé data so jobs "
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
        b_scrape = QtWidgets.QPushButton("Run scraper")
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
        """The persistent 'Interface size' control in the status bar: −/+ buttons
        (10% steps) and a slider (50-200%). All drive `_apply_scale`."""
        bar = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(QtWidgets.QLabel("Interface size:"))
        minus = QtWidgets.QPushButton("−")  # minus sign
        minus.setFixedWidth(26)
        minus.setToolTip("Smaller (-10%)")
        minus.clicked.connect(lambda: self._nudge_scale(-10))
        h.addWidget(minus)
        self._scale_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._scale_slider.setMinimum(50)
        self._scale_slider.setMaximum(200)
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
        snapped = max(50, min(200, round(value / 10) * 10))
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
        pct = max(50, min(200, int(pct)))
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

    def _build(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        vbox = QtWidgets.QVBoxLayout(central)
        vbox.setContentsMargins(8, 8, 8, 8)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)

        self.high_tab = self._make_jobs_tab("high", HIGH_SCORE_COLUMNS)
        self.all_tab = self._make_jobs_tab("all", ALL_COLUMNS)
        self.tracker_tab = self._make_jobs_tab("tracker", TRACKER_COLUMNS)
        self._setup_tracker_toolbar()
        self.stats_tab = StatsTab()
        self.settings_tab = SettingsForm(on_saved=self._on_settings_saved,
                                         vm_panel_factory=self._make_vm_panel)
        self.resume_data_tab = ResumeDataEditor()
        self.answers_tab = AnswersEditor()
        self._tab_widgets: dict[str, QtWidgets.QWidget] = {}
        pages = {"High Score (Unseen)": self.high_tab, "All Jobs": self.all_tab,
                 "Tracker": self.tracker_tab, "Stats": self.stats_tab,
                 "Resume Data": self.resume_data_tab, "Apply Answers": self.answers_tab,
                 "Settings": self.settings_tab}
        for title in TAB_TITLES:
            page = pages.get(title) or QtWidgets.QWidget()
            self._tab_widgets[title] = page
            self.tabs.addTab(page, title)

        self.high_tab.set_empty_widget(self._build_empty_hint())

        self.preview = ScorePreview()
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.splitter.addWidget(self.tabs)
        self.splitter.addWidget(self.preview)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([720, 200])

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
        self.setStatusBar(QtWidgets.QStatusBar())
        # Persistent interface-size control, pinned to the right of the status bar.
        self.statusBar().addPermanentWidget(self._build_scale_bar())

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

        self.btn_tailor = button("Tailor resume", self._tailor_selected, accent=True)
        button("Mark seen (selected)", self._mark_seen_selected)
        self.btn_undo_seen = button("Undo seen", self._undo_seen)
        button("Resume folder", self._open_resume_folder)
        button("Run scraper", self._run_scraper_dialog)
        button("Check setup", self._check_setup)
        # Apply is rightmost (and green only once the job is ready to apply to) — its
        # ready-state is the dashboard's "this one's good to go" signal.
        self.btn_apply = button("Apply", self._apply_selected)
        self.btn_apply.setEnabled(False)
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
        # Set status lives on the right-click menu (it was redundant as a button here).
        self.tracker_tab.add_toolbar_button("Mark followed up", self._tracker_followed_up)
        self.tracker_tab.add_toolbar_button("Interview prep", self._tracker_prep)
        self.tracker_tab.add_toolbar_button("Remove", self._tracker_remove)
        self.tracker_tab.add_toolbar_button("Export tracker…", self._export_tracker)
        self.tracker_tab.add_toolbar_button("Import tracker…", self._import_tracker)

    # ---- data ----------------------------------------------------------------

    def reload_data(self) -> None:
        df, id_to_path = load_files(self.csv_paths)
        df = drop_blocklisted(df, load_local_blocklist(self.csv_paths))
        self.id_to_path = id_to_path
        if not df.empty:
            if "is_seen" not in df.columns:
                df["is_seen"] = "no"
            df, _ = reconcile_is_seen(df, self.registry)
        self.df = df
        self._row_by_id = ({jid: i for i, jid in enumerate(df["job_posting_id"])}
                           if not df.empty else {})
        self._url_by_id = (dict(zip(df["job_posting_id"].astype(str), df["url"].astype(str)))
                           if not df.empty and "url" in df.columns else {})
        self.df_high = filter_high_unseen(df, self.min_score)
        resume_ids = self._resume_ids()
        self.high_tab.set_source_df(self.df_high, resume_ids)
        self.all_tab.set_source_df(df, resume_ids)
        self._refresh_tracker()
        self._refresh_stats()
        total = 0 if df.empty else len(df)
        self._set_status(f"{total:,} jobs · {len(self.df_high)} unseen >=4")
        self._refresh_apply_button()  # a freshly tailored job may now be apply-ready
        # Sources/folder may have changed (a local scrape appended paths) — keep the
        # auto-refresh watcher pointed at the current files. No-op before setup.
        if getattr(self, "_fs_watcher", None) is not None:
            self._rearm_watcher()

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
        if getattr(self, "tracker_due_only", None) is not None and self.tracker_due_only.isChecked():
            recs = [r for r in recs if r["follow_up"] == "DUE"]
        cols = [c for c, _ in TRACKER_COLUMNS] + ["job_posting_id"]
        tdf = pd.DataFrame(recs) if recs else pd.DataFrame(columns=cols)
        self.tracker_tab.set_source_df(tdf, self._resume_ids())

    def _resume_ids(self) -> frozenset:
        # Only ids whose tailored folder still EXISTS on disk are tinted blue, so a
        # folder deleted by hand drops its tint on the next reload (jobsdata keeps
        # the registry row — the tint returns if the folder comes back).
        try:
            return frozenset(jobsdata.live_resume_ids(self.registry.resume_paths()))
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
        if self._current_sig() != self._source_sig:
            self.reload_data()  # re-snapshots the signature via _rearm_watcher

    def _on_fs_change(self, _path: str) -> None:
        self._reload_timer.start()  # coalesce a flurry of events into one reload

    def _auto_reload(self) -> None:
        self.reload_data()

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
        self._refresh_apply_button()
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
            self.preview.show_segments([])
            return
        segs = jobsdata.job_detail_segments(self._row_for(jid), self._tracked.get(jid))
        self.preview.show_segments(segs)

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

    # ---- mark seen (with undo / redo) ----------------------------------------

    def _write_is_seen(self, ids: list[str], value: str) -> None:
        """Set is_seen=`value` for `ids` in whichever source CSV(s) hold them."""
        idset = set(ids)
        for path in {self.id_to_path[i] for i in ids if i in self.id_to_path}:
            try:
                df = read_csv_gz(path)
                df["job_posting_id"] = df["job_posting_id"].astype(str)
                mask = df["job_posting_id"].isin(idset)
                if mask.any():
                    df.loc[mask, "is_seen"] = value
                    write_csv_gz_atomic(df, path)
            except (OSError, ValueError):
                pass

    def _mark_ids_seen(self, ids: list[str], *, record_undo: bool = True) -> None:
        if not ids:
            return
        already = self.registry.all_ids()
        new_ids = [i for i in ids if i not in already]  # only the ones this click adds
        self.registry.mark(ids)
        self._write_is_seen(ids, "yes")
        if record_undo and new_ids:
            self._seen_undo.append(new_ids)
            self._update_seen_buttons()
        self.reload_data()

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
        self._write_is_seen(ids, "no")
        self._update_seen_buttons()
        self.reload_data()
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
        self._set_status(f"Blocked {company} — hidden now and skipped on the next scrape.")

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
            os.startfile(path)  # noqa: S606
        except OSError as e:
            self._set_status(f"Could not open {path}: {e}")

    # ---- run scraper (spend-guarded) -----------------------------------------

    @staticmethod
    def scraper_cmd(bounded: bool) -> list[str]:
        cmd = [_console_python(), "scraper.py"]
        if bounded:
            cmd += ["--max-keywords", "1", "--limit", "5"]
        return cmd

    @staticmethod
    def scorer_cmd() -> list[str]:
        return [_console_python(), "score_jobs.py"]

    @staticmethod
    def _scrape_log_path() -> Path:
        try:
            APPDATA.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return APPDATA / "scrape.log"

    def _confirm_scrape(self) -> str | None:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Run scraper")
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setText("Running the scraper collects fresh jobs from Bright Data — this spends real "
                    "money.\n\n- Small test run: 1 keyword, 5 postings/search (cheap check).\n"
                    "- Full run: your full search config (normal daily cost).\n\nIt then scores "
                    "the new jobs and refreshes the dashboard.")
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
            self._set_status("A scrape is already running.")
            return
        choice = self._confirm_scrape()
        if not choice:
            return
        self._scraping = True
        self._set_status(f"Starting scraper … progress in {self._scrape_log_path()}")
        workers.run_async(self, lambda: self._scrape_work(choice == "bounded"),
                          on_done=self._after_scrape, on_error=self._after_scrape_error)

    def _scrape_work(self, bounded: bool):
        """Run scraper.py then score_jobs.py, streaming their output to scrape.log.

        Output is captured (the dashboard runs under pythonw with no console) so a
        failure surfaces the real error instead of a dead 'check the console'.
        """
        repo = Path(__file__).resolve().parents[2]
        log_path = self._scrape_log_path()
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            for cmd in (self.scraper_cmd(bounded), self.scorer_cmd()):
                log.write(f"\n=== {' '.join(cmd)} ===\n")
                log.flush()
                proc = subprocess.Popen(
                    cmd, cwd=str(repo), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", creationflags=_no_window_flag())
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
                        f"{Path(cmd[1]).name} failed (exit {rc}).\n\n"
                        + ("\n".join(tail) if tail else "(no output captured)")
                        + f"\n\nFull log: {log_path}")
        return True

    def _after_scrape(self, _result) -> None:
        self._scraping = False
        # A local scrape writes to the repo dir, not the synced Drive folder this
        # window was opened against — fold the new scored run file(s) into the
        # sources so the freshly scraped jobs actually appear.
        for p in jobsdata.local_run_files():
            if p not in self.csv_paths:
                self.csv_paths.append(p)
        self.reload_data()
        self._set_status("Scrape + score complete — dashboard refreshed.")

    def _after_scrape_error(self, exc) -> None:
        self._scraping = False
        msg = str(exc)
        self._set_status(f"Run scraper failed — {msg.splitlines()[0] if msg else exc}")
        QtWidgets.QMessageBox.critical(self, "Run scraper", f"The run failed.\n\n{msg}")

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
            f"About to tailor {n} resumes in parallel — they run at the same time, each "
            f"making its own Gemini calls and launching pdflatex. Large batches can hit API "
            f"limits or strain your PC. Continue?"
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
        self._tailoring = True
        self.btn_tailor.setEnabled(False)
        self._apply_auth_env()
        plural = "resume" if len(jobs) == 1 else "resumes in parallel"
        self._set_status(f"Tailoring {len(jobs)} {plural} …")
        workers.run_async(self, lambda: self._tailor_work(jobs, opts),
                          on_done=self._finish_tailor, on_error=self._finish_tailor_error)

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
            self.tailor_progress.emit(f"Tailoring ({done}/{n} done): {label} finished")
            return result

        with ThreadPoolExecutor(max_workers=max(1, n)) as pool:
            return list(pool.map(one, jobs))

    def _finish_tailor(self, results: list[dict]) -> None:
        self._tailoring = False
        self.btn_tailor.setEnabled(True)
        results = results or []
        oks = [r for r in results if r.get("dir")]
        fails = [r for r in results if not r.get("dir")]
        # Registry writes on the UI thread (this slot runs there) — no cross-thread SQLite.
        for r in oks:
            if r.get("id"):
                try:
                    self.registry.record_resume(r["id"], str(r["dir"]))
                except Exception:  # noqa: BLE001 - bookkeeping only
                    pass
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
        # Only open File Explorer when the user opted in — off by default so a
        # multi-job batch doesn't spawn a window per résumé (Settings → Dashboard).
        if last and settings.load().get("tailor_open_folder", False):
            try:
                os.startfile(str(last))  # noqa: S606
            except OSError:
                pass
        self.reload_data()

    def _finish_tailor_error(self, exc) -> None:
        self._tailoring = False
        self.btn_tailor.setEnabled(True)
        QtWidgets.QMessageBox.warning(self, "Tailor resume", f"Tailoring failed: {exc}")
        self._set_status(f"Tailor failed: {exc}")

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
            auth = jobsdata._load_cfg().get("gemini_auth", "vertex")
            stored = settings.load()
            project = stored.get("GOOGLE_CLOUD_PROJECT", "") or os.environ.get(
                "GOOGLE_CLOUD_PROJECT", "")
            has_key = settings.secret_status().get("RESUME_TAILOR_GEMINI_API_KEY", False) or bool(
                os.environ.get("RESUME_TAILOR_GEMINI_API_KEY"))
            problems.extend(f"[Engine] {w}"
                            for w in jobsdata._engine_credential_warnings(auth, project, has_key))
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
            os.startfile(str(path))  # noqa: S606
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
