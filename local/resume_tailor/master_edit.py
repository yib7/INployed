"""Append new entries to master_experience.yaml, comment-preserving.

Append-only: never edits or deletes existing entries. Uses ruamel.yaml round-trip
so the hand-maintained file's comments, key order, and existing formatting survive
unchanged; only the freshly appended node is formatted by the dumper.
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Any, Dict, List

from ruamel.yaml import YAML

from . import assets, config

_SECTIONS = ("experience", "projects", "leadership")
_NAME_KEY = {"experience": "org", "projects": "name", "leadership": "org"}


def _yaml() -> YAML:
    y = YAML()                                   # round-trip mode (default)
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)    # match the file's block style
    y.width = 4096                               # do not auto-wrap long scalars
    return y


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "entry").lower()).strip("_")
    return s or "entry"


def _unique_ids(base: str, n: int, taken: set) -> List[str]:
    ids: List[str] = []
    for i in range(1, n + 1):
        cand = "%s_%d" % (base, i)
        while cand in taken:
            cand = cand + "_x"
        taken.add(cand)
        ids.append(cand)
    return ids


def _validate(section: str, data: Dict[str, Any]) -> None:
    if section not in _SECTIONS:
        raise ValueError("unknown section %r" % section)
    name = (data.get(_NAME_KEY[section]) or "").strip()
    if not name:
        raise ValueError("%s is required" % _NAME_KEY[section])
    if not (data.get("dates") or "").strip():
        raise ValueError("dates is required")
    achs = data.get("achievements") or []
    if not achs:
        raise ValueError("at least one achievement is required")
    for a in achs:
        if not (a.get("what") or "").strip():
            raise ValueError("each achievement needs a 'what'")
        if not [x for x in (a.get("angles") or []) if str(x).strip()]:
            raise ValueError("each achievement needs at least one angle")


def append_entry(section: str, data: Dict[str, Any]) -> None:
    """Validate, assign unique atom ids, append to `section`, write back, clear caches."""
    _validate(section, data)
    taken = set(assets.atoms_by_id().keys())
    name = data[_NAME_KEY[section]].strip()
    achs = data["achievements"]
    ids = _unique_ids(_slug(name), len(achs), taken)
    for atom, aid in zip(achs, ids):
        atom["id"] = aid
        atom["angles"] = [str(x).strip() for x in atom.get("angles", []) if str(x).strip()]
        impact = [str(x).strip() for x in atom.get("impact", []) if str(x).strip()]
        if impact:
            atom["impact"] = impact
        elif "impact" in atom:
            del atom["impact"]

    y = _yaml()
    with config.MASTER_YAML.open(encoding="utf-8") as fh:
        doc = y.load(fh)
    doc.setdefault(section, [])
    doc[section].append(data)
    target = config.MASTER_YAML
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".master_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            y.dump(doc, fh)
        os.replace(tmp, str(target))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    for fn in (assets.load_master, assets.tailor_config,
               assets.atoms_by_id, assets.blocks):
        fn.cache_clear()
