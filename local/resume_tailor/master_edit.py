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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ruamel.yaml import YAML, YAMLError

from . import assets, config

_SECTIONS = ("experience", "projects", "leadership")
_NAME_KEY = {"experience": "org", "projects": "name", "leadership": "org"}
_CACHED = (
    assets.load_master, assets.tailor_config, assets.atoms_by_id, assets.blocks,
    assets.skill_aliases, assets.skill_aliases_match_only,
)


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
    """Parse `path` with the round-trip loader.

    A hand-edited master that is no longer valid YAML raises ruamel's own
    `YAMLError`, which is not a `ValueError` and so slips past every write
    handler's `except (ValueError, OSError)`: the add dialogs lose what the
    user typed, and Save appears to do nothing. Re-raised here as a
    `ValueError` naming the line and column, one fix covers every caller below.
    """
    y = _yaml()
    try:
        with path.open(encoding="utf-8") as fh:
            return y, y.load(fh)
    except YAMLError as exc:
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            raise ValueError(
                "Invalid YAML at line %d, column %d: %s"
                % (mark.line + 1, mark.column + 1, problem)) from exc
        raise ValueError("Invalid YAML: %s" % problem) from exc


# ── shared atomic write + timestamped backup ring ─────────────────────────────
# Both writers of master_experience.yaml (this module and master_gaps.apply_to_file)
# route through _atomic_replace so a crash mid-write can never truncate the
# hand-maintained source of truth. _backup_ring keeps the stable `<name>.bak`
# (latest good copy, referenced by the revert flow and CLI messages) plus a small
# timestamped ring so a *second* bad save doesn't clobber the last good backup.
_BAK_RING_KEEP = 5


def _backup_ring(path: Path) -> None:
    """Back `path` up before it is replaced: refresh the stable `<name>.bak` and
    drop a timestamped `<name>.bak.<YYYYMMDD-HHMMSS-us>` ring entry, pruning the
    ring to the newest `_BAK_RING_KEEP`. No-op if the file doesn't exist yet."""
    if not path.exists():
        return
    shutil.copy2(str(path), str(path.with_name(path.name + ".bak")))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    shutil.copy2(str(path), str(path.with_name(path.name + ".bak." + stamp)))
    ring = sorted(path.parent.glob(path.name + ".bak.*"))  # timestamp sorts chronologically
    for old in ring[:-_BAK_RING_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def _atomic_replace(path: Path, write_fn: Callable[[Any], None], *,
                    binary: bool = False, backup: bool = True) -> None:
    """Write `path` atomically: dump to a sibling tempfile via `write_fn(fh)`, back
    the existing file up (stable .bak + timestamped ring), then `os.replace` the temp
    into place. If `write_fn` or the replace fails, the tempfile is removed and the
    original file survives intact (old-or-new, never a truncation)."""
    mode = "wb" if binary else "w"
    encoding = None if binary else "utf-8"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".master_", suffix=".tmp")
    try:
        with os.fdopen(fd, mode, encoding=encoding) as fh:
            write_fn(fh)
        if backup:
            _backup_ring(path)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, *, backup: bool = True) -> None:
    """Atomically write `text` to `path` with the same tempfile + backup ring the
    other master_experience.yaml writers use. Public so master_gaps.apply_to_file
    shares one crash-safe write path. Does NOT clear caches — the caller owns that."""
    _atomic_replace(path, lambda fh: fh.write(text), backup=backup)


def _write_doc(y: YAML, doc: Any, path: Path) -> None:
    """Atomically write `doc` (backing the existing file up first), then clear the
    assets caches so readers see the new content."""
    _atomic_replace(path, lambda fh: y.dump(doc, fh))
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

def entry_problems(section: str, data: Dict[str, Any]) -> List[str]:
    """Every problem with `data` as an entry for `section` ([] means it's fine).

    The pure, ALL-problems form of the checks `append_entry` has always enforced
    (name/dates required, at least one achievement, each achievement needs a
    'what' and an angle): `_validate` below raises on the first one so
    `append_entry`'s contract is unchanged, while this is what the add-entry
    dialog calls on every keystroke (to list every problem inline and keep OK
    disabled until there are none) and what the tab's Save calls, before
    writing anything, over every entry it is about to change.
    """
    if section not in _SECTIONS:
        return ["unknown section %r" % section]
    problems: List[str] = []
    name = (data.get(_NAME_KEY[section]) or "").strip()
    if not name:
        problems.append("%s is required" % _NAME_KEY[section])
    if not (data.get("dates") or "").strip():
        problems.append("dates is required")
    achs = data.get("achievements") or []
    if not achs:
        problems.append("at least one achievement is required")
    for a in achs:
        if not isinstance(a, dict):
            problems.append("each achievement must be a record")
            continue
        if not (a.get("what") or "").strip():
            problems.append("each achievement needs a 'what'")
        if not [x for x in (a.get("angles") or []) if str(x).strip()]:
            problems.append("each achievement needs at least one angle")
    return problems


def _validate(section: str, data: Dict[str, Any]) -> None:
    problems = entry_problems(section, data)
    if problems:
        raise ValueError(problems[0])


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


_EM_DASH = "—"


def _atom_text_fields(atom: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(label, text) for every free-text field a user types for one achievement,
    in the shape add_atom/append_entry receive them (what already a string,
    angles/impact already split into their own list items) -- read for the
    em-dash style check below, never rewritten."""
    out: List[Tuple[str, str]] = [("what", str(atom.get("what") or ""))]
    for a in atom.get("angles") or []:
        out.append(("an angle", str(a or "")))
    for line in atom.get("impact") or []:
        out.append(("an impact line", str(line or "")))
    return out


def atom_problems(section: str, index: int, atom: Dict[str, Any],
                  doc: Optional[Dict[str, Any]] = None) -> List[str]:
    """Every problem with `atom` as a new achievement for section[index] ([] means
    it's fine).

    entry_problems' twin, covering a single atom added through `add_atom` to an
    existing entry (entry_problems itself covers the first achievement of a
    brand new entry): the same "needs a 'what'" / "needs at least one angle"
    rules the add-atom dialog has always enforced on submit (an
    atom missing either one renders as an empty line in `assets.atom_line()` and is
    silently dropped, never reaching the tailored resume), the target entry must
    exist when `doc` is given (the same check `add_atom`'s `_entry()` raises on
    today, surfaced here before any write is attempted so `add_atom` can refuse
    in one clean step, fully up front), and an em dash anywhere in the
    atom's own text is flagged, since the user's style rule bans it from resume
    text -- this only reports the problem, it never rewrites what the user typed.

    `doc` is optional: the add-atom dialog reads the on-disk document once per
    open and passes it in (mirroring `_pending_problems`'s use of a loaded
    document); a caller with none yet just gets the content-only checks.
    """
    if section not in _SECTIONS:
        return ["unknown section %r" % section]
    problems: List[str] = []
    if doc is not None:
        seq = doc.get(section) or []
        if not (0 <= index < len(seq)):
            problems.append("%s index %d out of range" % (section, index))
    if not (atom.get("what") or "").strip():
        problems.append("achievement needs a 'what'")
    if not [x for x in (atom.get("angles") or []) if str(x).strip()]:
        problems.append("achievement needs at least one angle")
    for label, text in _atom_text_fields(atom):
        if _EM_DASH in text:
            problems.append(f"{label} has an em dash; remove it")
    return problems


def add_atom(section: str, index: int, atom: Dict[str, Any],
             path: Optional[Path] = None) -> None:
    """Append a new achievement atom to an entry, assigning a unique id."""
    target = _path(path)
    y, doc = _load_doc(target)
    problems = atom_problems(section, index, atom, doc)
    if problems:
        raise ValueError(problems[0])
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
    _atomic_replace(target, lambda fh: fh.write(data), binary=True)
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
