"""SP6: UI snappiness — delete / mark-seen / set-status must not freeze the UI.

The slow part of those actions is rewriting the ~27MB gzipped master CSV (plus
per-run files). That now happens on a background `SerialTaskQueue` (FIFO,
single-flight, so a mark-seen write can never race a delete rewrite), while the
UI updates optimistically from the in-memory `self.df` via `_apply_df_views()`
— zero disk I/O on the click path. Registry (SQLite) writes stay on the UI
thread: the connection is thread-affine and they're fast.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from PySide6 import QtCore, QtGui, QtWidgets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from qt import main_window as mw  # noqa: E402
from qt import workers  # noqa: E402
from qt.main_window import MainWindow  # noqa: E402


# ---- SerialTaskQueue ---------------------------------------------------------


def _sync_run_async(owner, fn, on_done=None, on_error=None):
    """Inline stand-in: run the task and its callback on the calling thread."""
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - mirror the real worker's catch
        if on_error is not None:
            on_error(exc)
        return None
    if on_done is not None:
        on_done(result)
    return None


class _HeldRunner:
    """A run_async stand-in that captures tasks so tests control completion."""

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


def test_queue_runs_submissions_in_fifo_order(monkeypatch):
    monkeypatch.setattr(workers, "run_async", _sync_run_async)
    q = workers.SerialTaskQueue(QtCore.QObject())
    ran = []
    q.submit(lambda: ran.append("a"))
    q.submit(lambda: ran.append("b"))
    q.submit(lambda: ran.append("c"))
    assert ran == ["a", "b", "c"]
    assert q.is_idle() and q.pending_count() == 0


def test_queue_is_single_flight_and_chains(monkeypatch):
    runner = _HeldRunner()
    monkeypatch.setattr(workers, "run_async", runner)
    q = workers.SerialTaskQueue(QtCore.QObject())
    ran = []
    q.submit(lambda: ran.append("a"))
    q.submit(lambda: ran.append("b"))     # queued while "a" is in flight
    assert len(runner.held) == 1          # single-flight: b is NOT started
    assert q.pending_count() == 2 and not q.is_idle()
    runner.complete_next()                # a finishes -> b chain-starts
    assert ran == ["a"]
    assert len(runner.held) == 1
    runner.complete_next()
    assert ran == ["a", "b"]
    assert q.is_idle()


def test_queue_invokes_callbacks_and_survives_task_errors(monkeypatch):
    monkeypatch.setattr(workers, "run_async", _sync_run_async)
    q = workers.SerialTaskQueue(QtCore.QObject())
    events = []

    def boom():
        raise RuntimeError("disk gone")

    q.submit(boom, on_error=lambda exc: events.append(("err", str(exc))))
    q.submit(lambda: 42, on_done=lambda r: events.append(("ok", r)))
    # the chain continued past the failing task and its error callback
    assert events == [("err", "disk gone"), ("ok", 42)]
    assert q.is_idle()


def test_queue_drain_flushes_real_threads(qtbot):
    # drain() is what closeEvent uses: it must block until queued writes land,
    # with the REAL run_async (QThread workers + queued signal callbacks).
    owner = QtCore.QObject()
    q = workers.SerialTaskQueue(owner)
    ran = []
    q.submit(lambda: (QtCore.QThread.msleep(30), ran.append("a"))[1])
    q.submit(lambda: ran.append("b"))
    assert q.drain(timeout_ms=10000) is True
    assert ran == ["a", "b"]
    assert q.is_idle() and q.pending_count() == 0


def test_queue_drain_bails_out_when_no_thread_backs_the_task(monkeypatch):
    # A held task with no real thread can never finish — drain must give up
    # (return False) instead of spinning until the timeout.
    runner = _HeldRunner()
    monkeypatch.setattr(workers, "run_async", runner)
    q = workers.SerialTaskQueue(QtCore.QObject())
    q.submit(lambda: None)
    assert q.drain(timeout_ms=200) is False
    assert not q.is_idle()


# ---- MainWindow: optimistic mark-seen / delete -------------------------------


def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    reg.all_ids.return_value = set()
    return reg


def _win(qtbot):
    w = MainWindow(csv_paths=[], registry=_fake_registry())
    qtbot.addWidget(w)
    return w


def _df():
    return pd.DataFrame([
        {"job_posting_id": "1", "job_title": "Eng", "company_name": "Acme",
         "score": "5", "is_seen": "no", "url": "http://x/1"},
        {"job_posting_id": "2", "job_title": "Analyst", "company_name": "Bcme",
         "score": "5", "is_seen": "no", "url": "http://x/2"},
    ])


def _seed(w, tmp_path):
    w.df = _df()
    w.id_to_path = {"1": tmp_path / "run.csv.gz", "2": tmp_path / "run.csv.gz"}
    w._apply_df_views()


def test_apply_df_views_refreshes_views_without_disk_io(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(mw, "load_files",
                        lambda *a, **k: pytest.fail("_apply_df_views must not read disk"))
    _seed(w, tmp_path)
    assert w._row_by_id == {"1": 0, "2": 1}
    assert w._url_by_id["2"] == "http://x/2"
    assert set(w.df_high["job_posting_id"]) == {"1", "2"}


def test_mark_ids_seen_is_optimistic_and_enqueues_one_write(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    _seed(w, tmp_path)
    runner = _HeldRunner()
    monkeypatch.setattr(workers, "run_async", runner)
    reloaded = []
    monkeypatch.setattr(w, "reload_data", lambda: reloaded.append(True))
    writes = []
    monkeypatch.setattr(w, "_write_is_seen",
                        lambda ids, value, paths=None: writes.append((list(ids), value)))

    w._mark_ids_seen(["1"])

    # UI path: registry + in-memory df + views updated instantly, no CSV write,
    # no blocking reload.
    w.registry.mark.assert_called_once_with(["1"])
    assert w.df.loc[w.df["job_posting_id"] == "1", "is_seen"].iloc[0] == "yes"
    assert set(w.df_high["job_posting_id"]) == {"2"}   # views refreshed in memory
    assert w._seen_undo == [["1"]]                      # undo stack preserved
    assert writes == [] and reloaded == []
    assert len(runner.held) == 1                        # exactly one queued write

    runner.complete_next()                              # background write runs
    assert writes == [(["1"], "yes")]
    assert reloaded == []


def test_undo_seen_mirrors_optimistically(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    _seed(w, tmp_path)
    runner = _HeldRunner()
    monkeypatch.setattr(workers, "run_async", runner)
    writes = []
    monkeypatch.setattr(w, "_write_is_seen",
                        lambda ids, value, paths=None: writes.append((list(ids), value)))
    w._mark_ids_seen(["1"])
    runner.complete_next()

    w._undo_seen()

    w.registry.unmark.assert_called_once_with(["1"])
    assert w.df.loc[w.df["job_posting_id"] == "1", "is_seen"].iloc[0] == "no"
    assert set(w.df_high["job_posting_id"]) == {"1", "2"}
    assert w._seen_undo == []
    runner.complete_next()
    assert writes == [(["1"], "yes"), (["1"], "no")]


def test_set_status_applied_routes_through_optimistic_path(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    _seed(w, tmp_path)
    runner = _HeldRunner()
    monkeypatch.setattr(workers, "run_async", runner)
    monkeypatch.setattr(w, "reload_data",
                        lambda: pytest.fail("'applied' must not trigger a blocking reload"))
    w._set_status_for(["1"], "applied")
    assert w.registry.set_status.called and w.registry.mark.called
    assert w.df.loc[w.df["job_posting_id"] == "1", "is_seen"].iloc[0] == "yes"
    assert len(runner.held) == 1
    runner.complete_next()   # leave the queue idle for teardown's closeEvent


def test_mark_applied_from_panel_does_not_block(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    _seed(w, tmp_path)
    runner = _HeldRunner()
    monkeypatch.setattr(workers, "run_async", runner)
    monkeypatch.setattr(w, "reload_data",
                        lambda: pytest.fail("panel 'I applied' must not block on a reload"))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    w._apply_panel_job = {"job_posting_id": "1", "title": "Eng",
                          "company": "Acme", "url": "http://x/1"}
    w._mark_applied_from_panel()
    assert w.registry.set_status.called and w.registry.mark.called
    assert len(runner.held) == 1
    runner.complete_next()   # leave the queue idle for teardown's closeEvent


def test_delete_jobs_drops_rows_before_background_delete_runs(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    _seed(w, tmp_path)
    runner = _HeldRunner()
    monkeypatch.setattr(workers, "run_async", runner)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    w.registry.resume_path.side_effect = lambda jid: f"C:/gen/{jid}"
    deleted = []
    monkeypatch.setattr(mw.jobsdata, "delete_jobs",
                        lambda ids, **k: deleted.append(list(ids)) or len(list(ids)))
    recycled = []
    monkeypatch.setattr(mw, "recycle_resume_folder",
                        lambda p: recycled.append(p) or True)

    w._delete_jobs(["1"])

    # optimistic: the row is gone from memory + views BEFORE any CSV rewrite
    assert set(w.df["job_posting_id"]) == {"2"}
    assert set(w.df_high["job_posting_id"]) == {"2"}
    assert "1" not in w.id_to_path
    assert deleted == [] and recycled == []
    assert "background" in w.statusBar().currentMessage().lower()
    # registry cleanup happened on the UI thread (SQLite is thread-affine)
    w.registry.clear_status.assert_any_call("1")
    w.registry.clear_resume_path.assert_any_call("1")

    runner.complete_next()   # the queued CSV rewrite + recycle
    assert deleted == [["1"]]
    assert recycled == ["C:/gen/1"]
    assert "Deleted 1 job(s)." in w.statusBar().currentMessage()


def test_delete_jobs_completion_reports_recycle_failures(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    _seed(w, tmp_path)
    monkeypatch.setattr(workers, "run_async", _sync_run_async)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    w.registry.resume_path.return_value = "C:/gen/x"
    monkeypatch.setattr(mw.jobsdata, "delete_jobs", lambda ids, **k: len(list(ids)))

    def boom(p):
        raise OSError("locked by Explorer")

    monkeypatch.setattr(mw, "recycle_resume_folder", boom)
    w._delete_jobs(["1"])
    msg = w.statusBar().currentMessage()
    assert "Deleted 1 job(s)." in msg and "Recycle Bin" in msg


# ---- self-write feedback suppression + failure resync -------------------------


def test_completed_write_suppresses_watcher_and_resnapshots_sig(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    _seed(w, tmp_path)
    runner = _HeldRunner()
    monkeypatch.setattr(workers, "run_async", runner)
    monkeypatch.setattr(w, "_write_is_seen", lambda ids, value, paths=None: None)
    w._mark_ids_seen(["1"])

    # a debounce scheduled by a REAL pre-write event is still pending; the
    # write-done callback must cancel it (fs events DURING the write are
    # ignored outright — see the mid-write suppression tests below)
    w._reload_timer.start()
    assert w._reload_timer.isActive()
    rearmed = []
    orig_rearm = w._rearm_watcher

    def rearm():
        rearmed.append(True)
        orig_rearm()

    monkeypatch.setattr(w, "_rearm_watcher", rearm)
    w._source_sig = ("stale-from-our-own-write",)

    runner.complete_next()   # write-done callback fires on the UI thread

    assert not w._reload_timer.isActive()      # pending self-triggered reload cancelled
    assert rearmed == [True]                   # watch paths re-added after atomic replace
    assert w._source_sig == w._current_sig()   # 15s poll stays quiet


def test_fs_events_are_ignored_while_a_write_is_in_flight(qtbot, tmp_path, monkeypatch):
    # A >1.5s gap between the write's own per-file replaces would let the debounce
    # fire MID-write and reload half-old data (e.g. resurrect just-deleted rows) —
    # and the write-done re-snapshot would then freeze that stale view. So while
    # the queue is busy, fs events must not even start the timer.
    w = _win(qtbot)
    _seed(w, tmp_path)
    runner = _HeldRunner()
    monkeypatch.setattr(workers, "run_async", runner)
    monkeypatch.setattr(w, "_write_is_seen", lambda ids, value, paths=None: None)
    w._mark_ids_seen(["1"])                    # queue now busy
    w._on_fs_change("whatever")
    assert not w._reload_timer.isActive()
    runner.complete_next()                     # idle again -> events flow normally
    w._on_fs_change("whatever")
    assert w._reload_timer.isActive()
    w._reload_timer.stop()


def test_poll_is_quiet_while_a_write_is_in_flight(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    _seed(w, tmp_path)
    runner = _HeldRunner()
    monkeypatch.setattr(workers, "run_async", runner)
    monkeypatch.setattr(w, "_write_is_seen", lambda ids, value, paths=None: None)
    monkeypatch.setattr(w, "reload_data_async",
                        lambda: pytest.fail("poll must not reload mid-write"))
    w._mark_ids_seen(["1"])                    # queue now busy
    w._source_sig = ("stale",)                 # poll would normally fire on this
    w._poll_for_changes()                      # ignored: our own write is in flight
    runner.complete_next()                     # leave the queue idle for teardown


def test_failed_background_write_warns_and_resyncs_from_disk(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    _seed(w, tmp_path)
    runner = _HeldRunner()
    monkeypatch.setattr(workers, "run_async", runner)
    monkeypatch.setattr(w, "_write_is_seen",
                        lambda ids, value, paths=None: (_ for _ in ()).throw(OSError("gz broke")))
    warned = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.setdefault("text", a[2])))
    reloaded = []
    monkeypatch.setattr(w, "reload_data", lambda: reloaded.append(True))

    w._mark_ids_seen(["1"])
    runner.complete_next()

    assert "gz broke" in warned["text"]
    assert reloaded == [True]                  # disk is truth after a failed write


def test_close_event_drains_pending_writes(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    _seed(w, tmp_path)
    drained = []
    monkeypatch.setattr(w._writes, "drain",
                        lambda timeout_ms=30000: drained.append(timeout_ms) or True)
    # make the queue look busy so closeEvent has something to flush
    monkeypatch.setattr(w._writes, "is_idle", lambda: False)
    w.close()
    assert drained                              # closeEvent waited for the queue


def test_close_event_warns_when_drain_fails(qtbot, tmp_path, monkeypatch):
    w = _win(qtbot)
    _seed(w, tmp_path)
    monkeypatch.setattr(w._writes, "is_idle", lambda: False)
    monkeypatch.setattr(w._writes, "drain", lambda timeout_ms=30000: False)
    monkeypatch.setattr(w._writes, "pending_count", lambda: 2)
    warned = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.setdefault("text", a[2])))
    w.close()
    assert "2" in warned.get("text", "")        # user told about the pending writes


# ---- closeEvent: don't strand an in-flight tailor run ------------------------


def _close_evt():
    return QtGui.QCloseEvent()


class _FakeThread:
    def __init__(self, running=True):
        self._running = running

    def isRunning(self):
        return self._running


def test_close_event_waits_for_tailor_when_user_confirms(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "_tailor_in_flight", lambda: True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    awaited = []
    monkeypatch.setattr(w, "_await_tailor",
                        lambda *a, **k: awaited.append(True) or True)
    monkeypatch.setattr(w._writes, "is_idle", lambda: True)   # skip the write drain
    evt = _close_evt()
    w.closeEvent(evt)
    assert awaited == [True]                     # waited for the tailor to save
    assert evt.isAccepted()                      # then closed


def test_close_event_skips_wait_when_user_declines(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "_tailor_in_flight", lambda: True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No))
    monkeypatch.setattr(w, "_await_tailor", lambda *a, **k: pytest.fail(
        "must not wait when the user declines"))
    monkeypatch.setattr(w._writes, "is_idle", lambda: True)
    evt = _close_evt()
    w.closeEvent(evt)
    assert evt.isAccepted()                      # closes now; recovery heals next launch


def test_close_event_cancel_keeps_window_open(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "_tailor_in_flight", lambda: True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Cancel))
    touched = []
    monkeypatch.setattr(w._writes, "is_idle",
                        lambda: touched.append(True) or True)
    evt = _close_evt()
    evt.accept()                                 # prove ignore() flips it back
    w.closeEvent(evt)
    assert not evt.isAccepted()                  # window stays open
    assert touched == []                         # bailed before the write drain


def test_close_event_warns_when_tailor_wait_times_out(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "_tailor_in_flight", lambda: True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(w, "_await_tailor", lambda *a, **k: False)   # timed out
    warned = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.setdefault("text", a[2])))
    monkeypatch.setattr(w._writes, "is_idle", lambda: True)
    evt = _close_evt()
    w.closeEvent(evt)
    assert "recovered" in warned.get("text", "").lower()
    assert evt.isAccepted()                      # still closes — recovery is the net


def test_close_event_no_tailor_prompt_when_idle(qtbot, monkeypatch):
    w = _win(qtbot)                              # fresh window: no tailor in flight
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: pytest.fail("no tailor prompt when nothing is tailoring")))
    monkeypatch.setattr(w._writes, "is_idle", lambda: True)
    evt = _close_evt()
    w.closeEvent(evt)
    assert evt.isAccepted()


# ---- closeEvent: don't silently orphan an in-flight scrape --------------------


def test_close_event_scrape_cancel_keeps_window_open(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "_scrape_in_flight", lambda: True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Cancel))
    evt = _close_evt()
    evt.accept()                                 # prove ignore() flips it back
    w.closeEvent(evt)
    assert not evt.isAccepted()                  # window stays open, scrape survives


def test_close_event_scrape_confirm_closes(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "_scrape_in_flight", lambda: True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(w._writes, "is_idle", lambda: True)
    evt = _close_evt()
    w.closeEvent(evt)
    assert evt.isAccepted()                      # user chose to close anyway


def test_scrape_in_flight_true_only_with_flag_and_live_thread(qtbot):
    w = _win(qtbot)
    assert w._scrape_in_flight() is False        # no flag
    w._scraping = True
    w._bg_threads = []
    assert w._scrape_in_flight() is False        # flag but no live thread
    w._bg_threads = [(_FakeThread(running=True), None)]
    assert w._scrape_in_flight() is True
    w._bg_threads = [(_FakeThread(running=False), None)]
    assert w._scrape_in_flight() is False


def test_tailor_in_flight_true_only_with_flag_and_live_thread(qtbot):
    w = _win(qtbot)
    assert w._tailor_in_flight() is False         # no flag, no threads
    w._tailoring = True
    w._bg_threads = []                            # flag set but nothing running
    assert w._tailor_in_flight() is False         # the console-wording-test case
    w._bg_threads = [(_FakeThread(running=False), object())]
    assert w._tailor_in_flight() is False         # thread already stopped
    w._bg_threads = [(_FakeThread(running=True), object())]
    assert w._tailor_in_flight() is True          # genuinely in flight
    w._tailoring = False


def test_await_tailor_returns_true_once_finalize_clears_flag(qtbot, monkeypatch):
    w = _win(qtbot)
    w._tailoring = True
    app = QtWidgets.QApplication.instance()
    calls = {"n": 0}

    def fake_pe(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:
            w._tailoring = False                 # the finalize signal 'delivered'

    monkeypatch.setattr(app, "processEvents", fake_pe)
    assert w._await_tailor(timeout_ms=5000) is True
    assert calls["n"] >= 2


def test_await_tailor_times_out_if_flag_never_clears(qtbot, monkeypatch):
    w = _win(qtbot)
    w._tailoring = True
    app = QtWidgets.QApplication.instance()
    monkeypatch.setattr(app, "processEvents", lambda *a, **k: None)   # never clears
    assert w._await_tailor(timeout_ms=50) is False
    w._tailoring = False
