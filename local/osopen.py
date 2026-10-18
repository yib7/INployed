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
from pathlib import Path


# What the dashboard ever opens: a job's folder, its résumé and cover letter
# PDFs, apply.md and the prep sheet. The paths come from files on disk (the
# tracker database, which Import tracker merges in from any file, and the
# auto-apply queue), so a path is opened only when it is one of these: the
# shell "open" verb on an .exe, .lnk or .bat would run it.
OPENABLE_SUFFIXES = frozenset({".pdf", ".md", ".txt", ".tex", ".docx"})


def _remote(p: str) -> bool:
    r"""A UNC or device path (\\host\share, //host/share, \\?\, \\.\): touching
    one, even to ask whether it exists, connects to that host with the user's
    Windows sign-in."""
    return p.startswith(("\\\\", "//"))


def can_open(path: str | os.PathLike | None) -> bool:
    """Is `path` a local folder or document `open_path` will open? Checked
    before the disk is touched, so a UNC path is never looked up."""
    if not path:
        return False
    p = str(path)
    if _remote(p):
        return False
    target = Path(p)
    try:
        if target.is_dir():
            return True
        return target.is_file() and target.suffix.lower() in OPENABLE_SUFFIXES
    except OSError:
        return False


def open_path(path: str | os.PathLike) -> None:
    """Open `path` (a local folder, or a document `can_open` admits) in the OS
    default handler / file manager. Anything else raises OSError unopened."""
    p = str(path)
    if not can_open(p):
        raise OSError(f"not a local folder or document: {Path(p).name or p}")
    if sys.platform == "win32":
        os.startfile(p)  # noqa: S606  # native shell "open"; p is a local app path
    elif sys.platform == "darwin":
        subprocess.run(["open", p], check=False)
    else:
        subprocess.run(["xdg-open", p], check=False)
