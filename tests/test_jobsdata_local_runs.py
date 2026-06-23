"""local_run_files(): the dashboard must pick up a LOCAL scrape's repo-dir output.

A local "Run scraper" writes <repo>/<label>/*_scored.csv.gz instead of the synced
Drive folder; this is what bridges that gap so the new jobs show up.
"""
import gzip
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402


def _write_gz(path: Path, ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"job_posting_id": ids, "job_title": ids, "score": ["5"] * len(ids)})
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        df.to_csv(fh, index=False)


def test_finds_scored_gz_per_label_ignoring_non_scored(tmp_path):
    _write_gz(tmp_path / "evening" / "linkedin_jobs_2026-06-23_evening_scored.csv.gz", ["1"])
    _write_gz(tmp_path / "morning" / "linkedin_jobs_2026-06-23_morning_scored.csv.gz", ["2"])
    # a raw (un-scored) run file must NOT be picked up
    _write_gz(tmp_path / "evening" / "linkedin_jobs_2026-06-23_evening.csv.gz", ["3"])

    found = jobsdata.local_run_files(tmp_path)
    names = {p.name for p in found}
    assert "linkedin_jobs_2026-06-23_evening_scored.csv.gz" in names
    assert "linkedin_jobs_2026-06-23_morning_scored.csv.gz" in names
    assert "linkedin_jobs_2026-06-23_evening.csv.gz" not in names


def test_empty_when_no_label_dirs(tmp_path):
    assert jobsdata.local_run_files(tmp_path) == []


def test_local_runs_merge_into_load_files(tmp_path):
    # a Drive "master" + a local scored run -> both sets of jobs, deduped by id
    master = tmp_path / "linkedin_jobs_master.csv.gz"
    _write_gz(master, ["a", "b"])
    local = tmp_path / "evening" / "linkedin_jobs_2026-06-23_evening_scored.csv.gz"
    _write_gz(local, ["b", "c"])  # 'b' overlaps -> must not double-count

    df, _ = jobsdata.load_files([master, *jobsdata.local_run_files(tmp_path)])
    assert set(df["job_posting_id"]) == {"a", "b", "c"}
