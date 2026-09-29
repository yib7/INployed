"""Cycle 21, Tasks 6 and 7: the mechanical filters follow the candidate profile.

Task 6: a candidate who has finished school gets intern and co-op titles dropped
before any scorer call (`filter_internship`). A candidate still in school keeps
them.

Task 7: the clearance filter is level-aware. A clearance the candidate holds
passes; a clearance the employer sponsors passes when the candidate is open to
sponsorship. Under the default profile (no clearance, not open) it blocks
exactly what `requires_clearance` blocks.

Every test runs against the sandboxed scoring constants that conftest's
_hermetic_repo_data rebinds, so the author's scoring_config.json never leaks in.
"""
import asyncio
import re
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
    "",
])
def test_a_title_that_only_contains_the_letters_is_not_an_internship_title(title):
    assert sj.is_internship_title(title) is False


@pytest.mark.parametrize("value", [None, float("nan"), 12345, pd.NA])
def test_a_non_string_title_is_not_an_internship_title(value):
    assert sj.is_internship_title(value) is False


def test_the_internship_regex_is_the_specified_pattern():
    assert sj.INTERNSHIP_TITLE_RE.pattern == (
        r"\b(?:intern|interns|internship|internships|co-?op|co-?ops)\b")
    assert sj.INTERNSHIP_TITLE_RE.flags & re.I


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
