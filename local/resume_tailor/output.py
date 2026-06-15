"""Resolve where a tailored resume PDF goes.

Downloads/Generated_Resumes/<Company>/<Job Title>/You_Tadesse_Resume.pdf
On collision (same company+title already has a resume), nest a dated subfolder.
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from . import assets, config

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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


def resolve_dir(company: str, job_title: str) -> Path:
    """Return the directory to write into, creating it. Dated subfolder on collision."""
    base = config.OUTPUT_ROOT / sanitize(company) / sanitize(job_title)
    target = base
    if (base / resume_filename()).exists():
        dated = base / date.today().isoformat()
        target = dated
        n = 2
        while (target / resume_filename()).exists():
            target = base / f"{date.today().isoformat()}-{n}"
            n += 1
    target.mkdir(parents=True, exist_ok=True)
    return target
