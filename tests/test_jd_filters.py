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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

from score_jobs import requires_clearance  # noqa: E402
from score_jobs import requires_advanced_degree  # noqa: E402
from score_jobs import is_junk_title  # noqa: E402


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
        # --- curly apostrophe: what a posting pasted from a web editor uses --
        # The normalize step here was a no-op for a whole release -- it read
        # `.replace("'", "'")`, both sides U+0027, under a comment claiming it
        # normalized the curly form. So a posting written with U+2019 never
        # matched, and every one of them scored as if it named no degree at all.
        # These four pin the character itself, because retyping the line in an
        # editor that helpfully straightens quotes is exactly how it broke.
        ("Master’s degree is required for this role.", True),
        ("Minimum: Master’s degree in statistics.", True),
        ("Master’s degree preferred but not required.", False),
        ("Bachelor’s or Master’s in CS.", False),
        # --- non-string input ----------------------------------------------
        (None, False),
        (float("nan"), False),
    ],
)
def test_requires_advanced_degree(text, expected):
    assert requires_advanced_degree(text) is expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # --- negatives: bare "architect" titles are a normal early/mid-career
        # AWS-style title, not inherently senior -----------------------------
        ("Solutions Architect", False),
        ("Architect", False),
        ("Cloud Architect", False),
        ("Enterprise Architect", False),
        # --- positives: still caught via the OTHER seniority words; bare
        # "architect" is no junk word of its own ------------------------------
        ("Senior Solutions Architect", True),
        ("Sr. Solutions Architect", True),
        ("Staff Architect", True),
        ("Principal Architect", True),
        ("Chief Architect", True),
        ("Solutions Architect II", True),          # roman numeral pattern
        ("Solutions Architect III", True),
        # --- unrelated senior titles: unaffected ----------------------------
        ("Senior Software Engineer", True),
        ("Engineering Manager", True),
        ("Software Engineer", False),
        # --- non-string input ------------------------------------------------
        (None, False),
        (float("nan"), False),
    ],
)
def test_is_junk_title(title, expected):
    assert is_junk_title(title) is expected


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


# --- Easy Apply filter -------------------------------------------------------
# Clean, non-junk, >=40-char descriptions + benign titles so the OTHER
# mechanical filters stay all-False and filtered_out reflects ONLY Easy Apply.

def _easy_apply_frame(easy_vals):
    n = len(easy_vals)
    return pd.DataFrame(
        {
            "desc": [
                "We are hiring a backend software engineer to build web apps and REST APIs."
            ] * n,
            "title": ["Software Engineer"] * n,
            "is_easy_apply": easy_vals,
        }
    )


def test_add_filter_columns_easy_apply_on_flags_truthy_rows():
    # str(True)->"True"->lower "true" matches; "false"/None/False do not.
    df = _easy_apply_frame([True, False, "true", "false", None])
    out = add_filter_columns(df, "desc", "title", drop_easy_apply=True)
    assert list(out["filter_easy_apply"]) == [True, False, True, False, False]
    # Other filters are all-False for these rows, so filtered_out == the flag.
    assert list(out["filtered_out"]) == [True, False, True, False, False]


def test_add_filter_columns_easy_apply_off_never_flags():
    df = _easy_apply_frame([True, False, "true", "false", None])
    out = add_filter_columns(df, "desc", "title", drop_easy_apply=False)
    assert list(out["filter_easy_apply"]) == [False, False, False, False, False]
    assert list(out["filtered_out"]) == [False, False, False, False, False]


def test_add_filter_columns_easy_apply_missing_column_is_all_false():
    # No is_easy_apply column at all: the column is still added (all-False),
    # no KeyError, so the scored-CSV/master schema stays stable.
    df = pd.DataFrame(
        {
            "desc": ["We are hiring a backend software engineer building web apps and APIs."],
            "title": ["Software Engineer"],
        }
    )
    out = add_filter_columns(df, "desc", "title", drop_easy_apply=True)
    assert "filter_easy_apply" in out.columns
    assert list(out["filter_easy_apply"]) == [False]
    assert list(out["filtered_out"]) == [False]


# --- advanced-degree softeners (real posting wording) --------------------------
@pytest.mark.parametrize("text", [
    "**Requirements**\n\n* BS/ MS degree in any STEM field\n* Passion for machine learning",
    "Required: B.S./M.S. in Computer Science or a related field.",
    "Required: BS or MS degree in Computer Science.",
    "Required: MS/BS in Computer Science.",
    "* No PhD or ML research background required.",
    "* Candidates may substitute the possession of a master's degree from an accredited "
    "college or university for the required experience.",
    "**The Must Haves**\n\n* Education: Master's degree in Computer Science, Computer "
    "Engineering, Electrical Engineering OR Bachelor's degree in Computer Science, "
    "Computer Engineering, Electrical Engineering",
])
def test_a_softened_advanced_degree_is_no_requirement(text):
    assert requires_advanced_degree(text) is False


@pytest.mark.parametrize("text", [
    # A hard requirement still fires.
    "Required: MS degree in Computer Science.",
    "Required: PhD in Statistics.",
    "Master's degree required. Experience with Python or BA tools.",
    "Master's degree in Statistics required for this research role, as the work is "
    "advanced. Experience with R or SAS.",
    "Related work experience may not substitute for the required master's degree.",
    "Must have a PhD; no exceptions.",
    "Minimum: Master's degree required. Experience may substitute for coursework but "
    "not the degree.",
])
def test_a_hard_advanced_degree_still_fires(text):
    assert requires_advanced_degree(text) is True


# --- junk_desc: a senior role the hire works with or grows into ------------------
from score_jobs import is_junk_desc  # noqa: E402


@pytest.mark.parametrize("text", [
    # Hubbell "Master Data Analyst": the HR boilerplate phrase.
    "It is intended to be an accurate reflection of those principal position elements "
    "essential for making decisions related to position performance.",
    # SC&H: the roles an associate grows into.
    "At SC&H consulting, the goal is for Associates to move into a Staff position within "
    "two years at the company and become a Senior Business Analyst within five.",
    # Careerscape "Junior Data Analyst".
    "- Clear path into senior analyst or analytics engineer",
    # JPMorgan: the leaders the hire meets.
    "Strong communication skills with ability to interact with individuals across "
    "departments/functions and with senior level executives",
    "Creates designs, unit tests, and code that is reviewed and approved by a more senior "
    "engineer.",
    "Review results with a senior scientist sharpen the approach",
    "You will be assigned to a Senior Analyst in the People Science practice.",
    "Your Senior Analyst will give you context.",
    "Enter and verify data under the supervision of a Senior Analyst",
    "Support scientific projects under the guidance of a designated senior level data "
    "scientist.",
    "Escalate complex issues to a manager, senior analyst, or subject matter expert.",
    "We are looking for candidates across all levels - junior through senior level AI "
    "Product Engineers.",
    "You can climb the ladder, achieving coveted positions such as Principal Engineer or "
    "Vice President of Software.",
    "**Future Progression:** Senior AI Engineer, AI Architect, Principal Engineer",
    "Frequent interaction with Executive and Senior level management.",
])
def test_a_senior_role_the_hire_works_with_or_grows_into_is_no_junk(text):
    assert is_junk_desc(text) is False


@pytest.mark.parametrize("text", [
    "This is a senior level role on the platform team.",
    "We are seeking a Senior Engineer to own our data platform.",
    "As a Lead Developer you will set the technical direction.",
    "We are seeking a Mid to Senior Level Software Engineer with strong experience.",
    "We are seeking Full Stack Developers at multiple levels, from Senior Developer "
    "through Lead Developer.",
    "This role is suited to a senior engineer who has hands-on skills.",
    "Apply to the Senior Engineer role if you have 5 years of experience.",
    "Managing and providing guidance to senior level staff on areas of expertise",
])
def test_the_job_own_senior_level_is_still_junk(text):
    assert is_junk_desc(text) is True


# --- junk_title: LinkedIn seniority and "Member of Technical Staff" --------------
@pytest.mark.parametrize(("title", "seniority", "expected"), [
    ("Software Engineer II", "Entry level", False),
    ("Data Analyst II", "Entry level", False),
    ("Software Engineer II", " entry level ", False),
    ("Software Engineer II", "Mid-Senior level", True),
    ("Software Engineer II", "Associate", True),
    ("Software Engineer II", None, True),
    ("Software Engineer II", float("nan"), True),
    # Only the bare "II" pattern yields to Entry level.
    ("Senior Software Engineer II", "Entry level", True),
    ("Software Engineer III", "Entry level", True),
    ("Lead Data Analyst", "Entry level", True),
    # "Member of Technical/Data Staff" is a flat title, not seniority.
    ("Member of Technical Staff - Applied AI", "Mid-Senior level", False),
    ("Member of Technical Staff (Software Engineer, Applied AI)", None, False),
    ("Member of Data Staff", None, False),
    ("Senior Member of Technical Staff", None, True),
    ("Principal Member of Technical Staff", None, True),
    ("Staff Software Engineer", None, True),
    ("Member of Technical Staff II", "Mid-Senior level", True),
])
def test_is_junk_title_reads_the_seniority(title, seniority, expected):
    assert is_junk_title(title, seniority) is expected


def test_is_junk_title_without_seniority_is_unchanged():
    assert is_junk_title("Software Engineer II") is True
    assert is_junk_title("Software Engineer") is False


def test_add_filter_columns_reads_the_linkedin_seniority_when_present():
    df = pd.DataFrame({
        "desc": ["We are hiring a software engineer to build web apps and REST APIs."] * 3,
        "title": ["Software Engineer II", "Software Engineer II", "Senior Software Engineer"],
        "job_seniority_level": ["Entry level", "Mid-Senior level", "Entry level"],
    })
    out = add_filter_columns(df, "desc", "title")
    assert list(out["filter_junk_title"]) == [False, True, True]
    assert list(out["filtered_out"]) == [False, True, True]


def test_add_filter_columns_without_a_seniority_column_reads_the_title_alone():
    df = pd.DataFrame({
        "desc": ["We are hiring a software engineer to build web apps and REST APIs."],
        "title": ["Software Engineer II"],
    })
    out = add_filter_columns(df, "desc", "title")
    assert list(out["filter_junk_title"]) == [True]
