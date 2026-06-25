"""Small JSON helpers shared by the dashboard (local/qt) and the watcher.

Both write local/config.json from separate processes; a naked write_text can
leave a half-written file or have one writer clobber the other mid write. An
atomic write (temp file in the same directory, then os.replace) makes each
write all-or-nothing so a concurrent reader never sees a partial file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any) -> None:
    """Serialize `data` to `path` atomically via a same-dir temp file + replace.

    The temp name includes the PID so two processes writing at once don't
    collide on the temp file itself. os.replace is atomic on the same
    filesystem, which a same-directory temp guarantees.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
