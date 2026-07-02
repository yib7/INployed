"""Lint master_experience.yaml and the answer store so the dashboard can show a
clear, friendly error instead of letting a malformed file break the pipeline
later. Pure functions over already-parsed data; `check_setup()` runs both against
the live files for the dashboard's "Check setup" button.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from . import apply_answers, assets

_SECTIONS = ("experience", "projects", "leadership")
_NAME_KEY = {"experience": "org", "projects": "name", "leadership": "org"}


def _norm_skill(item: str) -> str:
    """Match the engine's skill-normalization (paren-stripped, lowercased) so the
    anchoring check uses the same key as ats._norm_skill."""
    return re.sub(r"\(.*?\)", "", str(item)).strip().lower()


def validate_master(master: Dict[str, Any]) -> List[str]:
    """Human-readable problems with master_experience.yaml ([] = OK)."""
    errors: List[str] = []
    if not isinstance(master, dict):
        return ["master_experience.yaml must be a mapping (key: value structure)"]

    basics = master.get("basics")
    if not isinstance(basics, dict) or not str(basics.get("name", "")).strip():
        errors.append("basics.name is required (your name on the résumé)")
    elif not str(basics.get("email", "")).strip():
        errors.append("basics.email is required")

    seen: Dict[str, str] = {}
    for sec in _SECTIONS:
        entries = master.get(sec) or []
        if not isinstance(entries, list):
            errors.append("%s must be a list of entries" % sec)
            continue
        for idx, e in enumerate(entries):
            if not isinstance(e, dict):
                errors.append("%s[%d] is not a record" % (sec, idx))
                continue
            name = str(e.get(_NAME_KEY[sec], "")).strip()
            label = name or ("%s[%d]" % (sec, idx))
            if not name:
                errors.append("%s[%d]: %s is required" % (sec, idx, _NAME_KEY[sec]))
            for a in e.get("achievements") or []:
                if not isinstance(a, dict):
                    errors.append("%s: an achievement is not a record" % label)
                    continue
                aid = str(a.get("id", "")).strip()
                if not aid:
                    errors.append("%s: an achievement is missing an id" % label)
                    continue
                if not str(a.get("what", "")).strip():
                    errors.append("atom '%s': 'what' is required" % aid)
                if aid in seen:
                    errors.append("duplicate atom id '%s' (in %s and %s)" % (aid, seen[aid], label))
                else:
                    seen[aid] = label

    tailor = master.get("tailor")
    req = tailor.get("required") if isinstance(tailor, dict) else None
    if isinstance(req, dict):
        known = {
            sec: {str((e or {}).get(_NAME_KEY[sec], "")).strip()
                  for e in (master.get(sec) or []) if isinstance(e, dict)}
            for sec in _SECTIONS
        }
        for sec, names in req.items():
            if isinstance(names, list):
                for n in names:
                    if str(n).strip() and str(n).strip() not in known.get(sec, set()):
                        errors.append(
                            "tailor.required.%s names a block not in %s: '%s'" % (sec, sec, n))

    real = {_norm_skill(item)
            for pool in (master.get("skills", {}) or {}).values()
            for item in (pool or [])}
    for key in ("skill_aliases", "skill_aliases_match_only"):
        aliases = master.get(key)
        if isinstance(aliases, dict):
            for canon in aliases:
                if str(canon).strip() and _norm_skill(str(canon)) not in real:
                    errors.append(
                        "%s canonical '%s' is not a known skill — anchor it to a real entry in "
                        "`skills:` (usually concepts_and_methodologies) or remove it" % (key, canon))
    return errors


def validate_answers(answers: List[Dict[str, Any]]) -> List[str]:
    """Delegate to the answer store's own validator."""
    return apply_answers.validate(answers)


def check_setup() -> Dict[str, List[str]]:
    """Run both validators against the live files for the dashboard."""
    return {
        "master": validate_master(assets.load_master()),
        "answers": validate_answers(apply_answers.load()),
    }
