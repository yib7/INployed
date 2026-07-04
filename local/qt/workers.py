"""Run a blocking callable off the UI thread, Qt-style.

Replaces the old Tk `threading.Thread` + `root.after(0, ...)` marshaling: a
`Worker` runs `fn()` on a `QThread` and emits `finished(result)` / `failed(exc)`
back on the UI thread (Qt queues cross-thread signals). `run_async` wires it up,
keeps references alive until the thread ends, and returns the (thread, worker).

Tests monkeypatch `run_async` with a synchronous stand-in, so handlers stay
testable without real threads.
"""
from __future__ import annotations

import time
from collections import deque
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


class SerialTaskQueue:
    """A FIFO, single-flight wrapper over `run_async` for background file writes.

    The dashboard's mark-seen / delete actions rewrite the same source CSVs; running
    them on independent worker threads could interleave two read-modify-write cycles
    and lose one. Submitting them here guarantees at most ONE write task runs at a
    time and later submissions wait their turn — a mark-seen write can never race a
    delete rewrite.

    Callbacks run on the UI thread (same contract as `run_async`); after a task's
    callback returns, the next queued task chain-starts. `run_async` is resolved at
    call time from this module so tests that monkeypatch `workers.run_async` with a
    synchronous stand-in drive the queue inline, thread-free.
    """

    def __init__(self, owner: QtCore.QObject) -> None:
        self._owner = owner
        self._queue: deque = deque()   # of (fn, on_done, on_error)
        self._running = False
        self._thread: QtCore.QThread | None = None  # live QThread of the running task

    def submit(self, fn: Callable[[], object],
               on_done: Callable[[object], None] | None = None,
               on_error: Callable[[BaseException], None] | None = None) -> None:
        """Enqueue `fn`; start it immediately when the queue is idle."""
        self._queue.append((fn, on_done, on_error))
        if not self._running:
            self._start_next()

    def pending_count(self) -> int:
        """Tasks not yet finished (queued + the one in flight)."""
        return len(self._queue) + (1 if self._running else 0)

    def is_idle(self) -> bool:
        return not self._running and not self._queue

    def drain(self, timeout_ms: int = 30000) -> bool:
        """Synchronously wait for every queued task to finish (for closeEvent).

        Waits on the running task's QThread, then pumps the event queue so the
        worker's queued `finished` signal delivers the callback that chain-starts
        the next task. Returns False on timeout — or immediately when the running
        task has no real thread behind it (a synchronous/fake runner that already
        returned means no further progress is possible from here).
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        app = QtCore.QCoreApplication.instance()
        while not self.is_idle():
            if time.monotonic() > deadline:
                return False
            thread = self._thread
            if self._running and thread is None:
                return False   # nothing will ever complete it (see docstring)
            if thread is not None:
                try:
                    thread.wait(50)
                except RuntimeError:   # C++ side already deleted
                    pass
            if app is not None:
                app.processEvents()    # deliver finished/failed -> chain next task
        return True

    def _start_next(self) -> None:
        if not self._queue:
            return
        fn, on_done, on_error = self._queue.popleft()
        self._running = True

        def done(result: object) -> None:
            try:
                if on_done is not None:
                    on_done(result)
            finally:
                self._task_finished()

        def error(exc: BaseException) -> None:
            try:
                if on_error is not None:
                    on_error(exc)
            finally:
                self._task_finished()

        result = run_async(self._owner, fn, on_done=done, on_error=error)
        # A monkeypatched synchronous runner may have completed the task (and chained
        # the whole queue) inside that call — only record the thread while it's still
        # this task that is running.
        if self._running and isinstance(result, tuple) and result:
            self._thread = result[0]

    def _task_finished(self) -> None:
        self._running = False
        self._thread = None
        self._start_next()
