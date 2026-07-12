"""Run a blocking callable off the UI thread, Qt-style.

Replaces the old Tk `threading.Thread` + `root.after(0, ...)` marshaling: a
`Worker` runs `fn()` on a `QThread` and emits `finished(result)` / `failed(exc)`,
which `run_async` marshals back onto the OWNER's thread before invoking the
callbacks. The marshaling needs a real QObject receiver (`_Relay`): PySide6
invokes a plain-callable slot directly in the EMITTING thread, so connecting
`worker.finished` straight to the Python closure ran every callback on the
worker thread — model resets, dialogs and registry writes executed off the UI
thread, which corrupted Qt's heap and produced the recurring Qt6Core.dll
crashes (access violation in QCoreApplication::notifyInternal2 / 0xc0000409 in
QtPrivate::sizedFree) during batch tailor runs. A QObject slot, by contrast,
gets an auto-QUEUED connection to the receiver's thread — the behavior the
whole dashboard was written against.

Tests monkeypatch `run_async` with a synchronous stand-in, so handlers stay
testable without real threads; tests/test_qt_workers_affinity.py exercises the
real threaded path and pins the callbacks-on-owner-thread contract.
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


class _Relay(QtCore.QObject):
    """UI-thread landing pad for a worker's completion signals.

    Created (parentless) on the caller's thread, so `worker.finished ->
    self._done` is an auto-QUEUED connection and the Python callbacks run on
    that thread. Connecting the worker signals to the callbacks directly would
    run them on the WORKER thread — PySide6 calls non-QObject slots in the
    emitting thread (see module docstring for the crash history that caused).
    Deliberately NOT parented to the owner: a GC'd owner would take the relay's
    C++ half with it and the cleanup slot would never run; `_LIVE` keeps the
    relay alive exactly until the thread's `finished` is delivered.
    """

    def __init__(self,
                 on_done: Callable[[object], None] | None,
                 on_error: Callable[[BaseException], None] | None,
                 cleanup: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._on_done = on_done
        self._on_error = on_error
        self._cleanup_fn = cleanup

    @QtCore.Slot(object)
    def _done(self, result: object) -> None:
        if self._on_done is not None:
            self._on_done(result)

    @QtCore.Slot(object)
    def _error(self, exc: BaseException) -> None:
        if self._on_error is not None:
            self._on_error(exc)

    @QtCore.Slot()
    def _thread_finished(self) -> None:
        if self._cleanup_fn is not None:
            self._cleanup_fn()
        self.deleteLater()


# Every in-flight (thread, worker, relay) trio, alive until the thread's
# `finished` has been delivered on the owner's thread. Without this, the only
# Python references live in `owner._bg_threads` — and when the owner is
# garbage-collected right after a task completes (a test's local owner, a
# closing dialog), shiboken deletes the C++ QThread while its OS thread is
# still winding down: qFatal("QThread: Destroyed while thread is still
# running") aborts the process. Object lifetime must not depend on the
# owner's GC timing.
_LIVE: set = set()


def run_async(owner: QtCore.QObject, fn: Callable[[], object],
              on_done: Callable[[object], None] | None = None,
              on_error: Callable[[BaseException], None] | None = None):
    """Run `fn()` on a worker thread; call `on_done`/`on_error` on `owner`'s thread.

    `owner` keeps a reference to the live threads (in `owner._bg_threads`) so
    callers can see what's in flight; `_LIVE` guarantees the Qt objects survive
    until the thread has actually finished.
    """
    thread = QtCore.QThread()
    worker = Worker(fn)
    worker.moveToThread(thread)

    bag = getattr(owner, "_bg_threads", None)
    if bag is None:
        bag = []
        owner._bg_threads = bag

    def _cleanup() -> None:
        if (thread, worker) in bag:
            bag.remove((thread, worker))
        _LIVE.discard(trio)

    relay = _Relay(on_done, on_error, cleanup=_cleanup)
    trio = (thread, worker, relay)
    thread.started.connect(worker.run)
    worker.finished.connect(relay._done)
    worker.failed.connect(relay._error)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    # Bound-QObject slot => queued onto the relay's (owner's) thread; the plain
    # lambda this replaces ran on the dying worker thread itself.
    thread.finished.connect(relay._thread_finished)

    bag.append((thread, worker))
    _LIVE.add(trio)
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
