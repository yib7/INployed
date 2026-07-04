"""Tests for résumé bullet length formatting + LaTeX symbols (PLAN stage 5).

And: unicode math glyphs emitted by the model become proper LaTeX so they render.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor.latexutil import clean_bullet  # noqa: E402


def test_math_glyphs_converted_to_latex():
    out = clean_bullet("improved accuracy to ≥95% and cut latency ×3")
    assert r"$\ge$" in out
    assert r"$\times$" in out
    assert "≥" not in out and "×" not in out


def test_approx_tilde_still_converts():
    out = clean_bullet("processed ~2M records/day")
    assert r"$\sim$" in out
    assert "~" not in out.replace(r"$\sim$", "")


def test_ascii_comparison_digraphs_converted_to_latex():
    # The rephrase prompt tells the model to write ASCII '>=' / '<=' and promises
    # conversion to math notation. The Babel Street run printed a literal
    # '\textgreater{}= 95%' because only the unicode glyphs were mapped.
    out = clean_bullet("reached >= 95% accuracy with <= 2s latency")
    assert r"$\ge$" in out and r"$\le$" in out
    assert "textgreater" not in out and "textless" not in out


def test_plain_text_bullet_unchanged_except_period():
    out = clean_bullet("Built a data pipeline")
    assert out == "Built a data pipeline."


def test_unicode_minus_sign_is_sanitized():
    # The real bug: model emitted U+2212 (MINUS SIGN), not an ASCII hyphen.
    # pdflatex has no default mapping for it -> fatal compile error.
    out = clean_bullet("coefficient moved from +0.153 to −0.158 (p < 0.05)")
    assert "−" not in out
    assert "-0.158" in out  # rendered as an ASCII hyphen-minus


def test_clean_bullet_output_is_always_ascii():
    # Catch-all guarantee: NO non-ASCII glyph may survive, no matter how exotic,
    # so an unlisted character can never again kill the LaTeX compile.
    junk = (
        "alpha ☃ beta \U0001F600 gamma ≡ delta − epsilon "
        " thin nbsp résumé café"
    )
    out = clean_bullet(junk)
    assert out.isascii(), [hex(ord(c)) for c in out if ord(c) > 127]


def test_accented_letters_folded_to_ascii():
    out = clean_bullet("managed the résumé pipeline in Montréal")
    assert out.isascii()
    assert "resume" in out and "Montreal" in out


def test_known_math_glyphs_still_render_and_stay_ascii():
    out = clean_bullet("improved accuracy to ≥95% and cut latency ×3")
    assert r"$\ge$" in out and r"$\times$" in out
    assert out.isascii()


def test_to_latex_skills_path_is_ascii():
    from resume_tailor.latexutil import to_latex
    out = to_latex("C++, Rédis, ≥ 99.9% uptime, A/B−testing")
    assert out.isascii()
    assert r"\%" in out  # % still escaped for LaTeX
