"""Width-aware line measurement for resume body bullets.

The pipeline used to approximate a bullet's printed line count as
``len(text) / MAX_LINE_CHARS`` — a flat character count that can't tell a wide word
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

# Body text-column capacity in the same 1/1000-em units (calibrated; see module docstring).
BODY_LINE_CAPACITY = int(os.getenv("RESUME_TAILOR_BODY_LINE_CAPACITY", "53464"))
# A skills line shares the body text column AND font size (verified against the real PDF:
# both 9.96pt at the same x indent), so its one-line capacity is identical by default.
SKILL_LINE_CAPACITY = int(os.getenv("RESUME_TAILOR_SKILL_LINE_CAPACITY", str(BODY_LINE_CAPACITY)))


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
