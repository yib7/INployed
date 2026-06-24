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
