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
