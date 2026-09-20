"""Column-scoped search + multi-filter in jobsdata.filter_and_sort (pure DataFrame logic)."""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

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


# --- SP5: repost_key normalisation table ----------------------------------------

@pytest.mark.parametrize("title,company,location,expected", [
    ("Data Engineer", "Acme", "Seattle, WA", "data engineer|acme|seattle"),
    # a trailing parenthetical workplace marker on the title is dropped
    ("Data Engineer (Remote)", "Acme", "Seattle, WA", "data engineer|acme|seattle"),
    # a trailing dash marker too, any of the three workplace words
    ("Data Engineer - Hybrid", "Acme", "Seattle, WA", "data engineer|acme|seattle"),
    ("Data Engineer (On-site)", "Acme", "Seattle, WA", "data engineer|acme|seattle"),
    # punctuation collapses to single spaces, both in the title and the company
    ("Sr. Data Engineer!!", "Acme, Inc.", "New York, NY",
     "sr data engineer|acme inc|new york"),
    # NFKC first: full-width parens must normalise to ASCII before the
    # trailing-marker regex ever runs, or the marker survives in the key
    ("Data Engineer（Remote）", "Acme", "Seattle, WA",
     "data engineer|acme|seattle"),
    # location keeps only the part before the first comma
    ("Data Engineer", "Acme", "Seattle, WA, United States", "data engineer|acme|seattle"),
    # empty location is allowed; the key is still non-empty
    ("Data Engineer", "Acme", "", "data engineer|acme|"),
    ("Data Engineer", "Acme", None, "data engineer|acme|"),
    # an empty title or company means nothing safe to match on
    ("", "Acme", "Seattle, WA", ""),
    ("   ", "Acme", "Seattle, WA", ""),
    ("Data Engineer", "", "Seattle, WA", ""),
    ("Data Engineer", "   ", "Seattle, WA", ""),
])
def test_repost_key_normalisation_table(title, company, location, expected):
    assert jobsdata.repost_key(title, company, location) == expected


# --- SP5: suppress_reposts --------------------------------------------------------

def _row(jid, is_seen, extracted_date, title="Data Engineer", company="Acme",
         location="Seattle, WA", score="5"):
    return {"job_posting_id": jid, "job_title": title, "company_name": company,
            "job_location": location, "is_seen": is_seen,
            "extracted_date": extracted_date, "score": score}


TODAY = date(2026, 9, 19)


def test_suppress_reposts_window_zero_disables_and_returns_df_untouched():
    df = pd.DataFrame([_row("A", "yes", "2026-08-01"), _row("B", "no", "2026-08-20")])
    out, hidden = jobsdata.suppress_reposts(df, {"A": "2026-09-10T00:00:00+00:00"}, 0, today=TODAY)
    assert out is df
    assert hidden == 0


def test_suppress_reposts_in_window_hides_every_unseen_duplicate():
    df = pd.DataFrame([
        _row("A", "yes", "2026-07-01"),
        _row("B", "no", "2026-08-15"),
        _row("C", "no", "2026-08-20"),
    ])
    marked_at = {"A": "2026-09-09T00:00:00+00:00"}   # 10 days before TODAY
    out, hidden = jobsdata.suppress_reposts(df, marked_at, 30, today=TODAY)
    assert list(out["job_posting_id"]) == ["A"]      # only the (already seen) mark survives
    assert hidden == 2


def test_suppress_reposts_out_of_window_collapses_to_newest_unseen():
    df = pd.DataFrame([
        _row("A", "yes", "2026-07-01"),
        _row("B", "no", "2026-08-15"),
        _row("C", "no", "2026-08-20"),   # newest extracted_date of the two unseen rows
    ])
    marked_at = {"A": "2026-08-19T00:00:00+00:00"}   # 31 days before TODAY: outside the window
    out, hidden = jobsdata.suppress_reposts(df, marked_at, 30, today=TODAY)
    assert set(out["job_posting_id"]) == {"A", "C"}
    assert hidden == 1


def test_suppress_reposts_collapse_ties_keep_the_highest_id():
    # Same extracted_date -> the id tie-break, compared as a STRING (same digit
    # count here so string order and numeric order agree; "9" vs "10" would not).
    df = pd.DataFrame([
        _row("19", "no", "2026-08-20"),
        _row("20", "no", "2026-08-20"),
    ])
    out, hidden = jobsdata.suppress_reposts(df, {}, 30, today=TODAY)
    assert list(out["job_posting_id"]) == ["20"]
    assert hidden == 1


def test_suppress_reposts_empty_key_never_suppressed():
    # No company_name column at all -> repost_key is always "" -> never blocked
    # or collapsed, however many rows share every other field.
    df = pd.DataFrame([
        {"job_posting_id": "A", "job_title": "Data Engineer", "is_seen": "yes",
         "extracted_date": "2026-07-01", "score": "5"},
        {"job_posting_id": "B", "job_title": "Data Engineer", "is_seen": "no",
         "extracted_date": "2026-08-15", "score": "5"},
        {"job_posting_id": "C", "job_title": "Data Engineer", "is_seen": "no",
         "extracted_date": "2026-08-20", "score": "5"},
    ])
    marked_at = {"A": "2026-09-09T00:00:00+00:00"}
    out, hidden = jobsdata.suppress_reposts(df, marked_at, 30, today=TODAY)
    assert set(out["job_posting_id"]) == {"A", "B", "C"}
    assert hidden == 0


def test_suppress_reposts_keeps_original_index_and_columns():
    df = pd.DataFrame([
        _row("A", "yes", "2026-07-01"),
        _row("B", "no", "2026-08-15"),
        _row("C", "no", "2026-08-20"),
    ], index=[5, 9, 12])
    out, _hidden = jobsdata.suppress_reposts(
        df, {"A": "2026-09-09T00:00:00+00:00"}, 30, today=TODAY)
    assert list(out.columns) == list(df.columns)
    assert set(out.index) <= set(df.index)


# --- SP5: filter_high_unseen wired to the repost window --------------------------

def _checkpoint_df():
    return pd.DataFrame([
        _row("A", "yes", "2026-08-10"),
        _row("B", "no", "2026-08-15"),
        _row("C", "no", "2026-08-20"),   # newest of the two unseen duplicates
    ])


def test_checkpoint_mark_in_window_hides_all_unseen_reposts():
    df = _checkpoint_df()
    marked_at = {"A": "2026-09-09T00:00:00+00:00"}   # 10 days before TODAY
    out = jobsdata.filter_high_unseen(df, 4, marked_at=marked_at, window_days=30)
    assert out.empty


def test_checkpoint_mark_out_of_window_keeps_newest_unseen_repost():
    df = _checkpoint_df()
    marked_at = {"A": "2026-08-19T00:00:00+00:00"}   # 31 days before TODAY
    out = jobsdata.filter_high_unseen(df, 4, marked_at=marked_at, window_days=30)
    assert list(out["job_posting_id"]) == ["C"]


def test_filter_high_unseen_window_zero_ignores_marked_at():
    df = _checkpoint_df()
    marked_at = {"A": "2026-09-09T00:00:00+00:00"}
    out = jobsdata.filter_high_unseen(df, 4, marked_at=marked_at, window_days=0)
    assert set(out["job_posting_id"]) == {"B", "C"}   # unchanged: both unseen rows show


def test_filter_high_unseen_without_marked_at_is_unaffected():
    # No marked_at at all -> today's exact behaviour, unseen duplicates and all.
    df = _checkpoint_df()
    out = jobsdata.filter_high_unseen(df, 4)
    assert set(out["job_posting_id"]) == {"B", "C"}


def test_filter_high_unseen_with_count_reports_the_hidden_total():
    df = _checkpoint_df()
    marked_at = {"A": "2026-09-09T00:00:00+00:00"}
    out, hidden = jobsdata.filter_high_unseen_with_count(
        df, 4, marked_at=marked_at, window_days=30)
    assert out.empty
    assert hidden == 2
