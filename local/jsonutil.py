"""Small JSON helpers shared by the dashboard (local/qt) and the watcher.

Both write local/config.json from separate processes; a naked write_text can
leave a half-written file or have one writer clobber the other mid write. An
atomic write (temp file in the same directory, then os.replace) makes each
write all-or-nothing so a concurrent reader never sees a partial file.

Atomicity is not enough on its own: it stops a TORN file, not a LOST UPDATE.
`update_json_locked` adds the missing half — the read -> merge -> write cycle
runs inside an exclusive sidecar lock, so the settings save, the background
delete queue and the watcher's gdrive_root probe cannot overwrite each other's
keys. Everything that read-modify-writes a shared JSON file goes through it.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from locks import file_lock  # noqa: E402  (needs HERE on sys.path)

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


def read_json_dict(path: Path) -> dict:
    """`path` parsed as a JSON object, {} when missing, unreadable or not an
    object. The lock-free read half of the update cycle: os.replace means a
    reader sees either the previous or the next complete file, never a mix."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def update_json_locked(path: Path, updates: dict, *,
                       timeout: float | None = None) -> dict:
    """Merge `updates` into the JSON object at `path` under an exclusive lock.

    The whole read -> merge -> atomic-write cycle is serialized on the sidecar
    `<path>.lock`, which is what makes concurrent writers safe: without it two
    writers each persist the snapshot they read and the second one silently
    reverts the first. In this app that showed up as a deleted job reappearing
    (the dashboard's delete runs on a background queue) or a page of Settings
    reverting (that save runs on the UI thread), both with no error.

    Returns the merged dict that was written. Raises locks.FileLockTimeout if
    the lock cannot be taken, and OSError if the write itself fails; callers
    that must never crash the UI catch both.
    """
    path = Path(path)
    with file_lock(path, timeout=timeout):
        merged = read_json_dict(path)
        merged.update(updates)
        atomic_write_json(path, merged)
    return merged
