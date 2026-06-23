"""The master answer store: a reusable bank of screening-question answers.

This supersedes the flat `apply_config.json` for everything the user edits. Each
entry carries metadata the dashboard and the apply skill both use:

    {"id", "question", "answer",
     "kind":   "fixed" | "open-ended",       # fixed = never altered (auth, EEO);
                                              # open-ended = lightly adaptable per job
     "status": "active" | "needs-review"}     # needs-review = seen but no good answer yet

`answer` is always stored as a string (keeps the dashboard table editor simple);
the three legacy boolean keys are coerced back to bool only in
`as_standard_answers()`, which reproduces the exact flat dict the apply skill's
table expects. The store seeds from `apply_config.DEFAULTS` and, on first run,
migrates any overrides from a pre-existing `apply_config.json` — nothing lost.

The backing file (repo-root `apply_answers.json`) is personal, so it is
git-ignored; absent, `load()` returns the seeded+migrated defaults in memory.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Union

from . import apply_config

PKG_DIR = Path(__file__).resolve().parent          # local/resume_tailor
REPO_ROOT = PKG_DIR.parent.parent                  # scrape_data
STORE_PATH = REPO_ROOT / "apply_answers.json"

KINDS = ("fixed", "open-ended")
STATUSES = ("active", "needs-review")

# The legacy booleans — coerced back to bool when flattened for the skill.
BOOL_IDS = {"work_authorized", "requires_sponsorship", "willing_to_relocate"}

# Which seeded defaults are immutable facts (never re-angled per job) vs. tweakable.
_FIXED_IDS = {
    "work_authorized", "requires_sponsorship",
    "gender", "race_ethnicity", "veteran_status", "disability_status",
}

# Human-readable question text for each seeded default.
_QUESTIONS = {
    "work_authorized": "Are you legally authorized to work in the US?",
    "requires_sponsorship": "Will you now or in the future require visa sponsorship?",
    "years_experience": "How many years of relevant experience do you have?",
    "willing_to_relocate": "Are you willing to relocate?",
    "authorization_statement": "Work-authorization statement (free text).",
    "gender": "Gender (EEO self-identification).",
    "race_ethnicity": "Race / ethnicity (EEO self-identification).",
    "veteran_status": "Veteran status (EEO self-identification).",
    "disability_status": "Disability status (EEO self-identification).",
    "how_did_you_hear": "How did you hear about us?",
}


def _to_answer_str(entry_id: str, value: Any) -> str:
    if entry_id in BOOL_IDS:
        return "true" if bool(value) else "false"
    return str(value)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "yes", "1"}


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "answer").lower()).strip("_")
    return s or "answer"


def _unique_id(base: str, taken: set) -> str:
    cand, n = base, 2
    while cand in taken:
        cand = "%s_%d" % (base, n)
        n += 1
    return cand


def seed_defaults() -> List[Dict[str, Any]]:
    """One entry per `apply_config.DEFAULTS`, in that order, all active."""
    out: List[Dict[str, Any]] = []
    for key, value in apply_config.DEFAULTS.items():
        out.append({
            "id": key,
            "question": _QUESTIONS.get(key, key.replace("_", " ").capitalize() + "?"),
            "answer": _to_answer_str(key, value),
            "kind": "fixed" if key in _FIXED_IDS else "open-ended",
            "status": "active",
        })
    return out


def migrate_from_apply_config(answers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply any overrides from a pre-existing apply_config.json onto matching
    entries' answers. Idempotent — absent file means the merged config equals the
    defaults, leaving `answers` unchanged."""
    cfg = apply_config.load_apply_config()
    for e in answers:
        if e["id"] in cfg:
            e["answer"] = _to_answer_str(e["id"], cfg[e["id"]])
    return answers


def load(path: Union[Path, None] = None) -> List[Dict[str, Any]]:
    """Stored answers if the file exists, else the seeded+migrated defaults (in
    memory; not written)."""
    path = Path(path) if path is not None else STORE_PATH
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return migrate_from_apply_config(seed_defaults())
        if isinstance(data, dict) and isinstance(data.get("answers"), list):
            return data["answers"]
        return migrate_from_apply_config(seed_defaults())
    return migrate_from_apply_config(seed_defaults())


def validate(answers: List[Dict[str, Any]]) -> List[str]:
    """Human-readable problems with the store ([] = OK)."""
    errors: List[str] = []
    if not isinstance(answers, list):
        return ["the answer store must be a list of entries"]
    seen: set = set()
    for i, e in enumerate(answers):
        if not isinstance(e, dict):
            errors.append("entry %d is not a record" % (i + 1))
            continue
        eid = str(e.get("id", "")).strip()
        label = eid or ("entry %d" % (i + 1))
        if not eid:
            errors.append("%s: id is required" % label)
        if not str(e.get("question", "")).strip():
            errors.append("answer '%s': question is required" % label)
        if e.get("kind") not in KINDS:
            errors.append("answer '%s': kind must be one of %s" % (label, ", ".join(KINDS)))
        if e.get("status") not in STATUSES:
            errors.append("answer '%s': status must be one of %s" % (label, ", ".join(STATUSES)))
        if eid:
            if eid in seen:
                errors.append("duplicate answer id '%s'" % eid)
            seen.add(eid)
    return errors


def save(answers: List[Dict[str, Any]], path: Union[Path, None] = None) -> None:
    """Validate, then atomically write {"answers": [...]} (backing up any existing
    file to `<name>.bak` first). Raises ValueError if the store is invalid."""
    errs = validate(answers)
    if errs:
        raise ValueError("; ".join(errs))
    path = Path(path) if path is not None else STORE_PATH
    payload = json.dumps({"answers": answers}, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".answers_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        if path.exists():
            shutil.copy2(str(path), str(path.with_name(path.name + ".bak")))
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def as_standard_answers(answers: Union[List[Dict[str, Any]], None] = None) -> Dict[str, Any]:
    """Flatten the ACTIVE answers into the legacy {id: value} dict the apply
    skill's table expects. Boolean ids are coerced back to bool."""
    answers = answers if answers is not None else load()
    out: Dict[str, Any] = {}
    for e in answers:
        if e.get("status") != "active":
            continue
        eid = e.get("id")
        if not eid:
            continue
        out[eid] = _truthy(e.get("answer")) if eid in BOOL_IDS else e.get("answer")
    return out


def append_needs_review(items: List[Union[str, Dict[str, Any]]],
                        path: Union[Path, None] = None) -> List[Dict[str, Any]]:
    """Append captured questions as needs-review entries (dedupe by normalized
    question text), persist, and return the updated store. `items` may be plain
    question strings or {"question", "answer"?} dicts."""
    path = Path(path) if path is not None else STORE_PATH
    answers = load(path)
    seen = {_norm(e.get("question", "")) for e in answers}
    taken = {e.get("id") for e in answers}
    for item in items:
        if isinstance(item, str):
            question, answer = item, ""
        else:
            question, answer = item.get("question", ""), item.get("answer", "")
        if not str(question).strip():
            continue
        norm = _norm(question)
        if norm in seen:
            continue
        seen.add(norm)
        new_id = _unique_id(_slug(question), taken)
        taken.add(new_id)
        answers.append({
            "id": new_id,
            "question": str(question).strip(),
            "answer": str(answer),
            "kind": "open-ended",
            "status": "needs-review",
        })
    save(answers, path)
    return answers
