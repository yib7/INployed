"""High-precision JD pre-filters in score_jobs.py.

These drop jobs a 0-experience applicant provably cannot get BEFORE any Gemini
call. Precision is the whole game: a kept-junk job costs one cheap flash-lite
call; a wrongly-dropped good job is invisible and unrecoverable. So negative
cases ("clearance is a plus", "Master's preferred", "BS or MS required") must
NOT trip the filters.

Run:  python -m pytest tests/test_jd_filters.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from score_jobs import requires_clearance  # noqa: E402
from score_jobs import requires_advanced_degree  # noqa: E402


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # --- positives: genuinely clearance-gated ---------------------------
        ("Active TS/SCI clearance required for this role.", True),
        ("Must be able to obtain a Secret clearance.", True),
        ("Applicants must possess a top secret clearance.", True),
        ("This position requires a polygraph.", True),
        ("Ability to obtain a clearance is necessary.", True),
        ("Requires an active Secret clearance.", True),
        # --- negatives: must NOT trip --------------------------------------
        ("No clearance required for this position.", False),
        ("Security clearance is not required.", False),
        ("We build software for a security-cleared facility; tours available.", False),
        ("Backend engineer building web apps and REST APIs.", False),
        ("No polygraph required.", False),
        ("A polygraph is not required for this role.", False),
        ("You will collaborate with TS/SCI clearance holders on the team.", False),
        # --- non-string input ----------------------------------------------
        (None, False),
        (float("nan"), False),
        (12345, False),
    ],
)
def test_requires_clearance(text, expected):
    assert requires_clearance(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # --- positives: hard Master's/PhD requirement ----------------------
        ("PhD required in machine learning.", True),
        ("Master's degree is required for this role.", True),
        ("Requires an MS in Computer Science.", True),
        ("Minimum: Master's degree in statistics.", True),
        ("Position requiring a Master's degree; must have it to apply.", True),
        ("An advanced degree is required.", True),
        ("MBA required for this management-track role.", True),
        # --- negatives: must NOT trip (preferred / equivalent / bachelor) ---
        ("Help our MBA students; a CS background is required.", False),
        ("Our MBA alumni network is strong; SQL skills required.", False),
        ("Master's degree preferred but not required.", False),
        ("PhD a plus.", False),
        ("Master's or equivalent experience.", False),
        ("Bachelor's or Master's in CS.", False),
        ("BS or MS required.", False),                 # a bachelor's suffices
        ("Bachelor's degree required.", False),        # not an advanced degree
        ("Graduate from a 4-year program.", False),    # 'graduate' != 'graduate degree'
        ("Master's degree is a plus; we value hands-on experience.", False),
        # --- non-string input ----------------------------------------------
        (None, False),
        (float("nan"), False),
    ],
)
def test_requires_advanced_degree(text, expected):
    assert requires_advanced_degree(text) is expected


import pandas as pd  # noqa: E402

from score_jobs import add_filter_columns  # noqa: E402


def test_add_filter_columns_flags_clearance_and_degree():
    df = pd.DataFrame(
        {
            "desc": [
                "We are hiring a software engineer to build web apps and REST APIs.",
                "Backend engineer role; an active TS/SCI clearance is required here.",
                "Data role requiring a Master's degree in statistics; must have it.",
                "Entry-level analyst; a Master's degree is preferred but not required.",
            ],
            "title": [
                "Software Engineer",
                "Backend Engineer",
                "Data Scientist",
                "Analyst",
            ],
        }
    )
    out = add_filter_columns(df, "desc", "title")
    assert list(out["filter_clearance"]) == [False, True, False, False]
    assert list(out["filter_degree"]) == [False, False, True, False]
    assert list(out["filtered_out"]) == [False, True, True, False]
