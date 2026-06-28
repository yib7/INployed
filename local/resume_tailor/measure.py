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
# A typical mid-width glyph for anything outside the table (accented letters, symbols).
_DEFAULT_W = 500

# Body text-column capacity in the same 1/1000-em units (calibrated; see module docstring).
BODY_LINE_CAPACITY = int(os.getenv("RESUME_TAILOR_BODY_LINE_CAPACITY", "53464"))


def text_width(s: str) -> int:
    """Advance width of a string in 1/1000-em units (sum of per-glyph widths)."""
    return sum(_CHAR_W.get(c, _DEFAULT_W) for c in s)


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
