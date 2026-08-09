"""OS-level file locks shared across the dashboard, the watcher and the CLIs.

Two shapes, same primitive (msvcrt.locking on Windows, fcntl.flock elsewhere):

- `SingleInstance` — the "am I the only one running" guard. The dashboard
  (local/app.py, via jobsdata) uses it to no-op a relaunch over a live window;
  the watcher (local/watcher.py) to skip a trigger while a previous fire is
  still working. Non-blocking: it reports the conflict rather than waiting.
- `file_lock(path)` — a blocking-with-timeout exclusive lock on a sidecar
  `<path>.lock`, for wrapping a read -> modify -> atomic-write cycle on a file
  two processes share. atomic_write_json makes a write all-or-nothing but does
  nothing about LOST UPDATES: two writers that both read before either writes
  each persist their own snapshot and the second one wins. apply_queue.json has
  had this since it shipped (apply_queue.locked() delegates here);
  local/config.json got it after the dashboard's Settings tab and its
  background delete queue were found racing each other.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

# Lock-acquisition tuning. Module-level so tests can shorten the wait instead of
# sitting out the real timeout.
LOCK_TIMEOUT = 5.0     # seconds of retrying before FileLockTimeout
LOCK_RETRY = 0.025     # seconds between attempts


class FileLockTimeout(TimeoutError):
    """Could not take a sidecar lock within the timeout."""


def _lock_byte0(fh) -> None:
    """One non-blocking exclusive-lock attempt on byte 0; OSError when held."""
    if os.name == "nt":
        import msvcrt
        fh.seek(0)  # msvcrt.locking is positional; always lock byte 0
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_byte0(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path, *, timeout: float | None = None):
    """Exclusive cross-process lock on `<path>.lock`, held for the block.

    Blocking with a deadline: retries every LOCK_RETRY seconds and raises
    FileLockTimeout after `timeout` (default LOCK_TIMEOUT). The lock file is a
    sidecar, never the data file itself, so a lock-free reader is never blocked
    and never sees a partial file (the writer still swaps via os.replace).

    Locks are not reentrant on POSIX and are per-handle on Windows; nest one
    lock inside another only in a fixed global order. Today the two callers
    (apply_queue.json, config.json) never nest.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.with_name(path.name + ".lock")
    limit = LOCK_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + limit
    fh = open(lock_file, "a+b")
    got = False
    try:
        while True:
            try:
                _lock_byte0(fh)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise FileLockTimeout(
                        f"could not lock {lock_file} within {limit:.1f}s "
                        "(another process is holding it)") from None
                time.sleep(LOCK_RETRY)
        yield
    finally:
        if got:
            try:
                _unlock_byte0(fh)
            except OSError:
                pass
        fh.close()


class SingleInstance:
    """Concurrent-instance guard backed by an OS-level advisory lock on one file.

    `acquire()` opens (creating if needed) the lock file and takes a
    non-blocking exclusive lock on it, returning False instead of blocking when
    another process already holds it. `release()` frees the lock and closes the
    handle; safe to call even if `acquire()` was never called or already failed.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self._fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)  # msvcrt.locking is byte-range; always lock byte 0
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
            self._fh = None
