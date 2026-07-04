"""merge_incoming.py — folds ~/incoming outbox files into the VM master / run stats.

Pure pandas + tmp_path. Import from the repo root (the script is deployed standalone
beside scraper.py, so it must not import from local/).
"""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import merge_incoming  # noqa: E402


def _gz(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")


def _setup(tmp_path):
    inc = tmp_path / "incoming"
    master = tmp_path / "linkedin_jobs_master.csv"
    stats = tmp_path / "run_stats.csv"
    return inc, master, stats


def test_idle_run_creates_incoming_and_exits_zero(tmp_path):
    inc, master, stats = _setup(tmp_path)
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert inc.is_dir()


def test_rows_merge_master_wins_and_column_union(tmp_path):
    inc, master, stats = _setup(tmp_path)
    pd.DataFrame([
        {"job_posting_id": "1", "job_title": "VM title", "vm_only_col": "keep"},
    ]).to_csv(master, index=False)
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame([
        {"job_posting_id": "1", "job_title": "LOCAL title", "local_only_col": "x"},
        {"job_posting_id": "2", "job_title": "New local", "local_only_col": "y"},
    ]))
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    got = pd.read_csv(master, dtype={"job_posting_id": str})
    assert sorted(got["job_posting_id"]) == ["1", "2"]
    row1 = got[got["job_posting_id"] == "1"].iloc[0]
    assert row1["job_title"] == "VM title"          # master wins
    assert row1["vm_only_col"] == "keep"            # column union kept both ways
    assert "local_only_col" in got.columns
    assert not (inc / "local_rows_a.csv.gz").exists()  # consumed after the write


def test_rows_merge_str_id_cast(tmp_path):
    inc, master, stats = _setup(tmp_path)
    pd.DataFrame([{"job_posting_id": 1}]).to_csv(master, index=False)  # int on disk
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame([{"job_posting_id": "1"}]))
    merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0)
    got = pd.read_csv(master, dtype={"job_posting_id": str})
    assert len(got) == 1  # "1" and 1 deduped, not kept as twins


def test_rows_merge_missing_master_creates_it(tmp_path):
    inc, master, stats = _setup(tmp_path)
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame([{"job_posting_id": "7", "score": 4}]))
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert list(pd.read_csv(master, dtype=str)["job_posting_id"]) == ["7"]


def test_unreadable_master_aborts_loud_and_keeps_files(tmp_path):
    inc, master, stats = _setup(tmp_path)
    master.write_bytes(b'a,b\n"unclosed quote never ends\nx')  # unparseable CSV
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame([{"job_posting_id": "7"}]))
    rc = merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0)
    assert rc == 1
    assert (inc / "local_rows_a.csv.gz").exists()          # nothing consumed
    assert master.read_bytes().startswith(b"a,b")          # master untouched


def test_bad_rows_file_quarantined_others_merge(tmp_path):
    inc, master, stats = _setup(tmp_path)
    inc.mkdir(parents=True)
    (inc / "local_rows_bad.csv.gz").write_bytes(b"this is not gzip")
    _gz(inc / "local_rows_noid.csv.gz", pd.DataFrame([{"job_title": "no id col"}]))
    _gz(inc / "local_rows_ok.csv.gz", pd.DataFrame([{"job_posting_id": "5"}]))
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert list(pd.read_csv(master, dtype=str)["job_posting_id"]) == ["5"]
    bad = sorted(p.name for p in (inc / "bad").iterdir())
    assert bad == ["local_rows_bad.csv.gz", "local_rows_noid.csv.gz"]
    assert not (inc / "local_rows_ok.csv.gz").exists()


def test_all_duplicate_rows_still_consume_file(tmp_path):
    inc, master, stats = _setup(tmp_path)
    pd.DataFrame([{"job_posting_id": "1", "job_title": "t"}]).to_csv(master, index=False)
    before = master.read_bytes()
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame([{"job_posting_id": "1"}]))
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert not (inc / "local_rows_a.csv.gz").exists()   # re-push is a no-op, file drained
    assert master.read_bytes() == before                 # nothing added -> no rewrite


def test_stats_merge_dedups_on_timestamp_and_input(tmp_path):
    inc, master, stats = _setup(tmp_path)
    pd.DataFrame([
        {"timestamp": "t1", "input_csv": "a.csv", "rows_in": 10},
    ]).to_csv(stats, index=False)
    inc.mkdir(parents=True)
    pd.DataFrame([
        {"timestamp": "t1", "input_csv": "a.csv", "rows_in": 999},   # dupe: existing wins
        {"timestamp": "t2", "input_csv": "b.csv", "rows_in": 20},    # new
    ]).to_csv(inc / "local_stats_x.csv", index=False)
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    got = pd.read_csv(stats)
    assert len(got) == 2
    assert got[got["timestamp"] == "t1"].iloc[0]["rows_in"] == 10
    assert not (inc / "local_stats_x.csv").exists()


def test_stats_created_when_missing(tmp_path):
    inc, master, stats = _setup(tmp_path)
    inc.mkdir(parents=True)
    pd.DataFrame([{"timestamp": "t1", "input_csv": "a.csv"}]).to_csv(
        inc / "local_stats_x.csv", index=False)
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert len(pd.read_csv(stats)) == 1


def test_fresh_file_skipped_not_quarantined(tmp_path):
    # Mid-upload guard: a just-written file (default min_age_seconds=60) is left
    # queued — an scp may still be writing it. No merge, no quarantine, rc 0.
    inc, master, stats = _setup(tmp_path)
    _gz(inc / "local_rows_fresh.csv.gz", pd.DataFrame([{"job_posting_id": "9"}]))
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats) == 0
    assert (inc / "local_rows_fresh.csv.gz").exists()
    assert not (inc / "bad").exists()
    assert not master.exists()
