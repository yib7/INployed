"""Rebuild the scorer's `resume.md` from `master_experience.yaml` via Gemini.

The job scorer (`score_jobs.py`) matches every posting against `resume.md`. When
the user edits their Resume Data (the master YAML), this regenerates `resume.md`
so the two stay in sync — **faithfully: select and rephrase, never invent** (the
project's résumé rule). Output mirrors the existing `resume.md` section layout.

The Gemini call is **injected** (`llm_call`) so the build and tests never spend a
real paid API credit — the dashboard supplies the real transport only on the
user's explicit button click (the same posture as the VM operations).
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable

import yaml

# resume.md lives at the repo root — exactly where score_jobs.py reads it
# (OUTPUT_DIR / "resume.md", OUTPUT_DIR being the repo root). resume_md.py is in
# local/, so the root is one level up.
ROOT = Path(__file__).resolve().parent.parent
RESUME_MD_PATH = ROOT / "resume.md"
MASTER_YAML_PATH = ROOT / "resume_tailor_files" / "master_experience.yaml"

SYSTEM_PROMPT = (
    "You convert a candidate's master-experience YAML into a clean Markdown "
    "resume that an automated job-matching scorer reads. Absolute rule: SELECT "
    "and REPHRASE only what the YAML contains — NEVER invent employers, titles, "
    "dates, numbers, skills, or achievements. Include the candidate's full breadth "
    "(this resume is for matching against many jobs, not a one-page tailored "
    "version): every experience, project, leadership entry, and skill present in "
    "the YAML. Output ONLY the Markdown resume, no commentary or code fences."
)

# The exact section structure of resume.md the scorer expects.
_STRUCTURE = """\
Produce GitHub-flavored Markdown with these sections, in this order, using the
YAML's data:

# <Full name>

<City, ST | phone | email | linkedin | github>   (only the contact items present)

## Summary
<2-4 sentence professional summary drawn from the YAML's summary/tailor notes
and the strongest evidence; do not invent claims.>

## Education
<each education entry: school, GPA if present, degree/concentration/minor,
location, dates, honors if present.>

## Work Experience
### <Title> — <Org>
<Location | dates>
- <one bullet per achievement atom, rephrased faithfully from its what/impact/angles>

## Projects
### <Name> | <tech/stack if present>
- <one bullet per achievement atom>

## Leadership Experience
### <Org / role>
<dates>
- <one bullet per achievement atom>

## Technical Skills
**<Pool>:** <comma-separated items>   (one line per skills pool in the YAML)
ALWAYS include a **Concepts & Methodologies:** line listing every item from that pool
verbatim — it is what the scorer screens concept keywords against, so never summarize,
sample, or drop it.
"""


def resume_md_stale(master_path: Path | None = None,
                    resume_md_path: Path | None = None) -> bool:
    """True when `resume.md` is missing or older than `master_experience.yaml`.

    The scorer matches every job against `resume.md`, but the Resume Data editor
    edits the master YAML; if the user edits the YAML and forgets to regenerate,
    `resume.md` silently drifts and scoring degrades. Returns False when the master
    doesn't exist (nothing to compare against)."""
    master = Path(master_path) if master_path is not None else MASTER_YAML_PATH
    md = Path(resume_md_path) if resume_md_path is not None else RESUME_MD_PATH
    if not master.exists():
        return False
    if not md.exists():
        return True
    return md.stat().st_mtime < master.stat().st_mtime


def build_prompt(yaml_text: str) -> str:
    """The user-message prompt: the structure guide + the candidate's YAML."""
    return (
        f"{_STRUCTURE}\n\n"
        "Here is the candidate's master_experience.yaml. Convert it to resume.md "
        "following the structure above, faithfully and without inventing anything:\n\n"
        f"```yaml\n{yaml_text}\n```"
    )


def _default_llm_call(system: str, user: str, model: str) -> str:
    """Real Gemini transport (paid). Only reached on the user's runtime click —
    never from the build or tests, which inject a fake `llm_call`."""
    from resume_tailor import llm
    return llm._call_gemini(system, user, model, temperature=0.2)


def _clean(text: str) -> str:
    """Strip any ```markdown / ``` fences the model wrapped the output in, and
    guarantee a trailing newline."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", t, flags=re.IGNORECASE).strip()
    return t + "\n"


def _concepts_pool(yaml_text: str) -> list[str]:
    """The master's `skills.concepts_and_methodologies` items (or [] if absent / unparsable).

    These are the concept keywords the scorer screens a posting against; we use them to
    GUARANTEE they survive generation. Any parse failure is swallowed — a malformed master
    must never break the build (the guarantee just no-ops)."""
    try:
        data = yaml.safe_load(yaml_text)
    except Exception:  # noqa: BLE001 - a malformed master must never break generation
        return []
    if not isinstance(data, dict):
        return []
    skills = data.get("skills")
    pool = skills.get("concepts_and_methodologies") if isinstance(skills, dict) else None
    if isinstance(pool, str):
        pool = [pool]
    if not isinstance(pool, (list, tuple)):
        return []
    return [str(x).strip() for x in pool if str(x).strip()]


def _ensure_concepts(md: str, concepts: list[str]) -> str:
    """Deterministic, zero-cost guarantee that every `concepts_and_methodologies` item is
    present in resume.md (the scorer matches concept keywords against it). Any item the model
    dropped is appended verbatim in a Concepts & Methodologies line; items already on the page
    are left as-is, so nothing is duplicated and nothing is invented (it draws ONLY from the
    user's own pool). No LLM call."""
    if not concepts:
        return md
    low = md.lower()
    missing = [c for c in concepts if c.lower() not in low]
    if not missing:
        return md
    return md.rstrip("\n") + "\n**Concepts & Methodologies:** " + ", ".join(missing) + "\n"


def generate_resume_md(
    yaml_text: str,
    model: str,
    *,
    llm_call: Callable[[str, str, str], str] | None = None,
) -> str:
    """Return Markdown for resume.md, generated from `yaml_text` with `model`.

    `llm_call(system, user, model) -> str` is injectable; the default uses the
    résumé-tailor Gemini transport (a paid call). The build/tests always pass a
    fake so no real credit is spent.
    """
    call = llm_call or _default_llm_call
    out = call(SYSTEM_PROMPT, build_prompt(yaml_text), model)
    if not isinstance(out, str) or not out.strip():
        raise ValueError("The model returned no resume text.")
    # De-fence first, then guarantee the concepts pool survived (append any item the model
    # dropped) — so the scorer never under-scores a posting that screens for a concept the
    # candidate genuinely owns.
    return _ensure_concepts(_clean(out), _concepts_pool(yaml_text))


def write_resume_md(text: str, path: Path | None = None) -> Path:
    """Write resume.md, backing up any existing file to resume.md.bak first."""
    p = Path(path) if path is not None else RESUME_MD_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copy2(p, p.with_name(p.name + ".bak"))
    p.write_text(text, encoding="utf-8")
    return p
