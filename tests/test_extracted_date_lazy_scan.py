"""extracted_date derivation must not pay for the per-run scan it doesn't need.

The per-run scan (`extraction_dates_from_runs`) walks sibling morning/evening/…
folders and reads every scored .csv.gz to recover the day a job was first seen.
When those folders live on Google Drive File Stream, that walk blocks for
*minutes* on a cold/streaming mount -- and it runs on the dashboard's UI thread
during window construction, so the window never appears.

But that scan only ever supplies a fallback date for rows whose master row has
NO stored extracted_date. Every job the current scraper writes already carries
one, so the scan is pure waste. These tests pin that: the scan is invoked only
when a row actually lacks a date, and the stored > from_runs > posted priority is
unchanged when it is.
"""
import gzip
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402


def _write_gz(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        df.to_csv(fh, index=False)


# ---- unit: add_extracted_date takes a lazy provider ------------------------

def test_provider_not_called_when_every_row_has_stored_date():
    df = pd.DataFrame({"job_posting_id": ["1", "2"],
                       "extracted_date": ["2026-07-01", "2026-07-02"]})
    calls = []

    def provider():
        calls.append(1)
        return {"1": "2000-01-01", "2": "2000-01-02"}

    out = jobsdata.add_extracted_date(df, provider)

    assert calls == []  # the expensive per-run scan was skipped entirely
    assert list(out["extracted_date"]) == ["2026-07-01", "2026-07-02"]


def test_provider_called_when_a_row_lacks_a_stored_date():
    df = pd.DataFrame({"job_posting_id": ["1", "2"],
                       "extracted_date": ["2026-07-01", ""]})  # row 2 blank

    def provider():
        return {"2": "2026-06-30"}

    out = jobsdata.add_extracted_date(df, provider)

    got = dict(zip(out["job_posting_id"], out["extracted_date"]))
    assert got == {"1": "2026-07-01", "2": "2026-06-30"}


def test_provider_date_beats_posted_date_for_a_blank_stored_row():
    # Priority must stay stored > from_runs > posted: a blank-stored row prefers
    # the scrape date (provider) over job_posted_date.
    df = pd.DataFrame({"job_posting_id": ["2"],
                       "extracted_date": [""],
                       "job_posted_date": ["2026-06-25"]})

    def provider():
        return {"2": "2026-06-30"}

    out = jobsdata.add_extracted_date(df, provider)

    assert out["extracted_date"].iloc[0] == "2026-06-30"


# ---- integration: load_files does not scan runs when the master is dated ----

def test_load_files_skips_run_scan_when_master_rows_are_dated(tmp_path, monkeypatch):
    master = tmp_path / "linkedin_jobs_master.csv.gz"
    _write_gz(master, pd.DataFrame({"job_posting_id": ["a", "b"],
                                    "extracted_date": ["2026-07-01", "2026-07-02"]}))
    called = []
    monkeypatch.setattr(jobsdata, "extraction_dates_from_runs",
                        lambda paths: called.append(1) or {})

    df, _ = jobsdata.load_files([master])

    assert called == []  # no Drive/per-run walk when every row already has a date
    assert set(df["job_posting_id"]) == {"a", "b"}
