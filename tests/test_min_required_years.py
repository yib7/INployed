"""Regression tests for the experience-years filter in score_jobs.py.

min_required_years() is load-bearing and regex-driven: it decides which jobs
get scrapped before any LLM sees them. The semantics it must keep (see the
function docstring in score_jobs.py):

  * a RANGE ("1-3 years", "1 to 3 years") or open-ended "N+ years" is a
    requirement ON SIGHT and contributes its LOWER bound;
  * a BARE single number ("5 years") counts only with a requirement cue nearby;
  * marketing/tenure wrappers ("20+ years of excellence", "founded 30 years
    ago", "5 years of service") never count, even in range / "N+" form;
  * combined with MIN_FILTER_YEARS=1, only a 0-year floor (or no detected
    requirement) survives.

Run:  python -m pytest tests/ -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from score_jobs import has_too_many_years, min_required_years  # noqa: E402


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # --- ranges: requirement on sight, lower bound wins -------------------
        ("0-2 years of experience with Python", 0),
        ("1-2 years working with dashboards", 1),
        ("1 to 3 years in an analytical role", 1),
        ("10-15 yrs in software", 10),
        ("2–4 years of SQL", 2),                      # en dash
        # the Ford case: range far away from the word "experience" still counts
        ("1 to 3 years building reports, pipelines, and stakeholder decks", 1),
        # --- open-ended N+: requirement on sight ------------------------------
        ("1+ years required", 1),
        ("3+ years of Python", 3),
        ("0+ years — new grads welcome", 0),
        # --- bare number: needs a requirement cue nearby ----------------------
        ("minimum of 2 years", 2),
        ("at least 1 year", 1),
        ("must have 4 yrs", 4),
        ("5 years of professional experience required", 5),
        ("a proven track record over 3 years", 3),
        ("5 years", None),                                  # bare, no cue
        ("celebrating 90 years", None),                     # company age, no cue
        # cue exists but outside the ±40/45-char context window
        ("5 years " + "x" * 60 + " experience required", None),
        # --- marketing/tenure wrappers: never a requirement -------------------
        ("backed by 20+ years of excellence", None),
        ("founded 30 years ago", None),
        ("after 5 years of service you get a sabbatical", None),
        ("revenue doubled over the past 5 years", None),
        ("30+ years in business", None),
        ("a 75 year history of innovation", None),
        # --- multiple mentions: smallest minimum wins --------------------------
        ("5+ years preferred; 2+ years required", 2),
        ("0-1 years for juniors, 3+ years for seniors", 0),
        # --- things the regex deliberately does not catch ----------------------
        ("two years of experience", None),                  # spelled-out numbers
        ("no experience required", None),
        # --- non-string input ---------------------------------------------------
        (None, None),
        (float("nan"), None),
        (12345, None),
    ],
)
def test_min_required_years(text, expected):
    assert min_required_years(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # only a 0-year floor (or no detected requirement) survives the filter
        ("0-2 years of experience", False),
        ("entry level, no experience needed", False),
        ("1+ years of experience", True),
        ("1-2 years of experience", True),
        ("minimum 2 years experience", True),
        ("20+ years of excellence serving clients", False),
    ],
)
def test_has_too_many_years(text, expected):
    assert has_too_many_years(text) is expected
