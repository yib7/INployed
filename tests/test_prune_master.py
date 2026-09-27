import pandas as pd
from datetime import datetime, timezone
import prune_master as pm

BASE = {"job_posting_id": "1", "job_description_formatted": "FULL <b>desc</b>",
        "job_summary": "short summary", "extracted_date": "2026-06-01",
        "job_posted_date": "2026-06-01T00:00:00.000Z", "score": "8",
        "filtered_out": "False", "reason": "", "url": "http://x"}

def _write(tmp_path, rows):
    p = tmp_path / "m.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    return p

NOW = datetime(2026, 6, 10, tzinfo=timezone.utc)  # cutoff = 2026-06-07

def test_aged_row_desc_blanked_summary_kept(tmp_path):
    p = _write(tmp_path, [BASE])
    pm.prune(p, retention_days=3, now=NOW)
    df = pd.read_csv(p, dtype=str)
    assert df.loc[0, "job_description_formatted"] in ("", "nan") or pd.isna(df.loc[0, "job_description_formatted"])
    assert df.loc[0, "job_summary"] == "short summary"

def test_fresh_row_untouched(tmp_path):
    row = {**BASE, "extracted_date": "2026-06-09"}
    p = _write(tmp_path, [row])
    pm.prune(p, retention_days=3, now=NOW)
    df = pd.read_csv(p, dtype=str)
    assert df.loc[0, "job_description_formatted"] == "FULL <b>desc</b>"

def test_undatable_row_never_stripped(tmp_path):
    row = {**BASE, "extracted_date": "", "job_posted_date": ""}
    p = _write(tmp_path, [row])
    pm.prune(p, retention_days=3, now=NOW)
    df = pd.read_csv(p, dtype=str)
    assert df.loc[0, "job_description_formatted"] == "FULL <b>desc</b>"

def test_row_count_preserved_and_atomic(tmp_path):
    p = _write(tmp_path, [BASE, {**BASE, "job_posting_id": "2", "extracted_date": "2026-06-09"}])
    pm.prune(p, retention_days=3, now=NOW)
    assert len(pd.read_csv(p)) == 2
    assert not list(tmp_path.glob("*.tmp"))  # tempfile cleaned

def test_stripped_and_unscored_row_parked(tmp_path):
    row = {**BASE, "score": "", "filtered_out": "False", "reason": ""}
    p = _write(tmp_path, [row])
    pm.prune(p, retention_days=3, now=NOW)
    df = pd.read_csv(p, dtype=str)
    assert str(df.loc[0, "filtered_out"]).lower() in ("true", "1")
    assert df.loc[0, "reason"] == "pruned_no_desc"

def test_idempotent(tmp_path):
    p = _write(tmp_path, [BASE])
    a = pm.prune(p, retention_days=3, now=NOW)
    b = pm.prune(p, retention_days=3, now=NOW)
    assert a["stripped"] == 1 and b["stripped"] == 0

def test_fallback_to_posted_date(tmp_path):
    row = {**BASE, "extracted_date": "", "job_posted_date": "2026-06-01T00:00:00.000Z"}
    p = _write(tmp_path, [row])
    pm.prune(p, retention_days=3, now=NOW)
    df = pd.read_csv(p, dtype=str)
    assert df.loc[0, "job_description_formatted"] in ("", "nan") or pd.isna(df.loc[0, "job_description_formatted"])

# P2-11: filtered_out truth-vocabulary must recognise the float-upcast ("1.0")
# and trailing-space ("True ") spellings prune itself may have to skip -- kept
# consistent with score_jobs.rows_needing_rescore so prune-written filtered rows
# are not re-parked/retried forever.
def test_needs_rescore_treats_float_and_padded_filtered_out_as_filtered():
    chunk = pd.DataFrame([
        {"job_posting_id": "1", "score": "", "filtered_out": "1.0"},
        {"job_posting_id": "2", "score": "", "filtered_out": "True "},
        {"job_posting_id": "3", "score": "", "filtered_out": "False"},
    ])
    needs = pm._needs_rescore(chunk)
    assert not bool(needs.iloc[0])   # "1.0" -> already filtered
    assert not bool(needs.iloc[1])   # "True " -> already filtered
    assert bool(needs.iloc[2])       # "False" + unscored -> needs rescore


# P1-2: chunk.get(COL) returns a bare None for an absent column, and pandas
# turns that into a NaT/nan *scalar* whose .fillna/.isna raises AttributeError.
# Both shapes are reachable: `score` only exists after score_jobs.py has run,
# and the seen.db/CSV rebuild recipes can produce a master without
# `extracted_date`. run_scraper.sh swallows the exit code, so a crash here
# means the retention prune silently stops running.
def test_master_without_extracted_date_does_not_crash(tmp_path):
    row = {k: v for k, v in BASE.items() if k != "extracted_date"}
    p = _write(tmp_path, [row])
    r = pm.prune(p, retention_days=3, now=NOW)
    assert r["rows"] == 1
    assert r["stripped"] == 1          # falls back to job_posted_date
    assert len(pd.read_csv(p)) == 1

def test_master_without_score_column_does_not_crash(tmp_path):
    row = {k: v for k, v in BASE.items() if k != "score"}
    p = _write(tmp_path, [row])
    r = pm.prune(p, retention_days=3, now=NOW)
    assert r["rows"] == 1
    assert r["parked"] == 1            # no score at all -> park the aged row
    assert len(pd.read_csv(p)) == 1

def test_master_with_neither_date_column_strips_nothing(tmp_path):
    row = {k: v for k, v in BASE.items()
           if k not in ("extracted_date", "job_posted_date")}
    p = _write(tmp_path, [row])
    r = pm.prune(p, retention_days=3, now=NOW)
    assert r["stripped"] == 0          # undatable -> never stripped
    df = pd.read_csv(p, dtype=str)
    assert df.loc[0, "job_description_formatted"] == "FULL <b>desc</b>"

def test_main_reports_one_line_on_a_shape_surprise(tmp_path, capsys, monkeypatch):
    p = _write(tmp_path, [BASE])
    monkeypatch.setattr(pm, "prune",
                        lambda *a, **k: (_ for _ in ()).throw(AttributeError("boom")))
    rc = pm.main(["--master", str(p)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "prune_master: cannot process" in err and "AttributeError" in err


# MA-3: hand-added jobs (manual- ids, local/manual_add.py) keep blank score
# columns on purpose. The rescore pass (score_jobs.rows_needing_rescore) and the
# prune's park step (_needs_rescore) both skip them, and the two readers agree
# row for row on rows without an ERROR marker.
_RESCORE_ROWS = [
    {"job_posting_id": "1", "score": "", "filtered_out": ""},              # never scored
    {"job_posting_id": "2", "score": "4", "filtered_out": ""},             # scored
    {"job_posting_id": "3", "score": "", "filtered_out": "True "},         # filtered
    {"job_posting_id": "4", "score": "", "filtered_out": "1.0"},           # filtered
    {"job_posting_id": "5", "score": "nan", "filtered_out": "False"},      # never scored
    {"job_posting_id": "manual-1a2b", "score": "", "filtered_out": ""},    # hand-added
    {"job_posting_id": " manual-3c4d ", "score": "", "filtered_out": "False"},
    {"job_posting_id": "manual-5e6f", "score": "3", "filtered_out": ""},
    {"job_posting_id": "", "score": "", "filtered_out": ""},               # no id
    {"job_posting_id": "manualx-9", "score": "", "filtered_out": ""},      # not the prefix
]


def _both_reads(tmp_path, rows):
    p = tmp_path / "rescore.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    return (pd.read_csv(p, dtype=str, keep_default_na=False),      # how prune reads
            pd.read_csv(p, dtype={"job_posting_id": str}))         # how the rescore pass reads


def test_needs_rescore_agrees_with_rows_needing_rescore_and_skips_manual_rows(tmp_path):
    import score_jobs
    for frame in _both_reads(tmp_path, _RESCORE_ROWS):
        prune_mask = pm._needs_rescore(frame).tolist()
        rescore_mask = frame.index.isin(score_jobs.rows_needing_rescore(frame).index).tolist()
        assert prune_mask == rescore_mask
        picked = frame.loc[prune_mask, "job_posting_id"].fillna("").tolist()
        assert picked == ["1", "5", "", "manualx-9"]


def test_the_rescore_pass_skips_a_manual_row_even_with_an_error_marker():
    import score_jobs
    master = pd.DataFrame({"job_posting_id": ["manual-1a2b", "7", "manual-3c4d"],
                           "score": [None, None, 4.0],
                           "filtered_out": [False, False, False],
                           "reason": ["ERROR: old failure", "ERROR: old failure", ""],
                           "recommendation": ["", "", "ERROR: old failure"]})
    assert score_jobs.rows_needing_rescore(master)["job_posting_id"].tolist() == ["7"]


def test_a_master_without_ids_reads_every_blank_score_as_needing_a_rescore():
    import score_jobs
    frame = pd.DataFrame({"score": ["", "4"], "filtered_out": ["", ""]})
    assert pm._needs_rescore(frame).tolist() == [True, False]
    assert score_jobs.rows_needing_rescore(frame).index.tolist() == [0]


def test_the_manual_prefix_is_the_one_manual_add_writes():
    import manual_add
    import score_jobs
    assert pm.MANUAL_ID_PREFIX == score_jobs.MANUAL_ID_PREFIX == manual_add._MANUAL_ID_PREFIX


def test_prune_never_parks_an_aged_hand_added_row(tmp_path):
    row = {**BASE, "job_posting_id": "manual-1a2b", "score": "", "filtered_out": "",
           "reason": ""}
    p = _write(tmp_path, [row])
    r = pm.prune(p, retention_days=3, now=NOW)
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    assert r["parked"] == 0
    assert (df.loc[0, "filtered_out"], df.loc[0, "reason"]) == ("", "")
