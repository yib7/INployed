import asyncio

import pandas as pd

import score_jobs


def _full_load_reference(master, scored):
    """The CURRENT (pre-chunking) full-load semantics of update_master_scores,
    for equivalence checking against the chunked rewrite. Mirrors score_jobs.py
    lines ~560-576 exactly (cols selection, dedupe, add_cols, update)."""
    cols = [c for c in score_jobs.SCORE_COLS if c in scored.columns and c != "is_seen"]
    s = scored[["job_posting_id"] + cols].copy()
    s["job_posting_id"] = s["job_posting_id"].astype(str)
    s = s.drop_duplicates(subset=["job_posting_id"], keep="last").set_index("job_posting_id")

    master = master.copy()
    master["job_posting_id"] = master["job_posting_id"].astype(str)
    for c in cols:
        if c not in master.columns:
            master[c] = pd.NA
    master = master.set_index("job_posting_id")
    master.update(s)
    return master.reset_index(), cols


def test_chunked_matches_full_load_reference(tmp_path, monkeypatch):
    """Contract 1: chunked result frame-equals the current full-load
    master.set_index(...).update(s) result for ids present in `scored`."""
    # Job 2 already carries a prior score/recommendation (as a real master would
    # after an earlier run) so the CSV round-trip infers an object dtype for
    # these columns rather than an all-NaN float64 column -- matching how a
    # production master.csv actually looks (mixed scored/unscored rows), and
    # avoiding a pandas 3.0.3 update() dtype-upcast error that is orthogonal to
    # chunking (reproduced identically against the pre-chunking full-load code).
    master_df = pd.DataFrame({
        "job_posting_id": ["1", "2", "3", "4", "5"],
        "job_title": ["a", "b", "c", "d", "e"],
        "score": [None, 1, None, None, None],
        "recommendation": [None, "skip", None, None, None],
    })
    m = tmp_path / "linkedin_jobs_master.csv"
    master_df.to_csv(m, index=False)
    monkeypatch.setattr(score_jobs, "MASTER_CSV", m)
    monkeypatch.setattr(score_jobs, "CHUNK", 2)  # force multi-chunk (5 rows / 2 per chunk)

    scored = pd.DataFrame([
        {"job_posting_id": "1", "score": 5, "recommendation": "apply"},
        {"job_posting_id": "3", "score": 2, "recommendation": "skip"},
        {"job_posting_id": "5", "score": 4, "recommendation": "apply"},
    ])

    score_jobs.update_master_scores(scored)

    got = pd.read_csv(m, dtype={"job_posting_id": str}).sort_values("job_posting_id").reset_index(drop=True)
    ref, cols = _full_load_reference(master_df, scored)
    ref = ref.sort_values("job_posting_id").reset_index(drop=True)

    # "frame-equals" means NaN == NaN here (both sides "no value"), not literal
    # Python equality, which always treats float('nan') != float('nan').
    for c in ["job_posting_id"] + cols:
        got_vals, ref_vals = got[c].tolist(), ref[c].tolist()
        assert len(got_vals) == len(ref_vals), c
        for g, r in zip(got_vals, ref_vals):
            same = (pd.isna(g) and pd.isna(r)) or g == r
            assert same, (c, g, r)


def test_bool_filter_column_into_float64_master_does_not_raise(tmp_path, monkeypatch):
    """Regression: the master's boolean filter columns (filter_*, filtered_out)
    read back as float64 for a chunk of older rows that never carried them (the
    column is all-empty in that chunk), while a fresh scored frame carries them
    as real bool -- exactly what add_filter_columns produces. On pandas >= 3
    DataFrame.update refuses to write bool into a float64 block, and the numeric
    guard missed it because is_numeric_dtype(bool) is True. update_master_scores
    must widen those columns and complete without raising, landing the values.

    This is the production crash that stalled the whole VM pipeline: score_jobs.py
    died here (before the master + run-file uploads), so nothing synced to Drive.
    """
    master_df = pd.DataFrame({
        "job_posting_id": ["1", "2", "3"],
        "job_title": ["a", "b", "c"],
        "filtered_out": [None, None, None],       # all-empty -> reads back float64
        "filter_clearance": [None, None, None],
    })
    m = tmp_path / "linkedin_jobs_master.csv"
    master_df.to_csv(m, index=False)
    monkeypatch.setattr(score_jobs, "MASTER_CSV", m)
    monkeypatch.setattr(score_jobs, "CHUNK", 2)  # force multi-chunk

    scored = pd.DataFrame({
        "job_posting_id": ["1", "2"],
        "filtered_out": [True, False],
        "filter_clearance": [False, True],
    })
    assert scored["filtered_out"].dtype == bool  # as add_filter_columns produces

    score_jobs.update_master_scores(scored)  # must NOT raise TypeError

    out = pd.read_csv(m, dtype={"job_posting_id": str}).set_index("job_posting_id")
    # scored ids landed (bool serialized through the object-widened column);
    # compare via str() so it holds whether read-back infers bool or "True"/"False".
    assert str(out.loc["1", "filtered_out"]) == "True"
    assert str(out.loc["2", "filtered_out"]) == "False"
    assert str(out.loc["1", "filter_clearance"]) == "False"
    assert str(out.loc["2", "filter_clearance"]) == "True"
    assert pd.isna(out.loc["3", "filtered_out"])  # id not in scored -> untouched


def test_rows_absent_from_scored_are_unchanged(tmp_path, monkeypatch):
    """Contract 2: master rows whose id is not in `scored` keep their existing
    values untouched."""
    master_df = pd.DataFrame({
        "job_posting_id": ["1", "2", "3"],
        "job_title": ["a", "b", "c"],
        "score": [3, 3, 3],
    })
    m = tmp_path / "linkedin_jobs_master.csv"
    master_df.to_csv(m, index=False)
    monkeypatch.setattr(score_jobs, "MASTER_CSV", m)

    scored = pd.DataFrame([{"job_posting_id": "2", "score": 9}])
    score_jobs.update_master_scores(scored)

    out = pd.read_csv(m, dtype={"job_posting_id": str}).set_index("job_posting_id")
    assert out.loc["1", "score"] == 3
    assert out.loc["3", "score"] == 3
    assert out.loc["1", "job_title"] == "a"
    assert out.loc["3", "job_title"] == "c"
    assert out.loc["2", "score"] == 9  # sanity: the actual update did land


def test_is_seen_never_touched_even_when_present_in_both(tmp_path, monkeypatch):
    """Contract 3: is_seen is NEVER folded in by scoring, even when it is
    present in both master and scored."""
    master_df = pd.DataFrame([
        {"job_posting_id": "1", "job_title": "a", "is_seen": "yes"},
    ])
    m = tmp_path / "linkedin_jobs_master.csv"
    master_df.to_csv(m, index=False)
    monkeypatch.setattr(score_jobs, "MASTER_CSV", m)

    # scored carries is_seen (e.g. a fresh-scrape rescoring pass sets is_seen="no"
    # on the whole per-run frame) alongside a real score update.
    scored = pd.DataFrame([{"job_posting_id": "1", "score": 5, "is_seen": "no"}])
    score_jobs.update_master_scores(scored)

    out = pd.read_csv(m, dtype={"job_posting_id": str}).set_index("job_posting_id")
    assert out.loc["1", "score"] == 5       # the real update still lands
    assert out.loc["1", "is_seen"] == "yes"  # is_seen in the master is untouched


def test_multichunk_updates_span_chunk_boundaries(tmp_path, monkeypatch):
    """Contract 4: with CHUNK=2 and 5 master rows, updates whose ids fall in
    different chunks (rows 0-1, 2-3, 4) must all land correctly."""
    master_df = pd.DataFrame({
        "job_posting_id": ["1", "2", "3", "4", "5"],
        "job_title": ["a", "b", "c", "d", "e"],
        "score": [0, 0, 0, 0, 0],
    })
    m = tmp_path / "linkedin_jobs_master.csv"
    master_df.to_csv(m, index=False)
    monkeypatch.setattr(score_jobs, "MASTER_CSV", m)
    monkeypatch.setattr(score_jobs, "CHUNK", 2)  # chunks: [1,2] [3,4] [5]

    # one id per chunk, so the update genuinely spans chunk boundaries
    scored = pd.DataFrame([
        {"job_posting_id": "1", "score": 11},  # chunk 1
        {"job_posting_id": "4", "score": 44},  # chunk 2
        {"job_posting_id": "5", "score": 55},  # chunk 3
    ])
    score_jobs.update_master_scores(scored)

    out = pd.read_csv(m, dtype={"job_posting_id": str}).set_index("job_posting_id")
    assert out.loc["1", "score"] == 11
    assert out.loc["2", "score"] == 0    # unchanged, same chunk as id 1
    assert out.loc["3", "score"] == 0    # unchanged
    assert out.loc["4", "score"] == 44
    assert out.loc["5", "score"] == 55
    # all 5 rows must still be present -- no rows dropped by the chunking
    assert len(out) == 5


def test_score_column_missing_from_master_header_gets_added(tmp_path, monkeypatch):
    """Contract 5: a score column present in `scored` but absent from the
    master's header is added (the add_cols path), populated for scored ids."""
    master_df = pd.DataFrame({
        "job_posting_id": ["1", "2"],
        "job_title": ["a", "b"],
    })  # no "deep_score" column at all
    m = tmp_path / "linkedin_jobs_master.csv"
    master_df.to_csv(m, index=False)
    monkeypatch.setattr(score_jobs, "MASTER_CSV", m)

    scored = pd.DataFrame([{"job_posting_id": "1", "deep_score": 8}])
    score_jobs.update_master_scores(scored)

    out = pd.read_csv(m, dtype={"job_posting_id": str}).set_index("job_posting_id")
    assert "deep_score" in out.columns
    assert out.loc["1", "deep_score"] == 8
    assert pd.isna(out.loc["2", "deep_score"])  # id not in scored -> NA, not dropped


def test_multichunk_no_leftover_tmp_file_in_master_dir(tmp_path, monkeypatch):
    """The chunked write must be atomic: no stray .tmp file left behind in the
    master's directory after a successful run."""
    master_df = pd.DataFrame({
        "job_posting_id": ["1", "2", "3"],
        "score": [0, 0, 0],
    })
    m = tmp_path / "linkedin_jobs_master.csv"
    master_df.to_csv(m, index=False)
    monkeypatch.setattr(score_jobs, "MASTER_CSV", m)
    monkeypatch.setattr(score_jobs, "CHUNK", 2)

    scored = pd.DataFrame([{"job_posting_id": "2", "score": 9}])
    score_jobs.update_master_scores(scored)

    leftovers = [p for p in tmp_path.iterdir() if p.name != "linkedin_jobs_master.csv"]
    assert leftovers == []


# --- Task 4: usecols two-pass rescore_master_failures --------------------
#
# rescore_master_failures's tail calls Gemini scoring (run_scoring). None of
# these tests may let that happen for real: test_rescore_candidates_match_full_load
# and test_rescore_skips_big_columns stub run_scoring with a fake that just
# records which ids it was asked to score and returns an inert scored frame,
# so nothing leaves the machine and no real spend occurs.

def _rescore_master_df(n=6):
    """A small master frame with the two big text columns present (as the real
    master always has), plus a mix of already-scored, never-scored (NaN
    score, not filtered), an ERROR: row, and a retention-filtered row (which
    must NOT re-enter the candidate set even though its score is NaN)."""
    return pd.DataFrame({
        "job_posting_id": [str(i) for i in range(1, n + 1)],
        "job_title": [f"title {i}" for i in range(1, n + 1)],
        "job_description_formatted": [f"<p>desc {i}</p>" * 20 for i in range(1, n + 1)],
        "job_summary": [f"summary {i}" for i in range(1, n + 1)],
        "score": [5, None, None, 3, None, None],
        "filtered_out": [False, False, False, False, False, True],  # row 6: retention-stripped
        "reason": ["ok", None, "ERROR: boom", "ok", None, "filtered_out"],
        "recommendation": ["apply", None, None, "skip", None, None],
    })


def _fake_run_scoring_recording(calls_list):
    """Returns an async run_scoring stub that records the ids it was asked to
    score and returns df unchanged plus inert score columns -- no network."""
    async def _run_scoring(pool, resume, df):
        calls_list.append(sorted(df["job_posting_id"].tolist()))
        out = df.copy()
        out["score"] = 1
        out["reason"] = "stub"
        out["deep_score"] = None
        out["strengths"] = ""
        out["gaps"] = ""
        out["recommendation"] = "stub"
        return out
    return _run_scoring


def test_rescore_candidates_match_full_load(tmp_path, monkeypatch):
    """Candidates found via the light usecols read (as exercised by the real
    rescore_master_failures) equal those from rows_needing_rescore(full_master),
    tail-capped at RESCORE_CAP -- i.e. the two-pass read changes nothing about
    *which* rows get rescored."""
    master_df = _rescore_master_df()
    m = tmp_path / "linkedin_jobs_master.csv"
    master_df.to_csv(m, index=False)
    monkeypatch.setattr(score_jobs, "MASTER_CSV", m)

    calls = []
    monkeypatch.setattr(score_jobs, "run_scoring", _fake_run_scoring_recording(calls))

    asyncio.run(score_jobs.rescore_master_failures(pool=None, resume="resume"))

    assert len(calls) == 1
    got_ids = set(calls[0])

    expected_ids = set(
        score_jobs.rows_needing_rescore(master_df)["job_posting_id"]
        .astype(str)
        .tail(score_jobs.RESCORE_CAP)
        .tolist()
    )
    assert got_ids == expected_ids
    assert got_ids == {"2", "3", "5"}  # rows 1,4 already scored; row 6 retention-filtered


def test_rescore_skips_big_columns(tmp_path, monkeypatch):
    """The candidate-finding read must request neither job_description_formatted
    nor job_summary -- those are the two ~90 MB text columns this task exists
    to skip during candidate selection."""
    master_df = _rescore_master_df()
    m = tmp_path / "linkedin_jobs_master.csv"
    master_df.to_csv(m, index=False)
    monkeypatch.setattr(score_jobs, "MASTER_CSV", m)

    monkeypatch.setattr(score_jobs, "run_scoring", _fake_run_scoring_recording([]))

    calls = []
    real_read_csv = pd.read_csv

    def recording_read_csv(*args, **kwargs):
        calls.append(kwargs.get("usecols"))
        return real_read_csv(*args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", recording_read_csv)

    asyncio.run(score_jobs.rescore_master_failures(pool=None, resume="resume"))

    # The only usecols-restricted read is the light candidate-finding pass
    # (the nrows=0 header probe and the full-row _load_rows_by_id read pass
    # usecols=None). At least one call must have restricted usecols, and none
    # of the restricted-usecols calls may include the big text columns.
    restricted = [u for u in calls if u is not None]
    assert restricted, "expected at least one usecols-restricted read"
    for usecols in restricted:
        assert "job_description_formatted" not in usecols
        assert "job_summary" not in usecols


def test_load_rows_by_id_returns_only_requested(tmp_path, monkeypatch):
    """_load_rows_by_id returns exactly the requested ids with full columns,
    reading the master in chunks (CHUNK forced small to guarantee the
    requested ids are split across multiple chunks)."""
    master_df = pd.DataFrame({
        "job_posting_id": [str(i) for i in range(1, 8)],
        "job_title": [f"title {i}" for i in range(1, 8)],
        "job_description_formatted": [f"desc {i}" for i in range(1, 8)],
    })
    m = tmp_path / "linkedin_jobs_master.csv"
    master_df.to_csv(m, index=False)
    monkeypatch.setattr(score_jobs, "CHUNK", 2)  # 7 rows / 2 per chunk -> 4 chunks

    # ids 1 and 7 fall in the first and last chunk respectively, so a
    # correct implementation must scan every chunk, not just the first.
    got = score_jobs._load_rows_by_id(m, ["1", "7"])

    assert sorted(got["job_posting_id"].tolist()) == ["1", "7"]
    assert list(got.columns) == list(master_df.columns)
    assert got.loc[got["job_posting_id"] == "1", "job_title"].iloc[0] == "title 1"
    assert got.loc[got["job_posting_id"] == "7", "job_title"].iloc[0] == "title 7"
