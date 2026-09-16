"""Regression: every text field that reaches the résumé .tex must be ASCII-folded.

The résumé template carries no ``inputenc``/``fontenc``, so any non-ASCII glyph is
a fatal pdflatex error. Bullets already went through ``to_latex`` (which folds
unicode), but the structural fields (name, contact, job title, org, location,
project name, degree, school, honors, skill label) only ran ``escape_latex`` —
so a stray U+2212 MINUS SIGN (or an accent, or a smart quote) in any of them
crashed the compile with "Unicode character ... not set up for use with LaTeX".
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import coverletter, render  # noqa: E402
from resume_tailor.latexutil import to_latex  # noqa: E402

MINUS = "−"   # the exact char from the crash log: U+2212 MINUS SIGN
RSQUO = "’"   # right single quote
EACUTE = "é"  # accented letter


def test_to_latex_folds_minus_sign_keeping_the_sign():
    assert to_latex(f"changed +0.153 to {MINUS}0.158") == "changed +0.153 to -0.158"


def test_header_is_ascii_for_unicode_fields():
    out = render._header({"name": f"Jos{EACUTE} {MINUS}", "email": "a@b.co",
                          "location": f"shift {MINUS}0.158"})
    assert out.isascii(), "non-ASCII reached the résumé .tex (no inputenc -> fatal)"
    assert MINUS not in out


def test_education_is_ascii_for_unicode_fields():
    edu = [{"school": f"Universit{EACUTE} X", "degree": f"B.S. {MINUS} Stats",
            "location": "City", "dates": "2020/2024",
            "honors": [f"Dean{RSQUO}s List ({MINUS}top 5%)"]}]
    out = render._education(edu)
    assert out.isascii()
    assert MINUS not in out


def test_skills_is_ascii_for_unicode_label_and_items():
    out = render._skills([{"label": f"C{chr(0x2011)}tools", "items": f"effect {MINUS}0.158"}])
    assert out.isascii()
    assert MINUS not in out


def test_cover_letter_paragraphs_are_ascii():
    body = f"Drove the metric from +0.153 to {MINUS}0.158 (p < 0.05)."
    out = coverletter._paragraphs(body)
    assert out.isascii()
    assert MINUS not in out


# -- the fold must not UN-escape ------------------------------------------------
# ``to_latex`` escapes the ASCII specials first and ASCII-folds (NFKD) last, so a
# compatibility character whose decomposition IS an ASCII special used to arrive
# after the escaper had already run: FULLWIDTH REVERSE SOLIDUS (U+FF3C) folded to
# a live backslash, FULLWIDTH LEFT CURLY BRACKET (U+FF5B) to a live brace, and a
# bullet the model wrote in fullwidth glyphs reached pdflatex as a real command.
# Text handed to ``to_latex`` is model output steered by an untrusted job posting,
# so this is the LaTeX-injection chokepoint, and it must hold for every code point.

FW_BACKSLASH = "＼"     # NFKD -> "\\"
FW_LBRACE = "｛"        # NFKD -> "{"
FW_RBRACE = "｝"        # NFKD -> "}"
SMALL_BACKSLASH = "﹨"  # NFKD -> "\\"
SMALL_LBRACE = "﹛"
SMALL_RBRACE = "﹜"


def test_a_fullwidth_input_command_renders_as_literal_text():
    out = to_latex(f"{FW_BACKSLASH}input{FW_LBRACE}/etc/passwd{FW_RBRACE}")
    assert out.isascii()
    assert r"\input{" not in out
    assert out == r"\textbackslash{}input\{/etc/passwd\}"


def test_a_small_form_write18_renders_as_literal_text():
    out = to_latex(f"{SMALL_BACKSLASH}write18{SMALL_LBRACE}rm -rf ~{SMALL_RBRACE}")
    assert r"\write18" not in out
    assert out.startswith(r"\textbackslash{}write18\{")


def test_no_code_point_folds_into_an_unescaped_latex_special():
    """Whole code space: for every non-ASCII character, whatever ``to_latex``
    produces from it alone must contain no bare special. A bare backslash, brace,
    ``%`` (comment: truncates the line), ``#`` (macro parameter) or ``$`` (math
    shift) is exactly what the escaper exists to prevent."""
    from resume_tailor.latexutil import _LATEX_SPECIALS, _MATH_GLYPHS, escape_latex

    allowed = {escape_latex(ch) for ch in _LATEX_SPECIALS} | {""}
    offenders = []
    for cp in range(0x80, 0x110000):
        ch = chr(cp)
        if ch in _MATH_GLYPHS:
            continue                      # deliberate LaTeX ($\ge$ and friends)
        out = to_latex(ch)
        if out in allowed or not any(sp in out for sp in _LATEX_SPECIALS):
            continue
        # a decomposition may yield several characters ("..." or "(1)"); accept
        # it when every special in it is escaped
        stripped = out
        for esc in sorted(allowed, key=len, reverse=True):
            stripped = stripped.replace(esc, "")
        if any(sp in stripped for sp in _LATEX_SPECIALS):
            offenders.append((hex(cp), out))
    assert not offenders, offenders[:10]


def test_bullets_and_fields_go_through_the_same_chokepoint():
    from resume_tailor.latexutil import clean_bullet

    bullet = clean_bullet(f"Cut latency {FW_BACKSLASH}input{FW_LBRACE}secret{FW_RBRACE}")
    assert r"\input{" not in bullet
    edu = [{"school": f"U{FW_BACKSLASH}openout", "degree": "B.S.", "dates": "2020/2024"}]
    assert r"\openout" not in render._education(edu)
