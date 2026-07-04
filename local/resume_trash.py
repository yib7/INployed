"""Move a deleted job's tailored-résumé folder to the Recycle Bin.

Deleting a job from the dashboard used to leave its folder orphaned under
~/Downloads/Generated_Resumes. This helper sends the folder to the Recycle Bin
(recoverable — never a permanent delete) and prunes the now-empty ancestor
dirs the output layout creates (<Company>/, and <Company>/<Title>/ when the
registry points at a dated <Company>/<Title>/<date> subfolder).

Safety first: anything that is not an existing directory strictly inside
resume_tailor.config.OUTPUT_ROOT is refused with False — a stale or foreign
registry path can never delete data. OUTPUT_ROOT is read at call time so the
RESUME_TAILOR_OUTPUT env override (and test monkeypatching) is honored.
"""
from __future__ import annotations

from pathlib import Path

from send2trash import send2trash

from resume_tailor import config


def recycle_resume_folder(path_str) -> bool:
    """Send the folder at `path_str` to the Recycle Bin; prune empty ancestors.

    Returns False (touching nothing) when the path is falsy, missing, not a
    directory, outside OUTPUT_ROOT, or the root itself. A send2trash failure
    (OSError / TrashPermissionError, e.g. the folder is open in Explorer)
    propagates: False means "refused", an exception means "tried and failed".
    """
    if not path_str:
        return False
    root = Path(config.OUTPUT_ROOT).resolve()
    folder = Path(str(path_str)).resolve()
    if not folder.is_dir():
        return False
    if folder == root or not folder.is_relative_to(root):
        return False
    send2trash(str(folder))
    # Prune now-empty ancestors up to (never including) the root: rmdir only
    # succeeds on an empty dir, so the first non-empty ancestor stops the walk.
    parent = folder.parent
    while parent != root and parent.is_relative_to(root):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return True
