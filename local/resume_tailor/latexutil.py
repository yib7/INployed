"""LaTeX escaping, emphasis stripping, and date formatting.

Bullet/skill text from the model is plain prose; we escape it for LaTeX and
strip any stray bold/italic so the body honors the "no bolded words" rule.

This module is kept pure ASCII on purpose: it is the chokepoint that guarantees
no non-ASCII reaches pdflatex, so it must not itself carry encoding-fragile
literal glyphs. All unicode is referenced by integer code point.
"""
from __future__ import annotations

import calendar
import re
import unicodedata

_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    # In the template's OT1/T1 font, raw < > render as inverted punctuation.
    "<": r"\textless{}",
    ">": r"\textgreater{}",
    "|": r"\textbar{}",
}


def escape_latex(text: str) -> str:
    return "".join(_LATEX_SPECIALS.get(ch, ch) for ch in str(text))


def strip_emphasis(text: str) -> str:
    """Remove markdown/LaTeX bold+italic markers, keeping the inner words.

    Defends against a model that ignores "no markup": **x**, __x__, *x*,
    \\textbf{x}, \\emph{x}, \\textit{x} all collapse to x.
    """
    text = str(text)
    text = re.sub(r"\\(?:textbf|textit|emph|underline)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    return text.strip()


# Unicode math glyphs a model may emit in a bullet -> the LaTeX that renders them
# correctly in the template's OT1 font (raw glyphs render as tofu/wrong chars).
# Applied AFTER escape_latex so the replacement values keep their backslashes.
# Keyed by integer code point to keep this file pure ASCII.
_MATH_PAIRS = [
    (0x2265, r"$\ge$"), (0x2264, r"$\le$"), (0x2248, r"$\approx$"), (0x2260, r"$\neq$"),
    (0x00D7, r"$\times$"), (0x00F7, r"$\div$"), (0x00B1, r"$\pm$"), (0x2192, r"$\rightarrow$"),
    (0x2191, r"$\uparrow$"), (0x2193, r"$\downarrow$"), (0x221E, r"$\infty$"),
    (0x00B5, r"$\mu$"), (0x03BC, r"$\mu$"), (0x00B0, r"$^\circ$"),
]
_MATH_GLYPHS = {chr(cp): tex for cp, tex in _MATH_PAIRS}


def _math_to_latex(text: str) -> str:
    text = text.replace(r"\textasciitilde{}", r"$\sim$")  # '~' (approximately)
    # The ASCII digraphs the rephrase prompt asks the model to write ('>=', '<='),
    # already escaped by escape_latex — the promised math-notation conversion.
    text = text.replace(r"\textgreater{}=", r"$\ge$")
    text = text.replace(r"\textless{}=", r"$\le$")
    for glyph, tex in _MATH_GLYPHS.items():
        if glyph in text:
            text = text.replace(glyph, tex)
    return text


# Common non-ASCII punctuation a model emits, mapped to ASCII/LaTeX equivalents.
# These MUST be substituted explicitly (not dropped) so meaning survives -- e.g.
# U+2212 MINUS SIGN must become '-', or a coefficient like 'minus 0.158' would
# silently lose its sign. Applied before the NFKD ASCII-fold below.
_PUNCT_PAIRS = [
    (0x2018, "`"), (0x2019, "'"), (0x201A, ","), (0x201B, "'"),    # single quotes
    (0x201C, "``"), (0x201D, "''"), (0x201E, ",,"),                # double quotes
    (0x2032, "'"), (0x2033, "''"),                                 # prime / dbl prime
    (0x2013, "--"), (0x2014, "---"), (0x2015, "---"),              # en / em dash
    (0x2010, "-"), (0x2011, "-"), (0x2012, "-"), (0x2212, "-"),    # hyphens + MINUS
    (0x2026, "..."),                                               # ellipsis
    (0x2022, "-"), (0x00B7, "-"), (0x2027, "-"),                   # bullets / middots
    (0x00A0, " "), (0x2009, " "), (0x202F, " "), (0x2007, " "), (0x200A, " "),
    (0x200B, ""), (0x200C, ""), (0x200D, ""), (0xFEFF, ""),        # zero-width / BOM
]
_PUNCT_MAP = {chr(cp): rep for cp, rep in _PUNCT_PAIRS}


def _ascii_fallback(text: str) -> str:
    """Final safety net: guarantee ASCII-only output. The template has no
    inputenc/fontenc, so ANY undeclared non-ASCII glyph is a fatal pdflatex error.
    Map known punctuation to ASCII, decompose accents (e-acute -> e), then drop
    anything still non-ASCII. Runs LAST, after the math-glyph pass, so intentional
    LaTeX (which is already ASCII) is untouched."""
    for u, a in _PUNCT_MAP.items():
        if u in text:
            text = text.replace(u, a)
    text = unicodedata.normalize("NFKD", text)
    return text.encode("ascii", "ignore").decode("ascii")


def to_latex(text: str) -> str:
    """The single safe transform for any model-emitted text (bullets, skills):
    escape LaTeX specials, render known unicode math glyphs, then ASCII-fold the
    rest so an unlisted character can never kill the compile."""
    return _ascii_fallback(_math_to_latex(escape_latex(str(text))))


def clean_bullet(text: str) -> str:
    """Strip emphasis, escape for LaTeX, normalize whitespace, convert unicode math
    glyphs ('~', '>=', 'x', ...) to LaTeX, and ASCII-fold any remaining non-ASCII so
    the bullet always renders and never breaks the compile.
    """
    text = strip_emphasis(text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return to_latex(text) + "."


_MONTHS = {f"{i:02d}": calendar.month_name[i] for i in range(1, 13)}


def _fmt_one(token: str) -> str:
    token = token.strip()
    if not token or token.lower() in {"present", "current"}:
        return "Present"
    parts = token.split("-")
    year = parts[0]
    if len(parts) >= 2 and parts[1] in _MONTHS:
        return f"{_MONTHS[parts[1]]} {year}"
    return year


def fmt_dates(dates: str) -> str:
    """'2025-06 / 2025-07' -> 'June 2025 -- July 2025'."""
    if not dates:
        return ""
    if "/" in dates:
        a, b = dates.split("/", 1)
        return f"{_fmt_one(a)} -- {_fmt_one(b)}"
    return _fmt_one(dates)
