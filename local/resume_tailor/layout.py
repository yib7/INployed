"""HARD-CODED layout spec for the resume's fixed sections.

The template's fixed blocks (any config-pinned experience block, the Leadership orgs, Technical Skills)
must hit an EXACT printed-line / bullet budget. That budget is enforced in code
here — it is NOT left to the model's discretion ("vibe"). The model still writes
the prose, but this module decides how many bullets each block gets and the
character window each bullet must land in to render to the intended line count,
and a fit loop in run.py rejects/rewrites anything outside the window.

The character windows come from EMPIRICAL calibration of resume_template.tex
(probe bullets compiled with pdflatex, printed lines counted from the PDF):

  * Body bullet column (\\small, nested itemize): wraps 1->2 lines at ~92 chars
    and 2->3 lines at ~190 chars  ->  ~92 chars per printed line.
  * Skills line (\\small, leftmargin 0.15in, bold label prefix counts): wraps
    1->2 at ~105 total chars and 2->3 at ~210  ->  ~105 chars per printed line.

If the template's geometry or font changes, re-run the calibration and update the
constants below (or override the two CPL values via env without a code edit).
"""
from __future__ import annotations

import os
from math import ceil
from typing import Dict, List, Tuple

# ── Calibrated column capacities ─────────────────────────────────────────────
BODY_CPL = int(os.getenv("RESUME_TAILOR_BODY_CPL", "92"))    # body bullet column
SKILL_CPL = int(os.getenv("RESUME_TAILOR_SKILL_CPL", "105"))  # technical-skills line
# Fill floors that keep the page from looking sparse (PLAN stage 5):
#   - a SINGLE-line bullet must fill >= 75% of its line (no 7-word stubs);
#   - the LAST line of a MULTI-line bullet must fill >= 50% (it may "breathe").
MIN_SINGLE_LINE_FILL = float(os.getenv("RESUME_TAILOR_MIN_SINGLE_FILL", "0.75"))
MIN_FILL = float(os.getenv("RESUME_TAILOR_MIN_LINE_FILL", "0.5"))  # trailing line of a multi-line bullet
_SAFETY = 2  # keep a couple chars off the wrap boundary so a target line never spills

# ── The strict spec ──────────────────────────────────────────────────────────
# Per-bullet printed-line targets for the fixed EXPERIENCE blocks are config-driven
# (yaml `tailor.fixed_blocks.<block>.line_targets`, e.g. [2, 1] = a 2-line lead
# bullet + a 1-line tail bullet) so the spec isn't tied to one employer.
#
# Each Leadership org defaults to this many printed lines (overridable via
# `tailor.leadership_entry_lines`). Realised as either two 1-line bullets or one
# 2-line bullet, chosen deterministically from how many atoms the org has (see
# plan_leadership_lines).
LEADERSHIP_ENTRY_LINES = 2

# Technical Skills: 3 fixed category lines, total 3-4 printed lines. Languages
# must carry at least MIN_LANGUAGES items; every line must be "robust" (filled to
# at least its min-char floor, backfilled from the pool if the model under-picks).
MIN_LANGUAGES = 4
# (label, target_printed_lines, item-char cap, item-char floor)
#   caps/floors are on the ITEMS text only; the bold label width is folded in via
#   SKILL_LABEL_WIDTH so the printed-line math stays honest.
SKILL_LABEL_WIDTH = {
    "Languages": 12,                 # "Languages: "
    "Tools & Infrastructure": 25,    # "Tools & Infrastructure: "
    "Libraries & Frameworks": 25,    # "Libraries & Frameworks: "
}
SKILL_LINE_TARGET = {
    "Languages": 1,
    "Tools & Infrastructure": 1,
    "Libraries & Frameworks": 2,     # the one allowed to wrap -> 4 lines total
}


def _label_w(label: str) -> int:
    return SKILL_LABEL_WIDTH.get(label, 24)


# ── Body-bullet line math ────────────────────────────────────────────────────
def body_line_budget(target_lines: int) -> Tuple[int, int]:
    """(min_chars, max_chars) for a body bullet to render to EXACTLY target_lines
    and fill its last printed line adequately. A single-line bullet must reach
    MIN_SINGLE_LINE_FILL (75%); a multi-line bullet's trailing line must reach
    MIN_FILL (50%)."""
    hi = target_lines * BODY_CPL - _SAFETY
    last_line_fill = MIN_SINGLE_LINE_FILL if target_lines == 1 else MIN_FILL
    lo = ceil((target_lines - 1 + last_line_fill) * BODY_CPL)
    return lo, hi


def est_body_lines(text: str) -> int:
    return max(1, ceil(_visible_len(text) / BODY_CPL))


def body_fits(text: str, target_lines: int) -> bool:
    lo, hi = body_line_budget(target_lines)
    return lo <= _visible_len(text) <= hi


def _visible_len(text: str) -> int:
    """Approximate the rendered glyph count. The stored bullet is raw model prose;
    render adds a trailing period, so count it. LaTeX escaping (e.g. & -> \\&) adds
    source chars but renders as one glyph, so we estimate on the raw string."""
    return len((text or "").strip()) + 1


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
