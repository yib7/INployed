"""Regression tests for the experience-years filter in score_jobs.py.

min_required_years() is regex-driven and decides which jobs get scrapped before
any LLM sees them. The semantics it must keep (see the
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

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


# --- a years figure under a preferred-type section heading ---------------------
# Real postings from the false-negative audit: the only years figure sits under a
# "Preferred" heading, so the role asks for none. The heading can be a markdown
# "#" line, a **bold** line (any number of stars), a short line ending in ":", or
# a plain line made only of heading words.
PREFERRED_SECTION_CASES = [
    ("Requirements:\n\n● Solid knowledge of vector search concepts.\n\n"
     "Preferred Qualifications:\n\n● Master’s Degree in Computer Science.\n\n"
     "● 1+ year professional experience in Python web application development "
     "with either Flask or Django."),
    "**Preferred Skills And Experience**\n\n* 2+ years of relevant work experience",
    ("****Minimum Qualifications****\n\n* High School Diploma or equivalent\n\n"
     "****Preferred Skills/Experience****\n\n"
     "* 1 year of experience in software development or a related field"),
    ("Nice to Have Skills\n\n* Experience within retail.\n\n"
     "Preferred Education and Experience\n\n"
     "* 2 to 5 years of hands-on experience in Business Intelligence, Data Analytics, "
     "or related roles.\n\nOther Requirements\n\n* Willingness to work onsite."),
    "**Qualifications Desired**\n\n* Minimum 1-2 years’ experience in related field",
    ("**Minimum qualifications:**\n\n* Bachelor's degree in Statistics.\n\n"
     "**Preferred qualifications:**\n\n"
     "* 2 years of experience using analytics to solve product or business problems"),
    "## Nice to have\n- 3+ years of experience with Kubernetes",
    "**Bonus:** 2+ years of experience shipping mobile apps",
]


@pytest.mark.parametrize("text", PREFERRED_SECTION_CASES)
def test_a_years_figure_under_a_preferred_heading_is_no_requirement(text):
    assert min_required_years(text) is None
    assert has_too_many_years(text) is False


@pytest.mark.parametrize(("text", "expected"), [
    # A required-type heading after the preferred one ends the preferred section.
    ("Minimum Qualifications:\n- 3+ years of experience required\n"
     "Preferred Qualifications:\n- 5+ years of experience", 3),
    ("**Preferred Qualifications**\n* Master's degree\n"
     "**Required Qualifications**\n* 2+ years of SQL", 2),
    # So does any other heading: the nearest heading decides.
    ("**Preferred Qualifications**\n* Master's degree\n**About the team**\n"
     "* 2+ years of SQL", 2),
    # A short plain line that is not a heading does not open a section.
    ("Requirements\nBachelor's degree\nPython preferred\n3+ years of experience", 3),
    # A line under a preferred heading that says it is required still counts.
    ("Preferred Qualifications:\n- 3+ years of experience is required", 3),
    # Basic qualifications are a requirement.
    ("**Basic Qualifications**\n* 2+ years of Python\n**Preferred Qualifications**\n"
     "* 4+ years of Python", 2),
])
def test_a_years_figure_outside_a_preferred_section_still_counts(text, expected):
    assert min_required_years(text) == expected


# --- a years figure the same clause marks as preferred ---------------------------
@pytest.mark.parametrize("text", [
    "* At least 1 year experience in Business Analytics or related field preferred",
    "* 1-2 years of experience in a People Analyst role is preferred.",
    "* 2+ years business analysis experience (Preferred)",
    "* 2-4 years of relevant experience is preferred.",
    "* 1-3 years of professional experience preferred, but not required.",
    ("* Experience in Power BI, Tableau or similar software preferred."
     "1-2 years of related experience preferred."),
    "* 3+ years of Kubernetes experience a plus",
    "* Preferred: 2 years of experience with dbt",
])
def test_a_years_figure_marked_preferred_on_its_own_line_is_no_requirement(text):
    assert min_required_years(text) is None


@pytest.mark.parametrize(("text", "expected"), [
    ("5+ years preferred; 2+ years required", 2),
    ("* 3+ years of experience required, Master's preferred", 3),
    # The comma hands "a plus" to the next item, not to the years figure.
    ("* 2+ years of SQL, Tableau a plus", 2),
    ("* Minimum 2 years of experience preferred", 2),
    ("* Must have 2+ years of experience; healthcare preferred", 2),
])
def test_a_preference_word_that_does_not_govern_the_years_figure_leaves_it(text, expected):
    assert min_required_years(text) == expected


def test_a_decimal_years_figure_reads_its_whole_number():
    assert min_required_years("0–2.5 years of professional experience") == 0
    assert min_required_years("1.5+ years of professional experience") == 1


# --- age wording ----------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "**Minimum Requirements**\n\n* 18 years of age or older\n* Have a High School Diploma",
    "* Must be at least 21 years of age.",
    "* At least 18 years old and legally authorized to work in the United States",
    "* • Minimum age of 21 years",
    "* Minimum age 18 years.",
])
def test_an_age_rule_is_no_experience_requirement(text):
    assert min_required_years(text) is None


def test_an_age_rule_beside_a_real_requirement_leaves_the_requirement():
    text = "**Minimum Requirements**\n* 18 years of age or older\n* 2+ years of experience"
    assert min_required_years(text) == 2


# --- company history ------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "Backed by 45+ years of success, Uline offers opportunities to grow your career.",
    ("A leading employee benefits firm with over 35 years of experience, serving nearly "
     "5,000 clients across the United States, is seeking a Junior Data Engineer."),
    "Over the last 20+ years, our company has been honored with multiple employer awards.",
    ("Our multidisciplinary teams bring over 30 years of experience helping business "
     "leaders across key industries."),
    ("Western National is a private mutual insurance company with over 120 years of "
     "experience serving customers."),
])
def test_company_history_is_no_requirement(text):
    assert min_required_years(text) is None


@pytest.mark.parametrize(("text", "expected"), [
    ("* 3+ years of experience serving clients in a consulting role", 3),
    ("* 5+ years of experience supporting Department of Defense organizations", 5),
    ("* Has worked as an analyst for more than 2 years; experience required", 2),
])
def test_experience_wording_without_a_company_subject_still_counts(text, expected):
    assert min_required_years(text) == expected


# --- an upper bound only: "< 1 year", "up to 2 years" ----------------------------
@pytest.mark.parametrize("text", [
    ("Roles at this level typically require a university / college degree with "
     "< 1 year of professional experience"),
    "Less than 1 year of professional experience required.",
    "Under 1 year of experience required.",
    "Up to 2 years of experience in data analysis.",
])
def test_an_upper_bound_alone_is_a_zero_floor(text):
    assert min_required_years(text) == 0
    assert has_too_many_years(text) is False


# --- years offered as an alternative to a degree ---------------------------------
@pytest.mark.parametrize("text", [
    ("* Bachelor's degree in computer science, engineering, math, or STEM discipline "
     "OR 2+ years of experience in software development"),
    ("* Bachelor's degree OR High school / GED and at least 3 years of experience in "
     "Finance, IT, Strategy, Analytics, and/or Operations"),
    ("**Basic Qualifications**Bachelors degree in Computer Science, Engineering, Data "
     "Science or related field OR  \n  \nDiploma with 2+ years of experience in "
     "Computer Science, Engineering, Data Science, or related field"),
    "* Bachelor's degree in Computer Science or 4 years of equivalent experience",
    "* 4 years of relevant experience may be substituted in lieu of a degree.",
    "* In lieu of a degree, 4 years of relevant experience.",
    ("4. Requirements:\n\n• Educational background meeting one of the following "
     "criteria:\n\n* Bachelor’s degree in a relevant field\n* Associate degree plus 1 "
     "year of relevant professional experience\n* High school diploma (or equivalent) "
     "plus 2 years of relevant professional experience"),
])
def test_years_offered_instead_of_a_degree_are_no_requirement(text):
    assert min_required_years(text) is None


@pytest.mark.parametrize(("text", "expected"), [
    ("* Bachelor's degree and 2+ years of experience", 2),
    ("* Bachelor's degree with 2 years of experience", 2),
    ("* Bachelor's degree in CS or a related field and 2+ years of experience", 2),
    ("* Bachelor's degree in CS or a related field.\n* 2+ years of experience", 2),
    ("* High school diploma and 4 years of experience required", 4),
    ("* Bachelor's degree and at least 2 years of experience in Finance OR High school "
     "/ GED and at least 5 years of experience", 2),
])
def test_years_required_alongside_a_degree_still_count(text, expected):
    assert min_required_years(text) == expected


# --- a stated "Minimum Years Of Work Experience" field ---------------------------
def test_a_stated_minimum_years_field_still_counts_beside_a_preferred_figure():
    text = ("**Minimum Years Of Work Experience**\n\n* 3\n\n**Preferred Qualifications**\n\n"
            "* 4+ years of experience in business intelligence and analytics.")
    assert min_required_years(text) == 3


def test_a_stated_zero_minimum_years_field_is_a_zero_floor():
    text = ("**Minimum Years Of Work Experience**\n\n* 0\n\n**Preferred Qualifications**\n\n"
            "* 3+ years of experience in business intelligence and analytics.")
    assert min_required_years(text) == 0


def test_the_field_alone_reads_as_before():
    # Only a years figure the plain reading counts turns the field on, so a posting
    # the old rule kept is never newly dropped.
    assert min_required_years("**Minimum Years Of Work Experience**\n\n* 3") is None


def test_a_zero_floor_is_never_set_aside():
    # The "0 years ... with a PhD" alternative keeps its 0, as it always has.
    text = ("* 5+ years of relevant work experience with a Bachelor's Degree or at least 2 "
            "years of work experience with an Advanced degree or 0 years of work experience "
            "with a PhD")
    assert min_required_years(text) == 0


# --- degree alternatives stay narrow ---------------------------------------------
def test_years_beside_a_degree_still_count_when_in_lieu_names_another_figure():
    text = ("Bachelor's degree and 5 years of relevant work experience, or in lieu of a "
            "degree, a combined minimum of 9 years of relevant experience.")
    assert min_required_years(text) == 5


def test_an_in_lieu_aside_does_not_cancel_the_degree_and_years_figure():
    text = ("Bachelor's degree and 4-12 years of experience (additional experience may be "
            "considered in lieu of degree).")
    assert min_required_years(text) == 4


def test_an_or_on_an_earlier_bullet_does_not_reach_the_next_bullet():
    text = ("* Bachelor's degree in Computer Science (or a high school diploma / GED with a "
            "minimum of 4 years of experience)\n* Minimum of 5 years of data engineering "
            "experience")
    # The 4 is offered instead of the degree; the next bullet's 5 still counts.
    assert min_required_years(text) == 5


def test_an_or_between_two_years_figures_is_no_degree_alternative():
    text = ("* Bachelor's degree required\n* 3+ months of retail store experience or 1 year "
            "of customer service experience required")
    assert min_required_years(text) == 1


def test_not_less_than_is_a_floor_not_an_upper_bound():
    assert min_required_years("**Experience: 10**+ years (Not less than 10 years)") == 10
    assert min_required_years("Experience: no less than 3 years required") == 3


def test_a_pay_bonus_in_a_title_line_is_no_preferred_heading():
    text = ("**Full Stack Developer | Boston, MA | $120,000 to $165,000 base + 10-15% bonus**"
            "\n\nThis is an excellent opportunity for a developer with 4+ years of experience.")
    assert min_required_years(text) == 4
    assert min_required_years("**Bonus Points**\n\n* 4+ years of experience with Go") is None


# --- review fixes: field labels, plain headings, excluded-below bounds -------------
@pytest.mark.parametrize(("text", "expected"), [
    ("**Preferred Location:** Remote\n\nWe are hiring a data engineer. You have 3+ years "
     "of experience with Python.", 3),
    ("**Bonus:** 10% annual target\n\nYou bring 3+ years of experience in Java.", 3),
    ("Bonus Points\n- AWS cert\nWhat You'll Need\n- 3+ years of experience", 3),
    ("Nice to Have\n- AWS\nThe Team\nWe need 3+ years of experience in Go", 3),
    ("**Preferred Qualifications:**\n\n* 5+ years of Go", None),
    ("Candidates with less than 2 years of experience will not be considered. 3+ years "
     "of experience required.", 2),
    ("Applicants with fewer than 3 years of experience will not be considered", 3),
    ("This is a contract role for up to 2 years. Requires 3+ years of experience.", 2),
    ("Up to 2 years of experience in SQL", 0),
])
def test_review_shapes_keep_their_requirement(text, expected):
    assert min_required_years(text) == expected


def test_a_figure_set_aside_never_newly_drops_a_job_at_a_higher_threshold(monkeypatch):
    import score_jobs
    monkeypatch.setattr(score_jobs, "MIN_FILTER_YEARS", 3)
    for text in ("Experience: 2 years preferred. Must have 5+ years of Java experience.",
                 "Bachelor's degree or 2+ years of experience. 4+ years of experience "
                 "with Linux required.",
                 "Nice to have: 1-2 years of Go. Minimum Years Of Work Experience: 4"):
        assert score_jobs.has_too_many_years(text) is False, text
    assert score_jobs.has_too_many_years("Must have 5+ years of Java experience.") is True
