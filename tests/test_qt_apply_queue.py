"""The dashboard's auto-apply queueing UI.

Covers the three pieces on top of the queue backend (`apply_queue`):

  * JobsTab — the injected "Queue for auto-apply (N)" context-menu item;
  * MainWindow — `_queue_for_auto_apply` (applied-skip, batch cap, the
    ready/not-ready partition, the tailor-then-queue chain via the extracted
    `_start_tailor`) and `_set_ats_password`;
  * ApplyQueuePanel — the read-only Auto-apply tab mirroring the queue file
    (live refresh, Re-queue/Remove/Clear, kickoff command, password state).

Everything is hermetic: APPLY_QUEUE_PATH points at tmp_path, the registry is a
MagicMock, tailoring is a fake (never a real Gemini client), the password seam
is monkeypatched (the real Credential Manager is never queried), and the
clipboard is the offscreen QApplication's in-process one.
"""
import datetime as _dt
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from PySide6 import QtCore, QtGui, QtWidgets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_queue  # noqa: E402
import jev_switch  # noqa: E402
from qt import apply_queue_panel as aqp  # noqa: E402
from qt import main_window as mw  # noqa: E402
from qt import mw_queue  # noqa: E402
from qt.apply_queue_panel import (  # noqa: E402
    KICKOFF_COMMAND,
    LOGIN_COMMAND,
    ApplyQueuePanel,
)
from qt.jobs_tab import JobsTab  # noqa: E402
from qt.main_window import MainWindow  # noqa: E402

COLS = [("score", 50), ("job_title", 240), ("company_name", 170), ("url", 220)]


def _jobs_df():
    return pd.DataFrame([
        {"job_posting_id": "1", "score": "5", "recommendation": "apply",
         "job_title": "Data Analyst", "company_name": "Acme", "url": "https://x/1",
         "is_easy_apply": "True", "is_seen": "no", "extracted_date": "2026-07-01"},
        {"job_posting_id": "2", "score": "4", "recommendation": "consider",
         "job_title": "ML Engineer", "company_name": "Globex", "url": "https://x/2",
         "is_easy_apply": "False", "is_seen": "no", "extracted_date": "2026-07-02"},
        {"job_posting_id": "3", "score": "4", "recommendation": "apply",
         "job_title": "Data Engineer", "company_name": "Initech", "url": "https://x/3",
         "is_easy_apply": "False", "is_seen": "no", "extracted_date": "2026-07-02"},
    ])


# --- JobsTab context-menu wiring ------------------------------------------------


def _select_rows(tab, *rows):
    sm = tab.table.selectionModel()
    sm.clearSelection()
    flags = (QtCore.QItemSelectionModel.SelectionFlag.Select
             | QtCore.QItemSelectionModel.SelectionFlag.Rows)
    for r in rows:
        sm.select(tab.proxy.index(r, 0), flags)


def _menu_texts(monkeypatch, tab, choose: str | None = None):
    """Open the context menu with exec stubbed; return the action texts (and
    'click' the action whose text equals `choose`). Same FakeMenu trick as
    test_qt_jobs.py — patching the Shiboken class attribute doesn't intercept
    instance calls."""
    seen = {}

    class FakeMenu(QtWidgets.QMenu):
        def exec(self, *a, **k):
            seen["texts"] = [act.text() for act in self.actions()]
            if choose is not None:
                for act in self.actions():
                    if act.text() == choose:
                        return act
            return None

    monkeypatch.setattr(QtWidgets, "QMenu", FakeMenu)
    tab._context_menu(QtCore.QPoint(2, 2))
    return seen.get("texts", [])


def test_context_menu_queue_item_fires_with_multi_selection(qtbot, monkeypatch):
    fired = []
    tab = JobsTab("all", COLS, on_queue_apply=lambda ids: fired.append(list(ids)))
    qtbot.addWidget(tab)
    tab.set_source_df(_jobs_df())
    _select_rows(tab, 0, 1)
    expected = tab.selected_ids()
    assert len(expected) == 2
    texts = _menu_texts(monkeypatch, tab, choose="Queue for auto-apply (2)")
    assert "Queue for auto-apply (2)" in texts
    assert fired == [expected]          # the FULL multi-selection, one call


def test_context_menu_no_queue_item_when_unwired(qtbot, monkeypatch):
    tab = JobsTab("all", COLS)          # no on_queue_apply injected
    qtbot.addWidget(tab)
    tab.set_source_df(_jobs_df())
    _select_rows(tab, 0)
    texts = _menu_texts(monkeypatch, tab)
    assert texts and not any("auto-apply" in t.lower() for t in texts)


# --- MainWindow: _queue_for_auto_apply -------------------------------------------


def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    reg.resume_path.return_value = None
    reg.all_ids.return_value = set()
    return reg


class _InlineWrites:
    """Synchronous stand-in for MainWindow's SerialTaskQueue: queue mutations
    run inline so tests read the queue file right after the call."""

    def submit(self, fn, on_done=None, on_error=None):
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - mirror the real queue's catch
            if on_error is not None:
                on_error(exc)
            return
        if on_done is not None:
            on_done(result)

    def is_idle(self):
        return True

    def pending_count(self):
        return 0

    def drain(self, timeout_ms=30000):
        return True


class _HeldRunner:
    """A run_async stand-in that captures tasks so tests control completion —
    used to hold the TAILOR worker while the queue file is inspected."""

    def __init__(self):
        self.held = []

    def __call__(self, owner, fn, on_done=None, on_error=None):
        self.held.append((fn, on_done, on_error))
        return None

    def complete_next(self):
        fn, on_done, on_error = self.held.pop(0)
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            if on_error is not None:
                on_error(exc)
            return
        if on_done is not None:
            on_done(result)


def _qfile(tmp_path) -> Path:
    return tmp_path / "apply_queue.json"


def _win(qtbot, monkeypatch, tmp_path, df=None):
    monkeypatch.setenv("APPLY_QUEUE_PATH", str(_qfile(tmp_path)))
    w = MainWindow(csv_paths=[], registry=_fake_registry())
    qtbot.addWidget(w)
    # Patched AFTER construction (SettingsForm renders the real schema there).
    monkeypatch.setattr(mw.settings, "load", lambda: {"auto_apply_batch_cap": 10})
    w._writes = _InlineWrites()     # queue writes run inline (see _InlineWrites)
    if df is not None:
        w.df = df
        w._row_by_id = {str(j): i for i, j in enumerate(df["job_posting_id"])}
        w._url_by_id = dict(zip(df["job_posting_id"].astype(str),
                                df["url"].astype(str)))
    return w


def _ready_folder(tmp_path, monkeypatch, jid):
    """A tailored-output folder that satisfies _apply_ready, with every artifact."""
    from resume_tailor import output
    monkeypatch.setenv("RESUME_TAILOR_CANDIDATE", "Cand")
    folder = tmp_path / "resumes" / jid
    folder.mkdir(parents=True, exist_ok=True)
    for name in (output.resume_filename(), output.cover_filename(),
                 output.cover_tex_filename(), "apply.md"):
        (folder / name).write_text("x", encoding="utf-8")
    return folder


def test_main_window_has_queue_button_tab_and_wiring(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path)
    bar = w._action_bar
    texts = [bar.itemAt(i).widget().text() for i in range(bar.count())
             if isinstance(bar.itemAt(i).widget(), QtWidgets.QPushButton)]
    assert "Queue auto-apply" in texts
    # Apply sits on the job detail card; Queue auto-apply sits
    # immediately before the bar's primary (Find new jobs).
    assert "Apply" not in texts
    assert texts.index("Queue auto-apply") == texts.index("Find new jobs") - 1
    assert "Auto-apply" in w.tab_titles()
    assert isinstance(w._tab_widgets["Auto-apply"], ApplyQueuePanel)
    # every jobs tab fires the queue callback (context-menu wiring)
    for tab in (w.high_tab, w.all_tab, w.tracker_tab):
        assert tab._on_queue_apply is not None


def test_queue_ready_job_enqueues_with_artifact_paths(qtbot, monkeypatch, tmp_path):
    from resume_tailor import output
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())
    folder = _ready_folder(tmp_path, monkeypatch, "1")
    w.registry.resume_path.side_effect = \
        lambda jid: str(folder) if jid == "1" else None

    w._queue_for_auto_apply(["1"])

    jobs = apply_queue.load(_qfile(tmp_path))["jobs"]
    assert len(jobs) == 1
    e = jobs[0]
    assert e["job_posting_id"] == "1"
    assert e["status"] == "queued"
    assert e["company"] == "Acme" and e["title"] == "Data Analyst"
    assert e["apply_url"] == "https://x/1"
    assert e["is_easy_apply"] is True
    arts = e["artifacts"]
    assert arts["folder"] == str(folder)
    assert arts["resume_pdf"] == str(folder / output.resume_filename())
    assert arts["cover_letter_pdf"] == str(folder / output.cover_filename())
    assert arts["apply_md"] == str(folder / "apply.md")
    # the .txt export is gone — the paste text lives in apply.md's Cover letter section
    assert "cover_letter_txt" not in arts


def test_queue_tracker_only_ready_job_falls_back_for_entry_data(qtbot, monkeypatch, tmp_path):
    """A tracker-only id (in registry.status_rows(), absent from self.df) must
    enqueue with a REAL apply_url/company/title via the master-CSV fallback —
    not an empty entry the auto-apply run can't navigate (burns an attempt)."""
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())      # df carries 1-3 only
    jid = "T9"
    w.registry.status_rows.return_value = [
        {"job_posting_id": jid, "status": "saved", "status_date": "2026-07-01",
         "applied_date": "", "followed_up_at": "",
         "company": "", "job_title": "", "url": ""}]
    folder = _ready_folder(tmp_path, monkeypatch, jid)
    w.registry.resume_path.side_effect = \
        lambda j: str(folder) if j == jid else None
    monkeypatch.setattr(mw.jobsdata, "master_row",
                        lambda j, **k: ({"company_name": "TrackCo",
                                         "job_title": "Tracked Role",
                                         "url": "https://x/t9",
                                         "is_easy_apply": "True"}
                                        if str(j) == jid else None))

    w._queue_for_auto_apply([jid])

    jobs = apply_queue.load(_qfile(tmp_path))["jobs"]
    assert len(jobs) == 1
    e = jobs[0]
    assert e["job_posting_id"] == jid
    assert e["status"] == "queued"
    assert e["company"] == "TrackCo" and e["title"] == "Tracked Role"
    assert e["apply_url"] == "https://x/t9"
    assert e["is_easy_apply"] is True
    assert e["artifacts"]["folder"] == str(folder)
    assert "Queued 1" in w.statusBar().currentMessage()


def test_queue_refuses_job_with_no_apply_url_anywhere(qtbot, monkeypatch, tmp_path):
    """No df row, no tracker fields, no master row -> the entry would carry an
    empty apply_url; it must NOT be enqueued, and the status line says so."""
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())
    jid = "T9"
    w.registry.status_rows.return_value = [
        {"job_posting_id": jid, "status": "saved", "status_date": "2026-07-01",
         "applied_date": "", "followed_up_at": "",
         "company": "", "job_title": "", "url": ""}]
    folder = _ready_folder(tmp_path, monkeypatch, jid)          # apply-READY...
    w.registry.resume_path.side_effect = \
        lambda j: str(folder) if j == jid else None
    monkeypatch.setattr(mw.jobsdata, "master_row", lambda j, **k: None)

    w._queue_for_auto_apply([jid])

    assert apply_queue.load(_qfile(tmp_path))["jobs"] == []     # ...but refused
    msg = w.statusBar().currentMessage()
    assert "without job data" in msg
    assert "Queued" not in msg                                  # no "Queued 0"


def test_queue_not_ready_yes_tailors_then_flips_to_queued(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)
    monkeypatch.setattr(w, "reload_data", lambda: None)
    runner = _HeldRunner()
    monkeypatch.setattr(mw.workers, "run_async", runner)

    w._queue_for_auto_apply(["1"])
    jobs = apply_queue.load(_qfile(tmp_path))["jobs"]
    assert [e["status"] for e in jobs] == ["tailoring"]   # enqueued before the tailor
    assert w._tailoring is True and len(runner.held) == 1

    folder = _ready_folder(tmp_path, monkeypatch, "1")
    monkeypatch.setattr("resume_tailor.tailor", lambda job, **k: folder, raising=False)
    runner.complete_next()   # tailor finishes -> set_artifacts flips tailoring -> queued

    e = apply_queue.load(_qfile(tmp_path))["jobs"][0]
    assert e["status"] == "queued"
    assert e["artifacts"]["folder"] == str(folder)
    assert e["artifacts"]["apply_md"] == str(folder / "apply.md")
    assert w._tailoring is False
    w.registry.record_resume.assert_called_once_with("1", str(folder))


def test_queue_tailor_of_a_hand_added_job_reads_the_full_description_in_the_worker(
        qtbot, monkeypatch, tmp_path):
    # a hand-added job's dashboard row holds only the
    # 1000-character summary; the tailor the queue starts reads the full
    # description from the master CSV inside the worker, never on the UI thread
    full = "Requirements: 3+ years of SQL, dbt and Airflow. " * 60
    df = pd.DataFrame([{"job_posting_id": "manual-abc", "score": "", "recommendation": "",
                        "job_title": "Data Analyst", "company_name": "Acme",
                        "url": "https://x/m", "is_easy_apply": "False", "is_seen": "no",
                        "extracted_date": "2026-09-28", "job_summary": full[:1000]}])
    w = _win(qtbot, monkeypatch, tmp_path, df=df)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)
    monkeypatch.setattr(w, "reload_data", lambda: None)
    reads: list[str] = []
    monkeypatch.setattr(mw.jobsdata, "master_row",
                        lambda j, **k: reads.append(str(j)) or {
                            "job_posting_id": j, "job_description_formatted": full})
    runner = _HeldRunner()
    monkeypatch.setattr(mw.workers, "run_async", runner)

    w._queue_for_auto_apply(["manual-abc"])
    assert reads == [] and len(runner.held) == 1      # nothing read on the UI thread

    folder = _ready_folder(tmp_path, monkeypatch, "manual-abc")
    seen: list[dict] = []
    monkeypatch.setattr("resume_tailor.tailor",
                        lambda job, **k: seen.append(dict(job)) or folder, raising=False)
    runner.complete_next()
    assert reads == ["manual-abc"]
    assert seen[0]["job_description_formatted"] == full


def test_queue_tailor_failure_marks_entry_failed_with_note(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)
    monkeypatch.setattr(w, "reload_data", lambda: None)
    runner = _HeldRunner()
    monkeypatch.setattr(mw.workers, "run_async", runner)

    w._queue_for_auto_apply(["1"])

    def boom(job, **k):
        raise RuntimeError("no LaTeX on PATH")

    monkeypatch.setattr("resume_tailor.tailor", boom, raising=False)
    runner.complete_next()

    e = apply_queue.load(_qfile(tmp_path))["jobs"][0]
    assert e["status"] == "failed"
    assert "no LaTeX on PATH" in e["notes"]
    assert e["finished_at"]
    assert w._tailoring is False


def test_queue_not_ready_no_queues_only_ready(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())
    folder = _ready_folder(tmp_path, monkeypatch, "1")
    w.registry.resume_path.side_effect = \
        lambda jid: str(folder) if jid == "1" else None
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No))
    launched = []
    monkeypatch.setattr(mw.workers, "run_async",
                        lambda *a, **k: launched.append(a))

    w._queue_for_auto_apply(["1", "2"])

    jobs = apply_queue.load(_qfile(tmp_path))["jobs"]
    assert [e["job_posting_id"] for e in jobs] == ["1"]   # ready one only
    assert jobs[0]["status"] == "queued"
    assert launched == []                                  # No -> no tailor run


def test_queue_skips_already_applied(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())
    w.registry.status_rows.return_value = [
        {"job_posting_id": "1", "status": "applied"}]
    f2 = _ready_folder(tmp_path, monkeypatch, "2")
    w.registry.resume_path.side_effect = \
        lambda jid: str(f2) if jid == "2" else None

    w._queue_for_auto_apply(["1", "2"])

    jobs = apply_queue.load(_qfile(tmp_path))["jobs"]
    assert [e["job_posting_id"] for e in jobs] == ["2"]
    assert "already-applied" in w.statusBar().currentMessage()

    # all-applied selection -> nothing queued, no dialog
    w.registry.status_rows.return_value = [
        {"job_posting_id": "1", "status": "applied"},
        {"job_posting_id": "2", "status": "applied"}]
    w._queue_for_auto_apply(["1", "2"])
    assert len(apply_queue.load(_qfile(tmp_path))["jobs"]) == 1


def test_queue_enforces_batch_cap(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())
    monkeypatch.setattr(mw.settings, "load", lambda: {"auto_apply_batch_cap": 2})
    folders = {jid: _ready_folder(tmp_path, monkeypatch, jid) for jid in "123"}
    w.registry.resume_path.side_effect = \
        lambda jid: str(folders[jid]) if jid in folders else None

    w._queue_for_auto_apply(["1", "2", "3"])

    jobs = apply_queue.load(_qfile(tmp_path))["jobs"]
    assert [e["job_posting_id"] for e in jobs] == ["1", "2"]   # cap = 2, FIFO
    assert "cap" in w.statusBar().currentMessage()


def test_queue_respects_tailoring_guard(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())
    w._tailoring = True                       # a tailor run is already in flight
    launched = []
    monkeypatch.setattr(mw.workers, "run_async",
                        lambda *a, **k: launched.append(a))

    def no_dialog(*a, **k):
        raise AssertionError("no confirm dialog while a tailor run is in flight")

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(no_dialog))
    w._queue_for_auto_apply(["1"])            # untailored job
    assert launched == []
    assert apply_queue.load(_qfile(tmp_path))["jobs"] == []

    # the extracted worker launcher refuses re-entry outright
    assert w._start_tailor([{"job_posting_id": "1"}], {}) is False
    assert launched == []


def test_queue_tailor_launch_failure_resets_guard_and_parks_entries(
        qtbot, monkeypatch, tmp_path):
    """run_async raising at LAUNCH (thread-spawn failure) must not strand the
    UI: _tailoring resets, the button re-enables, and the just-enqueued
    "tailoring" entries are parked failed (they'd be unclaimable otherwise)."""
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)

    def boom(*a, **k):
        raise RuntimeError("thread spawn failed")

    monkeypatch.setattr(mw.workers, "run_async", boom)

    w._queue_for_auto_apply(["1"])   # surfaces in the status bar, no re-raise
    assert "thread spawn failed" in w.statusBar().currentMessage()

    assert w._tailoring is False                 # guard cleared -> not dead-locked
    assert w.btn_tailor.isEnabled()
    e = apply_queue.load(_qfile(tmp_path))["jobs"][0]
    assert e["status"] == "failed"               # not orphaned as "tailoring"
    assert "tailor launch failed" in e["notes"]
    assert w._queue_tailor_pending == []


def test_plain_tailor_launch_failure_resets_guard(qtbot, monkeypatch, tmp_path):
    """The no-queue path through _start_tailor gets the same hardening: a
    launch failure surfaces in the status bar (no re-raise into the Qt
    event loop) and leaves the Tailor button usable."""
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)

    def boom(*a, **k):
        raise RuntimeError("thread spawn failed")

    monkeypatch.setattr(mw.workers, "run_async", boom)

    job = {"job_posting_id": "1", "job_title": "T", "company_name": "C"}
    assert w._start_tailor([job], {}) is False   # reported, not re-raised
    assert "thread spawn failed" in w.statusBar().currentMessage()

    assert w._tailoring is False
    assert w.btn_tailor.isEnabled()
    assert apply_queue.load(_qfile(tmp_path))["jobs"] == []   # nothing enqueued


# --- MainWindow: _reconcile_orphaned_tailors (crash-safe finalize recovery) --------


def _orphan_tailoring(qfile, jid, company, title, url="https://x/o"):
    """Enqueue a "tailoring" entry as _queue_for_auto_apply does BEFORE the tailor
    finishes — an orphan if the window closes before the finalize callback runs."""
    apply_queue.enqueue(
        apply_queue.new_entry(jid, company=company, title=title, apply_url=url,
                              status="tailoring"), path=qfile)


def _tailored_at_base(monkeypatch, tmp_path, company, title):
    """A COMPLETE tailored folder at output.base_dir(company, title) under a tmp
    OUTPUT_ROOT — what a lost-callback tailor run leaves stranded on disk."""
    from resume_tailor import config, output
    monkeypatch.setenv("RESUME_TAILOR_CANDIDATE", "Cand")
    monkeypatch.setattr(config, "OUTPUT_ROOT", tmp_path / "gen")
    folder = output.base_dir(company, title)
    folder.mkdir(parents=True, exist_ok=True)
    for name in (output.resume_filename(), output.cover_filename(),
                 output.cover_tex_filename(), "apply.md"):
        (folder / name).write_text("x", encoding="utf-8")
    return folder


def test_reconcile_adopts_completed_orphan_tailoring_entry(qtbot, monkeypatch, tmp_path):
    """A "tailoring" entry whose folder is complete on disk (finalize was lost)
    is recovered: the résumé is recorded AND the entry flips tailoring -> queued
    with its artifact paths — exactly what _finish_tailor/_finish_queue_tailor do."""
    w = _win(qtbot, monkeypatch, tmp_path)
    _orphan_tailoring(_qfile(tmp_path), "77", "Distyl", "Applied AI Researcher")
    folder = _tailored_at_base(monkeypatch, tmp_path, "Distyl", "Applied AI Researcher")

    healed = w._reconcile_orphaned_tailors()

    assert healed == 1
    w.registry.record_resume.assert_called_once_with("77", str(folder))
    e = apply_queue.load(_qfile(tmp_path))["jobs"][0]
    assert e["status"] == "queued"
    assert e["queued_at"]                       # claimable now
    assert e["artifacts"]["folder"] == str(folder)
    assert e["artifacts"]["resume_pdf"].endswith("Cand_Resume.pdf")
    assert e["artifacts"]["apply_md"] == str(folder / "apply.md")


def test_reconcile_skips_orphan_with_no_completed_folder(qtbot, monkeypatch, tmp_path):
    """A "tailoring" entry whose folder is missing/incomplete (the tailor really
    didn't finish, or is still running elsewhere) is left untouched — never
    adopted as complete, never recorded."""
    from resume_tailor import config
    w = _win(qtbot, monkeypatch, tmp_path)
    monkeypatch.setenv("RESUME_TAILOR_CANDIDATE", "Cand")
    monkeypatch.setattr(config, "OUTPUT_ROOT", tmp_path / "gen")   # nothing on disk
    _orphan_tailoring(_qfile(tmp_path), "77", "Distyl", "Applied AI Researcher")

    healed = w._reconcile_orphaned_tailors()

    assert healed == 0
    w.registry.record_resume.assert_not_called()
    assert apply_queue.load(_qfile(tmp_path))["jobs"][0]["status"] == "tailoring"


def test_reconcile_backfills_requeued_orphan_missing_artifacts(qtbot, monkeypatch, tmp_path):
    """The user's manual escape hatch, Re-queue, flips a stranded orphan to
    "queued" but keeps its EMPTY artifacts — leaving it claimable with no résumé
    to upload. Reconcile backfills that too: record the résumé + fill artifacts,
    status staying "queued"."""
    w = _win(qtbot, monkeypatch, tmp_path)
    _orphan_tailoring(_qfile(tmp_path), "77", "Distyl", "Applied AI Researcher")
    apply_queue.requeue("77", path=_qfile(tmp_path))          # user hits Re-queue
    assert apply_queue.load(_qfile(tmp_path))["jobs"][0]["status"] == "queued"
    folder = _tailored_at_base(monkeypatch, tmp_path, "Distyl", "Applied AI Researcher")

    healed = w._reconcile_orphaned_tailors()

    assert healed == 1
    w.registry.record_resume.assert_called_once_with("77", str(folder))
    e = apply_queue.load(_qfile(tmp_path))["jobs"][0]
    assert e["status"] == "queued"
    assert e["artifacts"]["folder"] == str(folder)
    assert e["artifacts"]["apply_md"] == str(folder / "apply.md")


def test_reconcile_leaves_healthy_folder_linked_entry_alone(qtbot, monkeypatch, tmp_path):
    """An entry that already carries a folder artifact is healthy — reconcile
    never re-records or touches it, even though its folder is complete."""
    w = _win(qtbot, monkeypatch, tmp_path)
    folder = _tailored_at_base(monkeypatch, tmp_path, "Distyl", "Applied AI Researcher")
    entry = apply_queue.new_entry("77", company="Distyl", title="Applied AI Researcher",
                                  apply_url="https://x/o", status="queued")
    entry["artifacts"]["folder"] = str(folder)               # already linked
    apply_queue.enqueue(entry, path=_qfile(tmp_path))

    healed = w._reconcile_orphaned_tailors()

    assert healed == 0
    w.registry.record_resume.assert_not_called()
    assert apply_queue.load(_qfile(tmp_path))["jobs"][0]["status"] == "queued"
    assert folder.is_dir()


def test_start_runs_the_orphan_reconciler(qtbot, monkeypatch, tmp_path):
    """start() heals stranded tailor runs BEFORE the first data load, so a job
    tailored in a prior (interrupted) session shows up queued on relaunch."""
    w = _win(qtbot, monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(w, "_reconcile_orphaned_tailors",
                        lambda: called.append(True))
    monkeypatch.setattr(w, "reload_data_async", lambda: None)
    w.start()
    assert called == [True]


# --- MainWindow: _set_ats_password ------------------------------------------------


def _feed_password_dialogs(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: next(it)))


def test_set_ats_password_happy_path(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path)
    stored = []
    monkeypatch.setattr(mw_queue.ats_accounts, "set_master_password",
                        lambda pw: stored.append(pw) or True)
    _feed_password_dialogs(monkeypatch, [("fake-pw", True), ("fake-pw", True)])
    w._set_ats_password()
    assert stored == ["fake-pw"]      # the dialog string goes straight through


def test_apply_queue_mark_applied_sets_status_seen_and_removes(qtbot, monkeypatch, tmp_path):
    import os
    from pathlib import Path
    w = _win(qtbot, monkeypatch, tmp_path)
    apply_queue.enqueue(apply_queue.new_entry(
        "42", company="Acme", title="Analyst", apply_url="https://x/42"))  # default path = env qfile
    status_calls, seen_calls = [], []
    monkeypatch.setattr(w.registry, "set_status", lambda *a, **k: status_calls.append((a, k)))
    monkeypatch.setattr(w, "_mark_ids_seen", lambda ids, **k: seen_calls.append(list(ids)))
    monkeypatch.setattr(w, "_refresh_tracker", lambda: None)
    w._apply_queue_mark_applied(
        {"job_posting_id": "42", "company": "Acme", "title": "Analyst", "apply_url": "https://x/42"})
    assert status_calls and status_calls[0][0][0] == "42" and status_calls[0][0][1] == "applied"
    assert seen_calls == [["42"]]                       # applied implies seen
    assert apply_queue.load(Path(os.environ["APPLY_QUEUE_PATH"]))["jobs"] == []   # removed inline


def test_apply_queue_mark_seen_marks_seen_and_removes_without_status(qtbot, monkeypatch, tmp_path):
    import os
    from pathlib import Path
    w = _win(qtbot, monkeypatch, tmp_path)
    apply_queue.enqueue(apply_queue.new_entry(
        "42", company="Acme", title="Analyst", apply_url="https://x/42"))
    status_calls, seen_calls = [], []
    monkeypatch.setattr(w.registry, "set_status", lambda *a, **k: status_calls.append((a, k)))
    monkeypatch.setattr(w, "_mark_ids_seen", lambda ids, **k: seen_calls.append(list(ids)))
    w._apply_queue_mark_seen(
        {"job_posting_id": "42", "company": "Acme", "title": "Analyst", "apply_url": "https://x/42"})
    assert status_calls == []                           # no status → stays under All Jobs
    assert seen_calls == [["42"]]
    assert apply_queue.load(Path(os.environ["APPLY_QUEUE_PATH"]))["jobs"] == []


def test_set_ats_password_mismatch_blank_and_cancel_abort(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path)
    stored = []
    monkeypatch.setattr(mw_queue.ats_accounts, "set_master_password",
                        lambda pw: stored.append(pw) or True)
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a)))

    _feed_password_dialogs(monkeypatch, [("a", True), ("b", True)])   # mismatch
    w._set_ats_password()
    assert stored == [] and len(warned) == 1

    _feed_password_dialogs(monkeypatch, [("   ", True), ("   ", True)])  # blank
    w._set_ats_password()
    assert stored == [] and len(warned) == 2

    _feed_password_dialogs(monkeypatch, [("x", False)])               # cancel
    w._set_ats_password()
    assert stored == [] and len(warned) == 2   # cancel warns nobody


# --- ApplyQueuePanel ---------------------------------------------------------------


def _panel(qtbot, qfile, **kw):
    # The Jev gate would read the sandbox, where no key is set: a panel
    # test gets a run that can start, and a gate test passes its own
    # `jev_blocked` (None for the real one).
    kw.setdefault("jev_blocked", lambda: "")
    p = ApplyQueuePanel(queue_path=qfile, **kw)
    qtbot.addWidget(p)
    return p


def test_panel_renders_rows_and_counts_from_queue_file(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry(
        "1", company="Acme", title="Analyst", apply_url="https://x/1"), path=qfile)
    apply_queue.enqueue(apply_queue.new_entry(
        "2", company="Globex", title="Engineer", status="tailoring"), path=qfile)
    apply_queue.add_missing("1", "Salary expectation?", path=qfile)

    p = _panel(qtbot, qfile)
    assert p.table.rowCount() == 2
    assert {p.table.item(r, 0).text() for r in range(2)} == {"Acme", "Globex"}
    row1 = next(r for r in range(2) if p.table.item(r, 0).text() == "Acme")
    assert p.table.item(row1, 1).text() == "Analyst"
    assert p.table.item(row1, 2).text() == "queued"
    assert p.table.item(row1, 4).text() == "1"          # missing-answer count
    assert "queued: 1" in p.counts_label.text()
    assert "tailoring: 1" in p.counts_label.text()
    assert "total: 2" in p.counts_label.text()
    # details pane follows the selection
    p.table.selectRow(row1)
    assert "Salary expectation?" in p.details.toPlainText()


def test_panel_refreshes_after_external_rewrite(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"),
                        path=qfile)
    p = _panel(qtbot, qfile)
    assert p.table.rowCount() == 1
    # an external process (the agent CLI) rewrites the file atomically
    apply_queue.enqueue(apply_queue.new_entry("2", company="Globex", title="B"),
                        path=qfile)
    qtbot.waitUntil(lambda: p.table.rowCount() == 2, timeout=8000)


def test_panel_poll_catches_write_landing_mid_refresh(qtbot, tmp_path, monkeypatch):
    """A write landing in refresh()'s load->snapshot window must trip the NEXT
    mtime poll. The baseline sig has to be captured BEFORE the load — snapshot
    it after and the write hides until some later write (poll blind spot)."""
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"),
                        path=qfile)
    p = _panel(qtbot, qfile)
    # No fs events or running timers in this test: the poll ALONE must catch it.
    p._watcher.fileChanged.disconnect(p._on_fs_event)
    p._watcher.directoryChanged.disconnect(p._on_fs_event)
    p._poll.stop()
    p._debounce.stop()

    real_load = apply_queue.load

    def load_then_external_write(path, **kw):
        data = real_load(path, **kw)
        # One-shot: restore the seam, THEN land an external write (the agent
        # CLI) squarely between the panel's read and its mtime snapshot.
        monkeypatch.setattr(aqp.apply_queue, "load", real_load)
        apply_queue.enqueue(apply_queue.new_entry("2", company="Globex", title="B"),
                            path=qfile)
        return data

    monkeypatch.setattr(aqp.apply_queue, "load", load_then_external_write)
    p.refresh()
    assert p.table.rowCount() == 1      # this repaint predates the write — fine
    p._poll_for_changes()               # but the very next poll tick must see it
    assert p.table.rowCount() == 2


def test_panel_requeue_clears_missing_and_flips_status(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"),
                        path=qfile)
    apply_queue.add_missing("1", "Visa status?", path=qfile)
    apply_queue.finish("1", "needs_human", tab_note="review tab", path=qfile)

    p = _panel(qtbot, qfile)
    p.table.selectRow(0)
    p._requeue()

    e = apply_queue.load(qfile)["jobs"][0]
    assert e["status"] == "queued"
    assert e["missing_answers"] == []
    assert e["tab_note"] == ""
    assert p.table.item(0, 2).text() == "queued"      # the panel refreshed itself


def test_panel_remove_and_clear_finished(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"),
                        path=qfile)
    apply_queue.enqueue(apply_queue.new_entry("2", company="Globex", title="B"),
                        path=qfile)
    apply_queue.finish("2", "ready_to_submit", path=qfile)

    p = _panel(qtbot, qfile)
    row1 = next(r for r in range(2) if p.table.item(r, 0).text() == "Acme")
    p.table.selectRow(row1)
    p._remove()
    assert [e["job_posting_id"] for e in apply_queue.load(qfile)["jobs"]] == ["2"]

    p._clear_finished()                       # id 2 is terminal -> dropped
    assert apply_queue.load(qfile)["jobs"] == []
    assert p.table.rowCount() == 0


def test_panel_open_buttons_use_artifact_paths(qtbot, tmp_path, monkeypatch):
    qfile = _qfile(tmp_path)
    folder = tmp_path / "job1"
    folder.mkdir()
    record = folder / "application_record.md"
    record.write_text("q -> a", encoding="utf-8")
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"),
                        path=qfile)
    apply_queue.set_artifacts("1", {"folder": str(folder),
                                    "application_record": str(record)}, path=qfile)
    opened = []
    monkeypatch.setattr(aqp.osopen, "open_path", lambda p: opened.append(str(p)))

    p = _panel(qtbot, qfile)
    p.table.selectRow(0)
    p._open_folder()
    p._open_record()
    assert opened == [str(folder), str(record)]


def test_kickoff_and_login_commands_run_apply_run():
    """The Start button launches the code-owned Jev drain (`local/apply_run.py
    drain`); the sign-in button opens the persistent profile through its `login`
    verb. Neither command runs `claude`."""
    for cmd in (KICKOFF_COMMAND, LOGIN_COMMAND):
        prefix, verb = cmd.split("; ", 1)          # PowerShell chain (5.1-safe)
        # Single-quoted -LiteralPath: no `$` expansion, no backtick escapes, no
        # `[` wildcard, and spaces survive.
        assert prefix == f"Set-Location -LiteralPath '{aqp.REPO_ROOT}'"
        assert "claude" not in verb.lower()        # (the checkout path may say it)
    assert KICKOFF_COMMAND.endswith("python local/apply_run.py drain")
    assert LOGIN_COMMAND.endswith("python local/apply_run.py login")


def test_console_command_keeps_a_hostile_checkout_path_literal():
    """A checkout under a folder named with `$`, a backtick or `[` reaches the
    console byte for byte: single quotes are PowerShell's literal string, and a
    quote inside the path is doubled, the one escape that string knows."""
    root = Path("C:/Users/o'brien/$HOME/`x/[1]/scrape_data")
    cmd = aqp._console_command(root, "drain")
    literal = str(root).replace("'", "''")
    assert cmd == f"Set-Location -LiteralPath '{literal}'; python local/apply_run.py drain"
    for hostile in ("$HOME", "`x", "[1]", "o''brien"):
        assert hostile in cmd
    assert _decoded(aqp._console_argv(cmd)) == cmd


# --- ApplyQueuePanel: "Start auto-apply run" ---------------------------------------


def _actions_row(p):
    # item 0 is the "Waiting for you" card, then the two header rows
    return p.body.layout().itemAt(2).layout()


def test_panel_has_start_run_button(qtbot, tmp_path):
    p = _panel(qtbot, _qfile(tmp_path))
    assert p.start_run_btn.text() == "Start auto-apply run"
    # accent-styled, and the trailing action on the header's second row. The
    # header is two rows: status chips + counts caption over the actions
    # (master-password cluster, then the two small run buttons, then Start), so
    # that the chips keep their labels at 125% and 150% on a window narrower
    # than about 1600px.
    chips_row, actions = p.body.layout().itemAt(1).layout(), _actions_row(p)
    assert chips_row.itemAt(0).widget() is p.status_chips
    assert actions.itemAt(actions.count() - 1).widget() is p.start_run_btn
    row_buttons = [actions.itemAt(i).widget() for i in range(actions.count() - 1)
                   if isinstance(actions.itemAt(i).widget(), QtWidgets.QPushButton)]
    assert row_buttons == [p.copy_cmd_btn, p.login_btn]
    assert p.start_run_btn.property("accent") is True
    tip = p.start_run_btn.toolTip().lower()
    assert "new terminal" in tip
    assert "batch_cap" in tip or "batch cap" in tip
    assert "submits when" in tip
    assert "never submitted" not in tip and "nothing is ever submitted" not in tip
    # The Claude-era variants are gone from the panel.
    assert not hasattr(p, "kickoff_btn")
    assert not hasattr(p, "kickoff_scoped_btn")
    assert not hasattr(aqp, "KICKOFF_COMMAND_SCOPED")
    assert not hasattr(aqp, "KICKOFF_PROMPT")


def test_panel_copy_kickoff_button_copies_the_drain_command(qtbot, tmp_path):
    p = _panel(qtbot, _qfile(tmp_path))
    assert p.copy_cmd_btn.text() == "Copy kickoff command"
    p.copy_cmd_btn.click()
    assert QtWidgets.QApplication.clipboard().text() == KICKOFF_COMMAND
    assert "copied" in p.status_label.text().lower()


def test_panel_sign_in_button_fires_injected_login(qtbot, tmp_path):
    spy = []
    p = _panel(qtbot, _qfile(tmp_path), on_login=lambda: spy.append(True))
    assert p.login_btn.text() == "Sign in to sites"
    tip = p.login_btn.toolTip().lower()
    assert "linkedin" in tip and "inbox" in tip
    p.login_btn.click()
    assert spy == [True]
    assert "sign in" in p.status_label.text().lower()


def test_start_run_blocked_when_password_not_set(qtbot, tmp_path, monkeypatch):
    spy = []
    p = _panel(qtbot, _qfile(tmp_path), on_start_run=lambda: spy.append(True),
              password_exists=lambda: False)
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a)))
    confirms = []
    monkeypatch.setattr(p, "_confirm_run", lambda n: confirms.append(n) or True)

    p.start_run_btn.click()

    assert spy == []
    assert len(warned) == 1
    assert confirms == []             # never reached the confirm dialog


def test_start_run_blocked_on_empty_queue(qtbot, tmp_path, monkeypatch):
    spy = []
    p = _panel(qtbot, _qfile(tmp_path), on_start_run=lambda: spy.append(True),
              password_exists=lambda: True)
    informed = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: informed.append(a)))
    confirms = []
    monkeypatch.setattr(p, "_confirm_run", lambda n: confirms.append(n) or True)

    p.start_run_btn.click()          # queue file has no jobs at all

    assert spy == []
    assert len(informed) == 1
    assert confirms == []             # never reached the confirm dialog


def test_start_run_confirmed_fires_spawn(qtbot, tmp_path, monkeypatch):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"),
                        path=qfile)
    apply_queue.enqueue(apply_queue.new_entry("2", company="Globex", title="B"),
                        path=qfile)
    spy = []
    p = _panel(qtbot, qfile, on_start_run=lambda: spy.append(True),
              password_exists=lambda: True)
    monkeypatch.setattr(p, "_confirm_run", lambda n: True)

    p.start_run_btn.click()

    assert spy == [True]
    note = p.status_label.text().lower()
    assert "started" in note
    assert "new terminal" in note


def test_start_run_cancel_is_noop_and_passes_cap(qtbot, tmp_path, monkeypatch):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"),
                        path=qfile)
    spy = []
    seen = []
    p = _panel(qtbot, qfile, on_start_run=lambda: spy.append(True),
              password_exists=lambda: True)
    monkeypatch.setattr(p, "_confirm_run", lambda n: seen.append(n) or False)

    p.start_run_btn.click()

    assert spy == []                  # Cancel -> nothing launched
    assert seen == [1]                # N = min(queued=1, batch cap) reaches the dialog


def test_confirm_text_states_the_submit_gate(qtbot, tmp_path):
    p = _panel(qtbot, _qfile(tmp_path))
    text = p._confirm_text(3).lower()
    assert "3 queued job" in text
    assert "submits when" in text
    assert "never submitted" not in text and "nothing is ever submitted" not in text
    assert "auto_apply_submit" in text     # names the setting that turns the gate off


# --- ApplyQueuePanel: Start follows the Jev switch ----------------------------------

_JEV_OFF = "Auto-apply runs on Jev. Turn Jev on in Settings > Jev."


def test_start_is_off_with_the_reason_while_jev_cannot_run(qtbot, tmp_path):
    """The sentence `apply_run.py drain` prints is Start's tooltip and the notice
    under the buttons; a refresh reads the gate again and brings the run's own
    tooltip back."""
    reason = [_JEV_OFF]
    p = _panel(qtbot, _qfile(tmp_path), jev_blocked=lambda: reason[0])
    assert not p.start_run_btn.isEnabled()
    assert p.start_run_btn.toolTip() == _JEV_OFF
    assert p.jev_label.text() == _JEV_OFF and not p.jev_notice.isHidden()
    reason[0] = ""
    p.refresh()
    assert p.start_run_btn.isEnabled()
    assert "new terminal" in p.start_run_btn.toolTip().lower()
    assert p.jev_notice.isHidden()


def test_start_run_checks_jev_again_before_the_other_guards(qtbot, tmp_path, monkeypatch):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"), path=qfile)
    spy, confirms, reason = [], [], [""]
    p = _panel(qtbot, qfile, on_start_run=lambda: spy.append(True),
               password_exists=lambda: True, jev_blocked=lambda: reason[0])
    monkeypatch.setattr(p, "_confirm_run", lambda n: confirms.append(n) or True)
    assert p.start_run_btn.isEnabled()
    reason[0] = _JEV_OFF                    # switched off since the last refresh
    p._start_run()
    assert spy == [] and confirms == []
    assert p.status_label.text() == _JEV_OFF
    assert not p.start_run_btn.isEnabled()


def test_the_default_gate_is_jev_switch_with_a_saved_key_counted(monkeypatch):
    """The drain's console loads `.env` itself, so a key saved in Settings opens
    the gate before the dashboard restarts."""
    import settings
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: True)
    monkeypatch.setattr(settings, "secret_status", lambda: {"TYPESAFE_API_KEY": False})
    assert aqp._default_jev_blocked() == (
        "Auto-apply runs on Jev. Add the TypeSafe API key in Settings > Jev.")
    monkeypatch.setattr(settings, "secret_status", lambda: {"TYPESAFE_API_KEY": True})
    assert aqp._default_jev_blocked() == ""
    jev_switch.config_path().write_text('{"jev_enabled": false}', encoding="utf-8")
    assert aqp._default_jev_blocked() == _JEV_OFF


def test_a_gate_that_raises_leaves_start_on_for_the_drain_to_check(qtbot, tmp_path):
    """The panel never breaks on the gate. A probe that raises
    reads as "can start", since the drain asks again before a judge, a claim
    or a browser."""
    def broken():
        raise OSError("the config file is unreadable")
    p = _panel(qtbot, _qfile(tmp_path), jev_blocked=broken)
    assert p.start_run_btn.isEnabled()
    assert "new terminal" in p.start_run_btn.toolTip().lower()
    assert p.jev_notice.isHidden()
    assert p.refresh_jev_state() == ""


def test_the_default_gate_reads_a_broken_settings_backend_as_no_saved_key(monkeypatch):
    """An unreadable settings file counts as no saved key
    (`jev_switch.key_saved`), so Start names the key and the panel keeps
    working."""
    import settings

    def broken():
        raise OSError("the env file is unreadable")
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: True)
    monkeypatch.setattr(settings, "secret_status", broken)
    assert aqp._default_jev_blocked() == (
        "Auto-apply runs on Jev. Add the TypeSafe API key in Settings > Jev.")


def test_a_test_judge_leaves_start_off_with_the_drains_refusal(qtbot, tmp_path, monkeypatch):
    """The drain Start launches refuses the fake and replay
    judges (the Auto-apply judge setting) as fixture-only before its Jev gate,
    so Start is off with that sentence, with no key or SDK and the
    master switch on or off, and a click launches nothing."""
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: False)
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"), path=qfile)
    spy = []
    p = _panel(qtbot, qfile, on_start_run=lambda: spy.append(True),
               password_exists=lambda: True, jev_blocked=None)     # the real gate
    monkeypatch.setattr(p, "_confirm_run", lambda n: True)
    for mode in ("fake", "replay"):
        for switch in ("true", "false"):
            jev_switch.config_path().write_text(
                '{"jev_enabled": %s, "auto_apply_jev_mode": "%s"}' % (switch, mode),
                encoding="utf-8")
            p.refresh()
            case = (mode, switch)
            assert not p.start_run_btn.isEnabled(), case
            assert p.start_run_btn.toolTip() == jev_switch.FIXTURE_ONLY, case
            assert p.jev_label.text() == jev_switch.FIXTURE_ONLY, case
            assert not p.jev_notice.isHidden(), case
            p._start_run()
            assert spy == [], case
            assert p.status_label.text() == jev_switch.FIXTURE_ONLY, case


def test_an_unknown_judge_leaves_start_off_with_the_drains_refusal(qtbot, tmp_path,
                                                                   monkeypatch):
    """A hand-edited `"auto_apply_jev_mode": "typesaf"` with a key and the SDK
    in place would launch a drain that stops at `jev.get`. Start is off with
    the drain's refusal, with the master switch on or off, and a click
    launches nothing."""
    refusal = ("Unknown Auto-apply judge 'typesaf'; tick \"Show advanced settings\" "
               "and pick typesafe in Settings > Auto-apply.")
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: True)
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"), path=qfile)
    spy = []
    p = _panel(qtbot, qfile, on_start_run=lambda: spy.append(True),
               password_exists=lambda: True, jev_blocked=None)     # the real gate
    monkeypatch.setattr(p, "_confirm_run", lambda n: True)
    for switch in ("true", "false"):
        jev_switch.config_path().write_text(
            '{"jev_enabled": %s, "auto_apply_jev_mode": "typesaf"}' % switch, encoding="utf-8")
        p.refresh()
        assert not p.start_run_btn.isEnabled(), switch
        assert p.start_run_btn.toolTip() == refusal, switch
        assert p.jev_label.text() == refusal, switch
        assert not p.jev_notice.isHidden(), switch
        p._start_run()
        assert spy == [], switch
        assert p.status_label.text() == refusal, switch


def test_an_exported_test_mode_leaves_start_off(qtbot, tmp_path, monkeypatch):
    """The drain Start launches has no --jev flag and never reads
    AUTO_APPLY_JEV_MODE, so the shell's fake judge does not open the gate for a
    keyless live setting."""
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: True)
    monkeypatch.setenv("AUTO_APPLY_JEV_MODE", "fake")
    p = _panel(qtbot, _qfile(tmp_path), jev_blocked=None)     # the real gate
    assert not p.start_run_btn.isEnabled()
    assert p.start_run_btn.toolTip() == (
        "Auto-apply runs on Jev. Add the TypeSafe API key in Settings > Jev.")


def test_showing_the_tab_checks_the_jev_gate_again(qtbot, tmp_path, monkeypatch):
    """Start reads the gate on a queue change, a Settings save and a Start
    click (off, so out of reach), and a problem the user fixes outside the
    dashboard reaches none of them. Showing the tab reads the gate
    again, and so does the window coming back to the front while the tab
    shows, which is how a fix made in a terminal gets back here."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    sdk = [False]
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: sdk[0])
    tabs = QtWidgets.QTabWidget()
    qtbot.addWidget(tabs)
    tabs.addTab(QtWidgets.QWidget(), "Tracker")
    p = ApplyQueuePanel(queue_path=_qfile(tmp_path), jev_blocked=None)   # the real gate
    tabs.addTab(p, "Auto-apply")
    tabs.show()
    assert not p.start_run_btn.isEnabled()
    assert p.start_run_btn.toolTip() == (
        "Auto-apply runs on Jev. Install typesafe-sdk (venv\\Scripts\\python.exe -m pip install -r requirements.txt).")
    sdk[0] = True                                # the SDK got installed
    tabs.setCurrentWidget(p)
    assert p.start_run_btn.isEnabled() and p.jev_notice.isHidden()
    jev_switch.config_path().write_text('{"jev_enabled": false}', encoding="utf-8")
    QtWidgets.QApplication.sendEvent(tabs, QtCore.QEvent(QtCore.QEvent.Type.WindowActivate))
    assert not p.start_run_btn.isEnabled()
    assert p.start_run_btn.toolTip() == _JEV_OFF


def test_a_settings_save_checks_the_jev_gate_again(qtbot, monkeypatch, tmp_path):
    """Settings writes the switch to config.json and the save's refresh reads
    it, so Start follows the switch without a restart."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: True)
    w = _win(qtbot, monkeypatch, tmp_path)
    monkeypatch.setattr(w, "reload_data_async", lambda: None)
    start = w.apply_queue_panel.start_run_btn
    assert start.isEnabled()
    jev_switch.config_path().write_text('{"jev_enabled": false}', encoding="utf-8")
    w._on_settings_saved()
    assert not start.isEnabled() and start.toolTip() == _JEV_OFF
    jev_switch.config_path().write_text('{"jev_enabled": true}', encoding="utf-8")
    w._on_settings_saved()
    assert start.isEnabled()


def _decoded(argv):
    """The PowerShell command inside a `-EncodedCommand` argv (pure: no Popen)."""
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
    for i, a in enumerate(argv):
        if a.lower() in ("-encodedcommand", "/encodedcommand"):
            import base64
            return base64.b64decode(argv[i + 1]).decode("utf-16-le")
    return " ".join(argv)


def test_kickoff_and_login_argv_decode_to_their_commands(monkeypatch):
    called = []
    monkeypatch.setattr(aqp.subprocess, "Popen",
                        lambda *a, **k: called.append((a, k)))
    assert _decoded(aqp._kickoff_argv()) == KICKOFF_COMMAND
    assert _decoded(aqp._login_argv()) == LOGIN_COMMAND
    assert called == []               # pure - never spawns anything


def test_spawn_helpers_open_a_new_console_with_the_inherited_environment(monkeypatch):
    """`apply_run.py` is this project's own code and reads `.env` itself, so a
    first pasted key reaches it; the Settings tab's TYPESAFE_API_KEY row is
    marked `restart` because a rotated key sits behind the dashboard's startup
    snapshot: no scrub, no env override."""
    seen = []
    monkeypatch.setattr(aqp.subprocess, "Popen",
                        lambda *a, **k: seen.append((a, k)) or None)
    aqp._spawn_kickoff()
    aqp._spawn_login()
    assert [_decoded(a[0]) for a, _k in seen] == [KICKOFF_COMMAND, LOGIN_COMMAND]
    for _a, k in seen:
        assert k.get("env") is None
        assert k.get("creationflags") == getattr(aqp.subprocess, "CREATE_NEW_CONSOLE", 0)


def test_panel_password_label_flips_with_password_exists(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(aqp, "_default_password_exists", lambda: False)
    p = _panel(qtbot, _qfile(tmp_path))
    assert "NOT SET" in p.pw_label.text()
    monkeypatch.setattr(aqp, "_default_password_exists", lambda: True)
    p.refresh_password_state()
    assert "NOT SET" not in p.pw_label.text()
    assert "SET" in p.pw_label.text()


def test_panel_set_password_button_fires_injected_callback(qtbot, tmp_path):
    fired = []
    p = _panel(qtbot, _qfile(tmp_path), on_set_password=lambda: fired.append(True))
    p.pw_btn.click()
    assert fired == [True]


def test_panel_mark_applied_button_fires_injected_callback(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry(
        "1", company="Acme", title="A", apply_url="https://x/1"), path=qfile)
    got = []
    p = _panel(qtbot, qfile, on_mark_applied=lambda e: got.append(e))
    p.table.selectRow(0)
    p.mark_applied_btn.click()
    assert got and got[0]["job_posting_id"] == "1"


def test_panel_dont_apply_button_fires_injected_callback(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry(
        "1", company="Acme", title="A", apply_url="https://x/1"), path=qfile)
    got = []
    p = _panel(qtbot, qfile, on_mark_seen=lambda e: got.append(e))
    p.table.selectRow(0)
    p.dont_apply_btn.click()
    assert got and got[0]["job_posting_id"] == "1"


def test_panel_copy_password_button_copies_and_confirms(qtbot, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(aqp.ats_accounts, "copy_password_to_clipboard",
                        lambda: called.append(True) or True)
    p = _panel(qtbot, _qfile(tmp_path), password_exists=lambda: True)  # enables copy_pw_btn
    p.copy_pw_btn.click()
    assert called == [True]
    note = p.status_label.text()
    assert "copied" in note.lower()
    # secret-safety: the confirmation is generic — it renders no password value.
    assert "password" in note.lower() and ":" not in note  # a plain confirmation, not a value dump


def test_panel_copy_password_refused_when_unset(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(aqp.ats_accounts, "copy_password_to_clipboard", lambda: False)
    p = _panel(qtbot, _qfile(tmp_path), password_exists=lambda: True)
    p.copy_pw_btn.click()
    assert "set" in p.status_label.text().lower()   # "click 'Set…' first"


def test_panel_mutations_go_through_injected_submit_write(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"),
                        path=qfile)
    calls = []

    def fake_submit(fn, on_done=None, on_error=None):
        calls.append(fn)          # captured, NOT executed -> file must not change

    p = _panel(qtbot, qfile, submit_write=fake_submit)
    p.table.selectRow(0)
    p._requeue()
    p._remove()
    p._clear_finished()
    assert len(calls) == 3
    assert len(apply_queue.load(qfile)["jobs"]) == 1   # nothing ran yet


# --- settings: the Auto-apply section ---------------------------------------------


def test_settings_schema_has_auto_apply_fields():
    import settings
    by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
    cap = by_key["auto_apply_batch_cap"]
    assert (cap.type, cap.default, cap.min, cap.max) == ("int", 10, 1, 25)
    assert cap.section == "Auto-apply" and cap.target == "config"
    assert "submits when" in cap.help and "never submitted" not in cap.help
    # defaults surface through load() even with no backing file on disk
    values = settings.load(targets={})
    assert values["auto_apply_batch_cap"] == 10
    # the 1-25 range is enforced
    assert "auto_apply_batch_cap" in settings.validate({"auto_apply_batch_cap": 26})
    assert "auto_apply_batch_cap" in settings.validate({"auto_apply_batch_cap": 0})
    assert settings.validate({"auto_apply_batch_cap": 10}) == {}


# --- header layout at other interface scales ---------------------------------


def test_count_columns_stay_wider_than_their_own_headings(qtbot, tmp_path):
    """A header section clips instead of eliding, so a column narrower than its
    title silently drops the first and last letter: Attempts and Missing shipped
    at 70px and 60px with 18px of padding inside them and read "ttempt" and
    "lissin" at 150%. They size themselves now, so no scale can strand them."""
    from qt import theme
    app = QtWidgets.QApplication.instance()
    p = _panel(qtbot, _qfile(tmp_path))
    header = p.table.horizontalHeader()
    try:
        for scale in (0.75, 1.0, 1.25, 1.5):
            theme.set_scale(app, scale)
            p.table.resizeColumnsToContents()
            for name in ("Attempts", "Missing"):
                i = aqp.COLUMNS.index(name)
                assert header.sectionResizeMode(i) == \
                    QtWidgets.QHeaderView.ResizeMode.ResizeToContents
                assert header.sectionSize(i) >= \
                    header.fontMetrics().horizontalAdvance(name), (name, scale)
    finally:
        theme.set_scale(app, 1.0)


def test_status_chips_keep_their_labels_on_a_narrow_window(qtbot, tmp_path):
    """One header row needed 1428px at 150%; below that Qt took the deficit out
    of the chips and they read "Ready to su" / "Needs revie"."""
    from qt import theme
    app = QtWidgets.QApplication.instance()
    p = _panel(qtbot, _qfile(tmp_path))
    p.show()
    try:
        for scale in (1.0, 1.25, 1.5):
            theme.set_scale(app, scale)
            for width in (1000, 1280, 1920):
                p.resize(width, 700)
                app.processEvents()
                for key, chip in p.status_chips._chips.items():
                    assert chip.width() >= chip.sizeHint().width(), \
                        (key, scale, width)
    finally:
        theme.set_scale(app, 1.0)
        p.hide()


# --- the difficulty check -----------------------------------------------------------------

_NOW = _dt.datetime(2026, 9, 25, 12, 0, 0)


class _Clock(_dt.datetime):
    """apply_assess's `datetime` with now pinned to `_NOW`."""
    @classmethod
    def now(cls, tz=None):
        return cls.fromisoformat(_NOW.isoformat()) if tz is None else _NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def _assess_clock(monkeypatch):
    """The difficulty column's ages ("N days old", "failed today") read `_NOW`,
    the same moment the tests stamp their results with, so no run that crosses
    a day boundary between the stamp and the read moves a result's age."""
    import apply_assess
    monkeypatch.setattr(apply_assess, "datetime", _Clock)


def _difficulty(score=7, *, days_old=0, questions=None, reasons=None):
    from apply_assess import band_for
    checked = (_NOW - _dt.timedelta(days=days_old)).isoformat(timespec="seconds")
    return {"score": score, "band": band_for(score), "checked_at": checked, "system": "lever",
            "reasons": reasons or ["Application system: Lever (base 2)"],
            "questions": questions or [], "jev_usd": 0.0}


_QUESTION = {"label": "Which public dataset do you know best?", "help": "One line is plenty",
             "options": [], "required": True, "type": "text"}
_CHOICE = {"label": "Preferred office", "help": "", "options": ["Austin", "Remote"],
           "required": True, "type": "choice"}
_UNSET = object()


def _checked(qfile, jid="1", difficulty=_UNSET):
    apply_queue.enqueue(apply_queue.new_entry(jid, company="Acme", title="Analyst",
                                              apply_url=f"https://x/{jid}"), path=qfile)
    if difficulty is not None:
        apply_queue.set_difficulty(jid, _difficulty() if difficulty is _UNSET else difficulty,
                                   path=qfile)


def _row_of(p, jid):
    return next(r for r in range(p.table.rowCount())
                if p.table.item(r, 0).data(QtCore.Qt.ItemDataRole.UserRole) == jid)


def _dpanel(qtbot, qfile, **kw):
    for name, value in (("difficulty_blocked", lambda: ""), ("difficulty_hidden", lambda: False),
                        ("profile_busy", lambda: False)):
        kw.setdefault(name, value)
    return _panel(qtbot, qfile, **kw)


def test_the_difficulty_column_sits_before_updated():
    assert aqp.COLUMNS.index("Difficulty") == aqp.COLUMNS.index("Missing") + 1
    assert aqp.COLUMN_IDS[aqp.COLUMNS.index("Difficulty")] == "difficulty"
    assert aqp.COLUMNS[-1] == "Note"             # the stretch column stays last
    assert len(aqp.COLUMNS) == len(aqp.COLUMN_IDS)


@pytest.mark.parametrize("score, family", [(2, "success"), (5, "warning"), (9, "danger")])
def test_the_difficulty_cell_shows_the_score_coloured_by_band(qtbot, tmp_path, score, family):
    from qt.delegates import BAND_ROLE
    qfile = _qfile(tmp_path)
    _checked(qfile, difficulty=_difficulty(score))
    p = _dpanel(qtbot, qfile)
    item = p.table.item(0, aqp.COLUMNS.index("Difficulty"))
    assert item.text() == f"{score}/10"
    assert item.data(BAND_ROLE) == family
    assert not p.table.grab().isNull()           # the pill paints


def test_an_unchecked_job_shows_no_difficulty(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    _checked(qfile, difficulty=None)
    p = _dpanel(qtbot, qfile)
    assert p.table.item(0, aqp.COLUMNS.index("Difficulty")).text() == ""


def test_the_difficulty_tooltip_lists_the_reasons_and_the_exact_questions(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    _checked(qfile, difficulty=_difficulty(
        5, questions=[_QUESTION, _CHOICE],
        reasons=["Application system: Lever (base 2)",
                 "2 required questions your answers cannot fill (+3)"]))
    p = _dpanel(qtbot, qfile)
    tip = p.table.item(0, aqp.COLUMNS.index("Difficulty")).toolTip()
    for part in ("5/10", "May need an answer or two", "Application system: Lever (base 2)",
                 "2 required questions your answers cannot fill (+3)",
                 "Which public dataset do you know best?", "One line is plenty",
                 "Preferred office", "Austin, Remote"):
        assert part in tip, part


def test_the_difficulty_tooltip_escapes_the_pages_words(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    odd = dict(_QUESTION, label="Salary <b>range</b> & notes")
    _checked(qfile, difficulty=_difficulty(5, questions=[odd]))
    p = _dpanel(qtbot, qfile)
    tip = p.table.item(0, aqp.COLUMNS.index("Difficulty")).toolTip()
    assert "Salary &lt;b&gt;range&lt;/b&gt; &amp; notes" in tip


def test_a_result_older_than_seven_days_shows_its_age(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    _checked(qfile, "1", difficulty=_difficulty(3, days_old=10))
    _checked(qfile, "2", difficulty=_difficulty(3, days_old=6))
    p = _dpanel(qtbot, qfile)
    col = aqp.COLUMNS.index("Difficulty")
    assert p.table.item(_row_of(p, "1"), col).text() == "3/10 (10 days old)"
    assert "10 days old" in p.table.item(_row_of(p, "1"), col).toolTip()
    assert p.table.item(_row_of(p, "2"), col).text() == "3/10"


def test_the_check_is_hidden_while_its_switch_is_off(qtbot, tmp_path):
    hidden = [True]
    p = _dpanel(qtbot, _qfile(tmp_path), difficulty_hidden=lambda: hidden[0])
    assert p.check_difficulty_btn.isHidden()
    hidden[0] = False
    p.refresh_jev_state()
    assert not p.check_difficulty_btn.isHidden()


def test_the_check_is_off_with_the_reason_while_jev_cannot_run(qtbot, tmp_path):
    why = ["Auto-apply runs on Jev. Install typesafe-sdk (venv\\Scripts\\python.exe -m pip install -r requirements.txt)."]
    p = _dpanel(qtbot, _qfile(tmp_path), difficulty_blocked=lambda: why[0])
    assert not p.check_difficulty_btn.isHidden()
    assert not p.check_difficulty_btn.isEnabled()
    assert p.check_difficulty_btn.toolTip() == why[0]
    why[0] = ""
    p.showEvent(QtGui.QShowEvent())            # the gate is read again when the tab shows
    assert p.check_difficulty_btn.isEnabled()
    assert p.check_difficulty_btn.toolTip() == p._check_tip


def test_the_check_is_off_while_a_browser_holds_the_profile(qtbot, tmp_path):
    busy = [True]
    p = _dpanel(qtbot, _qfile(tmp_path), profile_busy=lambda: busy[0])
    assert not p.check_difficulty_btn.isEnabled()
    assert p.check_difficulty_btn.toolTip() == aqp.apply_assess.PROFILE_BUSY
    busy[0] = False
    p.refresh_jev_state()
    assert p.check_difficulty_btn.isEnabled()


def test_the_check_runs_the_selected_job(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    _checked(qfile, "1", difficulty=None)
    _checked(qfile, "2", difficulty=None)
    calls = []
    p = _dpanel(qtbot, qfile, on_check_difficulty=calls.append)
    p.table.selectRow(_row_of(p, "2"))
    p.check_difficulty_btn.click()
    assert calls == [["2"]]


def test_the_check_with_no_selection_asks_before_checking_every_queued_job(qtbot, tmp_path,
                                                                           monkeypatch):
    qfile = _qfile(tmp_path)
    _checked(qfile, "1", difficulty=None)
    _checked(qfile, "2", difficulty=None)
    calls, asked = [], []
    p = _dpanel(qtbot, qfile, on_check_difficulty=calls.append)
    answer = [False]
    monkeypatch.setattr(p, "_confirm_check", lambda n: asked.append(n) or answer[0])
    p.table.clearSelection()
    p.check_difficulty_btn.click()
    assert asked == [2] and calls == []
    answer[0] = True
    p.check_difficulty_btn.click()
    assert calls == [[]]


def test_the_check_reads_the_gate_again_at_the_click(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    _checked(qfile, "1", difficulty=None)
    busy, calls = [False], []
    p = _dpanel(qtbot, qfile, profile_busy=lambda: busy[0],
                on_check_difficulty=calls.append)
    p.table.selectRow(0)
    busy[0] = True                              # a drain started since the last refresh
    p._check_difficulty()
    assert calls == [] and p.status_label.text() == aqp.apply_assess.PROFILE_BUSY


def test_the_check_command_runs_apply_assess_in_the_repo():
    root = Path("C:/Users/o'brien/[repo]")
    literal = str(root).replace("'", "''")
    assert aqp._assess_command(root, ["1", "2"]) == (
        f"Set-Location -LiteralPath '{literal}'; python local/apply_assess.py '1' '2'")
    assert aqp._assess_command(root, []).endswith(
        "python local/apply_assess.py --all")
    assert aqp._assess_command(root, ["manual-0a1b_C"]).endswith("'manual-0a1b_C'")


def test_the_default_check_spawns_its_own_console(monkeypatch):
    import base64
    spawned = []
    monkeypatch.setattr(aqp, "_spawn_console", spawned.append)
    aqp._spawn_check(["7"])
    [argv] = spawned
    assert argv[:3] == ["powershell", "-NoExit", "-EncodedCommand"]
    assert base64.b64decode(argv[3]).decode("utf-16-le") == \
        aqp._assess_command(aqp.REPO_ROOT, ["7"])


def test_the_default_gate_is_the_checks_own(monkeypatch):
    seen = []
    monkeypatch.setattr(jev_switch, "key_saved", lambda: True)
    monkeypatch.setattr(aqp.apply_assess, "refusal", lambda **kw: seen.append(kw) or "no")
    assert aqp._default_difficulty_blocked() == "no"
    assert seen == [{"saved_key": True}]


def test_the_default_hiding_follows_the_switches():
    path = jev_switch.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"jev_enabled": true, "jev_difficulty": false}', encoding="utf-8")
    assert aqp._default_difficulty_hidden() is True
    path.write_text('{"jev_enabled": false, "jev_difficulty": true}', encoding="utf-8")
    assert aqp._default_difficulty_hidden() is True
    path.write_text('{"jev_enabled": true, "jev_difficulty": true}', encoding="utf-8")
    assert aqp._default_difficulty_hidden() is False


# --- the profile gate, the order of the gates, the check's tooltips ------------------------

def test_start_is_off_with_the_sentence_while_a_browser_holds_the_profile(qtbot, tmp_path,
                                                                          monkeypatch):
    import profile_lock
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"), path=qfile)
    busy, spy = [True], []
    p = _panel(qtbot, qfile, on_start_run=lambda: spy.append(True),
               password_exists=lambda: True, profile_busy=lambda: busy[0])
    monkeypatch.setattr(p, "_confirm_run", lambda n: True)
    assert not p.start_run_btn.isEnabled()
    assert p.start_run_btn.toolTip() == profile_lock.RUN_BUSY
    assert p.jev_label.text() == profile_lock.RUN_BUSY and not p.jev_notice.isHidden()
    p._start_run()
    assert spy == [] and p.status_label.text() == profile_lock.RUN_BUSY
    busy[0] = False
    p.refresh_jev_state()
    assert p.start_run_btn.isEnabled() and p.jev_notice.isHidden()
    p._start_run()
    assert spy == [True]


def test_the_jev_gate_speaks_before_the_profile(qtbot, tmp_path):
    p = _panel(qtbot, _qfile(tmp_path), jev_blocked=lambda: "Jev is off",
               profile_busy=lambda: True)
    assert p.start_run_btn.toolTip() == "Jev is off"


@pytest.mark.parametrize("holder", ["chrome", "sentinel"])
def test_both_gates_see_chromes_lock_and_the_sentinel(qtbot, tmp_path, monkeypatch, holder):
    # the panel's own profile seam: Start is off while a check holds the
    # profile, Check difficulty while a run does, by either sign
    import profile_lock
    from test_profile_lock import hold_chrome_lock, hold_sentinel
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    qfile = _qfile(tmp_path)
    _checked(qfile, "1", difficulty=None)
    p = _panel(qtbot, qfile, difficulty_blocked=lambda: "", difficulty_hidden=lambda: False)
    assert p.start_run_btn.isEnabled() and p.check_difficulty_btn.isEnabled()
    profile = profile_lock.default_profile_dir()
    release = hold_chrome_lock(profile) if holder == "chrome" else hold_sentinel(profile).release
    try:
        p.refresh_jev_state()
        assert not p.start_run_btn.isEnabled() and not p.check_difficulty_btn.isEnabled()
        assert p.start_run_btn.toolTip() == profile_lock.RUN_BUSY
        assert p.check_difficulty_btn.toolTip() == aqp.apply_assess.PROFILE_BUSY
    finally:
        release()
    p.refresh_jev_state()
    assert p.start_run_btn.isEnabled() and p.check_difficulty_btn.isEnabled()


_UNKNOWN_JUDGE = ("Unknown Auto-apply judge 'typesaf'; tick \"Show advanced settings\" "
                  "and pick typesafe in Settings > Auto-apply.")


def test_an_unknown_judge_leaves_check_difficulty_off_in_the_checks_words(qtbot, tmp_path,
                                                                          monkeypatch, capsys):
    """The coordinator's heads-up: for an unknown mode `jev_on("difficulty")`
    reads on while `client` builds nothing, so the button follows the
    check's refusal, the sentence `apply_assess.py` prints."""
    import apply_run
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: True)
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    monkeypatch.setattr(apply_run, "_load_env", lambda: None)
    monkeypatch.setattr(apply_run, "load_settings", lambda: dict(apply_run.DEFAULT_SETTINGS))
    path = jev_switch.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"jev_enabled": true, "jev_difficulty": true, '
                    '"auto_apply_jev_mode": " TypeSaf "}', encoding="utf-8")
    p = _dpanel(qtbot, _qfile(tmp_path), difficulty_blocked=None)      # the real gate
    assert not p.check_difficulty_btn.isHidden()
    assert not p.check_difficulty_btn.isEnabled()
    assert p.check_difficulty_btn.toolTip() == _UNKNOWN_JUDGE
    assert aqp.apply_assess.main(["--all"]) == 2
    assert capsys.readouterr().err.strip() == p.check_difficulty_btn.toolTip()


_UPLOAD = {"label": "Portfolio (PDF)", "help": "", "options": [], "required": True,
           "type": "file"}


def test_a_required_upload_shows_in_the_tooltip(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    _checked(qfile, "1", difficulty=_difficulty(
        6, questions=[_UPLOAD, _QUESTION],
        reasons=["Application system: Lever (base 2)",
                 "2 required questions your answers cannot fill (+3)"]))
    p = _dpanel(qtbot, qfile)
    tip = p.table.item(0, aqp.COLUMNS.index("Difficulty")).toolTip()
    assert "Portfolio (PDF) (an upload: put the file in the job folder)" in tip


def test_a_failed_check_shows_in_the_tooltip_over_the_earlier_result(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    _checked(qfile, "1", difficulty=_difficulty(3))
    now = _NOW.isoformat(timespec="seconds")
    apply_queue.note_difficulty_failure("1", "the posting did not load (TimeoutError)",
                                        at=now, path=qfile)
    _checked(qfile, "2", difficulty=None)
    apply_queue.note_difficulty_failure("2", "the posting did not load (TimeoutError)",
                                        at=now, path=qfile)
    p = _dpanel(qtbot, qfile)
    col = aqp.COLUMNS.index("Difficulty")
    one, two = p.table.item(_row_of(p, "1"), col), p.table.item(_row_of(p, "2"), col)
    assert one.text() == "3/10"
    assert ("Last check failed today: the posting did not load (TimeoutError). Showing the "
            "earlier result.") in one.toolTip()
    assert "3/10" in one.toolTip()
    assert two.text() == "check failed"
    assert "Last check failed today: the posting did not load (TimeoutError)." in two.toolTip()



# --- the poll re-reads the profile gate ---------------------------------------------------

def test_the_poll_reads_the_profile_gate_again_when_a_browser_closes(qtbot, tmp_path,
                                                                     monkeypatch):
    """Start and Check difficulty come back on the next poll tick after the
    browser holding the profile closes, with no queue change, no tab show and
    no window activate; the poll re-reads the gates only when the profile's
    state moved."""
    qfile = _qfile(tmp_path)
    _checked(qfile, "1", difficulty=None)
    busy, gate_reads = [True], []
    p = _dpanel(qtbot, qfile, profile_busy=lambda: busy[0],
                jev_blocked=lambda: gate_reads.append(True) or "")
    monkeypatch.setattr(p, "refresh", lambda: pytest.fail("the queue did not change"))
    assert not p.start_run_btn.isEnabled() and not p.check_difficulty_btn.isEnabled()
    before = len(gate_reads)
    p._poll_for_changes()
    assert len(gate_reads) == before              # still held: nothing to repaint
    busy[0] = False
    p._poll_for_changes()
    assert p.start_run_btn.isEnabled() and p.check_difficulty_btn.isEnabled()
    assert p.jev_notice.isHidden()
    busy[0] = True
    p._poll_for_changes()
    assert not p.start_run_btn.isEnabled() and not p.check_difficulty_btn.isEnabled()


# --- the "Waiting for you" card and Answer now -----------------------------------------------

import apply_pause  # noqa: E402
from qt import apply_pause_card as apc  # noqa: E402
from resume_tailor import apply_answers  # noqa: E402

_PAUSE_JOB = {"job_posting_id": "42", "company": "Fabrikam", "title": "Analytics Engineer"}


def _q(key, label, widget, **kw):
    q = {"key": str(key), "field_id": f"f{key}", "label": label, "help": "",
         "placeholder": "", "type": "text", "field_type": "text", "widget": widget,
         "options": [], "required": True, "sensitive": False}
    q.update(kw)
    return q


_PAUSE_QUESTIONS = [
    _q(1, "Preferred team", apply_pause.W_CHOICE, options=["Data", "Platform"], type="choice",
       help="Pick the team you would join first"),
    _q(2, "Preferred office", apply_pause.W_CHOICE, type="choice",
       options=["Austin", "Boston", "Chicago", "Denver", "Remote"]),
    _q(3, "I agree to be contacted by text message", apply_pause.W_YES_NO, type="yes_no",
       field_type="checkbox"),
    _q(4, "Number of conference talks given", apply_pause.W_NUMBER, type="number",
       field_type="number"),
    _q(5, "What is your favourite query language?", apply_pause.W_TEXT),
    _q(6, "Date of birth", apply_pause.W_BROWSER, sensitive=True),
]


@pytest.fixture
def pause_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    return tmp_path


def _ask(questions=None, job=None, headless=False, pause_id="p1"):
    return apply_pause.write_request(job or _PAUSE_JOB, "https://careers.example/apply",
                                     "required field without an answer: Preferred team",
                                     questions if questions is not None else _PAUSE_QUESTIONS,
                                     headless=headless, pause_id=pause_id)


def _card(qtbot, request=None):
    card = apc.PauseCard()
    qtbot.addWidget(card)
    card.set_request(request or apply_pause.read_request(apply_pause.request_path("42")))
    return card


def _row_for(card, key):
    return next(r for r in card.rows if r["question"]["key"] == str(key))


def test_the_card_shows_each_question_in_its_real_widget(qtbot, pause_home):
    _ask()
    card = _card(qtbot)
    assert not card.isHidden()
    assert "Waiting for you" in card.title_label.text() and "Fabrikam" in card.title_label.text()
    assert card.reason_label.text() == "required field without an answer: Preferred team"
    kinds = {r["question"]["key"]: r["kind"] for r in card.rows}
    assert kinds == {"1": "radio", "2": "combo", "3": "combo", "4": "line", "5": "line",
                     "6": apply_pause.W_BROWSER}
    assert [b.text() for b in _row_for(card, 1)["widget"].group.buttons()] == ["Data", "Platform"]
    office = _row_for(card, 2)["widget"]
    assert [office.itemText(i) for i in range(office.count())] == [
        apc.NOTHING, "Austin", "Boston", "Chicago", "Denver", "Remote"]
    yes_no = _row_for(card, 3)["widget"]
    assert [yes_no.itemText(i) for i in range(yes_no.count())] == [apc.NOTHING, "Yes", "No"]
    assert _row_for(card, 4)["widget"].validator() is not None
    # the help shows under its question; a sensitive field only points at the browser
    texts = [w.text() for w in card.findChildren(QtWidgets.QLabel)]
    assert "Pick the team you would join first" in texts
    assert apc.BROWSER_ONLY in texts
    # a save box per question, off by default, none for the sensitive field
    assert _row_for(card, 6)["save"] is None and _row_for(card, 6)["widget"] is None
    for key in (1, 2, 3, 4, 5):
        box = _row_for(card, key)["save"]
        assert box.text() == apc.SAVE_LABEL and not box.isChecked() and box.isEnabled()
    assert not card.browser_btn.isHidden()


def test_the_save_box_is_off_with_the_reason_add_answer_would_refuse(qtbot, pause_home):
    apply_answers.save([{"id": "preferred_team", "question": "Preferred team Pick the team you "
                         "would join first", "type": "text", "answer": "Data", "note": "",
                         "confirmed": True, "status": "active"}])
    builtin = apply_answers.BUILTINS["work_authorized"].question
    _ask(_PAUSE_QUESTIONS[:1] + [_q(7, builtin, apply_pause.W_YES_NO, type="yes_no")])
    card = _card(qtbot)
    dup, own = _row_for(card, 1)["save"], _row_for(card, 7)["save"]
    assert not dup.isEnabled() and "already has this question" in dup.toolTip()
    assert not own.isEnabled() and "built-in answer" in own.toolTip()


def test_fill_and_continue_writes_the_answers_and_the_save_flags(qtbot, pause_home):
    _ask()
    card = _card(qtbot)
    sent = []
    card.answered.connect(lambda job, mode: sent.append((job, mode)))
    card.set_value("1", "Platform")
    card.set_value("2", "Remote")
    card.set_value("3", "Yes")
    _row_for(card, 4)["widget"].setText("37")
    _row_for(card, 5)["widget"].setText("Datalog")
    _row_for(card, 1)["save"].setChecked(True)
    _row_for(card, 5)["save"].setChecked(True)
    card.fill_btn.click()
    assert sent == [("42", "fill")]
    assert card.isHidden()
    got = apply_pause.read_answer("42", "p1")
    assert got["mode"] == "fill"
    assert got["values"] == {"1": "Platform", "2": "Remote", "3": "Yes", "4": "37",
                             "5": "Datalog"}
    assert got["save"] == {"1": True, "5": True}
    assert apply_pause.pending_requests() == []


def test_a_number_box_takes_only_a_number(qtbot, pause_home):
    _ask()
    card = _card(qtbot)
    box = _row_for(card, 4)["widget"]
    qtbot.keyClicks(box, "3a7")
    assert box.text() == "37"


def test_park_it_and_the_browser_button_write_their_modes(qtbot, pause_home):
    _ask()
    card = _card(qtbot)
    card.set_value("1", "Platform")
    card.browser_btn.click()
    assert apply_pause.read_answer("42", "p1") == {"mode": "browser", "values": {}, "save": {}}
    _ask(pause_id="p2")
    card = _card(qtbot)
    card.park_btn.click()
    assert apply_pause.read_answer("42", "p2")["mode"] == "park"


def test_a_headless_run_hides_the_browser_button(qtbot, pause_home):
    _ask(headless=True)
    card = _card(qtbot)
    assert card.browser_btn.isHidden()
    assert not card.fill_btn.isHidden() and not card.park_btn.isHidden()
    texts = [w.text() for w in card.findChildren(QtWidgets.QLabel)]
    assert apc.BROWSER_ONLY_HEADLESS in texts


def test_the_poll_shows_a_new_request_flashes_once_and_keeps_a_half_typed_answer(
        qtbot, tmp_path, pause_home):
    flashes = []
    p = _dpanel(qtbot, _qfile(tmp_path), alert=flashes.append)
    assert p.pause_card.isHidden() and flashes == []
    _ask()
    p._poll_for_changes()
    assert not p.pause_card.isHidden() and p.pause_card.job_id() == "42"
    assert flashes == [p]
    _row_for(p.pause_card, 5)["widget"].setText("Datal")
    p._poll_for_changes()
    assert flashes == [p]                               # seen once, flashed once
    assert _row_for(p.pause_card, 5)["widget"].text() == "Datal"
    # a second run waits behind it
    _ask(job={"job_posting_id": "43", "company": "Contoso", "title": "Analyst"}, pause_id="p9")
    p._poll_for_changes()
    assert p.pause_card.more_label.text() == "1 more waiting" and len(flashes) == 2
    p.pause_card.park_btn.click()
    assert p.pause_card.job_id() == "43"                # the next one shows at once
    assert "The job parks" in p.status_label.text()
    apply_pause.clear("43")
    p._poll_for_changes()
    assert p.pause_card.isHidden()


def _lum(c: QtGui.QColor) -> float:
    def ch(v):
        v = v / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def _ratio(a: QtGui.QColor, b: QtGui.QColor) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_cards_choices_sit_on_the_callout_and_show_their_rings(qtbot, pause_home):
    """Inside the warning callout a save box and a radio button painted the
    window's colour behind themselves, a dark band across the amber card,
    and a radio's ring (dark on dark) did not show at all."""
    from qt import theme
    _ask()
    card = _card(qtbot)
    host = QtWidgets.QWidget()          # the tab's window-coloured ground
    qtbot.addWidget(host)
    host.setStyleSheet(theme._qss())
    QtWidgets.QVBoxLayout(host).addWidget(card)
    host.resize(700, host.sizeHint().height())
    host.show()
    QtWidgets.QApplication.processEvents()
    img = host.grab().toImage()
    dpr = img.devicePixelRatio()

    def at(widget, x, y):
        pt = widget.mapTo(host, QtCore.QPoint(x, y))
        return img.pixelColor(round(pt.x() * dpr), round(pt.y() * dpr))

    save = _row_for(card, 1)["save"]
    title = card.title_label           # a label: transparent on the callout
    fill = at(title, title.width() - 2, title.height() // 2)
    beside = at(save, save.width() - 3, save.height() // 2)
    assert beside.name() == fill.name()
    radio = _row_for(card, 1)["widget"].group.buttons()[0]
    ground = at(radio, radio.width() - 2, 1)
    assert ground.name() == fill.name()
    # WCAG 1.4.11: a control's boundary 3:1 on its ground. The ring is drawn in
    # BORDER_INPUT; its antialiased pixels come out a shade under the token.
    assert _ratio(QtGui.QColor(theme.BORDER_INPUT), ground) >= 3.0
    ring = max(_ratio(at(radio, x, radio.height() // 2), ground) for x in range(0, 4))
    assert ring >= 2.7


def test_the_pause_card_sits_at_the_top_of_the_tab(qtbot, tmp_path, pause_home):
    p = _dpanel(qtbot, _qfile(tmp_path), alert=lambda w: None)
    assert p.body.layout().itemAt(0).widget() is p.pause_card


_LONG_PAUSE = _PAUSE_QUESTIONS + [
    _q(10 + i, f"Screening question {i}", apply_pause.W_TEXT, help="A sentence is enough")
    for i in range(8)]


def test_a_tall_page_leaves_the_other_tabs_minimum_alone(qtbot, monkeypatch, tmp_path):
    """A QTabWidget is as tall at minimum as its tallest page, and under High
    Score that height came out of the job detail card."""
    w = _win(qtbot, monkeypatch, tmp_path)
    tall = QtWidgets.QWidget()
    tall.setMinimumHeight(900)
    w.tracker_tab.layout().addWidget(tall)
    page_min = w.tracker_tab.minimumSizeHint().height()
    high = w._tab_widgets["High Score (Unseen)"]
    w.tabs.setCurrentWidget(high)
    assert high.minimumSizeHint().height() < page_min
    assert w.tabs.minimumSizeHint().height() < page_min
    w.tabs.setCurrentWidget(w.tracker_tab)
    assert w.tabs.minimumSizeHint().height() >= page_min


def test_a_waiting_pause_scrolls_the_tab_and_never_crushes_a_row(
        qtbot, tmp_path, pause_home):
    """With a pause waiting, the Jev notice and a parked job selected, the tab
    asked for more height than a 1100x700 window has (704px at 100%, 889px at
    150%), and the buttons came out a few pixels tall. The tab scrolls, and
    every part of it keeps its own height: each question, a few queue rows,
    and the whole Missing answers note."""
    qfile = _qfile(tmp_path)
    _parked(qfile, ("Preferred team", {}), ("Desired salary", {}))
    _ask(questions=_LONG_PAUSE)
    p = _dpanel(qtbot, qfile, alert=lambda w: None)
    p.jev_notice.setVisible(True)
    host = QtWidgets.QWidget()
    qtbot.addWidget(host)
    QtWidgets.QVBoxLayout(host).addWidget(p)
    host.setFixedSize(1100, 450)
    host.show()
    p.table.selectRow(0)
    QtWidgets.QApplication.processEvents()
    assert not p.pause_card.isHidden() and not p.details.answer_now_btn.isHidden()
    for b in p.findChildren(QtWidgets.QPushButton):
        if b.isVisibleTo(p):
            assert b.height() >= b.minimumSizeHint().height(), b.text()
    assert len(p.pause_card.rows) == len(_LONG_PAUSE)
    for r in p.pause_card.rows:
        frame = r["frame"]
        assert frame.height() >= frame.minimumSizeHint().height(), r["question"]["label"]
    rows = p.table.verticalHeader().defaultSectionSize()
    assert p.table.viewport().height() >= 3 * rows
    note = p.details.callout_label
    assert note.height() >= note.heightForWidth(note.width())
    assert p.scroll.verticalScrollBar().maximum() > 0


def test_the_queue_table_takes_the_room_a_tall_window_has(qtbot, tmp_path, pause_home):
    qfile = _qfile(tmp_path)
    _parked(qfile, ("Preferred team", {}))
    _ask()
    p = _dpanel(qtbot, qfile, alert=lambda w: None)
    host = QtWidgets.QWidget()
    qtbot.addWidget(host)
    QtWidgets.QVBoxLayout(host).addWidget(p)
    host.setFixedSize(1600, 1600)
    host.show()
    QtWidgets.QApplication.processEvents()
    assert p.scroll.verticalScrollBar().maximum() == 0
    assert p.table.height() > p.table.sizeHint().height()


def test_the_default_flash_alerts_the_window(monkeypatch, qtbot):
    seen = []
    monkeypatch.setattr(QtWidgets.QApplication, "alert",
                        staticmethod(lambda w, ms=0: seen.append((w, ms))))
    w = QtWidgets.QWidget()
    qtbot.addWidget(w)
    aqp._default_alert(w)
    assert seen == [(w, 0)]


def _parked(qfile, *items):
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="Analyst",
                                              apply_url="https://x/1"), path=qfile)
    for question, kw in items:
        apply_queue.add_missing("1", question, path=qfile, **kw)
    apply_queue.finish("1", "needs_human", notes="required field without an answer",
                       path=qfile)


def test_answer_now_opens_add_answer_prefilled_then_offers_requeue(qtbot, tmp_path,
                                                                    monkeypatch):
    qfile = _qfile(tmp_path)
    _parked(qfile, ("Preferred team", {"help": "Pick one", "options": ["Data", "Platform"],
                                       "type": "choice"}))
    opened, asked = [], []
    p = _dpanel(qtbot, qfile, on_answer_now=lambda prefill: opened.append(prefill) or True)
    monkeypatch.setattr(p, "_confirm_requeue", lambda e: asked.append(e["company"]) or True)
    p.table.selectRow(0)
    p.details.answer_now_btn.click()
    assert opened == [{"question": "Preferred team", "help": "Pick one", "type": "choice",
                       "options": ["Data", "Platform"]}]
    assert asked == ["Acme"]
    assert apply_queue.load(qfile)["jobs"][0]["status"] == "queued"


def test_answer_now_offers_no_requeue_when_the_answer_was_not_saved(qtbot, tmp_path,
                                                                     monkeypatch):
    qfile = _qfile(tmp_path)
    _parked(qfile, ("Preferred team", {}))
    p = _dpanel(qtbot, qfile, on_answer_now=lambda prefill: False)
    monkeypatch.setattr(p, "_confirm_requeue", lambda e: pytest.fail("nothing was saved"))
    p.table.selectRow(0)
    p.details.answer_now_btn.click()
    assert apply_queue.load(qfile)["jobs"][0]["status"] == "needs_human"


_CHECK_SENT = "check whether the application went through"


@pytest.mark.parametrize("status, notes, tab_note, offered", [
    ("needs_human", "required field without an answer: Preferred team", "", True),
    ("failed", "TimeoutError at fill", "", True),
    # a job that may have been sent, or was, is never re-queued here
    ("needs_human", f"{_CHECK_SENT}: the page moved on during the pause (it shows 'thank "
                    "you for applying')", f"{_CHECK_SENT}, then Mark applied or Re-queue", False),
    ("needs_human", "the submit click found no confirmation",
     f"{_CHECK_SENT}, then Mark applied or Re-queue", False),
    ("submitted", "confirmation page", "", False),
    ("ready_to_submit", "auto_apply_submit is off", "review and submit", False),
])
def test_answer_now_offers_requeue_only_for_a_job_nothing_was_sent_for(
        qtbot, tmp_path, monkeypatch, status, notes, tab_note, offered):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="Analyst",
                                              apply_url="https://x/1"), path=qfile)
    apply_queue.add_missing("1", "Preferred team", path=qfile)
    apply_queue.finish("1", status, notes=notes, tab_note=tab_note, path=qfile)
    asked = []
    p = _dpanel(qtbot, qfile, on_answer_now=lambda prefill: True)
    monkeypatch.setattr(p, "_confirm_requeue", lambda e: asked.append(e["company"]) or True)
    p.table.selectRow(0)
    p._answer_with({"question": "Preferred team"})
    assert asked == (["Acme"] if offered else []), (status, notes)
    assert apply_queue.load(qfile)["jobs"][0]["status"] == ("queued" if offered else status)


def test_possibly_sent_reads_the_runs_check_whether_words():
    import apply_run
    assert apply_queue.CHECK_SENT_WORDS == apply_run.CHECK_SENT_REASON
    assert apply_run.CHECK_SENT_NOTE.startswith(apply_queue.CHECK_SENT_WORDS)
    assert apply_queue.possibly_sent({"status": "needs_human",
                                      "notes": f"{_CHECK_SENT}: the run stopped"})
    assert apply_queue.possibly_sent({"status": "needs_human",
                                      "tab_note": apply_run.CHECK_SENT_NOTE})
    assert apply_queue.possibly_sent({"status": "submitted"})
    assert not apply_queue.possibly_sent({"status": "needs_human", "notes": "no submit button"})


def test_answer_now_with_several_questions_offers_each(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    _parked(qfile, ("Preferred team", {}), ("Number of talks", {"type": "number"}))
    opened = []
    p = _dpanel(qtbot, qfile, on_answer_now=lambda prefill: opened.append(prefill))
    p.table.selectRow(0)
    menu = p._answer_menu()
    assert [a.text() for a in menu.actions()] == ["Preferred team", "Number of talks"]
    menu.actions()[1].trigger()
    assert opened == [{"question": "Number of talks", "help": "", "type": "number",
                       "options": []}]


def test_answer_now_in_the_window_saves_the_prefilled_answer(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(w.answers_tab, "answer_now",
                        lambda prefill=None: calls.append(("answer_now", prefill)) or True)
    monkeypatch.setattr(w.answers_tab, "save", lambda: calls.append(("save",)) or True)
    prefill = {"question": "Preferred team", "help": "", "type": "choice",
               "options": ["Data", "Platform"]}
    assert w._answer_now(prefill) is True
    # the new answer is written on its own: the tab's Save (every pending edit) never runs
    assert calls == [("answer_now", prefill)]
    assert w.tabs.tabText(w.tabs.currentIndex()) == "Apply Answers"
    # a cancelled dialog saves nothing
    calls.clear()
    monkeypatch.setattr(w.answers_tab, "answer_now", lambda prefill=None: False)
    assert w._answer_now(prefill) is False and calls == []
    assert w._answer_now(None) is False


# --- a multi-row selection, the parallel check, Remove on several -----------------------


def _multi(p, *jids):
    """Select the rows of `jids` together, as a Ctrl-click would."""
    sm = p.table.selectionModel()
    sm.clearSelection()
    flags = (QtCore.QItemSelectionModel.SelectionFlag.Select
             | QtCore.QItemSelectionModel.SelectionFlag.Rows)
    for jid in jids:
        sm.select(p.table.model().index(_row_of(p, jid), 0), flags)


def _three(tmp_path):
    qfile = _qfile(tmp_path)
    for jid in ("1", "2", "3"):
        _checked(qfile, jid, difficulty=None)
    return qfile


def _single_job_buttons(p):
    return (p.requeue_btn, p.mark_applied_btn, p.dont_apply_btn,
            p.open_folder_btn, p.open_record_btn, p.details.answer_now_btn)


def test_the_queue_table_takes_a_multi_row_selection(qtbot, tmp_path):
    p = _dpanel(qtbot, _three(tmp_path))
    assert p.table.selectionMode() == \
        QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
    _multi(p, "3", "1")
    assert p._selected_job_ids() == [p.table.item(r, 0).data(QtCore.Qt.ItemDataRole.UserRole)
                                     for r in sorted((_row_of(p, "1"), _row_of(p, "3")))]
    assert p._selected_job_id() == ""          # a single-job action has no one job
    assert p._selected_entry() is None
    _multi(p, "2")
    assert p._selected_job_ids() == ["2"] and p._selected_job_id() == "2"


def test_the_check_on_three_selected_jobs_passes_all_three_with_the_note(qtbot, tmp_path,
                                                                         monkeypatch):
    calls, asked = [], []
    qfile = _three(tmp_path)
    _checked(qfile, "4", difficulty=None)      # queued, not selected: the ids are named
    p = _dpanel(qtbot, qfile, on_check_difficulty=calls.append,
                check_parallel=lambda: 2)
    monkeypatch.setattr(p, "_confirm_check", lambda n: asked.append(n) or True)
    _multi(p, "1", "2", "3")
    p.check_difficulty_btn.click()
    assert len(calls) == 1 and sorted(calls[0]) == ["1", "2", "3"]
    assert calls[0] == p._selected_job_ids()   # table order
    assert asked == []                         # a chosen selection needs no confirm
    assert p.status_label.text() == \
        "Checking 3 selected jobs, up to 2 at once, in a new terminal."


def test_the_checks_at_once_in_the_note_never_exceed_the_selection(qtbot, tmp_path):
    calls = []
    p = _dpanel(qtbot, _three(tmp_path), on_check_difficulty=calls.append,
                check_parallel=lambda: 10)
    _multi(p, "1", "3")
    p.check_difficulty_btn.click()
    assert p.status_label.text() == \
        "Checking 2 selected jobs, up to 2 at once, in a new terminal."


def test_the_note_says_one_at_a_time_when_the_setting_is_1(qtbot, tmp_path):
    p = _dpanel(qtbot, _three(tmp_path), on_check_difficulty=lambda ids: None,
                check_parallel=lambda: 1)
    _multi(p, "1", "2", "3")
    p.check_difficulty_btn.click()
    assert p.status_label.text() == \
        "Checking 3 selected jobs, one at a time, in a new terminal."


def test_the_single_row_check_is_unchanged(qtbot, tmp_path):
    calls = []
    p = _dpanel(qtbot, _three(tmp_path), on_check_difficulty=calls.append,
                check_parallel=lambda: 10)
    p.table.selectRow(_row_of(p, "2"))
    p.check_difficulty_btn.click()
    assert calls == [["2"]]
    assert p.status_label.text() == "Checking the selected job's difficulty in a new terminal."


def test_single_job_buttons_are_off_with_select_one_job_on_a_multi_selection(qtbot, tmp_path):
    p = _dpanel(qtbot, _three(tmp_path))
    p.table.selectRow(_row_of(p, "1"))
    tips = [b.toolTip() for b in _single_job_buttons(p)]
    assert all(b.isEnabled() for b in _single_job_buttons(p))
    assert "Select one job" not in tips

    _multi(p, "1", "2")
    for b in _single_job_buttons(p):
        assert not b.isEnabled(), b.text()
        assert b.toolTip() == "Select one job", b.text()
    # the actions on every selected row stay on, and Clear finished is not a row action
    assert p.check_difficulty_btn.isEnabled()
    assert p.remove_btn.isEnabled() and p.clear_btn.isEnabled()

    _multi(p, "2")
    assert all(b.isEnabled() for b in _single_job_buttons(p))
    assert [b.toolTip() for b in _single_job_buttons(p)] == tips


def test_the_details_pane_shows_the_current_row_on_a_multi_selection(qtbot, tmp_path):
    p = _dpanel(qtbot, _three(tmp_path))
    p.table.selectRow(_row_of(p, "1"))
    flags = (QtCore.QItemSelectionModel.SelectionFlag.Select
             | QtCore.QItemSelectionModel.SelectionFlag.Rows)
    p.table.selectionModel().setCurrentIndex(p.table.model().index(_row_of(p, "3"), 0), flags)
    assert len(p._selected_job_ids()) == 2
    assert "https://x/3" in p.details.toPlainText()
    assert not p.open_folder_btn.isEnabled()


def test_a_refresh_keeps_every_selected_job_selected(qtbot, tmp_path):
    qfile = _three(tmp_path)
    p = _dpanel(qtbot, qfile)
    _multi(p, "1", "3")
    p.refresh()
    assert sorted(p._selected_job_ids()) == ["1", "3"]
    # a write elsewhere that moves the rows keeps the same jobs selected
    apply_queue.remove("2", path=qfile)
    apply_queue.enqueue(apply_queue.new_entry("0", company="Zeta", title="T"), path=qfile)
    p.refresh()
    assert sorted(p._selected_job_ids()) == ["1", "3"]
    assert not p.requeue_btn.isEnabled()       # still a multi-selection after the refresh


def test_remove_on_several_rows_asks_then_removes_them_all_in_one_write(qtbot, tmp_path,
                                                                        monkeypatch):
    qfile = _three(tmp_path)
    writes, asked = [], []

    def submit(fn, on_done=None, on_error=None):
        writes.append(fn)
        aqp._run_inline(fn, on_done, on_error)

    p = _dpanel(qtbot, qfile, submit_write=submit)
    monkeypatch.setattr(p, "_confirm_remove", lambda n: asked.append(n) or True)
    monkeypatch.setattr(aqp.apply_queue, "remove",
                        lambda *a, **k: pytest.fail("one write, not one per job"))
    _multi(p, "1", "3")
    p.remove_btn.click()
    assert asked == [2] and len(writes) == 1
    assert [e["job_posting_id"] for e in apply_queue.load(qfile)["jobs"]] == ["2"]
    assert p.table.rowCount() == 1
    assert "Removed 2" in p.status_label.text()


def test_a_cancelled_remove_removes_nothing(qtbot, tmp_path, monkeypatch):
    qfile = _three(tmp_path)
    writes = []
    p = _dpanel(qtbot, qfile, submit_write=lambda fn, **k: writes.append(fn))
    monkeypatch.setattr(p, "_confirm_remove", lambda n: False)
    _multi(p, "1", "2", "3")
    p.remove_btn.click()
    assert writes == []
    assert len(apply_queue.load(qfile)["jobs"]) == 3


def test_remove_on_one_row_does_not_ask(qtbot, tmp_path, monkeypatch):
    qfile = _three(tmp_path)
    p = _dpanel(qtbot, qfile)
    monkeypatch.setattr(p, "_confirm_remove",
                        lambda n: pytest.fail("one row is removed as before"))
    p.table.selectRow(_row_of(p, "2"))
    p.remove_btn.click()
    assert sorted(e["job_posting_id"] for e in apply_queue.load(qfile)["jobs"]) == ["1", "3"]


def test_the_check_tip_names_the_selection_and_the_parallel_windows(qtbot, tmp_path):
    p = _dpanel(qtbot, _qfile(tmp_path))
    tip = p._check_tip
    assert "selected jobs" in tip and "with none selected, every queued job" in tip
    assert "own browser window" in tip and "Difficulty checks at once" in tip
    assert "types nothing, signs in nowhere and submits nothing" in tip
    assert "About 2 to 4 Jev requests per job." in tip
    assert "—" not in tip


@pytest.mark.parametrize("stored, expected", [(4, 4), (25, 10), (0, 1), ("lots", 10),
                                              (None, 10)])
def test_the_default_checks_at_once_reads_the_setting_clamped(monkeypatch, stored, expected):
    import settings
    monkeypatch.setattr(settings, "load", lambda *a, **k: (
        {} if stored is None else {"auto_apply_check_parallel": stored}))
    assert aqp._default_check_parallel() == expected


def test_the_default_checks_at_once_survives_a_broken_settings_backend(monkeypatch):
    import settings

    def broken(*a, **k):
        raise OSError("config.json unreadable")

    monkeypatch.setattr(settings, "load", broken)
    assert aqp._default_check_parallel() == 10


# --- a large selection, a spawn that fails, an id outside the safe set ----------------------


def test_a_selection_of_every_queued_job_runs_as_all(qtbot, tmp_path):
    calls = []
    p = _dpanel(qtbot, _three(tmp_path), on_check_difficulty=calls.append,
                check_parallel=lambda: 2)
    _multi(p, "1", "2", "3")
    p.check_difficulty_btn.click()
    assert calls == [[]]                       # `--all`: the command line stays short
    assert p.status_label.text() == \
        "Checking 3 selected jobs, up to 2 at once, in a new terminal."


def test_a_selection_of_some_queued_jobs_names_them(qtbot, tmp_path):
    calls = []
    p = _dpanel(qtbot, _three(tmp_path), on_check_difficulty=calls.append,
                check_parallel=lambda: 2)
    _multi(p, "1", "3")
    p.check_difficulty_btn.click()
    assert calls == [p._selected_job_ids()] and sorted(calls[0]) == ["1", "3"]


def test_a_selection_with_a_parked_job_names_every_one(qtbot, tmp_path):
    qfile = _three(tmp_path)
    _checked(qfile, "4", difficulty=None)
    apply_queue.finish("4", "needs_human", tab_note="review tab", path=qfile)
    calls = []
    p = _dpanel(qtbot, qfile, on_check_difficulty=calls.append, check_parallel=lambda: 2)
    _multi(p, "1", "2", "3", "4")
    p.check_difficulty_btn.click()
    assert len(calls) == 1 and sorted(calls[0]) == ["1", "2", "3", "4"]


@pytest.mark.parametrize("pick", [("1", "3"), ("2",)])
def test_a_check_that_will_not_start_says_so(qtbot, tmp_path, pick):
    def too_long(ids):
        raise OSError(206, "The filename or extension is too long")
    p = _dpanel(qtbot, _three(tmp_path), on_check_difficulty=too_long,
                check_parallel=lambda: 2)
    _multi(p, *pick)
    p.check_difficulty_btn.click()             # no exception escapes the slot
    note = p.status_label.text()
    assert note.startswith("The difficulty check did not start (OSError).")
    assert ("Select fewer jobs" in note) == (len(pick) > 1)


def test_a_check_of_every_queued_job_that_will_not_start_says_so(qtbot, tmp_path,
                                                                 monkeypatch):
    def broken(ids):
        raise OSError("powershell missing")
    p = _dpanel(qtbot, _three(tmp_path), on_check_difficulty=broken)
    monkeypatch.setattr(p, "_confirm_check", lambda n: True)
    p.table.clearSelection()
    p.check_difficulty_btn.click()
    assert p.status_label.text().startswith("The difficulty check did not start (OSError).")


# --- a console that will not start says so on the panel ----------------------------------

def _no_console():
    raise FileNotFoundError(2, "The system cannot find the file specified",
                            r"C:\Users\someone\secret\powershell.exe")


def test_sign_in_that_cannot_start_a_console_says_so(qtbot, tmp_path):
    p = _panel(qtbot, _qfile(tmp_path), on_login=_no_console)
    p.login_btn.click()
    note = p.status_label.text()
    assert note.startswith("The sign-in did not start:")
    assert "someone" not in note


def test_start_run_that_cannot_start_a_console_says_so(qtbot, tmp_path, monkeypatch):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", company="Acme", title="A"), path=qfile)
    p = _panel(qtbot, qfile, on_start_run=_no_console, password_exists=lambda: True)
    monkeypatch.setattr(p, "_confirm_run", lambda n: True)
    p.start_run_btn.click()
    note = p.status_label.text()
    assert note.startswith("The auto-apply run did not start:")
    assert "someone" not in note


def test_the_save_box_tooltip_shows_the_page_question_as_text(qtbot, pause_home):
    """A form's label is the page's words: markup in it shows as written and is
    never rendered in the tooltip (an <img> there would load)."""
    _ask([_q(1, "<b>Bold</b> question <img src='x.png'>", apply_pause.W_TEXT)])
    card = _card(qtbot)
    tip = _row_for(card, 1)["save"].toolTip()
    doc = QtGui.QTextDocument()
    doc.setHtml(tip)
    assert "<b>Bold</b> question <img src='x.png'>" in doc.toPlainText()


@pytest.mark.parametrize("bad", ["it's", "x\u2019; Write-Output INJECTED; \u2019",
                                 "1 2", "1;calc", "$(calc)", "", "1\n2", "\uff11"])
def test_the_check_command_refuses_an_id_outside_the_safe_set(bad):
    with pytest.raises(ValueError):
        aqp._assess_command(Path("C:/repo"), ["1", bad])


@pytest.mark.parametrize("quote", ["'", "\u2018", "\u2019", "\u201a", "\u201b"])
def test_every_powershell_single_quote_in_the_root_is_doubled(quote):
    """PowerShell 5.1 ends a single-quoted string at a curly quote too."""
    root = Path(f"C:/Users/o{quote}brien/repo")
    for line in (aqp._console_command(root, "drain"), aqp._assess_command(root, ["7"])):
        literal = line.split("-LiteralPath ", 1)[1].split("; python", 1)[0]
        assert literal == "'" + str(root).replace(quote, quote * 2) + "'"


def test_a_check_refused_for_a_bad_id_says_so(qtbot, tmp_path):
    def refuse(ids):
        raise ValueError("job id outside the safe set")
    p = _dpanel(qtbot, _three(tmp_path), on_check_difficulty=refuse)
    _multi(p, "2")
    p.check_difficulty_btn.click()             # no exception escapes the slot
    assert p.status_label.text().startswith("The difficulty check did not start")
