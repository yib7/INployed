"""Audit P2-21/P2-22 + BACKLOG chunked-stream item: the whole-master readers now
stream in bounded chunks. These tests force multi-chunk paths (chunk=2) and
assert BEHAVIOR EQUIVALENCE with the old full-read implementations.
"""
import gzip
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import csv_io  # noqa: E402
import jobsdata  # noqa: E402
import outbox  # noqa: E402
import watcher  # noqa: E402


def _write_master(path: Path, rows: list[dict], compression=None) -> None:
    df = pd.DataFrame(rows)
    if compression == "gzip":
        with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
            df.to_csv(fh, index=False)
    else:
        df.to_csv(path, index=False)


_ROWS = [
    {"job_posting_id": "1", "job_title": "A", "score": "5", "is_seen": "yes"},
    {"job_posting_id": "2", "job_title": "B", "score": "3", "is_seen": "no"},
    {"job_posting_id": "3", "job_title": "C", "score": "9", "is_seen": "no"},
    {"job_posting_id": "4", "job_title": "D", "score": "", "is_seen": "no"},
    {"job_posting_id": "5", "job_title": "E", "score": "8", "is_seen": "yes"},
]


# ── watcher.has_unseen_high_score (P2-21) ────────────────────────────────────

def test_high_score_probe_finds_hit_beyond_first_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "_SCAN_CHUNK", 2)
    p = tmp_path / "master.csv.gz"
    _write_master(p, _ROWS, compression="gzip")
    assert watcher.has_unseen_high_score(p, 9) is True    # row 3, chunk 2
    assert watcher.has_unseen_high_score(p, 10) is False  # nothing that high
    # unseen NaN-score row never counts; seen high row never counts
    assert watcher.has_unseen_high_score(p, 8) is True    # 9 >= 8 (row 3)


def test_high_score_probe_missing_columns_is_false(tmp_path):
    p = tmp_path / "master.csv"
    pd.DataFrame({"job_posting_id": ["1"]}).to_csv(p, index=False)
    assert watcher.has_unseen_high_score(p, 5) is False


# ── csv_io.reconcile_file (P2-21) ────────────────────────────────────────────

class _Reg:
    def __init__(self, ids):
        self._ids = set(ids)

    def all_ids(self):
        return set(self._ids)


def test_reconcile_file_chunked_marks_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_io, "_RECONCILE_CHUNK", 2)
    p = tmp_path / "master.csv.gz"
    _write_master(p, _ROWS, compression="gzip")
    n = csv_io.reconcile_file(p, _Reg({"2", "3"}))
    assert n == 2
    back = pd.read_csv(p, dtype=str)
    assert list(back["is_seen"]) == ["yes", "yes", "yes", "no", "yes"]
    assert list(back["job_posting_id"]) == ["1", "2", "3", "4", "5"]


def test_reconcile_file_no_change_means_no_rewrite(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_io, "_RECONCILE_CHUNK", 2)
    p = tmp_path / "master.csv.gz"
    _write_master(p, _ROWS, compression="gzip")
    before = p.read_bytes()
    assert csv_io.reconcile_file(p, _Reg({"1", "5"})) == 0   # already seen
    assert p.read_bytes() == before                          # untouched
    assert not list(tmp_path.glob("*.tmp"))                  # tmp cleaned up


# ── outbox.write_rows_outbox (P2-22) ─────────────────────────────────────────

def test_rows_outbox_chunked_matches_ids_across_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "_ROWS_CHUNK", 2)
    master = tmp_path / "master.csv"
    _write_master(master, _ROWS)
    out = outbox.write_rows_outbox(["1", "5"], master_csv=master,
                                   outbox_dir=tmp_path / "ob")
    got = pd.read_csv(out, dtype=str, compression="gzip")
    assert sorted(got["job_posting_id"]) == ["1", "5"]
    assert sorted(got["job_title"]) == ["A", "E"]


# ── jobsdata append/drop streaming rewrites ──────────────────────────────────

def test_append_dedup_streams_and_keeps_first(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "_RW_CHUNK", 2)
    master = tmp_path / "master.csv"
    _write_master(master, _ROWS + [dict(_ROWS[1])])   # a pre-existing duplicate of id 2
    added = jobsdata.append_manual_job(
        {"job_posting_id": "6", "job_title": "F", "score": "7"}, master_csv=master)
    assert added is True
    back = pd.read_csv(master, dtype=str)
    assert list(back["job_posting_id"]) == ["1", "2", "3", "4", "5", "6"]  # dupe gone
    # re-adding an existing id: no duplicate, returns False
    assert jobsdata.append_manual_job(
        {"job_posting_id": "3", "job_title": "ZZZ"}, master_csv=master) is False
    back = pd.read_csv(master, dtype=str)
    assert list(back["job_posting_id"]) == ["1", "2", "3", "4", "5", "6"]
    assert back.loc[back["job_posting_id"] == "3", "job_title"].iloc[0] == "C"


def test_drop_ids_streams_across_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "_RW_CHUNK", 2)
    master = tmp_path / "master.csv"
    _write_master(master, _ROWS)
    jobsdata._drop_ids_from_csv(master, {"2", "5"})
    back = pd.read_csv(master, dtype=str)
    assert list(back["job_posting_id"]) == ["1", "3", "4"]
    before = master.read_bytes()
    jobsdata._drop_ids_from_csv(master, {"999"})      # no match → untouched
    assert master.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))
