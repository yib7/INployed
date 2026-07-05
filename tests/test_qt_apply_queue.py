"""SP3 (cycle 33): the dashboard's auto-apply queueing UI.

Covers the three pieces this phase adds on top of SP2's queue backend:

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
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from PySide6 import QtCore, QtWidgets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_queue  # noqa: E402
from qt import apply_queue_panel as aqp  # noqa: E402
from qt import main_window as mw  # noqa: E402
from qt.apply_queue_panel import (  # noqa: E402
    KICKOFF_COMMAND,
    KICKOFF_PROMPT,
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
                 output.cover_txt_filename(), "apply.md"):
        (folder / name).write_text("x", encoding="utf-8")
    return folder


def test_main_window_has_queue_button_tab_and_wiring(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path)
    bar = w._action_bar
    texts = [bar.itemAt(i).widget().text() for i in range(bar.count())
             if isinstance(bar.itemAt(i).widget(), QtWidgets.QPushButton)]
    assert "Queue auto-apply" in texts
    # the button sits beside Apply (immediately before it)
    assert texts.index("Queue auto-apply") == texts.index("Apply") - 1
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
    assert arts["cover_letter_txt"] == str(folder / output.cover_txt_filename())
    assert arts["apply_md"] == str(folder / "apply.md")


def test_queue_tracker_only_ready_job_falls_back_for_entry_data(qtbot, monkeypatch, tmp_path):
    """A tracker-only id (in registry.status_rows(), absent from self.df) must
    enqueue with a REAL apply_url/company/title via the master-CSV fallback —
    not an empty entry the SP4 agent can't navigate (burns an attempt)."""
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

    with pytest.raises(RuntimeError, match="thread spawn failed"):
        w._queue_for_auto_apply(["1"])

    assert w._tailoring is False                 # guard cleared -> not dead-locked
    assert w.btn_tailor.isEnabled()
    e = apply_queue.load(_qfile(tmp_path))["jobs"][0]
    assert e["status"] == "failed"               # not orphaned as "tailoring"
    assert "tailor launch failed" in e["notes"]
    assert w._queue_tailor_pending == []


def test_plain_tailor_launch_failure_resets_guard(qtbot, monkeypatch, tmp_path):
    """The no-queue path through _start_tailor gets the same hardening: a
    launch failure re-raises but leaves the Tailor button usable."""
    w = _win(qtbot, monkeypatch, tmp_path, df=_jobs_df())
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)

    def boom(*a, **k):
        raise RuntimeError("thread spawn failed")

    monkeypatch.setattr(mw.workers, "run_async", boom)

    job = {"job_posting_id": "1", "job_title": "T", "company_name": "C"}
    with pytest.raises(RuntimeError, match="thread spawn failed"):
        w._start_tailor([job], {})

    assert w._tailoring is False
    assert w.btn_tailor.isEnabled()
    assert apply_queue.load(_qfile(tmp_path))["jobs"] == []   # nothing enqueued


# --- MainWindow: _set_ats_password ------------------------------------------------


def _feed_password_dialogs(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: next(it)))


def test_set_ats_password_happy_path(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path)
    stored = []
    monkeypatch.setattr(mw.ats_accounts, "set_master_password",
                        lambda pw: stored.append(pw) or True)
    _feed_password_dialogs(monkeypatch, [("fake-pw", True), ("fake-pw", True)])
    w._set_ats_password()
    assert stored == ["fake-pw"]      # the dialog string goes straight through


def test_set_ats_password_mismatch_blank_and_cancel_abort(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot, monkeypatch, tmp_path)
    stored = []
    monkeypatch.setattr(mw.ats_accounts, "set_master_password",
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


def test_kickoff_button_puts_exact_command_on_clipboard(qtbot, tmp_path):
    p = _panel(qtbot, _qfile(tmp_path))
    p.kickoff_btn.click()
    assert QtWidgets.QApplication.clipboard().text() == KICKOFF_COMMAND
    # SP4 mirrors the prompt constant; the command is PowerShell-shaped
    assert KICKOFF_PROMPT == "Use the auto-apply skill: drain the apply queue"
    assert KICKOFF_COMMAND.startswith("cd ")
    assert f'claude "{KICKOFF_PROMPT}"' in KICKOFF_COMMAND
    assert ";" in KICKOFF_COMMAND             # PowerShell chain, not && (5.1-safe)


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
    inbox = by_key["auto_apply_inbox_url"]
    assert inbox.type == "str" and inbox.default == "https://mail.google.com"
    assert inbox.section == "Auto-apply" and inbox.target == "config"
    # defaults surface through load() even with no backing file on disk
    values = settings.load(targets={})
    assert values["auto_apply_batch_cap"] == 10
    assert values["auto_apply_inbox_url"] == "https://mail.google.com"
    # the 1-25 range is enforced
    assert "auto_apply_batch_cap" in settings.validate({"auto_apply_batch_cap": 26})
    assert "auto_apply_batch_cap" in settings.validate({"auto_apply_batch_cap": 0})
    assert settings.validate({"auto_apply_batch_cap": 10}) == {}
