"""Width-aware line measurement for resume body bullets.

The pipeline used to approximate a bullet's printed line count as
``len(text) / <chars per line>`` — a flat character count that can't tell a wide word
('cross-encoder') from a narrow one ('it'), so a bullet sitting at the 2-line boundary
could silently wrap to a 3rd line. This models the ACTUAL render instead: each
character's advance width (standard Times-Roman metrics, in 1/1000 em — close to the
template's Latin Modern serif after the column is calibrated) summed per word, greedily
wrapped against the body text-column's capacity, exactly as LaTeX breaks lines.

The capacity was calibrated against a real compiled PDF: from the true line breaks of 14
bullets the feasible window was [53410, 53518) units; the midpoint reproduces every
bullet's real line count. It is env-overridable for a customized template font/geometry.
"""
from __future__ import annotations

import os
from math import ceil

# Times-Roman AFM advance widths, units = 1/1000 em.
_CHAR_W = {
    " ": 250, "!": 333, '"': 408, "#": 500, "$": 500, "%": 833, "&": 778, "'": 180,
    "(": 333, ")": 333, "*": 500, "+": 564, ",": 250, "-": 333, ".": 250, "/": 278,
    "0": 500, "1": 500, "2": 500, "3": 500, "4": 500, "5": 500, "6": 500, "7": 500,
    "8": 500, "9": 500, ":": 278, ";": 278, "<": 564, "=": 564, ">": 564, "?": 444,
    "@": 921, "A": 722, "B": 667, "C": 667, "D": 722, "E": 611, "F": 556, "G": 722,
    "H": 722, "I": 333, "J": 389, "K": 722, "L": 611, "M": 889, "N": 722, "O": 722,
    "P": 556, "Q": 722, "R": 667, "S": 556, "T": 611, "U": 722, "V": 722, "W": 944,
    "X": 722, "Y": 722, "Z": 611, "[": 333, "\\": 278, "]": 333, "^": 469, "_": 500,
    "`": 333, "a": 444, "b": 500, "c": 444, "d": 500, "e": 444, "f": 333, "g": 500,
    "h": 500, "i": 278, "j": 278, "k": 500, "l": 278, "m": 778, "n": 500, "o": 500,
    "p": 500, "q": 500, "r": 333, "s": 389, "t": 278, "u": 500, "v": 500, "w": 722,
    "x": 500, "y": 500, "z": 444, "{": 480, "|": 200, "}": 480, "~": 541,
}
# Times-BOLD AFM advance widths (1/1000 em) — used for the bold skills-category label.
_CHAR_W_BOLD = {
    " ": 250, "!": 333, '"': 555, "#": 500, "$": 500, "%": 1000, "&": 833, "'": 278,
    "(": 333, ")": 333, "*": 500, "+": 570, ",": 250, "-": 333, ".": 250, "/": 278,
    "0": 500, "1": 500, "2": 500, "3": 500, "4": 500, "5": 500, "6": 500, "7": 500,
    "8": 500, "9": 500, ":": 333, ";": 333, "<": 570, "=": 570, ">": 570, "?": 500,
    "@": 930, "A": 722, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 778, "I": 389, "J": 500, "K": 778, "L": 667, "M": 944, "N": 722, "O": 778,
    "P": 611, "Q": 778, "R": 722, "S": 556, "T": 667, "U": 722, "V": 722, "W": 1000,
    "X": 722, "Y": 722, "Z": 667, "[": 333, "\\": 278, "]": 333, "^": 581, "_": 500,
    "`": 333, "a": 500, "b": 556, "c": 444, "d": 556, "e": 444, "f": 333, "g": 500,
    "h": 556, "i": 278, "j": 333, "k": 556, "l": 278, "m": 833, "n": 556, "o": 500,
    "p": 556, "q": 556, "r": 444, "s": 389, "t": 333, "u": 556, "v": 500, "w": 722,
    "x": 500, "y": 500, "z": 444, "{": 394, "|": 220, "}": 394, "~": 520,
}
# A typical mid-width glyph for anything outside the table (accented letters, symbols).
_DEFAULT_W = 500
_DEFAULT_W_BOLD = 556

def _env_int(name: str, default: int, lo: int = 1) -> int:
    """A positive int from the environment, falling back to `default` for anything that
    is not one. Same contract as `_env_fraction` below, and for the same reason: these
    run at IMPORT scope, so a bare `int(os.getenv(...))` would turn a typo in a .env
    (`RESUME_TAILOR_BODY_LINE_CAPACITY=53,464`) into a ValueError that takes down the
    whole package with a raw traceback before any handler exists to catch it."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val >= lo else default


# Body text-column capacity in the same 1/1000-em units (calibrated; see module docstring).
BODY_LINE_CAPACITY = _env_int("RESUME_TAILOR_BODY_LINE_CAPACITY", 53464)
# A skills line shares the body text column AND font size (verified against the real PDF:
# both 9.96pt at the same x indent), so its one-line capacity is identical by default.
SKILL_LINE_CAPACITY = _env_int("RESUME_TAILOR_SKILL_LINE_CAPACITY", BODY_LINE_CAPACITY)


def text_width(s: str, bold: bool = False) -> int:
    """Advance width of a string in 1/1000-em units (sum of per-glyph widths). `bold`
    uses the Times-Bold metrics (the skills-category label is rendered bold)."""
    if bold:
        return sum(_CHAR_W_BOLD.get(c, _DEFAULT_W_BOLD) for c in s)
    return sum(_CHAR_W.get(c, _DEFAULT_W) for c in s)


def skill_line_width(label: str, items: str) -> int:
    """Rendered width of one skills line: the bold category label + ': ' + the regular
    item list (matches render._skills: ``\\textbf{label}{: } items``). Compared against
    SKILL_LINE_CAPACITY to decide, by real width, whether a tech-stack item must be cut."""
    return text_width(label, bold=True) + text_width(": " + items)


def line_count(text: str, capacity: int | None = None) -> int:
    """How many printed lines `text` wraps to in the body column, via greedy word-wrap
    on real glyph widths. A single word wider than the column still counts as one line
    (LaTeX overfull hbox). Empty/blank text is one line."""
    cap = BODY_LINE_CAPACITY if capacity is None else capacity
    words = text.split()
    if not words:
        return 1
    space = _CHAR_W[" "]
    n = 1
    cur = text_width(words[0])
    for w in words[1:]:
        ww = text_width(w)
        if cur + space + ww <= cap:
            cur += space + ww
        else:
            n += 1
            cur = ww
    return n


# ── The character budget we ASK the model for ────────────────────────────────
# The rephrase prompt has to state a bullet's length as a CHARACTER cap (the model cannot
# measure glyph widths), so we convert the width budget above into characters here.
#
# Two things make that conversion non-obvious, and both move the number:
#
#  1. Characters are not all the same width, so the cap has to assume the WIDEST realistic
#     prose, not the average. Measured over a set of representative bullets — binary-search
#     the longest prefix with line_count(prefix) <= n — the effective width per character at
#     the tightest bullet lands at 421-424 units, against a 411-unit mean. Using the mean
#     would invite an over-length bullet on every denser-than-average line.
#
#  2. Capacity is SUBLINEAR in the line count. Greedy word wrap loses part of a line at every
#     break: the word that will not fit is pushed down whole, leaving the line before it
#     short. So an n-line bullet holds LESS than n times a one-line bullet — measured, the
#     per-line char rate falls 127 -> 126.5 -> 126 as n goes 1 -> 2 -> 3. `n * per_line` is
#     therefore wrong in PRINCIPLE, not merely mistuned; it overshoots by ~10 chars at n=2 and
#     ~13 at n=3, which is exactly enough to push a bullet onto an extra line and hand it to
#     the deterministic trim. Do not "simplify" this back to a flat multiply.
#
# Both constants are in the same 1/1000-em units as BODY_LINE_CAPACITY / are dimensionless, so
# char_budget() tracks the capacity if the template is ever recalibrated.
_BUDGET_CHAR_WIDTH = 422   # conservative advance width of one character of bullet prose
_WRAP_WASTE = 0.06         # fraction of a line greedy wrap loses at each break


def char_budget(target_lines: int, capacity: int | None = None) -> int:
    """The largest character count we can ask the model for and still expect the bullet to
    render within `target_lines` printed lines. Deliberately at or below the MINIMUM real
    capacity measured for that line count (126 / 245 / 364 at the calibrated default, against
    real minima of 127 / 250 / 377), because a bullet that overshoots gets cut by the
    deterministic trim — a cap that is a few characters short costs nothing by comparison.
    See the block comment above for why this is not `target_lines * chars_per_line`."""
    cap = BODY_LINE_CAPACITY if capacity is None else capacity
    n = max(1, int(target_lines))
    usable = (n - (n - 1) * _WRAP_WASTE) * cap
    return max(1, int(usable / _BUDGET_CHAR_WIDTH))


def _env_fraction(name: str, default: float, lo: float = 0.05, hi: float = 1.0) -> float:
    """A 0-1 fill fraction from the environment, falling back to `default` for anything that
    is not a real number inside [lo, hi]. These feed the prompt's fill AIM and the underfull
    RESCUE trigger, so a 0 (every bullet is 'full enough') or a 2.0 (nothing ever is) must
    not reach the engine — a typo in a .env should degrade to the documented default, never
    crash the run or silently disable a stage."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if not (lo <= val <= hi):   # also rejects nan, which compares False against everything
        return default
    return val


# Two distinct fill fractions, intentionally DECOUPLED:
#   * the rephrase AIM (FULL_LINE_FILL / LAST_LINE_FILL) is how full we ASK the model to make a
#     bullet (compose._length_hint): a single-line bullet aims for >=90% of its line, a multi-line
#     bullet's last line for >=75%. Unchanged — we still want lines to read full.
#   * the underfull TRIGGER (UNDERFULL_FILL) is how empty a line must be before we RESCUE it by
#     folding in a spare same-block atom (fill_floor_width -> is_underfull -> compose.fill_underfull).
#     Set low (50%) so only a sparse line is grown; some white space above it is fine and
#     kept for readability.
# All three are env-overridable (power users only — deliberately NOT Settings GUI fields, same
# call as RESUME_TAILOR_TIMEOUTS: a fraction a non-technical user can set to 0 is a footgun,
# and the three interact). Out-of-range or unparseable values fall back to the default below.
FULL_LINE_FILL = _env_fraction("RESUME_TAILOR_FULL_LINE_FILL", 0.90)   # aim (single line)
LAST_LINE_FILL = _env_fraction("RESUME_TAILOR_LAST_LINE_FILL", 0.75)   # aim (last line, multi)
UNDERFULL_FILL = _env_fraction("RESUME_TAILOR_UNDERFULL_FILL", 0.50)   # rescue trigger


def fill_floor_width(target_lines: int, capacity: int | None = None) -> int:
    """Width (1/1000-em units) below which a `target_lines` bullet is treated as UNDERFULL and may
    have a spare atom folded in: a 1-line bullet below UNDERFULL_FILL of its line, a multi-line
    bullet below UNDERFULL_FILL into its last line — i.e. ((target_lines - 1) + UNDERFULL_FILL) *
    capacity. This is the rescue TRIGGER only; the rephrase still AIMS for the fuller
    FULL_LINE_FILL / LAST_LINE_FILL (compose._length_hint), so a 50-90% line is left as readable
    white space rather than padded."""
    cap = BODY_LINE_CAPACITY if capacity is None else capacity
    if target_lines <= 1:
        return ceil(UNDERFULL_FILL * cap)
    return ceil(((target_lines - 1) + UNDERFULL_FILL) * cap)


def is_underfull(text: str, target_lines: int, capacity: int | None = None) -> bool:
    """True when `text`'s rendered width falls below its line budget's fill floor, so the
    engine may try to fold in more grounded detail (compose.fill_underfull). Width-aware
    (real glyph widths), the mirror of the over-length check line_count() drives."""
    return text_width(text) < fill_floor_width(target_lines, capacity)
