"""Straight quotes reach TeX as real opening/closing quotes. TeX prints a straight '
as a closing quote, so 'mimeosomes' used to come out with two closing quotes."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor.latexutil import clean_bullet, to_latex  # noqa: E402


@pytest.mark.parametrize("text, tex", [
    ("the query 'mimeosomes' went", "the query `mimeosomes' went"),
    ('said "hello" twice', "said ``hello'' twice"),
    ('"quoted" at the start', "``quoted'' at the start"),
    ("(as 'x')", "(as `x')"),
    ("'single' at the start", "`single' at the start"),
])
def test_opening_quotes_turn_into_tex_openers(text, tex):
    assert to_latex(text) == tex


@pytest.mark.parametrize("text", ["don't stop", "the '90s", "users' data", "it's 5' tall"])
def test_apostrophes_are_left_alone(text):
    assert to_latex(text) == text


def test_curly_quotes_still_map_the_same_way():
    assert to_latex("‘x’ and “y”") == "`x' and ``y''"


def test_a_bullet_keeps_its_quotes_and_its_full_stop():
    assert clean_bullet("Fixed the 'cameo' bug") == "Fixed the `cameo' bug."
