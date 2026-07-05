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
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                break
            except OSError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(_REPLACE_RETRY)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
