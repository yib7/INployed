"""LaTeX escaping, emphasis stripping, and date formatting.

Bullet/skill text from the model is plain prose; we escape it for LaTeX and
strip any stray bold/italic so the body honors the "no bolded words" rule.
"""
from __future__ import annotations

import calendar
import re

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
    # In the template's OT1/T1 font, raw < > render as inverted punctuation (¡ ¿).
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
_MATH_GLYPHS = {
    "≥": r"$\ge$", "≤": r"$\le$", "≈": r"$\approx$", "≠": r"$\neq$",
    "×": r"$\times$", "÷": r"$\div$", "±": r"$\pm$", "→": r"$\rightarrow$",
    "↑": r"$\uparrow$", "↓": r"$\downarrow$", "∞": r"$\infty$", "µ": r"$\mu$",
    "μ": r"$\mu$", "°": r"$^\circ$",
}


def _math_to_latex(text: str) -> str:
    text = text.replace(r"\textasciitilde{}", r"$\sim$")  # '~' (approximately)
    for glyph, tex in _MATH_GLYPHS.items():
        if glyph in text:
            text = text.replace(glyph, tex)
    return text


def clean_bullet(text: str) -> str:
    """Strip emphasis, escape for LaTeX, normalize whitespace, and convert any
    unicode math glyphs ('~', '≥', '×', …) to LaTeX math so they render in the PDF
    instead of dropping out or rendering as the wrong character.
    """
    text = strip_emphasis(text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    text = _math_to_latex(escape_latex(text))
    return text + "."


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
