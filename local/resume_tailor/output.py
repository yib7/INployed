"""Resolve where a tailored resume PDF goes.

Downloads/Generated_Resumes/<Company>/<Job Title>/<Candidate>_Resume.pdf
On collision (same company+title already has a resume), nest a dated subfolder.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import date
from pathlib import Path

from . import assets, config

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Serialize directory resolution and remember every folder handed out this process
# so two parallel tailor jobs for the SAME company+title get DISTINCT folders. The
# résumé file isn't written until much later in tailor(), so the on-disk check alone
# can't catch a same-batch collision — the claimed set closes that race. Per-process
# working state, not persisted across runs.
_resolve_lock = threading.Lock()
_claimed: set[Path] = set()


def sanitize(name: str, *, max_len: int = 80) -> str:
    name = _ILLEGAL.sub(" ", str(name or "")).strip()
    name = re.sub(r"\s+", " ", name).rstrip(". ")
    return (name[:max_len].rstrip(". ") or "Unknown")


def candidate_slug() -> str:
    """File-name stem for the candidate. RESUME_TAILOR_CANDIDATE wins if set;
    otherwise derived from yaml basics.name; else a safe default."""
    env = os.getenv("RESUME_TAILOR_CANDIDATE")
    if env:
        return env
    try:
        name = (assets.load_master().get("basics", {}) or {}).get("name", "")
    except Exception:
        name = ""
    return sanitize(name).replace(" ", "_") if name else config.CANDIDATE_NAME


def resume_filename() -> str:
    return f"{candidate_slug()}_Resume.pdf"


def cover_filename() -> str:
    return f"{candidate_slug()}_Cover_Letter.pdf"


def base_dir(company: str, job_title: str) -> Path:
    """The canonical (un-nested) folder for a company+title, WITHOUT creating it
    or nesting a dated subfolder. Use this to LOCATE an already-tailored folder;
    use resolve_dir() only when WRITING a new resume."""
    return config.OUTPUT_ROOT / sanitize(company) / sanitize(job_title)


def resolve_dir(company: str, job_title: str) -> Path:
    """Return the directory to write into, creating it. Dated subfolder on collision.

    A folder is "taken" if it already holds a résumé on disk OR was already handed
    out earlier this process (the claimed set) — the latter is what keeps concurrent
    same-company+title tailor jobs from clobbering one another. The whole
    check-claim-mkdir runs under a lock so no two threads pick the same target."""
    base = config.OUTPUT_ROOT / sanitize(company) / sanitize(job_title)
    fname = resume_filename()

    def taken(d: Path) -> bool:
        return d in _claimed or (d / fname).exists()

    with _resolve_lock:
        target = base
        if taken(base):
            today = date.today().isoformat()
            target = base / today
            n = 2
            while taken(target):
                target = base / f"{today}-{n}"
                n += 1
        _claimed.add(target)
        target.mkdir(parents=True, exist_ok=True)
    return target
