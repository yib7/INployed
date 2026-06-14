"""Resolve where a tailored resume PDF goes.

Downloads/Generated_Resumes/<Company>/<Job Title>/You_Tadesse_Resume.pdf
On collision (same company+title already has a resume), nest a dated subfolder.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from . import config

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, *, max_len: int = 80) -> str:
    name = _ILLEGAL.sub(" ", str(name or "")).strip()
    name = re.sub(r"\s+", " ", name).rstrip(". ")
    return (name[:max_len].rstrip(". ") or "Unknown")


def resume_filename() -> str:
    return f"{config.CANDIDATE_NAME}_Resume.pdf"


def cover_filename() -> str:
    return f"{config.CANDIDATE_NAME}_Cover_Letter.pdf"


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
