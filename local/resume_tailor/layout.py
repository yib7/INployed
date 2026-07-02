"""Layout spec for the resume's fixed sections (skill counts + leadership planning).

Line FITTING (both body bullets and skills lines) is now width-aware: run._trim_to_caps
and compose._cap_items measure the real rendered glyph width (resume_tailor/measure.py,
calibrated against the compiled PDF) instead of a flat character count, so a wide-word
bullet or a wide tech-stack item is cut only when it actually overflows its printed line.
This module keeps the candidate-COUNT spec (best-N per skill line) and leadership planning.
"""
from __future__ import annotations

import os
from typing import Dict, List

# ── The strict spec ──────────────────────────────────────────────────────────
# Each Leadership org defaults to this many printed lines (overridable via
# `tailor.leadership_entry_lines`). Realised as either two 1-line bullets or one
# 2-line bullet, chosen deterministically from how many atoms the org has (see
# plan_leadership_lines).
LEADERSHIP_ENTRY_LINES = 2

# Technical Skills: 4 fixed category lines (Languages / Frameworks / Developer
# Tools / Libraries), ~4 printed lines total. Each line lists the best N most
# JD-relevant skills (the LLM ranks; `_finalize_skill_lines` takes the top N,
# completing from the pool when the model under-returns, then trims from the tail
# until the rendered line fits one printed line by real width — measure.skill_line_width
# / SKILL_LINE_CAPACITY). A pool with fewer than N skills contributes all of them.
# There is NO fill floor: the line width is a hard ceiling, not a target — a short
# list of relevant skills stays short rather than being padded.
def skill_targets() -> Dict[str, int]:
    """Best-N item count per category. 'Methods' is the optional 5th concepts line
    (compose.methods_line); the four tool lines ignore it. Override via
    RESUME_TAILOR_SKILL_TARGETS="Languages=7,Frameworks=7,Developer Tools=10,Libraries=10,Methods=7"."""
    targets = {"Languages": 7, "Frameworks": 7, "Developer Tools": 10, "Libraries": 10,
               "Methods": 7}
    for part in os.getenv("RESUME_TAILOR_SKILL_TARGETS", "").split(","):
        key, sep, val = part.partition("=")
        if sep:
            try:
                targets[key.strip()] = int(val.strip())
            except ValueError:
                pass
    return targets


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
