"""Small JSON helpers shared by the dashboard (local/qt) and the watcher.

Both write local/config.json from separate processes; a naked write_text can
leave a half-written file or have one writer clobber the other mid write. An
atomic write (temp file in the same directory, then os.replace) makes each
write all-or-nothing so a concurrent reader never sees a partial file.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# os.replace retry tuning: on Windows, CPython's open() doesn't grant
# FILE_SHARE_DELETE, so a writer's MoveFileEx fails with PermissionError while
# a concurrent lock-free reader holds the destination open at that instant.
# The window is microseconds wide; a few short retries absorb it. Module-level
# so tests can monkeypatch the sleep away.
_REPLACE_TRIES = 5
_REPLACE_RETRY = 0.02     # seconds between attempts


def replace_with_retry(src, dst, *, retries: int = _REPLACE_TRIES) -> None:
    """`os.replace(src, dst)` with the bounded Windows-lock retry (see the module
    comment). Retries a transiently locked destination `retries` times, sleeping
    `_REPLACE_RETRY` between attempts, then re-raises the OSError. The single
    shared retrying-replace used by every atomic tmp+replace writer here
    (atomic_write_json, csv_io.write_csv_gz_atomic, the apply.md / outbox rows
    writers) so the fix lives in exactly one place."""
    # Clamp (audit C6-7): retries=0 made `range(0)` skip the loop entirely, so
    # the function returned having never called os.replace — the caller then
    # deleted its tmp file in `finally` and the write vanished with no error.
    # One attempt is the floor; "no retries" still means "try once".
    retries = max(1, int(retries))
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt == retries - 1:
                raise
            time.sleep(_REPLACE_RETRY)


def atomic_write_json(path: Path, data: Any) -> None:
    """Serialize `data` to `path` atomically via a same-dir temp file + replace.

    The temp name includes the PID so two processes writing at once don't
    collide on the temp file itself. os.replace is atomic on the same
    filesystem, which a same-directory temp guarantees. A transiently locked
    destination (a lock-free reader mid-read on Windows) is retried
    _REPLACE_TRIES times before the OSError is re-raised. If the replace never
    lands, the tmp file is unlinked rather than left stranded -- same
    try/finally pattern as csv_io.write_csv_gz_atomic / scraper._atomic_to_csv.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
