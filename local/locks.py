"""Single-instance file lock shared by the dashboard (local/app.py, via jobsdata)
and the watcher (local/watcher.py).

Both processes need the same "am I the only one running" guard: the dashboard
to no-op a relaunch over a live window, the watcher to skip a trigger while a
previous fire is still working. Uses msvcrt.locking on Windows; fcntl elsewhere.
"""
from __future__ import annotations

import os
from pathlib import Path


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
