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
        # --- non-string input ----------------------------------------------
        (None, False),
        (float("nan"), False),
        (12345, False),
    ],
)
def test_requires_clearance(text, expected):
    assert requires_clearance(text) is expected
