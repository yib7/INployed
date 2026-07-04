"""Column-scoped search + multi-filter in jobsdata.filter_and_sort (pure DataFrame logic)."""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402


def _df():
    return pd.DataFrame([
        {"job_title": "Data Analyst", "company_name": "Acme", "job_location": "Seattle, WA",
         "score": "5", "recommendation": "apply", "url": "u1"},
        {"job_title": "ML Engineer", "company_name": "Globex", "job_location": "Austin, TX",
         "score": "4", "recommendation": "consider", "url": "u2"},
    ])


def _call(df, search, column=None):
    return jobsdata.filter_and_sort(df, search, "Any", "All", "All", "All", False, column)


def test_column_search_matches_only_that_column():
    out = _call(_df(), "seattle", "job_location")
    assert list(out["company_name"]) == ["Acme"]


def test_column_search_no_match():
    out = _call(_df(), "seattle", "company_name")
    assert out.empty


def test_all_columns_default_behaviour():
    out = _call(_df(), "globex", None)
    assert list(out["job_title"]) == ["ML Engineer"]


def test_search_column_precomputed():
    """filter_and_sort uses the _search column when search_column is None/All."""
    df = _df().copy()
    df["_search"] = ["data analyst acme seattle", "ml engineer globex austin tx"]
    out = _call(df, "acme", None)
    assert list(out["company_name"]) == ["Acme"]


def test_min_score_filter():
    out = jobsdata.filter_and_sort(_df(), "", "5", "All", "All", "All", False, None)
    assert list(out["company_name"]) == ["Acme"]   # only the score-5 row


# --- three-state Easy Apply filter: All / Easy Apply / Not Easy Apply ---

def _easy_df():
    return pd.DataFrame([
        {"job_title": "A", "company_name": "Acme", "url": "u1", "is_easy_apply": "True"},
        {"job_title": "B", "company_name": "Globex", "url": "u2", "is_easy_apply": "false"},
        {"job_title": "C", "company_name": "Initech", "url": "u3", "is_easy_apply": None},
        {"job_title": "D", "company_name": "Umbrella", "url": "u4", "is_easy_apply": ""},
        {"job_title": "E", "company_name": "Hooli", "url": "u5", "is_easy_apply": "1"},
    ])


def _easy(df, easy):
    return jobsdata.filter_and_sort(df, "", "Any", "All", "All", "All", easy, None)


def test_easy_all_keeps_every_row():
    assert len(_easy(_easy_df(), "All")) == 5


def test_easy_apply_keeps_only_truthy():
    out = _easy(_easy_df(), "Easy Apply")
    assert sorted(out["job_title"]) == ["A", "E"]


def test_not_easy_apply_keeps_the_complement():
    # NaN/blank is_easy_apply counts as NOT easy apply.
    out = _easy(_easy_df(), "Not Easy Apply")
    assert sorted(out["job_title"]) == ["B", "C", "D"]


def test_easy_nan_never_lands_in_easy_apply():
    easy = set(_easy(_easy_df(), "Easy Apply")["job_title"])
    assert "C" not in easy and "D" not in easy


def test_easy_legacy_bools_still_work():
    # True -> "Easy Apply", False -> "All" (pre-combo callers).
    assert sorted(_easy(_easy_df(), True)["job_title"]) == ["A", "E"]
    assert len(_easy(_easy_df(), False)) == 5


def test_easy_filter_without_column_is_a_noop():
    out = _easy(_df(), "Easy Apply")     # _df has no is_easy_apply column
    assert len(out) == 2


# --- live_resume_ids: the blue "tailored" tint follows on-disk folder existence ---

def test_live_resume_ids_keeps_only_existing_folders(tmp_path):
    live_dir = tmp_path / "have"
    live_dir.mkdir()
    paths = {"1": str(live_dir), "2": str(tmp_path / "deleted"),
             "3": "", "4": None}
    assert jobsdata.live_resume_ids(paths) == {"1"}   # only the folder that exists


def test_live_resume_ids_excludes_a_file_path(tmp_path):
    # a recorded path that is a FILE, not a directory, is not a live tailored folder.
    f = tmp_path / "not_a_dir.pdf"
    f.write_bytes(b"%PDF")
    assert jobsdata.live_resume_ids({"9": str(f)}) == set()


def test_live_resume_ids_handles_empty_or_non_dict():
    assert jobsdata.live_resume_ids({}) == set()
    assert jobsdata.live_resume_ids(set()) == set()   # tolerant of a non-mapping
