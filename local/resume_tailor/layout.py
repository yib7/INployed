"""Layout spec for the resume's fixed sections (skills math + leadership planning).

The technical-skills column width comes from EMPIRICAL calibration of resume_template.tex
(probe bullets compiled with pdflatex, printed lines counted from the PDF):

  * Skills line (\\small, leftmargin 0.15in, bold label prefix counts): wraps
    1->2 at ~105 total chars and 2->3 at ~210  ->  ~105 chars per printed line.

If the template's geometry or font changes, re-run the calibration and update the
constant below (or override via env without a code edit).

Body-bullet character math has been removed; length is now enforced deterministically
by run._trim_to_caps (measures with len(text.strip()), cap = target_lines * config.MAX_LINE_CHARS).
"""
from __future__ import annotations

import os
from math import ceil
from typing import Dict, List

# ── Calibrated column capacities ─────────────────────────────────────────────
SKILL_CPL = int(os.getenv("RESUME_TAILOR_SKILL_CPL", "105"))  # technical-skills line
MIN_FILL = float(os.getenv("RESUME_TAILOR_MIN_LINE_FILL", "0.7"))  # trailing line floor for skills
_SAFETY = 2  # keep a couple chars off the wrap boundary

# ── The strict spec ──────────────────────────────────────────────────────────
# Each Leadership org defaults to this many printed lines (overridable via
# `tailor.leadership_entry_lines`). Realised as either two 1-line bullets or one
# 2-line bullet, chosen deterministically from how many atoms the org has (see
# plan_leadership_lines).
LEADERSHIP_ENTRY_LINES = 2

# Technical Skills: 4 fixed category lines (Languages / Frameworks / Developer
# Tools / Libraries), ~4 printed lines total. Languages must carry at least
# MIN_LANGUAGES items; every line must be "robust" (filled to at least its
# min-char floor, backfilled from the pool if the model under-picks).
MIN_LANGUAGES = 4
# (label, target_printed_lines, item-char cap, item-char floor)
#   caps/floors are on the ITEMS text only; the bold label width is folded in via
#   SKILL_LABEL_WIDTH so the printed-line math stays honest.
SKILL_LABEL_WIDTH = {
    "Languages": 12,                 # "Languages: "
    "Frameworks": 13,                # "Frameworks: "
    "Developer Tools": 18,           # "Developer Tools: "
    "Libraries": 12,                 # "Libraries: "
}
SKILL_LINE_TARGET = {
    "Languages": 1,
    "Frameworks": 1,
    "Developer Tools": 1,
    "Libraries": 1,
}


def _label_w(label: str) -> int:
    return SKILL_LABEL_WIDTH.get(label, 24)


# ── Skills line math ─────────────────────────────────────────────────────────
def skill_caps() -> Dict[str, int]:
    """Max ITEMS chars per category so each line hits its printed-line target."""
    caps: Dict[str, int] = {}
    for label, tgt in SKILL_LINE_TARGET.items():
        caps[label] = tgt * SKILL_CPL - _label_w(label) - _SAFETY
    return caps


def skill_floors() -> Dict[str, int]:
    """Min ITEMS chars per category so no skills line sits >half empty ('robust')."""
    floors: Dict[str, int] = {}
    for label, tgt in SKILL_LINE_TARGET.items():
        # floor = fill the last target line at least MIN_FILL, minus the label width
        floors[label] = max(0, ceil((tgt - 1 + MIN_FILL) * SKILL_CPL) - _label_w(label))
    return floors


# ── Bullet-count planning for the fixed blocks ───────────────────────────────
def plan_leadership_lines(group_count: int, entry_lines: int = LEADERSHIP_ENTRY_LINES) -> List[int]:
    """Per-bullet line targets for one Leadership org given how many bullets it has.

    2+ bullets -> two 1-line bullets (extra bullets, if any, also 1 line);
    1 bullet   -> a single `entry_lines`-line bullet.  Either way the org totals
    ~`entry_lines` printed lines. `entry_lines` is config-overridable (yaml
    `tailor.leadership_entry_lines`) so the spec isn't tied to one resume.
    """
    if group_count <= 1:
        return [entry_lines]
    return [1] * group_count
