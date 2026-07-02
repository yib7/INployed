"""P1-2: csv_io.write_csv_gz_atomic generalized with a `compression` parameter so
_drop_ids_from_csv and _append_dedup_csv (local/jobsdata.py) can reuse the same
atomic tmp+os.replace helper for PLAIN csv writes, not just gz. Default stays gz
so both existing call sites (csv_io.reconcile_file, qt/main_window._write_is_seen)
keep working unchanged.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import csv_io  # noqa: E402


def test_write_csv_gz_atomic_default_is_still_gzip(tmp_path):
    path = tmp_path / "out.csv.gz"
    df = pd.DataFrame([{"job_posting_id": "1", "score": 5}])
    csv_io.write_csv_gz_atomic(df, path)
    round_tripped = pd.read_csv(path, compression="gzip", dtype={"job_posting_id": str})
    assert list(round_tripped["job_posting_id"]) == ["1"]


def test_write_csv_gz_atomic_compression_none_writes_plain_csv(tmp_path):
    path = tmp_path / "out.csv"
    df = pd.DataFrame([{"job_posting_id": "1", "job_title": "A"},
                       {"job_posting_id": "2", "job_title": "B"}])
    csv_io.write_csv_gz_atomic(df, path, compression=None)
    # A plain (non-gzip) CSV must be readable WITHOUT compression="gzip" -- if the
    # helper still hardcoded gzip, this read would raise (bad gzip magic number).
    round_tripped = pd.read_csv(path, dtype={"job_posting_id": str})
    assert list(round_tripped["job_posting_id"]) == ["1", "2"]
    assert list(round_tripped["job_title"]) == ["A", "B"]


def test_write_csv_gz_atomic_plain_csv_is_atomic_no_leftovers(tmp_path):
    path = tmp_path / "out.csv"
    df = pd.DataFrame([{"job_posting_id": "1"}])
    csv_io.write_csv_gz_atomic(df, path, compression=None)
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.csv"]
    assert leftovers == []


def test_write_csv_gz_atomic_cleans_up_tmp_on_failure(monkeypatch, tmp_path):
    path = tmp_path / "out.csv"
    path.write_text("job_posting_id\n1\n", encoding="utf-8")
    before = path.read_bytes()

    def boom(self, *a, **k):
        raise ValueError("kaboom mid-write")
    monkeypatch.setattr(pd.DataFrame, "to_csv", boom)

    df = pd.DataFrame([{"job_posting_id": "2"}])
    with pytest.raises(ValueError):
        csv_io.write_csv_gz_atomic(df, path, compression=None)

    assert path.read_bytes() == before
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.csv"]
    assert leftovers == []


# MB-1: pd.concat([existing, df]) in scraper.py's append_to_master column-unions a
# fresh row against a master that already HAS an is_seen column -- the fresh row
# gets is_seen=NaN (no per-row fill), not "no". score_jobs.py's update_master_scores
# deliberately never sets is_seen (that is local triage state -- see its docstring),
# so nothing downstream ever stamps it. Every local consumer tests
# astype(str) == "no"; NaN -> "nan" -> False, so the row is silently invisible to
# both the High Score tab (jobsdata.filter_high_unseen) and the watcher popup
# (watcher.has_unseen_high_score). read_csv_gz is the one function both paths read
# the master through, so it is the chokepoint that must normalize NaN -> "no".

def test_read_csv_gz_normalizes_nan_is_seen_to_no(tmp_path):
    path = tmp_path / "master.csv.gz"
    # Mimics pd.concat column-union: the row's is_seen was never set, so it lands
    # as NaN once the frame round-trips through a real CSV (not just in-memory).
    df = pd.DataFrame([
        {"job_posting_id": "1", "score": 5, "is_seen": pd.NA},
        {"job_posting_id": "2", "score": 5, "is_seen": "yes"},
    ])
    df.to_csv(path, index=False, encoding="utf-8", compression="gzip")

    out = csv_io.read_csv_gz(path)

    seen = dict(zip(out["job_posting_id"].astype(str), out["is_seen"]))
    assert seen["1"] == "no"   # NaN normalized -- must not stay NaN/"nan"
    assert seen["2"] == "yes"  # a real value is left untouched


def test_read_csv_gz_nan_is_seen_row_visible_to_filter_high_unseen(tmp_path):
    sys.path.insert(0, str(REPO / "local"))
    import jobsdata  # noqa: E402  (local import -- keeps this module's sys.path edit scoped to this test)

    path = tmp_path / "master.csv.gz"
    df = pd.DataFrame([{"job_posting_id": "1", "score": 5, "is_seen": pd.NA}])
    df.to_csv(path, index=False, encoding="utf-8", compression="gzip")

    out = csv_io.read_csv_gz(path)
    visible = jobsdata.filter_high_unseen(out, min_score=4)

    assert list(visible["job_posting_id"]) == ["1"]


def test_read_csv_gz_nan_is_seen_row_visible_to_watcher_popup(tmp_path):
    sys.path.insert(0, str(REPO / "local"))
    import watcher  # noqa: E402  (local import -- keeps this module's sys.path edit scoped to this test)

    path = tmp_path / "master.csv.gz"
    df = pd.DataFrame([{"job_posting_id": "1", "score": 5, "is_seen": pd.NA}])
    df.to_csv(path, index=False, encoding="utf-8", compression="gzip")

    assert watcher.has_unseen_high_score(path, min_score=4) is True
