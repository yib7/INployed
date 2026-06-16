"""Interview-prep sheet: JD requirement ↔ evidence mapping + likely questions.

One flash call. Grounded the same way as the resume: the model may only cite
evidence that exists in master_experience.yaml's atoms (and the tailored
bullets, when a generated resume.tex is available) — a requirement with no
matching atom is reported honestly as a gap to prepare an answer for.

Output: interview_prep.md in the job's resume folder (or a fresh folder when
no resume was tailored yet).
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from . import compose, config, output
from .llm import call


def _tailored_bullets(out_dir: Path) -> List[str]:
    """Bullets from a previously generated resume.tex, if one exists."""
    tex = out_dir / "resume.tex"
    if not tex.exists():
        return []
    try:
        text = tex.read_text(encoding="utf-8")
    except OSError:
        return []
    return [m.strip() for m in re.findall(r"\\resumeItem\{(.*?)\}", text, re.S)]


def generate_prep_sheet(job: Dict[str, str], out_dir: Optional[Path] = None) -> Path:
    """Write interview_prep.md for one job; returns the file path.

    job needs company_name, job_title and a description field (same dict shape
    the dashboard passes to tailor()). out_dir defaults to the job's resume
    folder convention under Generated_Resumes/.
    """
    from .run import _field, _job_description_text  # local import — avoids a cycle

    company = _field(job, "company_name") or "Unknown Company"
    job_title = _field(job, "job_title") or "Role"
    jd = _job_description_text(job)
    if len(jd) < 40:
        raise RuntimeError("Job description is empty/too short to build a prep sheet.")

    if out_dir is None:
        out_dir = output.resolve_dir(company, job_title)
    out_dir.mkdir(parents=True, exist_ok=True)

    bullets = _tailored_bullets(out_dir)
    bullets_block = (
        "\n\nTAILORED RESUME BULLETS (already on the submitted resume):\n"
        + "\n".join(f"- {b}" for b in bullets)
        if bullets
        else ""
    )

    system = (
        "You build interview-prep sheets for an early-career data/SWE candidate. "
        "Be honest and specific. You may ONLY cite evidence that appears in the "
        "atom catalog (or the tailored bullets); never invent experience. When a "
        "JD requirement has no matching evidence, mark it as a GAP and suggest how "
        "to talk about it honestly (adjacent experience, willingness to learn). "
        "Return clean markdown."
    )
    user = f"""TARGET ROLE: {job_title} at {company}

JOB DESCRIPTION:
{jd[:7000]}

CANDIDATE EVIDENCE — ATOM CATALOG (the only allowed source of claims):
{compose._catalog()}{bullets_block}

Write a markdown prep sheet with EXACTLY these sections:

# Interview Prep — {job_title} @ {company}

## Requirement → Evidence Map
One line per major JD requirement: `- **<requirement>** → <atom id(s) / bullet> — <one-clause talking point>`.
Use `→ GAP` when nothing matches, with a one-clause honest mitigation.

## Likely Screening Questions
6-10 questions this specific role would ask (mix technical + behavioral),
each followed by an indented pointer to the atom(s)/story to answer with.

## Gaps To Prepare For
The 2-4 weakest spots vs. this JD and a suggested honest framing for each.

## Questions To Ask Them
4-6 specific, informed questions for the interviewer (about the team, stack,
roadmap — tied to details actually in the JD)."""

    text = call(system, user, config.TIER_FLASH, json_out=False, temperature=0.3)
    path = out_dir / "interview_prep.md"
    header = f"<!-- generated {date.today().isoformat()} from job {json.dumps(_field(job, 'job_posting_id') or '?')} -->\n"
    path.write_text(header + text.strip() + "\n", encoding="utf-8")
    return path
