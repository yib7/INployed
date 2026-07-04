import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
import prune_master as pm

BASE = {"job_posting_id": "1", "job_description_formatted": "FULL <b>desc</b>",
        "job_summary": "short summary", "extracted_date": "2026-06-01",
        "job_posted_date": "2026-06-01T00:00:00.000Z", "score": "8",
        "filtered_out": "False", "reason": "", "url": "http://x"}

def _write(tmp_path, rows):
    p = tmp_path / "m.csv"; pd.DataFrame(rows).to_csv(p, index=False); return p

NOW = datetime(2026, 6, 10, tzinfo=timezone.utc)  # cutoff = 2026-06-07

def test_aged_row_desc_blanked_summary_kept(tmp_path):
    p = _write(tmp_path, [BASE])
    pm.prune(p, retention_days=3, now=NOW)
    df = pd.read_csv(p, dtype=str)
    assert df.loc[0, "job_description_formatted"] in ("", "nan") or pd.isna(df.loc[0, "job_description_formatted"])
    assert df.loc[0, "job_summary"] == "short summary"

def test_fresh_row_untouched(tmp_path):
    row = {**BASE, "extracted_date": "2026-06-09"}
    p = _write(tmp_path, [row]); pm.prune(p, retention_days=3, now=NOW)
    df = pd.read_csv(p, dtype=str)
    assert df.loc[0, "job_description_formatted"] == "FULL <b>desc</b>"

def test_undatable_row_never_stripped(tmp_path):
    row = {**BASE, "extracted_date": "", "job_posted_date": ""}
    p = _write(tmp_path, [row]); pm.prune(p, retention_days=3, now=NOW)
    df = pd.read_csv(p, dtype=str)
    assert df.loc[0, "job_description_formatted"] == "FULL <b>desc</b>"

def test_row_count_preserved_and_atomic(tmp_path):
    p = _write(tmp_path, [BASE, {**BASE, "job_posting_id": "2", "extracted_date": "2026-06-09"}])
    pm.prune(p, retention_days=3, now=NOW)
    assert len(pd.read_csv(p)) == 2
    assert not list(tmp_path.glob("*.tmp"))  # tempfile cleaned

def test_stripped_and_unscored_row_parked(tmp_path):
    row = {**BASE, "score": "", "filtered_out": "False", "reason": ""}
    p = _write(tmp_path, [row]); pm.prune(p, retention_days=3, now=NOW)
    df = pd.read_csv(p, dtype=str)
    assert str(df.loc[0, "filtered_out"]).lower() in ("true", "1")
    assert df.loc[0, "reason"] == "pruned_no_desc"

def test_idempotent(tmp_path):
    p = _write(tmp_path, [BASE])
    a = pm.prune(p, retention_days=3, now=NOW); b = pm.prune(p, retention_days=3, now=NOW)
    assert a["stripped"] == 1 and b["stripped"] == 0

def test_fallback_to_posted_date(tmp_path):
    row = {**BASE, "extracted_date": "", "job_posted_date": "2026-06-01T00:00:00.000Z"}
    p = _write(tmp_path, [row]); pm.prune(p, retention_days=3, now=NOW)
    df = pd.read_csv(p, dtype=str)
    assert df.loc[0, "job_description_formatted"] in ("", "nan") or pd.isna(df.loc[0, "job_description_formatted"])
