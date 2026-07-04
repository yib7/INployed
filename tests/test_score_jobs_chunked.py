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
