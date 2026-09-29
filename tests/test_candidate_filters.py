"""Cycle 21, Tasks 6 and 7: the mechanical filters follow the candidate profile.

Task 6: a candidate who has finished school gets intern and co-op titles dropped
before any scorer call (`filter_internship`). A candidate still in school keeps
them.

Task 7: the clearance filter is level-aware. A clearance the candidate holds
passes; a clearance the employer sponsors passes when the candidate is open to
sponsorship, unless the same sentence refuses to sponsor it. Only a sponsorship
that governs the clearance counts, so visa and work-authorization sponsorship
language never changes the verdict. Under the default profile (no clearance,
closed to sponsorship) it blocks exactly what `requires_clearance` blocks.

Every test runs against the sandboxed scoring constants that conftest's
_hermetic_repo_data rebinds, so the author's scoring_config.json never leaks in.
"""
import asyncio
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import score_jobs as sj  # noqa: E402

TODAY = date(2026, 9, 29)

FINISHED = sj.candidate_profile(status="finished", graduation="", today=TODAY)
UNDERGRAD = sj.candidate_profile(status="undergrad", graduation="May 2027", today=TODAY)
GRAD = sj.candidate_profile(status="grad", graduation="May 2027", today=TODAY)

# Clean, non-junk, 40+ character description so the other filters stay quiet.
CLEAN_DESC = "We are hiring a backend software engineer to build web apps and REST APIs."


# --- Task 6: the title detector --------------------------------------------------

@pytest.mark.parametrize("title", [
    "Data Science Intern",
    "Intern - Data Analyst",
    "Software Engineering Internship",
    "Data Engineer (Co-op)",
    "Co-Op Analyst",
    "Summer Interns",
    "Data Analyst Coop",
    "DATA ANALYST INTERNSHIPS",
    "Analytics Co-ops",
    "Data Analyst Co op",
    "Data Analyst Co\u2013op",   # en dash
    "Data Analyst Co\u2011op",   # non-breaking hyphen
    "Data Analyst Co\u2010op",   # hyphen
    "Data Analyst Co\u2014Op",   # em dash
    "Data Analyst Co ops",
    "Data Analyst Co\u2212op",   # minus sign
    "Data Analyst Co\u00adop",   # soft hyphen
    "Data Analyst Co/op",
    "Data Analyst Co.op",
    # A role word before the token, or apart from it, names the student's team.
    "Product Manager Intern",
    "Project Manager Intern",
    "Marketing Coordinator Intern",
    "Recruiting Intern",
    "Talent Recruiter Intern",
    "Talent Recruiter Intern - Summer 2026",
    "Program Manager, Summer Intern",
    "Sales Co-op (Account Manager)",
    "Data Analyst Intern (Reports to Director of Analytics)",
    "Business Analyst Co-op, Campus Recruiting",
    "Analyst - Manager Track Co-op",
    # A trailing "- Interns" reads as a student posting, even with a role word before it.
    "University Recruiting Coordinator - Interns",
    # "program" counts only when it follows the token directly.
    "Intern - Program Analyst",
    "Data Analyst Intern (Program Management Office)",
    # A bare singular "program" after the token names the student posting.
    "Software Engineer Intern Program",
    "Summer 2027 Internship Program",
    "Accounting Co-op Program",
    "Data Science Intern Program - Summer 2027",
    # A role word before the token names the team, so a recruiter title with
    # "(Internship Program)" reads as a student posting too.
    "Campus Recruiter (Internship Program)",
])
def test_an_intern_or_coop_title_is_an_internship_title(title):
    assert sj.is_internship_title(title) is True


@pytest.mark.parametrize("title", [
    "Internal Audit Analyst",
    "International Data Analyst",
    "Internet Engineer",
    "Data Engineer 1",
    "Cooperative Systems Analyst",
    "Data Analyst",
    "Co Operations Analyst",
    "",
])
def test_a_title_that_only_contains_the_letters_is_not_an_internship_title(title):
    assert sj.is_internship_title(title) is False


@pytest.mark.parametrize("title", [
    "Internship Program Manager",
    "Intern Recruiter",
    "Director of Intern Programs",
    "Co-op Program Coordinator",
    "Manager, Intern Experience",
    "Internship Recruiting Lead (Campus Recruiters)",
    "Intern Program Lead",
    "Head of Internships",
    "Coordinator of the Co-op Program",
    # The token followed by "programs", or by "program" and a staff role word,
    # names the program.
    "Program Manager - Intern Programs",
    "Internship Program Specialist",
    "Internship Program Associate",
    "VP, Intern Programs",
    "Co-op Program Specialist",
])
def test_a_title_that_runs_the_program_is_not_an_internship_title(title):
    """A manager, recruiter, coordinator or director of interns is a full-time job:
    the role word follows the intern token, or leads to it with "of", or the token
    is followed by "program"."""
    assert sj.is_internship_title(title) is False


@pytest.mark.parametrize("value", [None, float("nan"), 12345, pd.NA])
def test_a_non_string_title_is_not_an_internship_title(value):
    assert sj.is_internship_title(value) is False


# --- Task 6: the filter column ---------------------------------------------------

def _title_frame(titles, desc=CLEAN_DESC):
    return pd.DataFrame({"desc": [desc] * len(titles), "title": titles})


TITLES = ["Data Science Intern", "Data Engineer 1", "Data Engineer (Co-op)",
          "Internal Audit Analyst"]


def test_finished_school_drops_intern_and_coop_titles():
    out = sj.add_filter_columns(_title_frame(TITLES), "desc", "title", profile=FINISHED)
    assert list(out["filter_internship"]) == [True, False, True, False]
    assert list(out["filtered_out"]) == [True, False, True, False]


@pytest.mark.parametrize("profile", [UNDERGRAD, GRAD], ids=["undergrad", "grad"])
def test_a_student_keeps_intern_and_coop_titles(profile):
    out = sj.add_filter_columns(_title_frame(TITLES), "desc", "title", profile=profile)
    assert list(out["filter_internship"]) == [False, False, False, False]
    assert list(out["filtered_out"]) == [False, False, False, False]


def test_the_default_profile_is_finished_so_it_drops_intern_titles():
    out = sj.add_filter_columns(_title_frame(TITLES), "desc", "title")
    assert list(out["filter_internship"]) == [True, False, True, False]


def test_a_missing_profile_reads_the_configured_constants_at_call_time(monkeypatch):
    monkeypatch.setattr(sj, "EDUCATION_STATUS", "In school: undergraduate")
    monkeypatch.setattr(sj, "GRADUATION_MONTH", "")
    out = sj.add_filter_columns(_title_frame(TITLES), "desc", "title")
    assert list(out["filter_internship"]) == [False, False, False, False]


def test_the_column_is_added_all_false_when_there_is_no_title_column():
    df = pd.DataFrame({"desc": [CLEAN_DESC, CLEAN_DESC]})
    out = sj.add_filter_columns(df, "desc", None, profile=FINISHED)
    assert "filter_internship" in out.columns
    assert list(out["filter_internship"]) == [False, False]
    assert list(out["filtered_out"]) == [False, False]


def test_a_blank_title_cell_is_never_flagged():
    out = sj.add_filter_columns(_title_frame([None, float("nan"), ""]), "desc", "title",
                                profile=FINISHED)
    assert list(out["filter_internship"]) == [False, False, False]


def test_the_internship_filter_is_the_only_reason_such_a_row_is_dropped():
    out = sj.add_filter_columns(_title_frame(["Data Analyst Intern"]), "desc", "title",
                                profile=FINISHED)
    row = out.iloc[0]
    assert bool(row["filter_internship"]) is True
    for other in ("filter_junk_title", "filter_junk_desc", "filter_too_many_years",
                  "filter_clearance", "filter_degree", "filter_easy_apply"):
        assert bool(row[other]) is False, other
    assert bool(row["filtered_out"]) is True


def test_the_easy_apply_filter_is_independent_of_the_internship_filter():
    df = _title_frame(["Data Analyst Intern", "Data Analyst"])
    df["is_easy_apply"] = [False, True]
    out = sj.add_filter_columns(df, "desc", "title", drop_easy_apply=True, profile=FINISHED)
    assert list(out["filter_internship"]) == [True, False]
    assert list(out["filter_easy_apply"]) == [False, True]
    assert list(out["filtered_out"]) == [True, True]


def test_the_column_is_a_plain_bool_column():
    out = sj.add_filter_columns(_title_frame(TITLES), "desc", "title", profile=FINISHED)
    assert out["filter_internship"].dtype == bool
    out = sj.add_filter_columns(_title_frame(TITLES), "desc", "title", profile=UNDERGRAD)
    assert out["filter_internship"].dtype == bool


# --- Task 6: SCORE_COLS and the master fold --------------------------------------

def test_score_cols_carry_filter_internship_right_after_filter_easy_apply():
    cols = sj.SCORE_COLS
    assert cols.index("filter_internship") == cols.index("filter_easy_apply") + 1
    assert cols.count("filter_internship") == 1


def test_the_master_fold_adds_the_new_column_to_an_older_master(tmp_path, monkeypatch):
    """A master written before this column existed gets it on the first fold."""
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame({"job_posting_id": ["1", "2", "3"], "job_title": ["a", "b", "c"],
                  "filtered_out": ["False", "False", "False"]}).to_csv(master, index=False)
    monkeypatch.setattr(sj, "MASTER_CSV", master)
    monkeypatch.setattr(sj, "CHUNK", 2)

    scored = pd.DataFrame({"job_posting_id": ["1", "2"],
                           "filter_internship": [True, False],
                           "filtered_out": [True, False]})
    assert scored["filter_internship"].dtype == bool
    sj.update_master_scores(scored)

    out = pd.read_csv(master, dtype={"job_posting_id": str}).set_index("job_posting_id")
    assert "filter_internship" in out.columns
    assert str(out.loc["1", "filter_internship"]) == "True"
    assert str(out.loc["2", "filter_internship"]) == "False"
    assert pd.isna(out.loc["3", "filter_internship"])       # not in this run: untouched


def test_a_bool_internship_column_folds_into_an_all_empty_float64_master_chunk(
        tmp_path, monkeypatch):
    """The chunked path reads an all-empty column as float64; pandas 3 refuses to
    write bool into it, so the fold widens the column first (mirrors
    tests/test_score_jobs_chunked.py::test_bool_filter_column_into_float64_master_does_not_raise)."""
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame({
        "job_posting_id": ["1", "2", "3"],
        "job_title": ["a", "b", "c"],
        "filtered_out": [None, None, None],
        "filter_internship": [None, None, None],
    }).to_csv(master, index=False)
    monkeypatch.setattr(sj, "MASTER_CSV", master)
    monkeypatch.setattr(sj, "CHUNK", 2)

    scored = pd.DataFrame({"job_posting_id": ["1", "2"],
                           "filtered_out": [True, False],
                           "filter_internship": [True, False]})
    sj.update_master_scores(scored)

    out = pd.read_csv(master, dtype={"job_posting_id": str}).set_index("job_posting_id")
    assert str(out.loc["1", "filter_internship"]) == "True"
    assert str(out.loc["2", "filter_internship"]) == "False"
    assert pd.isna(out.loc["3", "filter_internship"])


# --- Task 6: the rescore pass ----------------------------------------------------

def _rescore_master(tmp_path, monkeypatch):
    """A master with two never-scored rows: an intern posting and a plain one."""
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame({
        "job_posting_id": ["1", "2"],
        "job_title": ["Data Science Intern", "Data Analyst"],
        "job_description_formatted": [f"<p>{CLEAN_DESC}</p>"] * 2,
        "score": [None, None],
        "filtered_out": [False, False],
        "reason": [None, None],
        "recommendation": [None, None],
    }).to_csv(master, index=False)
    monkeypatch.setattr(sj, "MASTER_CSV", master)

    seen = []

    async def run_scoring(pool, resume, df, **_kw):
        seen.append(df.copy())
        out = df.copy()
        out["score"] = 1
        out["reason"] = "stub"
        out["deep_score"] = None
        out["strengths"] = ""
        out["gaps"] = ""
        out["recommendation"] = "stub"
        return out

    monkeypatch.setattr(sj, "run_scoring", run_scoring)
    return seen


def test_the_rescore_pass_drops_an_intern_row_for_a_finished_candidate(tmp_path, monkeypatch):
    seen = _rescore_master(tmp_path, monkeypatch)
    asyncio.run(sj.rescore_master_failures(pool=None, resume="resume"))
    frame = seen[0].set_index("job_posting_id")
    assert bool(frame.loc["1", "filter_internship"]) is True
    assert bool(frame.loc["1", "filtered_out"]) is True
    assert bool(frame.loc["2", "filter_internship"]) is False
    assert bool(frame.loc["2", "filtered_out"]) is False


def test_the_rescore_pass_keeps_an_intern_row_for_a_student(tmp_path, monkeypatch):
    seen = _rescore_master(tmp_path, monkeypatch)
    monkeypatch.setattr(sj, "EDUCATION_STATUS", "In school: undergraduate")
    monkeypatch.setattr(sj, "GRADUATION_MONTH", "")
    asyncio.run(sj.rescore_master_failures(pool=None, resume="resume"))
    frame = seen[0].set_index("job_posting_id")
    assert bool(frame.loc["1", "filter_internship"]) is False
    assert bool(frame.loc["1", "filtered_out"]) is False


# --- Task 6: main() --------------------------------------------------------------

def _stub_main(monkeypatch, tmp_path, titles):
    """main() over a one-file input CSV with the scoring call faked."""
    inp = tmp_path / "linkedin_jobs_2026-09-29.csv"
    pd.DataFrame({
        "job_posting_id": [str(i) for i in range(len(titles))],
        "job_title": titles,
        "job_description_formatted": [f"<p>{CLEAN_DESC}</p>"] * len(titles),
    }).to_csv(inp, index=False)

    class Pool:
        def stats(self):
            return {}

    async def rescore(pool, resume, *, jev_run=None):
        return 0, 0

    async def run_scoring(pool, resume, df, **_kw):
        out = df.copy()
        out["score"] = 1
        out["reason"] = "stub"
        out["deep_score"] = None
        out["strengths"] = ""
        out["gaps"] = ""
        out["recommendation"] = "stub"
        return out

    ns = {"csv": str(inp), "heal_reused": False, "dry_run": False}
    monkeypatch.setattr(sj, "parse_args", lambda: SimpleNamespace(**ns))
    monkeypatch.setattr(sj, "load_resume", lambda: "resume")
    monkeypatch.setattr(sj, "make_jev_judge", lambda: None)
    monkeypatch.setattr(sj, "make_pool", lambda required=True: Pool())
    monkeypatch.setattr(sj, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")
    monkeypatch.setattr(sj, "load_master_for_reuse", lambda: None)
    monkeypatch.setattr(sj, "run_scoring", run_scoring)
    monkeypatch.setattr(sj, "save_output", lambda df, path: path)
    monkeypatch.setattr(sj, "heal_master_reuse", lambda dry_run=False: 0)
    monkeypatch.setattr(sj, "rescore_master_failures", rescore)
    stats = []
    monkeypatch.setattr(sj, "append_run_stats", stats.append)
    return stats


def test_main_prints_the_internship_count_for_a_finished_candidate(
        monkeypatch, tmp_path, capsys):
    _stub_main(monkeypatch, tmp_path, ["Data Analyst Intern", "Data Analyst", "Co-op Analyst"])
    asyncio.run(sj.main())
    out = capsys.readouterr().out
    assert "Dropped 2 internship / co-op titles (finished school)" in out.splitlines()


def test_main_prints_no_internship_line_when_none_are_dropped(monkeypatch, tmp_path, capsys):
    _stub_main(monkeypatch, tmp_path, ["Data Analyst", "Data Engineer 1"])
    asyncio.run(sj.main())
    assert "internship / co-op" not in capsys.readouterr().out


def test_main_prints_no_internship_line_for_a_student(monkeypatch, tmp_path, capsys):
    _stub_main(monkeypatch, tmp_path, ["Data Analyst Intern"])
    monkeypatch.setattr(sj, "EDUCATION_STATUS", "In school: undergraduate")
    monkeypatch.setattr(sj, "GRADUATION_MONTH", "")
    asyncio.run(sj.main())
    assert "internship / co-op" not in capsys.readouterr().out


# --- Task 7: the clearance filter is level-aware ---------------------------------

# The clearance table of tests/test_jd_filters.py::test_requires_clearance, copied
# so the default profile is pinned to today's requires_clearance verdicts.
JD_FILTER_CASES = [
    "Active TS/SCI clearance required for this role.",
    "Must be able to obtain a Secret clearance.",
    "Applicants must possess a top secret clearance.",
    "This position requires a polygraph.",
    "Ability to obtain a clearance is necessary.",
    "Requires an active Secret clearance.",
    "No clearance required for this position.",
    "Security clearance is not required.",
    "We build software for a security-cleared facility; tours available.",
    "Backend engineer building web apps and REST APIs.",
    "No polygraph required.",
    "A polygraph is not required for this role.",
    "You will collaborate with TS/SCI clearance holders on the team.",
    None,
    float("nan"),
    12345,
]

POSITIVE_SENTENCES = [
    "Active TS/SCI clearance required for this role.",
    "Must be able to obtain a Secret clearance.",
    "Applicants must possess a top secret clearance.",
    "This position requires a polygraph.",
    "Ability to obtain a clearance is necessary.",
    "Requires an active Secret clearance.",
    "Public Trust clearance is required.",
    "Current Top-Secret clearance required",
    "Must hold an interim Secret clearance pending adjudication.",
    "Willingness to undergo a polygraph is required.",
    "Eligibility for a TS clearance is required.",
    "Clearance is required.",
    "Secret clearance sponsorship available for the right candidate.",
    "Active Secret clearance required; ability to obtain TS/SCI",
]
# Each one is a single mention that carries an offer cue and a refusal to sponsor the
# clearance itself. Sentences that a `;` or `.` splits into a requirement and a
# refusal belong to NO_OFFER_SENTENCES, because the refusal is then no mention.
REFUSING_SENTENCES = [
    "We are unable to sponsor a Secret clearance.",
    "Sponsorship for a Secret clearance is not available.",
    "Active Secret clearance required and the company does not sponsor clearances.",
    "Must have an active Secret clearance, interim not accepted.",
    "Active Secret clearance required and no clearance sponsorship is available.",
    "Active Secret clearance required and we do not offer clearance sponsorship.",
    "Secret clearance sponsorship is not offered.",
    "Secret clearance sponsorship is not provided.",
    # Sponsorship first, then the words that refuse it, then the clearance.
    "Sponsorship is not available for a Secret clearance.",
    "Sponsorship is unavailable for a Secret clearance.",
    "Sponsorship isn't available for a Secret clearance.",
    "Sponsorship is not offered for a Secret clearance.",
    # Clearance sponsorship first, then wording that refuses it.
    "Secret clearance sponsorship isn't offered.",
    "Secret clearance sponsorship isn't provided.",
    "Secret clearance sponsorship is not currently available.",
    "Secret clearance sponsorship is no longer available.",
    "Secret clearance sponsorship is not an option.",
    "Secret clearance sponsorship is not supported.",
]
# No mention carries an offer cue, so an open candidate is blocked for want of one and
# no refusal is involved. The first two rows refuse sponsorship in a sentence of their
# own, and a sentence that names no clearance pattern is no mention.
NO_OFFER_SENTENCES = [
    # "obtain" counts only when the thing obtained is a clearance.
    "Active Secret clearance required and must obtain Security+ within 90 days.",
    "Must hold an active Secret clearance and be able to obtain a CISSP within 12 months.",
    "Must hold an active Secret clearance and obtain a CompTIA Security+ certification.",
    "Sponsorship is no longer available for a Secret clearance.",
    "Active Secret clearance required; no clearance sponsorship available.",
    "Active Secret clearance required. We do not offer clearance sponsorship.",
    "Requires an active Secret clearance (no sponsorship).",
    "Must have obtained an active Secret clearance.",
    "US citizenship and an active Secret clearance required, no visa sponsorship available.",
    "Must be eligible to work in the US and hold an active Secret clearance.",
    "Must be eligible for employment in the US and hold an active Secret clearance.",
    "Candidates must be authorized to work in the US and hold a Secret clearance.",
    "Must have work authorization and an active Secret clearance.",
]
ACCEPTING_SENTENCES = [
    "Must be able to obtain a Secret clearance.",
    "Candidates must be eligible to obtain a Secret clearance.",
    "The company will sponsor a Secret clearance for the selected candidate.",
    "Interim Secret clearance acceptable; we sponsor the final clearance.",
    "Secret clearance sponsorship is available for candidates who do not already hold one.",
    "We will sponsor a Secret clearance for candidates who are not yet cleared.",
    "Secret clearance will be obtained through company sponsorship.",
]
NEGATED_SENTENCES = [
    "No clearance required for this position.",
    "Security clearance is not required.",
    "No polygraph required.",
    "A polygraph is not required for this role.",
    "You will collaborate with TS/SCI clearance holders on the team.",
    "No sponsorship or clearance required.",
    "No visa sponsorship or clearance required.",
    "We will not sponsor visas or require a clearance.",
    "Requires: no security clearance\nSponsorship available",
    "Relocation is not offered, no clearance needed.",
    "Sponsorship is not available and no clearance is required.",
    "Visa sponsorship is not available; a clearance is not required.",
    "No clearance required. Sponsorship is not available.",
]
NEUTRAL_SENTENCES = [
    "Backend engineer building web apps and REST APIs.",
    "We build software for a security-cleared facility; tours available.",
    "Strong SQL and Python skills.",
    "Team lunches on Fridays!",
]
SEPARATORS = (" ", "\n", "; ", ". ")


def _synthetic_postings():
    pool = (POSITIVE_SENTENCES + REFUSING_SENTENCES + NO_OFFER_SENTENCES
            + ACCEPTING_SENTENCES + NEGATED_SENTENCES + NEUTRAL_SENTENCES)
    yield from pool
    for a in pool:
        for b in pool:
            for sep in SEPARATORS:
                yield a + sep + b
    yield ("About us\nWe build things.\nRequirements:\n- Python\n- Active Secret clearance\n"
           "- Must be able to obtain a TS clearance\nBenefits: lunch")
    yield "Must be able to hold; a clearance"
    yield "Who you are? Someone who must hold! a Secret clearance."


@pytest.mark.parametrize("text", JD_FILTER_CASES, ids=lambda t: repr(t)[:40])
def test_the_default_profile_blocks_what_requires_clearance_blocks_on_the_jd_filter_cases(text):
    assert sj.clearance_blocks(text) is sj.requires_clearance(text)


def test_the_default_profile_blocks_what_requires_clearance_blocks_on_synthetic_postings():
    seen = 0
    for text in _synthetic_postings():
        assert sj.clearance_blocks(text) is sj.requires_clearance(text), text
        seen += 1
    assert seen > 1000


@pytest.mark.parametrize("text", [
    "Active Secret clearance required; no clearance sponsorship available.",
    "Active Secret clearance required. We do not offer clearance sponsorship.",
    "Active Secret clearance required. We don't provide a clearance sponsorship program.",
    "Requires a Secret clearance. The company does not sponsor a clearance.",
    "Sponsorship is not available for a Secret clearance.",
    "Sponsorship is not offered for a Secret clearance.",
    "Sponsorship is not currently available for a Secret clearance.",
])
def test_a_refusal_to_sponsor_does_not_read_as_no_clearance_required(text):
    """The negation guard used to read "no clearance sponsorship" as "no clearance
    required", and "not available for a Secret clearance" as no requirement, so
    these passed for every profile."""
    assert sj.requires_clearance(text) is True
    assert sj.clearance_blocks(text) is True


@pytest.mark.parametrize("text", [
    "Active Secret clearance required. We will not sponsor a clearance.",
    "Active Secret clearance required. There is no sponsorship for your clearance.",
    "Active Secret clearance required. We do not sponsor candidates for a security clearance.",
])
def test_a_refusal_that_names_the_clearance_it_will_not_sponsor_is_still_required(text):
    assert sj.requires_clearance(text) is True
    assert sj.clearance_blocks(text, 0, True) is True


@pytest.mark.parametrize("text", [
    "No sponsorship or clearance required.",
    "No visa sponsorship or clearance required.",
    "We will not sponsor visas or require a clearance.",
    "Requires: no security clearance\nSponsorship available",
    "Requires: no security clearance\nSponsorship available for the right candidate",
    "No clearance required, and sponsorship is available.",
    "No clearance needed. Visa sponsorship is not available.",
])
def test_a_negation_with_sponsorship_wording_nearby_still_passes(text):
    """Only a sponsorship of the clearance itself keeps a negation out of the
    suppressor. Sponsorship of a visa or of relocation, joined to the clearance by
    "or" or placed on the next line, says nothing about the clearance."""
    assert sj.requires_clearance(text) is False
    assert sj.clearance_requirement(text) == []
    assert sj.clearance_blocks(text) is False
    assert sj.clearance_blocks(text, 0, True) is False


@pytest.mark.parametrize("text", [
    "No clearance required.",
    "This role does not require a security clearance.",
    "Clearance not required.",
    "No polygraph required.",
    "We do not require a clearance for this role.",
    "You do not need a security clearance.",
    "Without a clearance, you can still apply.",
    "No clearance needed. Sponsorship for relocation is available.",
])
def test_a_negated_clearance_requirement_still_passes(text):
    assert sj.requires_clearance(text) is False
    assert sj.clearance_blocks(text) is False


def test_the_default_arguments_are_no_clearance_and_not_open():
    text = "Active Secret clearance required."
    assert sj.clearance_blocks(text) is True
    assert sj.clearance_blocks(text, 0, False) is True
    assert sj.clearance_blocks(text, held_rank=0, sponsorship=False) is True


# --- Task 7: the requirement list ------------------------------------------------

@pytest.mark.parametrize(("text", "expected"), [
    ("Public Trust clearance is required.", [(1, False)]),
    ("Active Secret clearance required.", [(2, False)]),
    ("Top Secret clearance required.", [(3, False)]),
    ("Top-Secret clearance required.", [(3, False)]),
    ("TS clearance required.", [(3, False)]),
    ("Active TS/SCI clearance required.", [(4, False)]),
    ("TS-SCI clearance required.", [(4, False)]),
    ("Top Secret/SCI clearance required.", [(4, False)]),
    ("SCI clearance required.", [(4, False)]),
    ("This position requires a polygraph.", [(4, False)]),
    ("Clearance is required.", [(0, False)]),
    ("Requires an active clearance.", [(0, False)]),
    ("Must be able to obtain a Secret clearance.", [(2, True)]),
    ("Eligibility for a Top Secret clearance is required.", [(3, True)]),
    ("An interim Secret clearance is fine pending final adjudication.", [(2, True)]),
    ("Willingness to undergo a polygraph is required.", [(4, True)]),
    ("Secret clearance sponsorship available for the right candidate.", [(2, True)]),
    ("Must be able to obtain a Public Trust clearance.", [(1, True)]),
    ("Active Secret clearance required. Must obtain a TS/SCI clearance.",
     [(2, False), (4, True)]),
    ("Requirements:\nActive Secret clearance\nMust be able to obtain a TS clearance",
     [(2, False), (3, True)]),
    ("Active Secret clearance required; ability to obtain TS/SCI", [(2, False)]),
])
def test_the_requirement_is_one_pair_per_sentence_that_names_a_clearance(text, expected):
    assert sj.clearance_requirement(text) == expected


@pytest.mark.parametrize("text", NEGATED_SENTENCES + NEUTRAL_SENTENCES
                         + [None, float("nan"), 12345, ""])
def test_a_posting_without_a_clearance_requirement_has_an_empty_list(text):
    assert sj.clearance_requirement(text) == []


def test_the_list_is_empty_exactly_when_requires_clearance_is_false():
    for text in _synthetic_postings():
        assert (sj.clearance_requirement(text) == []) is (not sj.requires_clearance(text)), text


def test_a_match_that_spans_a_sentence_break_falls_back_to_one_unnamed_mention():
    """CLEARANCE_PATTERNS may match across a `;`, `!` or `?`, so splitting into
    sentences can leave no single sentence that matches. The fallback keeps the
    default profile blocking what requires_clearance blocks."""
    for text in ("Must be able to hold; a clearance",
                 "Must hold! a clearance",
                 "Must you maintain? a clearance"):
        assert sj.requires_clearance(text) is True
        assert sj.clearance_requirement(text) == [(0, False)], text
        assert sj.clearance_blocks(text) is True
    # An unnamed level counts as Secret, so a Secret holder passes it.
    assert sj.clearance_blocks("Must be able to hold; a clearance", 2) is False


# --- Task 7: the level table -----------------------------------------------------

SECRET_REQUIRED = "Active Secret clearance required."
PUBLIC_TRUST_REQUIRED = "Public Trust clearance is required."
TOP_SECRET_REQUIRED = "Top Secret clearance required."
TSSCI_REQUIRED = "Active TS/SCI clearance required."
POLYGRAPH_REQUIRED = "This position requires a polygraph."
OBTAIN_SECRET = "Must be able to obtain a Secret clearance."
SPONSORED_SECRET = "Secret clearance sponsorship available for the right candidate."
SPONSORED_REQUIRED = "Active Secret clearance required, with clearance sponsorship available."
MIXED = "Active Secret clearance required; ability to obtain TS/SCI"
TWO_MENTIONS = "Active Secret clearance required. Must obtain a TS/SCI clearance."


@pytest.mark.parametrize(("text", "held", "open_", "blocks"), [
    # held Secret
    (SECRET_REQUIRED, 2, False, False),
    (PUBLIC_TRUST_REQUIRED, 2, False, False),
    (TSSCI_REQUIRED, 2, False, True),
    (TOP_SECRET_REQUIRED, 2, False, True),
    (POLYGRAPH_REQUIRED, 2, False, True),
    ("Clearance is required.", 2, False, False),
    # held TS/SCI passes every level, the polygraph included
    (SECRET_REQUIRED, 4, False, False),
    (PUBLIC_TRUST_REQUIRED, 4, False, False),
    (TOP_SECRET_REQUIRED, 4, False, False),
    (TSSCI_REQUIRED, 4, False, False),
    (POLYGRAPH_REQUIRED, 4, False, False),
    # held Top Secret stops at the SCI and the polygraph
    (TOP_SECRET_REQUIRED, 3, False, False),
    (TSSCI_REQUIRED, 3, False, True),
    (POLYGRAPH_REQUIRED, 3, False, True),
    # held Public Trust
    (PUBLIC_TRUST_REQUIRED, 1, False, False),
    (SECRET_REQUIRED, 1, False, True),
    ("Clearance is required.", 1, False, True),
    # no clearance and closed to sponsorship: everything blocks
    (PUBLIC_TRUST_REQUIRED, 0, False, True),
    (OBTAIN_SECRET, 0, False, True),
    (SPONSORED_SECRET, 0, False, True),
    # no clearance, open to sponsorship: only an obtainable clearance passes
    (OBTAIN_SECRET, 0, True, False),
    (SPONSORED_SECRET, 0, True, False),
    ("Eligibility for a Top Secret clearance is required.", 0, True, False),
    ("Willingness to undergo a polygraph is required.", 0, True, False),
    ("An interim Secret clearance is fine pending final adjudication.", 0, True, False),
    (SPONSORED_REQUIRED, 0, True, False),
    (SPONSORED_REQUIRED, 0, False, True),
    (TSSCI_REQUIRED, 0, True, True),
    (SECRET_REQUIRED, 0, True, True),
    (POLYGRAPH_REQUIRED, 0, True, True),
    ("Clearance is required.", 0, True, True),
    # open does not help a candidate who is already short of a required level
    (SECRET_REQUIRED, 1, True, True),
    # every mention has to be satisfied
    (MIXED, 2, True, False),
    (MIXED, 2, False, False),
    (MIXED, 0, True, True),
    (MIXED, 0, False, True),
    (TWO_MENTIONS, 2, False, True),
    (TWO_MENTIONS, 2, True, False),
    (TWO_MENTIONS, 0, True, True),
    ("Must obtain a Secret clearance. Must obtain a TS/SCI clearance.", 0, True, False),
    ("Must obtain a Secret clearance. Active TS/SCI clearance required.", 0, True, True),
])
def test_the_level_table(text, held, open_, blocks):
    assert sj.clearance_blocks(text, held, open_) is blocks


# --- Task 7: a posting that refuses to sponsor blocks an open candidate ------------

@pytest.mark.parametrize("text", REFUSING_SENTENCES + [
    "The company can't sponsor a Secret clearance.",
    "The company can\u2019t sponsor a Secret clearance.",
    "We will not sponsor a Secret clearance.",
    "We won't sponsor a Secret clearance.",
    "Secret clearance sponsorship isn't available.",
    "Secret clearance sponsorship is unavailable.",
])
def test_a_posting_that_refuses_to_sponsor_the_clearance_blocks_an_open_candidate(text):
    assert sj.requires_clearance(text) is True
    assert sj.clearance_blocks(text, held_rank=0, sponsorship=True) is True
    assert sj.clearance_blocks(text, held_rank=0, sponsorship=False) is True


@pytest.mark.parametrize("text", NO_OFFER_SENTENCES)
def test_a_posting_with_no_offer_of_the_clearance_blocks_an_open_candidate(text):
    """No refusal is involved: the sentence never offers to obtain or sponsor the
    clearance, so there is no offer cue for the open candidate to rely on."""
    assert sj.requires_clearance(text) is True
    assert sj.clearance_requirement(text) == [(2, False)]
    assert sj.clearance_blocks(text, held_rank=0, sponsorship=True) is True
    assert sj.clearance_blocks(text, held_rank=0, sponsorship=False) is True


@pytest.mark.parametrize("text", [
    "Must be able to obtain a Secret clearance.",
    "Candidates must be eligible to obtain a Secret clearance.",
    "The company will sponsor a Secret clearance for the selected candidate.",
    "Interim Secret clearance acceptable; we sponsor the final clearance.",
    "Eligibility for a Top Secret clearance is required.",
    "Must be eligible for a Secret clearance.",
    "Must be eligible for a TS/SCI clearance.",
    "Must be eligible for a Top-Secret clearance.",
    "Must be eligible to hold a Secret clearance.",
    "An interim Secret clearance is fine pending final adjudication.",
    "Secret clearance sponsorship available for the right candidate.",
    "We will sponsor candidates for a Secret clearance.",
    "The employer will sponsor you for a Top-Secret clearance.",
    "The company will sponsor eligible candidates for a Secret clearance.",
    # A visa or work-authorization line in the same sentence says nothing about the
    # clearance, so the offer to obtain it still counts.
    "Must be authorized to work in the US and able to obtain a Secret clearance.",
    "Must be a US citizen with work authorization and able to obtain a Secret clearance.",
    "No visa sponsorship is available and candidates must be able to obtain a Secret clearance.",
    "Visa sponsorship is not available and you must be able to obtain a Secret clearance.",
    # An offer to sponsor is still an offer when it mentions who is not yet cleared.
    "Secret clearance sponsorship is available for candidates who do not already hold one.",
    "We will sponsor a Secret clearance for candidates who are not yet cleared.",
    "Secret clearance will be obtained through company sponsorship.",
    # A negator in another clause of the sentence does not cancel the offer.
    "US citizens, no visas required, able to obtain a Secret clearance.",
    "No visas and able to obtain a Secret clearance.",
    "We do not sponsor visas but will sponsor a Secret clearance.",
    # A visa or relocation refusal after the offer, set off by a parenthesis, a dash or
    # a conjunction, refuses nothing about the clearance.
    "We will sponsor a Secret clearance (visa sponsorship is not available).",
    "We will sponsor a Secret clearance and visa sponsorship is not available.",
    "We will sponsor a Secret clearance but visa sponsorship is not available.",
    "We will sponsor a Secret clearance - visa sponsorship is not available.",
    "We will sponsor a Secret clearance \u2013 visa sponsorship is not available.",
    "We will sponsor a Secret clearance \u2014 visa sponsorship is not available.",
    "We will sponsor a Secret clearance (relocation assistance is not offered).",
    "We will sponsor a Secret clearance (H-1B sponsorship is not provided).",
    "Secret clearance sponsorship is available (visa sponsorship is not).",
    "Secret clearance sponsorship is available and work authorization sponsorship is not available.",
    # The sponsor's object is a pronoun or a group of people.
    "Must hold a Secret clearance, or we will sponsor you.",
    "Must hold a Secret clearance or we can sponsor you.",
    "Must hold a Secret clearance, or we'll sponsor you.",
    "Must hold a Secret clearance, or we will sponsor them.",
    "Must hold a Secret clearance or we will sponsor candidates.",
    "Must hold a Secret clearance or we will sponsor new hires.",
    "Must hold a Secret clearance or we will sponsor you (visa sponsorship is not available).",
    "Must hold a Secret clearance or we will sponsor you - visa sponsorship is not available.",
    "Must hold a Secret clearance or we will sponsor you but visa sponsorship is not available.",
    "Have a Secret clearance or be sponsored for one.",
    "Have a Secret clearance or be sponsored for it.",
    # "eligible for" reaches a clearance across a level or an agency name.
    "Must be eligible for a Top Secret security clearance.",
    "Must be eligible for a DoD Top Secret clearance.",
    "Must be eligible for a US Government Top Secret clearance.",
    "Must be eligible for and maintain a Secret clearance.",
    # "obtain" reaches the clearance across a few words or through a pronoun.
    "Must be able to obtain and maintain a Secret clearance.",
    "Must obtain, and maintain, a Secret clearance.",
    "An active Secret clearance or the ability to obtain one is required.",
    "You will need to obtain a Secret clearance within 90 days of hire.",
    "Must be capable of obtaining a Secret clearance.",
    "Must be able to obtain/maintain a Secret clearance.",
    "Must be able to obtain and maintain a Top Secret security clearance.",
    "Must be able to obtain a Secret (or higher) clearance.",
    "An active Secret clearance is required or must be obtainable within 90 days.",
])
def test_a_posting_that_offers_the_clearance_keeps_an_open_candidate(text):
    assert sj.clearance_blocks(text, held_rank=0, sponsorship=True) is False
    assert sj.clearance_blocks(text, held_rank=0, sponsorship=False) is True


@pytest.mark.parametrize("clause", [
    "we will not sponsor you",
    "we cannot sponsor you",
    "we do not sponsor candidates",
    "we will never sponsor new hires",
    "we can sponsor you for a work visa",
    "we will sponsor you for H-1B status",
    "we will sponsor them for a green card",
    "we will sponsor you with a visa",
    "we will sponsor you in the US",
    "we will sponsor you through H-1B",
    "we will sponsor you and your family for a visa",
    "you will not be sponsored for one",
    "you will not be sponsored for it",
])
def test_a_sponsor_with_a_pronoun_object_is_no_offer_when_negated_or_about_a_visa(clause):
    """"sponsor you" offers the clearance only when the sentence is not about a visa
    and nothing negates it, so a held clearance stays required."""
    text = f"Must hold an active Secret clearance, and {clause}."
    assert sj.clearance_blocks(text, 0, True) is True, text
    assert sj.clearance_blocks(text, 2, True) is False, text


def _up(text):
    return text[0].upper() + text[1:]


def _joined(clause, visa):
    """The clause and a visa phrase in four sentence shapes."""
    return [
        f"{_up(clause)}, {visa}.",
        f"{_up(visa)} and {clause}.",
        f"Candidates {clause} ({visa}).",
        f"{_up(visa)}, and candidates {clause}.",
    ]


VISA_PHRASES = [
    "no visa sponsorship",
    "visa sponsorship is not available",
    "we do not sponsor visas",
    "we do not sponsor work visas",
    "we cannot sponsor H-1B visas",
    "no sponsorship for work authorization",
    "no sponsorship available",
    "without sponsorship",
    "without visa sponsorship",
    "sponsorship is not available for work authorization",
    "H-1B sponsorship is available",
    "we sponsor work visas",
    "must not require sponsorship now or in the future",
    "authorized to work in the US without sponsorship",
    "US citizens only, sponsorship not offered",
    "we do not offer employment sponsorship",
    "sponsorship for work authorization is unavailable",
]
OFFER_CLAUSE = "must be able to obtain a Secret clearance"
HELD_CLAUSE = "must hold an active Secret clearance"


@pytest.mark.parametrize("visa", VISA_PHRASES)
def test_a_visa_line_beside_an_offer_to_obtain_the_clearance_keeps_an_open_candidate(visa):
    for text in _joined(OFFER_CLAUSE, visa):
        assert sj.clearance_blocks(text, 0, True) is False, text
        assert sj.clearance_blocks(text, 0, False) is True, text


@pytest.mark.parametrize("visa", VISA_PHRASES)
def test_a_visa_line_beside_a_held_clearance_still_blocks_an_open_candidate(visa):
    """"we sponsor work visas" and "H-1B sponsorship is available" offer nothing
    toward the clearance, so the held clearance stays required."""
    for text in _joined(HELD_CLAUSE, visa):
        assert sj.clearance_blocks(text, 0, True) is True, text
        assert sj.clearance_blocks(text, 2, True) is False, text


@pytest.mark.parametrize("text", [
    "Must be eligible for employment in the US and hold an active Secret clearance.",
    "Must have an active Secret clearance and be eligible for federal employment.",
    "Must be eligible for US employment and hold an active Secret clearance.",
    "Must be eligible for hire with a Secret clearance.",
    "Must be eligible for work in a Secret clearance facility.",
])
def test_eligible_for_something_other_than_a_clearance_is_no_offer(text):
    """"eligible for" is an offer cue only when a clearance follows within a
    short window."""
    assert sj.clearance_requirement(text) == [(2, False)]
    assert sj.clearance_blocks(text, 0, True) is True


@pytest.mark.parametrize("text", [
    "Must be eligible to work in the US and hold an active Secret clearance.",
    "Must have obtained an active Secret clearance.",
])
def test_eligible_to_work_and_obtained_do_not_read_as_obtainable(text):
    assert sj.clearance_requirement(text) == [(2, False)]


@pytest.mark.parametrize("text", REFUSING_SENTENCES + NO_OFFER_SENTENCES)
def test_a_posting_without_an_offer_blocks_a_candidate_who_lacks_the_level(text):
    assert sj.clearance_blocks(text, 1, True) is True


@pytest.mark.parametrize("text", REFUSING_SENTENCES + NO_OFFER_SENTENCES)
def test_a_posting_without_an_offer_never_blocks_a_candidate_who_holds_the_level(text):
    assert sj.clearance_blocks(text, 2, True) is False
    assert sj.clearance_blocks(text, 2, False) is False


@pytest.mark.parametrize("text", NEGATED_SENTENCES + [
    "No clearance required. Pay is competitive.",
    None, float("nan"), 12345,
])
@pytest.mark.parametrize("held", range(5))
@pytest.mark.parametrize("open_", [False, True])
def test_a_negated_or_absent_requirement_never_blocks(text, held, open_):
    assert sj.clearance_blocks(text, held, open_) is False


def test_more_clearance_or_openness_never_turns_a_pass_into_a_block():
    for text in _synthetic_postings():
        for open_ in (False, True):
            verdicts = [sj.clearance_blocks(text, rank, open_) for rank in range(5)]
            assert verdicts == sorted(verdicts, reverse=True), (text, open_, verdicts)
        for rank in range(5):
            if sj.clearance_blocks(text, rank, True):
                assert sj.clearance_blocks(text, rank, False), (text, rank)


# --- Task 7: the filter column follows the profile -------------------------------

def _clearance_frame(texts):
    return pd.DataFrame({"desc": [f"{t} {CLEAN_DESC}" for t in texts],
                         "title": ["Software Engineer"] * len(texts)})


CLEARANCE_TEXTS = [OBTAIN_SECRET, SECRET_REQUIRED, TSSCI_REQUIRED, "No clearance required.",
                   "Build web apps."]


def test_the_default_profile_drops_every_clearance_posting():
    out = sj.add_filter_columns(_clearance_frame(CLEARANCE_TEXTS), "desc", "title",
                                profile=sj.candidate_profile(today=TODAY))
    assert list(out["filter_clearance"]) == [True, True, True, False, False]
    assert list(out["filtered_out"]) == [True, True, True, False, False]


def test_a_held_secret_keeps_the_secret_postings_and_drops_the_ts_sci_one():
    profile = sj.candidate_profile(clearance="Secret", today=TODAY)
    out = sj.add_filter_columns(_clearance_frame(CLEARANCE_TEXTS), "desc", "title",
                                profile=profile)
    assert list(out["filter_clearance"]) == [False, False, True, False, False]
    assert list(out["filtered_out"]) == [False, False, True, False, False]


def test_open_to_sponsorship_keeps_the_obtainable_clearance_only():
    profile = sj.candidate_profile(sponsorship=True, today=TODAY)
    out = sj.add_filter_columns(_clearance_frame(CLEARANCE_TEXTS), "desc", "title",
                                profile=profile)
    assert list(out["filter_clearance"]) == [False, True, True, False, False]


def test_a_missing_profile_reads_the_clearance_constants_at_call_time(monkeypatch):
    monkeypatch.setattr(sj, "CLEARANCE_LEVEL", "Secret")
    out = sj.add_filter_columns(_clearance_frame(CLEARANCE_TEXTS), "desc", "title")
    assert list(out["filter_clearance"]) == [False, False, True, False, False]
    monkeypatch.setattr(sj, "CLEARANCE_LEVEL", "None")
    monkeypatch.setattr(sj, "CLEARANCE_SPONSORSHIP", True)
    out = sj.add_filter_columns(_clearance_frame(CLEARANCE_TEXTS), "desc", "title")
    assert list(out["filter_clearance"]) == [False, True, True, False, False]


def test_the_clearance_column_is_a_plain_bool_column():
    out = sj.add_filter_columns(_clearance_frame(CLEARANCE_TEXTS), "desc", "title",
                                profile=FINISHED)
    assert out["filter_clearance"].dtype == bool


def test_the_rescore_pass_uses_the_clearance_profile(tmp_path, monkeypatch):
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame({
        "job_posting_id": ["1", "2"],
        "job_title": ["Data Analyst", "Data Engineer"],
        "job_description_formatted": [f"<p>{SECRET_REQUIRED} {CLEAN_DESC}</p>",
                                      f"<p>{TSSCI_REQUIRED} {CLEAN_DESC}</p>"],
        "score": [None, None], "filtered_out": [False, False],
        "reason": [None, None], "recommendation": [None, None],
    }).to_csv(master, index=False)
    monkeypatch.setattr(sj, "MASTER_CSV", master)
    monkeypatch.setattr(sj, "CLEARANCE_LEVEL", "Secret")
    seen = []

    async def run_scoring(pool, resume, df, **_kw):
        seen.append(df.copy())
        out = df.copy()
        for col, val in (("score", 1), ("reason", "stub"), ("deep_score", None),
                         ("strengths", ""), ("gaps", ""), ("recommendation", "stub")):
            out[col] = val
        return out

    monkeypatch.setattr(sj, "run_scoring", run_scoring)
    asyncio.run(sj.rescore_master_failures(pool=None, resume="resume"))
    frame = seen[0].set_index("job_posting_id")
    assert bool(frame.loc["1", "filter_clearance"]) is False
    assert bool(frame.loc["2", "filter_clearance"]) is True


# --- Task 7: jev_facts and the student cue ---------------------------------------

@pytest.mark.parametrize("text", [
    "Summer internship program for data analysts.",
    "Data Science Intern",
    "Our interns work on real projects.",
    "Co-op position starting in January.",
    "Coops rotate every four months.",
    "Currently enrolled in a bachelor's program.",
    "Proof of enrollment is required.",
    "Pursuing a degree in computer science.",
    "Graduating in May 2027.",
    "Recent graduates are encouraged to apply.",
    "Class of 2027 candidates welcome.",
    "This is a new grad role.",
    "New-grad program for analysts.",
    "New Grads welcome.",
    "Join our campus recruiting team.",
    "Students welcome to apply.",
    "A student with SQL skills.",
])
def test_a_student_cue_is_found(text):
    assert sj.has_student_cue(text) is True


@pytest.mark.parametrize("text", [
    "Internal tools team building dashboards.",
    "International clients across Europe.",
    "Internet-scale data pipelines.",
    "Build data pipelines for an analytics team.",
    "Cooperative culture and flat teams.",
    "We value studentship and mentoring.",
    "Newgrade software for the classroom.",
    "",
    None,
    float("nan"),
    12345,
])
def test_no_student_cue_is_found(text):
    assert sj.has_student_cue(text) is False


def test_jev_facts_carries_the_four_facts():
    facts = sj.jev_facts(CLEAN_DESC)
    assert facts == {"min_years": None, "advanced_degree": False, "clearance": False,
                     "student_cue": False}


def test_jev_facts_reads_the_student_cue():
    assert sj.jev_facts(CLEAN_DESC + " Open to current students.")["student_cue"] is True


def test_jev_facts_clearance_under_the_default_profile_is_requires_clearance():
    for text in _synthetic_postings():
        assert sj.jev_facts(text)["clearance"] is sj.requires_clearance(text), text


def test_jev_facts_clearance_follows_the_profile_argument():
    text = f"{OBTAIN_SECRET} {CLEAN_DESC}"
    held = sj.candidate_profile(clearance="Secret", today=TODAY)
    open_ = sj.candidate_profile(sponsorship=True, today=TODAY)
    assert sj.jev_facts(text)["clearance"] is True
    assert sj.jev_facts(text, held)["clearance"] is False
    assert sj.jev_facts(text, open_)["clearance"] is False
    assert sj.jev_facts(f"{TSSCI_REQUIRED} {CLEAN_DESC}", held)["clearance"] is True
    assert sj.jev_facts(f"{TSSCI_REQUIRED} {CLEAN_DESC}", open_)["clearance"] is True


def test_jev_facts_reads_the_configured_profile_when_none_is_given(monkeypatch):
    text = f"{SECRET_REQUIRED} {CLEAN_DESC}"
    monkeypatch.setattr(sj, "CLEARANCE_LEVEL", "TS/SCI")
    assert sj.jev_facts(text)["clearance"] is False


# --- Final review, A: a graduate student keeps the postings for their own degree ---

GRAD_ENROLLMENT_TEXTS = [
    "Minimum qualifications: currently enrolled in a PhD program.",
    "Requirements: Currently pursuing a Master's or PhD in Computer Science.",
    "Qualifications: pursuing a Master's degree (required).",
]


@pytest.mark.parametrize("text", GRAD_ENROLLMENT_TEXTS)
def test_the_degree_filter_reads_graduate_enrollment_wording_as_a_requirement(text):
    assert sj.requires_advanced_degree(f"{CLEAN_DESC} {text}") is True


@pytest.mark.parametrize("text", GRAD_ENROLLMENT_TEXTS)
@pytest.mark.parametrize("profile,dropped", [(FINISHED, True), (UNDERGRAD, True), (GRAD, False)],
                         ids=["finished", "undergrad", "grad"])
def test_the_degree_filter_follows_the_school_status(profile, dropped, text):
    frame = _title_frame(["Data Science Intern"], desc=f"{CLEAN_DESC} {text}")
    out = sj.add_filter_columns(frame, "desc", "title", profile=profile)
    assert bool(out.iloc[0]["filter_degree"]) is dropped
    assert bool(out.iloc[0]["filtered_out"]) is dropped
    assert out["filter_degree"].dtype == bool


def test_a_graduate_student_keeps_a_row_only_the_degree_filter_would_drop():
    text = f"{CLEAN_DESC} Minimum qualifications: currently enrolled in a PhD program."
    out = sj.add_filter_columns(_title_frame(["Data Science Intern"], desc=text), "desc",
                                "title", profile=GRAD)
    assert bool(out.iloc[0]["filtered_out"]) is False


def test_the_degree_column_stays_when_the_status_is_graduate():
    out = sj.add_filter_columns(_title_frame(TITLES), "desc", "title", profile=GRAD)
    assert "filter_degree" in out.columns
    assert list(out["filter_degree"]) == [False] * len(TITLES)


def test_the_degree_filter_reads_the_configured_status_when_no_profile_is_given(monkeypatch):
    text = f"{CLEAN_DESC} Minimum qualifications: currently enrolled in a PhD program."
    monkeypatch.setattr(sj, "EDUCATION_STATUS", "In school: graduate")
    monkeypatch.setattr(sj, "GRADUATION_MONTH", "May 2099")
    out = sj.add_filter_columns(_title_frame(["Data Science Intern"], desc=text), "desc", "title")
    assert bool(out.iloc[0]["filter_degree"]) is False
    monkeypatch.setattr(sj, "EDUCATION_STATUS", "Finished school")
    out = sj.add_filter_columns(_title_frame(["Data Science Intern"], desc=text), "desc", "title")
    assert bool(out.iloc[0]["filter_degree"]) is True


@pytest.mark.parametrize("profile,expected", [(FINISHED, True), (UNDERGRAD, True), (GRAD, False)],
                         ids=["finished", "undergrad", "grad"])
def test_jev_facts_advanced_degree_follows_the_school_status(profile, expected):
    text = f"{CLEAN_DESC} Minimum qualifications: currently enrolled in a PhD program."
    assert sj.jev_facts(text, profile)["advanced_degree"] is expected


def test_jev_facts_advanced_degree_is_the_detector_verdict_under_the_default_profile():
    for text in (f"{CLEAN_DESC} Master's degree required.", CLEAN_DESC,
                 f"{CLEAN_DESC} Master's degree preferred."):
        assert sj.jev_facts(text)["advanced_degree"] is sj.requires_advanced_degree(text), text
