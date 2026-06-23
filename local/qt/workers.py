"""Run a blocking callable off the UI thread, Qt-style.

Replaces the old Tk `threading.Thread` + `root.after(0, ...)` marshaling: a
`Worker` runs `fn()` on a `QThread` and emits `finished(result)` / `failed(exc)`
back on the UI thread (Qt queues cross-thread signals). `run_async` wires it up,
keeps references alive until the thread ends, and returns the (thread, worker).

Tests monkeypatch `run_async` with a synchronous stand-in, so handlers stay
testable without real threads.
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore


class Worker(QtCore.QObject):
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(object)   # the exception

    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self._fn = fn

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - report, never crash the UI thread
            self.failed.emit(exc)
        else:
            self.finished.emit(result)


def run_async(owner: QtCore.QObject, fn: Callable[[], object],
              on_done: Callable[[object], None] | None = None,
              on_error: Callable[[BaseException], None] | None = None):
    """Run `fn()` on a worker thread; call `on_done`/`on_error` on the UI thread.

    `owner` keeps a reference to the live threads (in `owner._bg_threads`) so they
    aren't garbage-collected mid-run.
    """
    thread = QtCore.QThread()
    worker = Worker(fn)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    if on_done is not None:
        worker.finished.connect(on_done)
    if on_error is not None:
        worker.failed.connect(on_error)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)

    bag = getattr(owner, "_bg_threads", None)
    if bag is None:
        bag = []
        owner._bg_threads = bag
    bag.append((thread, worker))
    thread.finished.connect(lambda: bag.remove((thread, worker)) if (thread, worker) in bag else None)

    thread.start()
    return thread, worker
