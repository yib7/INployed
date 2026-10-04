"""The apply.md sheet parser: turns a job's apply sheet into structured fields.

`parse_apply_md(text)` reads a job folder's `apply.md` (the per-job sheet
`resume_tailor/apply_data.py` writes from the answer store) into the
Candidate and Address blocks, the Standard answers list and the signature
name, tolerant of a hand edit or a missing section. `split_name(full)` breaks
a full name into (first, last) for the fields that ask for the two
separately. `apply_facts.py`'s fact catalog (the Jev-judged auto-apply run)
is the one production caller.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# `- **Label:** value`: the shape every Candidate/Address/Standard-answer line uses.
# The bold label may hold escaped stars (`\*`, written by apply_data._md_text);
# a lone backslash is allowed anywhere else, so the label ends at the first
# unescaped `**`.
_KV_RE = re.compile(r"^\s*-\s+\*\*(?P<label>(?:\\\*|\\(?!\*)|[^*\\])+?)\*\*\s*(?P<value>.*?)\s*$")
# A `## ` / `### ` section heading; text after `#`s, trailing space tolerated.
_HEADING_RE = re.compile(r"^\s*#{2,3}\s+(?P<name>.+?)\s*$")

# The sections apply_data writes AFTER the résumé bullets and the cover letter,
# the two parts a model writes. A heading-shaped line that got into either sits
# above the writer's own heading for these, so each is read from its LAST
# heading; the sections written before them (Candidate, Address, Education)
# keep their FIRST heading. Either way the writer's heading is the one read.
_TAIL_SECTIONS = ("cover letter", "standard answers", "electronic signature")


def _tail_key(section: str) -> str:
    """The `_TAIL_SECTIONS` entry a lowercased heading names, else ""."""
    for key in _TAIL_SECTIONS:
        if section == key or (key == "electronic signature" and section.startswith(key)):
            return key
    return ""


def _last_tail_headings(lines: List[str]) -> Dict[str, int]:
    """`_TAIL_SECTIONS` key -> the index of the last line that is its heading."""
    out: Dict[str, int] = {}
    for i, line in enumerate(lines):
        h = _HEADING_RE.match(line)
        if h:
            key = _tail_key(h.group("name").strip().lower())
            if key:
                out[key] = i
    return out


def split_name(full: str) -> Tuple[str, str]:
    """Split a full name into (first, last). One token → ("Name", ""); three+ →
    first token is first name, everything after is the last name (keeps
    multi-word surnames intact). Empty in → ("", "")."""
    parts = str(full or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def parse_apply_md(text: str) -> Dict[str, Any]:
    """Parse an apply.md into the field values a form fill needs.

    Returns {"candidate": {label: value}, "address": {label: value},
    "standard_answers": [(question, answer)], "signature_name": str}. Labels are
    lowercased with the trailing colon stripped ("First" not "**First:**"); the
    `## Standard answers` questions keep their original text (they ARE the form
    question), a trailing colon included, and a `\\*` in a question or an answer
    reads back as `*`. A `  - Note: ...` line under an answer is ignored.
    Tolerant of hand edits and missing sections (an absent one reads empty).
    """
    candidate: Dict[str, str] = {}
    address: Dict[str, str] = {}
    standard: List[Tuple[str, str]] = []
    signature_name = ""
    section = ""
    seen: set = set()
    lines = str(text or "").splitlines()
    last_tail = _last_tail_headings(lines)
    for i, line in enumerate(lines):
        h = _HEADING_RE.match(line)
        if h:
            section = h.group("name").strip().lower()
            # The sections written after the model-written résumé and letter
            # are read from their LAST heading (`_TAIL_SECTIONS`).
            tail = _tail_key(section)
            if tail and last_tail.get(tail) != i:
                section = ""
                continue
            # A REPEATED section heading is ignored. apply_data writes each
            # section exactly once, so a second `## Candidate` can only come
            # from something that got INTO the file; letting the later copy
            # overwrite the earlier one would let a forged block decide the
            # email address typed into a real application.
            # apply_data._defuse_structure escapes that shape out of the
            # free-text sections on the writing side; this is the same rule on
            # the reading side, which is also where a hand-edited file arrives.
            if section in seen:
                section = ""
                continue
            seen.add(section)
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        label = m.group("label").strip()
        value = m.group("value").strip()
        if section == "standard answers":
            # The whole bold span is the question; the value is the answer.
            standard.append((label.replace("\\*", "*"), value.replace("\\*", "*")))
            continue
        if label.endswith(":"):
            label = label[:-1].rstrip()
        low = label.lower()
        if section == "candidate":
            candidate[low] = value
        elif section == "address":
            address[low] = value
        elif section.startswith("electronic signature"):
            if low.startswith("signature"):
                signature_name = value
    return {"candidate": candidate, "address": address,
            "standard_answers": standard, "signature_name": signature_name}
