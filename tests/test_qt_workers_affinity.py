"""run_async / SerialTaskQueue callbacks MUST run on the owner's (UI) thread.

PySide6 invokes a slot that is a plain Python callable in the EMITTING thread —
not the thread the connection was created in. Connecting `worker.finished`
straight to the `on_done` closure therefore ran every callback on the worker
thread, so all the heavy UI work those callbacks do (model resets, QMessageBox,
QFileSystemWatcher re-arms, SQLite registry writes) executed off the UI thread.
That is undefined behavior in Qt and was corrupting the heap: the dashboard's
recurring Qt6Core.dll crash pair — access violations inside
QCoreApplication::notifyInternal2 and 0xc0000409 fastfails inside
QtPrivate::sizedFree — hit during batch tailor runs (5 crashes, 07-03..07-08).

These tests exercise the REAL threaded path (no synchronous monkeypatch, unlike
the rest of the suite) and pin the marshaling contract.
"""
import threading
from pathlib import Path
import sys

from PySide6 import QtCore

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from qt import workers  # noqa: E402


def test_run_async_on_done_runs_on_ui_thread(qapp, qtbot):
    owner = QtCore.QObject()
    rec = {}

    def work():
        rec["worker_ident"] = threading.get_ident()
        return "payload"

    def on_done(result):
        rec["result"] = result
        rec["done_ident"] = threading.get_ident()
        rec["done_qt_main"] = QtCore.QThread.currentThread() is qapp.thread()

    workers.run_async(owner, work, on_done=on_done)
    qtbot.waitUntil(lambda: "done_ident" in rec, timeout=10000)

    assert rec["result"] == "payload"
    assert rec["worker_ident"] != threading.get_ident()  # really ran off-thread
    assert rec["done_ident"] == threading.get_ident()
    assert rec["done_qt_main"] is True


def test_run_async_on_error_runs_on_ui_thread(qapp, qtbot):
    owner = QtCore.QObject()
    rec = {}
    boom = RuntimeError("boom")

    def work():
        raise boom

    def on_error(exc):
        rec["exc"] = exc
        rec["error_ident"] = threading.get_ident()
        rec["error_qt_main"] = QtCore.QThread.currentThread() is qapp.thread()

    workers.run_async(owner, work, on_error=on_error)
    qtbot.waitUntil(lambda: "error_ident" in rec, timeout=10000)

    assert rec["exc"] is boom
    assert rec["error_ident"] == threading.get_ident()
    assert rec["error_qt_main"] is True


def test_serial_queue_callbacks_on_ui_thread_with_real_threads(qapp, qtbot):
    """The write queue chains through run_async; with real threads its
    callbacks must also land on the UI thread, in FIFO order."""
    q = workers.SerialTaskQueue(QtCore.QObject())
    idents = []
    order = []

    def make(tag):
        def done(_result):
            idents.append(threading.get_ident())
            order.append(tag)
        return done

    q.submit(lambda: "a", on_done=make("a"))
    q.submit(lambda: "b", on_done=make("b"))
    qtbot.waitUntil(q.is_idle, timeout=10000)

    assert order == ["a", "b"]
    assert set(idents) == {threading.get_ident()}
