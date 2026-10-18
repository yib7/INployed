"""resume_dates: an optional free-text date an entry shows on the PDF ("Summer 2025")
in place of its exact `dates`, which application forms keep using."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import render  # noqa: E402

_SEL = {"experience": [{"name": "Acme Analytics", "groups": [["a1"]]}],
        "projects": [{"name": "Proj", "groups": [["p1"]]}],
        "leadership": [{"name": "Club", "groups": [["l1"]]}]}
_BULLETS = {"a1": "Built a thing", "p1": "Shipped a tool", "l1": "Led a team"}


def _meta(monkeypatch, **extra):
    blocks = {
        "experience": [{"name": "Acme Analytics", "title": "Analyst Intern", "location": "Springfield",
                        "dates": "2025-06 / 2025-07", **extra}],
        "projects": [{"name": "Proj", "dates": "2026-05 / 2026-08", **extra}],
        "leadership": [{"name": "Club", "dates": "2024-03 / 2026-05", **extra}],
    }
    monkeypatch.setattr(render.assets, "blocks", lambda: blocks)


def test_without_resume_dates_the_exact_months_print(monkeypatch):
    _meta(monkeypatch)
    tex = render._experience(_SEL, _BULLETS)
    assert "June 2025 -- July 2025" in tex


def test_resume_dates_replaces_the_months_on_every_section(monkeypatch):
    _meta(monkeypatch, resume_dates="Summer 2025")
    for section in (render._experience, render._leadership):
        tex = section(_SEL, _BULLETS)
        assert "Summer 2025" in tex
        assert "June 2025" not in tex


def test_resume_dates_still_formats_a_range(monkeypatch):
    _meta(monkeypatch, resume_dates="2025-06 / 2025-08")
    assert "June 2025 -- August 2025" in render._experience(_SEL, _BULLETS)


def test_a_blank_resume_dates_falls_back_to_dates(monkeypatch):
    _meta(monkeypatch, resume_dates="  ")
    assert "June 2025 -- July 2025" in render._experience(_SEL, _BULLETS)


def test_education_honors_resume_dates_too():
    tex = render._education([{"school": "U", "degree": "BS", "dates": "2021-08 / 2025-05",
                              "resume_dates": "Class of 2025"}])
    assert "Class of 2025" in tex and "August 2021" not in tex


def test_resume_dates_is_escaped_for_latex(monkeypatch):
    _meta(monkeypatch, resume_dates="Summer 2025 & Fall 2025")
    assert "Summer 2025 \\& Fall 2025" in render._experience(_SEL, _BULLETS)


def test_blocks_carries_resume_dates_from_the_master(monkeypatch):
    master = {
        "experience": [{"org": "Acme Analytics", "title": "Intern", "dates": "2025-06 / 2025-07",
                        "resume_dates": "Summer 2025", "achievements": [{"id": "a1"}]}],
        "projects": [],
        "leadership": [{"org": "Club", "dates": "2024-03 / 2026-05",
                        "resume_dates": "2024 -- 2026", "achievements": [{"id": "l1"}]}],
    }
    render.assets.blocks.cache_clear()
    monkeypatch.setattr(render.assets, "load_master", lambda: master)
    try:
        assert "Summer 2025" in render._experience(_SEL, _BULLETS)
        assert "2024 -- 2026" in render._leadership(_SEL, _BULLETS)
    finally:
        render.assets.blocks.cache_clear()


# The dates box is free text with no validation, so the shapes people type by
# hand have to print as dates, not as raw ISO text or a range from 06 to 2025.

@pytest.mark.parametrize("typed, printed", [
    ("2025-6", "June 2025"),                                  # one-digit month
    ("2025-6 / 2025-8", "June 2025 -- August 2025"),
    ("2025-06 – 2025-08", "June 2025 -- August 2025"),  # an en dash range
    ("2025-06 - 2025-08", "June 2025 -- August 2025"),
    ("2025-06--2025-08", "June 2025 -- August 2025"),
    ("06/2025", "June 2025"),                                 # MM/YYYY is one month
    ("6/2025", "June 2025"),
    ("2023 - Present", "2023 -- Present"),
    ("Summer 2025", "Summer 2025"),
    ("2024 -- 2026", "2024 -- 2026"),
])
def test_hand_typed_dates_print_as_dates(typed, printed):
    from resume_tailor.latexutil import fmt_dates
    assert fmt_dates(typed) == printed
