"""Edit master_experience.yaml, comment-preserving.

Uses ruamel.yaml round-trip so the hand-maintained file's comments, key order, and
existing formatting survive unchanged; only mutated/appended nodes are reformatted.

Supports append, plus full edit/delete (the dashboard's Résumé Data editor needs
it). Every write backs the file up to `<name>.bak` first, so a mistake is always
recoverable — paired with the editor's "Revert to opening state" snapshot.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ruamel.yaml import YAML

from . import assets, config

_SECTIONS = ("experience", "projects", "leadership")
_NAME_KEY = {"experience": "org", "projects": "name", "leadership": "org"}
_CACHED = (assets.load_master, assets.tailor_config, assets.atoms_by_id, assets.blocks)


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


def _all_ids(doc: Dict[str, Any]) -> set:
    ids: set = set()
    for sec in _SECTIONS:
        for e in doc.get(sec) or []:
            for a in (e.get("achievements") if isinstance(e, dict) else None) or []:
                if isinstance(a, dict) and a.get("id"):
                    ids.add(a["id"])
    return ids


def _normalize_atom(atom: Dict[str, Any]) -> Dict[str, Any]:
    atom["angles"] = [str(x).strip() for x in atom.get("angles", []) if str(x).strip()]
    impact = [str(x).strip() for x in atom.get("impact", []) if str(x).strip()]
    if impact:
        atom["impact"] = impact
    elif "impact" in atom:
        del atom["impact"]
    return atom


def _path(path: Optional[Path]) -> Path:
    return Path(path) if path is not None else config.MASTER_YAML


def _load_doc(path: Path):
    y = _yaml()
    with path.open(encoding="utf-8") as fh:
        return y, y.load(fh)


def _write_doc(y: YAML, doc: Any, path: Path) -> None:
    """Atomically write `doc`, backing any existing file up to `<name>.bak` first,
    then clear the assets caches so readers see the new content."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".master_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            y.dump(doc, fh)
        if path.exists():
            shutil.copy2(str(path), str(path.with_name(path.name + ".bak")))
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    for fn in _CACHED:
        fn.cache_clear()


def _seq(doc: Dict[str, Any], section: str):
    if section not in _SECTIONS:
        raise ValueError("unknown section %r" % section)
    return doc.get(section) or []


def _entry(doc: Dict[str, Any], section: str, index: int):
    seq = _seq(doc, section)
    if not (0 <= index < len(seq)):
        raise ValueError("%s index %d out of range" % (section, index))
    return seq


def _del_list_item(seq: Any, index: int) -> None:
    """Delete `seq[index]` and fix up ruamel's per-item comment map, so a comment
    attached to the removed item doesn't dangle and corrupt the dumped YAML."""
    del seq[index]
    ca = getattr(seq, "ca", None)
    items = getattr(ca, "items", None) if ca is not None else None
    if not items:
        return
    items.pop(index, None)
    for k in sorted(k for k in list(items) if isinstance(k, int) and k > index):
        items[k - 1] = items.pop(k)


# ── append (existing behaviour, now via the shared backup-then-write) ──────────

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


def append_entry(section: str, data: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Validate, assign unique atom ids, append to `section`, write back, clear caches."""
    _validate(section, data)
    target = _path(path)
    y, doc = _load_doc(target)
    name = data[_NAME_KEY[section]].strip()
    achs = data["achievements"]
    ids = _unique_ids(_slug(name), len(achs), _all_ids(doc))
    for atom, aid in zip(achs, ids):
        atom["id"] = aid
        _normalize_atom(atom)
    doc.setdefault(section, [])
    doc[section].append(data)
    _write_doc(y, doc, target)


# ── full edit / delete ────────────────────────────────────────────────────────

def update_entry(section: str, index: int, fields: Dict[str, Any],
                 path: Optional[Path] = None) -> None:
    """Set top-level fields (org/title/dates/…) on an entry by its order in the
    section. `achievements` is ignored here — use the atom ops for bullets."""
    target = _path(path)
    y, doc = _load_doc(target)
    entry = _entry(doc, section, index)[index]
    for k, v in fields.items():
        if k == "achievements":
            continue
        entry[k] = v
    _write_doc(y, doc, target)


def delete_entry(section: str, index: int, path: Optional[Path] = None) -> None:
    """Remove an entire entry (and its atoms) from a section by order."""
    target = _path(path)
    y, doc = _load_doc(target)
    seq = _entry(doc, section, index)
    _del_list_item(seq, index)
    if len(seq) == 0:
        # A header comment between `section:` and its first item dangles once the
        # list is empty and corrupts the dump. Drop the comment slot and replace
        # the value with a fresh empty list so ruamel emits a clean `section: []`.
        ca = getattr(doc, "ca", None)
        if ca is not None and getattr(ca, "items", None):
            ca.items.pop(section, None)
        doc[section] = []
    _write_doc(y, doc, target)


def add_atom(section: str, index: int, atom: Dict[str, Any],
             path: Optional[Path] = None) -> None:
    """Append a new achievement atom to an entry, assigning a unique id."""
    target = _path(path)
    y, doc = _load_doc(target)
    entry = _entry(doc, section, index)[index]
    name = str(entry.get(_NAME_KEY[section], "atom"))
    [aid] = _unique_ids(_slug(name), 1, _all_ids(doc))
    atom["id"] = aid
    _normalize_atom(atom)
    entry.setdefault("achievements", [])
    entry["achievements"].append(atom)
    _write_doc(y, doc, target)


def update_basics(fields: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Set fields on the top-level `basics` mapping (name/email/phone/…)."""
    target = _path(path)
    y, doc = _load_doc(target)
    basics = doc.get("basics")
    if not isinstance(basics, dict):
        doc["basics"] = {}
        basics = doc["basics"]
    for k, v in fields.items():
        basics[k] = v
    _write_doc(y, doc, target)


def restore_bytes(data: bytes, path: Optional[Path] = None) -> None:
    """Overwrite the master file with raw bytes (the editor's "revert to opening
    state"), backing up the current file to `<name>.bak` first and clearing caches."""
    target = _path(path)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".master_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if target.exists():
            shutil.copy2(str(target), str(target.with_name(target.name + ".bak")))
        os.replace(tmp, str(target))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    for fn in _CACHED:
        fn.cache_clear()


def _find_atom(doc: Dict[str, Any], atom_id: str):
    for sec in _SECTIONS:
        for e in doc.get(sec) or []:
            achs = e.get("achievements") if isinstance(e, dict) else None
            for i, a in enumerate(achs or []):
                if isinstance(a, dict) and a.get("id") == atom_id:
                    return achs, i
    return None, None


def update_atom(atom_id: str, fields: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Update fields of an existing atom (matched by its unique id; id immutable)."""
    target = _path(path)
    y, doc = _load_doc(target)
    achs, i = _find_atom(doc, atom_id)
    if achs is None:
        raise ValueError("no atom with id %r" % atom_id)
    atom = achs[i]
    for k, v in fields.items():
        if k == "id":
            continue
        atom[k] = v
    _write_doc(y, doc, target)


def delete_atom(atom_id: str, path: Optional[Path] = None) -> None:
    """Remove a single atom by its unique id."""
    target = _path(path)
    y, doc = _load_doc(target)
    achs, i = _find_atom(doc, atom_id)
    if achs is None:
        raise ValueError("no atom with id %r" % atom_id)
    _del_list_item(achs, i)
    _write_doc(y, doc, target)
