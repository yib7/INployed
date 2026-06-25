"""Open a file or folder in the OS file manager -- cross-platform.

The dashboard's "open folder" / "reveal generated resume" actions need to work on
Windows, macOS, and Linux. `os.startfile` exists only on Windows (it raises
AttributeError elsewhere), so this single helper dispatches per platform:

    Windows -> os.startfile (the native shell "open" verb)
    macOS   -> `open <path>`
    Linux   -> `xdg-open <path>`

Best-effort and fire-and-forget, matching the old os.startfile call sites: it does
not wait on the child and only surfaces failures as OSError (a missing `open` /
`xdg-open` raises FileNotFoundError, an OSError subclass), so existing
`except OSError` handlers keep working unchanged.
"""
from __future__ import annotations

import os
import subprocess
import sys


def open_path(path: str | os.PathLike) -> None:
    """Open `path` (a file or directory) in the OS default handler / file manager."""
    p = str(path)
    if sys.platform == "win32":
        os.startfile(p)  # noqa: S606  # native shell "open"; p is a local app path
    elif sys.platform == "darwin":
        subprocess.run(["open", p], check=False)
    else:
        subprocess.run(["xdg-open", p], check=False)
