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


def clean_bullet(text: str) -> str:
    """Strip emphasis, escape for LaTeX, normalize whitespace.

    Renders '~' (approximately) as the math tilde $\\sim$, matching the
    template's own typography instead of a raised ASCII tilde.
    """
    text = strip_emphasis(text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    text = escape_latex(text).replace(r"\textasciitilde{}", r"$\sim$")
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
