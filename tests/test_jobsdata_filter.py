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

# Shared with tests/test_score_jobs.py (SP6): that file asserts its own
# pipeline-local repost_key copy agrees with jobsdata.repost_key on every row
# here, so the two implementations can never quietly drift apart.
REPOST_KEY_CASES = [
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
    # a float NaN, the real-world shape a missing pandas cell takes, is also
    # "nothing safe to match on" -- it must not stringify to the word "nan"
    (float("nan"), "Acme", "Seattle, WA", ""),
    ("Data Engineer", float("nan"), "Seattle, WA", ""),
]


@pytest.mark.parametrize("title,company,location,expected", REPOST_KEY_CASES)
def test_repost_key_normalisation_table(title, company, location, expected):
    assert jobsdata.repost_key(title, company, location) == expected


_VECTORIZED_KEY_ROWS = [
    ("Data Engineer", "Acme", "Seattle, WA"),
    ("Data Engineer (Remote)", "Acme", "Seattle, WA"),
    ("Data Engineer - Hybrid", "Acme", "Seattle, WA"),
    ("Data Engineer (On-site)", "Acme", "Seattle, WA"),
    ("Sr. Data Engineer!!", "Acme, Inc.", "New York, NY"),
    ("Data Engineer（Remote）", "Acme", "Seattle, WA"),
    ("Data Engineer", "Acme", "Seattle, WA, United States"),
    ("Data Engineer", "Acme", ""),
    ("", "Acme", "Seattle, WA"),
    ("   ", "Acme", "Seattle, WA"),
    ("Data Engineer", "", "Seattle, WA"),
    ("Data Engineer", "   ", "Seattle, WA"),
]


def _assert_matches_scalar(keys_fn):
    """`keys_fn` must agree with the scalar `repost_key` exactly, row by row,
    over the same normalisation table above (minus the NaN rows: a pandas
    column represents those as a real missing value, a distinct case already
    covered by the scalar table's own NaN entries)."""
    df = pd.DataFrame(_VECTORIZED_KEY_ROWS, columns=["job_title", "company_name", "job_location"])
    vectorized = list(keys_fn(df))
    scalar = [jobsdata.repost_key(t, c, loc) for t, c, loc in _VECTORIZED_KEY_ROWS]
    assert vectorized == scalar


def test_repost_keys_vectorized_matches_scalar_row_by_row():
    # Whichever backend the dispatcher picks in this environment.
    _assert_matches_scalar(jobsdata._repost_keys_vectorized)


def test_repost_keys_pandas_fallback_matches_scalar_row_by_row():
    # The pandas `.str`-accessor path, exercised directly so it stays correct
    # even on a machine (like this test run) where pyarrow is also installed
    # and the dispatcher would otherwise never touch it.
    _assert_matches_scalar(jobsdata._repost_keys_pandas)


@pytest.mark.skipif(jobsdata.pa is None, reason="pyarrow not installed")
def test_repost_keys_pyarrow_matches_scalar_row_by_row():
    _assert_matches_scalar(jobsdata._repost_keys_pyarrow)


def test_repost_keys_vectorized_treats_a_missing_column_as_blank():
    # No company_name column at all: same "" fallback as the scalar reference
    # via _repost_col, so every key comes out "".
    df = pd.DataFrame({"job_title": ["Data Engineer", "ML Engineer"]})
    assert list(jobsdata._repost_keys_vectorized(df)) == ["", ""]


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


def test_suppress_reposts_collapse_compares_digit_ids_numerically():
    # "9" > "10" as plain strings but must lose the tie numerically.
    df = pd.DataFrame([
        _row("9", "no", "2026-08-20"),
        _row("10", "no", "2026-08-20"),
    ])
    out, hidden = jobsdata.suppress_reposts(df, {}, 30, today=TODAY)
    assert list(out["job_posting_id"]) == ["10"]
    assert hidden == 1


def test_suppress_reposts_collapse_handles_mixed_date_formats():
    # add_extracted_date merges a bare-date source with a full-timestamp one,
    # so the SAME column can hold both shapes. The timestamped row here is the
    # newer one and must survive -- a single-format parse turns it into NaT
    # and silently loses the tie-break to the older bare date.
    df = pd.DataFrame([
        _row("B", "no", "2026-08-01"),
        _row("C", "no", "2026-08-15T10:30:00"),
    ])
    out, hidden = jobsdata.suppress_reposts(df, {}, 30, today=TODAY)
    assert list(out["job_posting_id"]) == ["C"]
    assert hidden == 1


def test_suppress_reposts_handles_a_timezone_mix_in_the_marks_and_the_dates():
    """The live seen.db holds naive and offset-bearing timestamps in one column
    (2026-09-20: `pd.to_datetime(format="mixed")` raised "Mixed timezones
    detected" on the real data and the filter never ran). Both shapes parse,
    both marks block, and the newest extracted_date still wins the collapse."""
    df = pd.DataFrame([
        _row("A", "yes", "2026-08-01"),
        _row("B", "yes", "2026-08-02T09:00:00+00:00"),
        _row("C", "no", "2026-08-15 10:30:00"),
        _row("D", "no", "2026-08-16T10:30:00+00:00"),
        _row("E", "no", "2026-08-17T10:30:00-04:00"),
    ])
    marked_at = {"A": "2026-09-14 08:00:00", "B": "2026-09-15T08:00:00+00:00"}
    out, hidden = jobsdata.suppress_reposts(df, marked_at, 30, today=TODAY)
    assert list(out["job_posting_id"]) == ["A", "B"]      # every unseen repost blocked
    assert hidden == 3
    # with no mark in the window, the collapse keeps the newest of the mixed dates
    out, hidden = jobsdata.suppress_reposts(df.iloc[2:], {}, 30, today=TODAY)
    assert list(out["job_posting_id"]) == ["E"] and hidden == 2
    parsed = jobsdata._mixed_timestamps(pd.Series(["2026-08-01", "2026-08-02T09:00:00+00:00",
                                                   "garbage", None]))
    assert parsed.dt.tz is None
    assert parsed.notna().tolist() == [True, True, False, False]


def test_filter_high_unseen_survives_a_suppression_failure(monkeypatch, caplog):
    """The filter runs on the UI thread with no guard at the call site; a
    failure inside suppression shows the unsuppressed list with a warning
    (until 2026-09-20 the timezone mix raised and the refresh died with it)."""
    df = pd.DataFrame([_row("A", "no", "2026-08-01", score="5"),
                       _row("B", "no", "2026-08-02", score="5")])

    def boom(*a, **k):
        raise ValueError("Mixed timezones detected")

    monkeypatch.setattr(jobsdata, "suppress_reposts", boom)
    with caplog.at_level("WARNING", logger="jobsdata"):
        out, hidden = jobsdata.filter_high_unseen_with_count(
            df, 4, marked_at={"A": "2026-09-14"}, window_days=30, today=TODAY)
    assert sorted(out["job_posting_id"]) == ["A", "B"] and hidden == 0
    assert "repost suppression skipped: Mixed timezones detected" in caplog.text


def test_suppress_reposts_key_source_blocks_regardless_of_the_marked_rows_own_score():
    # The mark itself (A) can sit at any score -- blocking is purely about the
    # repost key -- so key_source must be the FULL frame, ahead of any
    # score-based narrowing the caller has already done.
    full_df = pd.DataFrame([
        _row("A", "yes", "2026-08-01", score="2"),   # marked, low score
        _row("C", "no", "2026-08-20", score="5"),    # unseen repost, high score
    ])
    scored_only = full_df[full_df["score"].astype(int) >= 4].copy()   # excludes A
    marked_at = {"A": "2026-09-14T00:00:00+00:00"}   # 5 days before TODAY
    out, hidden = jobsdata.suppress_reposts(
        scored_only, marked_at, 30, today=TODAY, key_source=full_df)
    assert out.empty
    assert hidden == 1


def test_filter_high_unseen_blocks_a_repost_of_a_low_score_mark():
    # End-to-end reproduction of the reported bug: A is marked seen at score 2
    # (below min_score), C is an unseen score-5 repost of it, marked 5 days
    # ago. C must NOT survive just because A never entered the scored frame.
    df = pd.DataFrame([
        _row("A", "yes", "2026-08-01", score="2"),
        _row("C", "no", "2026-08-20", score="5"),
    ])
    marked_at = {"A": "2026-09-14T00:00:00+00:00"}   # 5 days before TODAY
    out, hidden = jobsdata.filter_high_unseen_with_count(
        df, 4, marked_at=marked_at, window_days=30, today=TODAY)
    assert out.empty
    assert hidden == 1


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
    out = jobsdata.filter_high_unseen(df, 4, marked_at=marked_at, window_days=30, today=TODAY)
    assert out.empty


def test_checkpoint_mark_out_of_window_keeps_newest_unseen_repost():
    df = _checkpoint_df()
    marked_at = {"A": "2026-08-19T00:00:00+00:00"}   # 31 days before TODAY
    out = jobsdata.filter_high_unseen(df, 4, marked_at=marked_at, window_days=30, today=TODAY)
    assert list(out["job_posting_id"]) == ["C"]


def test_filter_high_unseen_window_zero_ignores_marked_at():
    df = _checkpoint_df()
    marked_at = {"A": "2026-09-09T00:00:00+00:00"}
    out = jobsdata.filter_high_unseen(df, 4, marked_at=marked_at, window_days=0, today=TODAY)
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
        df, 4, marked_at=marked_at, window_days=30, today=TODAY)
    assert out.empty
    assert hidden == 2


# --- SP5/MA-3: hand-added (manual-*) rows are never scored, so a score-based
# filter must not hide them, regardless of min_score. They are NOT exempt from
# the is_seen filter -- once dismissed/applied, they leave like any other row.

def test_filter_high_unseen_with_count_always_includes_manual_rows_regardless_of_min_score():
    df = pd.DataFrame([
        _row("manual-1", "no", "2026-08-20", score=""),    # hand-added, never scored
        _row("B", "no", "2026-08-20", score="2"),          # normal row, below min_score
    ])
    out, _hidden = jobsdata.filter_high_unseen_with_count(df, 4)
    assert list(out["job_posting_id"]) == ["manual-1"]


def test_filter_high_unseen_with_count_manual_row_still_hidden_once_seen():
    df = pd.DataFrame([_row("manual-1", "yes", "2026-08-20", score="")])
    out, _hidden = jobsdata.filter_high_unseen_with_count(df, 4)
    assert out.empty


def test_filter_high_unseen_with_count_manual_row_survives_missing_score_column():
    # No "score" column at all -- e.g. the very first hand-added job on a fresh
    # install, before any scored run has ever landed. Must behave like a
    # present-but-blank column: the manual row still shows, is_seen still applies.
    df = pd.DataFrame([
        {"job_posting_id": "manual-1", "is_seen": "no"},
        {"job_posting_id": "2", "is_seen": "no"},
        {"job_posting_id": "manual-2", "is_seen": "yes"},
    ])
    out, _hidden = jobsdata.filter_high_unseen_with_count(df, 4)
    assert list(out["job_posting_id"]) == ["manual-1"]


def test_filter_and_sort_min_score_keeps_manual_rows():
    df = pd.DataFrame([
        {"job_posting_id": "manual-1", "job_title": "Data Analyst",
         "company_name": "Acme", "score": ""},
        {"job_posting_id": "2", "job_title": "ML Engineer",
         "company_name": "Globex", "score": "1"},
    ])
    out = jobsdata.filter_and_sort(df, "", "4", "All", "All", "All", False, None)
    assert list(out["job_posting_id"]) == ["manual-1"]


def test_the_repost_window_counts_days_on_the_utc_clock_its_marks_use(monkeypatch):
    """`marked_at` is UTC, so the default "today" is the UTC date too: in the
    evening on the US east coast the UTC date is already tomorrow, and a local
    date there held a mark in the window a day longer than set."""
    from datetime import datetime, timezone

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(jobsdata, "datetime", _Clock, raising=False)
    df = pd.DataFrame([_row("A", "yes", "2026-07-01"), _row("B", "no", "2026-08-15")])
    marked_at = {"A": "2026-09-20T02:00:00+00:00"}   # one UTC day before the clock
    out, hidden = jobsdata.suppress_reposts(df, marked_at, 1)
    assert list(out["job_posting_id"]) == ["A"] and hidden == 1
    assert jobsdata.blocked_repost_keys(df, marked_at, 1) != set()
