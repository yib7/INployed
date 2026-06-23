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
