"""The on-disk job-description cache (local/desc_cache.py).

Why: the dashboard's loaded frame was ~500 MB, ~440 MB of it description text
that only the detail card and the tailor payload ever read, one job at a time.
The text now lives in a SQLite file under the app-data folder, keyed by
(source file, job id) and stamped with each source's (size, mtime_ns), and the
frame is loaded without it. The cache is pure derived data: a broken file is
deleted and rebuilt, never an error.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import desc_cache  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "descriptions.db"
    monkeypatch.setattr(desc_cache, "DB_PATH", path)
    return path


def _src(tmp_path, name="run.csv.gz", text="x") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _frame(**cols):
    return pd.DataFrame(cols)


def test_db_path_follows_the_redirected_test_appdata():
    assert str(desc_cache._default_db_path()).startswith(os.environ["LOCALAPPDATA"])


def test_store_then_lookup_returns_the_non_empty_description_fields(db, tmp_path):
    src = _src(tmp_path)
    df = _frame(job_posting_id=["1", "2"], job_title=["A", "B"],
                job_summary=["sum one", None], job_description_md=["## md one", ""])
    assert desc_cache.store(str(src), src.stat(), df) is True
    assert desc_cache.lookup("1", str(src)) == {"job_summary": "sum one",
                                                "job_description_md": "## md one"}
    # a row with nothing to keep is not stored
    assert desc_cache.lookup("2", str(src)) == {}
    assert desc_cache.lookup("3", str(src)) == {}


def test_store_keeps_text_exactly(db, tmp_path):
    src = _src(tmp_path)
    text = "Line one\n\n- bullet with \"quotes\" and unicode: café, 東京\r\n  trailing  "
    df = _frame(job_posting_id=["9"], job_description_formatted=[text])
    desc_cache.store(str(src), src.stat(), df)
    assert desc_cache.lookup("9", str(src)) == {"job_description_formatted": text}


def test_is_current_tracks_size_and_mtime(db, tmp_path):
    src = _src(tmp_path, text="abc")
    assert desc_cache.is_current(str(src), src) is False
    desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1"], job_summary=["s"]))
    assert desc_cache.is_current(str(src), src) is True
    st = src.stat()
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert desc_cache.is_current(str(src), src) is False


def test_is_current_is_false_for_a_missing_file(db, tmp_path):
    assert desc_cache.is_current(str(tmp_path / "gone"), tmp_path / "gone") is False


def test_a_restore_replaces_the_sources_old_rows(db, tmp_path):
    src = _src(tmp_path)
    desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1", "2"],
                                                  job_summary=["old one", "old two"]))
    src.write_text("changed contents", encoding="utf-8")
    desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1"],
                                                  job_summary=["new one"]))
    assert desc_cache.is_current(str(src), src) is True
    assert desc_cache.lookup("1", str(src)) == {"job_summary": "new one"}
    # "2" left the file, so the exact source no longer answers for it
    assert desc_cache.lookup("2", str(src)) == {}


def test_lookup_falls_back_to_another_source_holding_the_id(db, tmp_path):
    master = _src(tmp_path, "master.csv.gz")
    run = _src(tmp_path, "run.csv.gz")
    desc_cache.store(str(master), master.stat(),
                     _frame(job_posting_id=["7"], job_summary=["from master"]))
    assert desc_cache.lookup("7", str(run)) == {"job_summary": "from master"}
    assert desc_cache.lookup("7", "") == {"job_summary": "from master"}


def test_lookup_prefers_the_exact_source_then_the_newest_other(db, tmp_path):
    a = _src(tmp_path, "a.csv.gz")
    b = _src(tmp_path, "b.csv.gz")
    c = _src(tmp_path, "c.csv.gz")
    desc_cache.store(str(a), a.stat(), _frame(job_posting_id=["7"], job_summary=["A"]))
    desc_cache.store(str(b), b.stat(), _frame(job_posting_id=["7"], job_summary=["B"]))
    desc_cache.store(str(c), c.stat(), _frame(job_posting_id=["8"], job_summary=["C"]))
    assert desc_cache.lookup("7", str(a)) == {"job_summary": "A"}
    assert desc_cache.lookup("7", str(c)) == {"job_summary": "B"}   # newest stored
    desc_cache.store(str(a), a.stat(), _frame(job_posting_id=["7"], job_summary=["A2"]))
    assert desc_cache.lookup("7", str(c)) == {"job_summary": "A2"}


def test_a_frame_without_description_columns_stores_nothing_but_stamps(db, tmp_path):
    src = _src(tmp_path)
    assert desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1"])) is True
    assert desc_cache.is_current(str(src), src) is True
    assert desc_cache.lookup("1", str(src)) == {}


def test_prune_forgets_sources_no_longer_loaded(db, tmp_path):
    a = _src(tmp_path, "a.csv.gz")
    b = _src(tmp_path, "b.csv.gz")
    desc_cache.store(str(a), a.stat(), _frame(job_posting_id=["1"], job_summary=["A"]))
    desc_cache.store(str(b), b.stat(), _frame(job_posting_id=["2"], job_summary=["B"]))
    desc_cache.prune([str(a)])
    assert desc_cache.lookup("1", str(a)) == {"job_summary": "A"}
    assert desc_cache.lookup("2", str(b)) == {}
    assert desc_cache.is_current(str(b), b) is False


def test_the_db_uses_a_rollback_journal_not_wal(db, tmp_path):
    src = _src(tmp_path)
    desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1"], job_summary=["s"]))
    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"
    finally:
        con.close()
    assert not Path(str(db) + "-wal").exists()


def test_a_garbage_db_file_heals_into_a_working_cache(db, tmp_path):
    db.write_bytes(b"this is not a sqlite database at all" * 200)
    src = _src(tmp_path)
    assert desc_cache.is_current(str(src), src) is False
    assert desc_cache.lookup("1", str(src)) == {}
    assert desc_cache.store(str(src), src.stat(),
                            _frame(job_posting_id=["1"], job_summary=["healed"])) is True
    assert desc_cache.lookup("1", str(src)) == {"job_summary": "healed"}


def test_a_db_from_an_older_schema_is_rebuilt(db, tmp_path):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE files (source TEXT PRIMARY KEY)")
    con.execute("CREATE TABLE desc (whatever TEXT)")
    con.commit()
    con.close()
    src = _src(tmp_path)
    assert desc_cache.store(str(src), src.stat(),
                            _frame(job_posting_id=["1"], job_summary=["s"])) is True
    assert desc_cache.lookup("1", str(src)) == {"job_summary": "s"}


def test_a_bad_blob_reads_as_a_miss(db, tmp_path):
    src = _src(tmp_path)
    desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1"], job_summary=["s"]))
    con = sqlite3.connect(db)
    con.execute("UPDATE desc SET blob = ?", (b"not zlib",))
    con.commit()
    con.close()
    assert desc_cache.lookup("1", str(src)) == {}


def test_store_reports_failure_when_the_db_cannot_be_opened(tmp_path, monkeypatch):
    # a directory where the file should be: sqlite cannot open it, nor can it be
    # deleted as a file, so the cache is unusable and says so instead of raising
    bad = tmp_path / "descriptions.db"
    bad.mkdir()
    monkeypatch.setattr(desc_cache, "DB_PATH", bad)
    src = _src(tmp_path)
    df = _frame(job_posting_id=["1"], job_summary=["s"])
    assert desc_cache.store(str(src), src.stat(), df) is False
    assert desc_cache.is_current(str(src), src) is False
    assert desc_cache.lookup("1", str(src)) == {}


def test_a_lock_is_not_mistaken_for_corruption(db, tmp_path, monkeypatch):
    src = _src(tmp_path)
    desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1"], job_summary=["s"]))
    monkeypatch.setattr(desc_cache, "_BUSY_TIMEOUT_S", 0.05)
    holder = sqlite3.connect(db)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        other = _src(tmp_path, "other.csv.gz")
        assert desc_cache.store(str(other), other.stat(),
                                _frame(job_posting_id=["2"], job_summary=["t"])) is False
    finally:
        holder.rollback()
        holder.close()
    # the existing cache survived the busy error
    assert desc_cache.lookup("1", str(src)) == {"job_summary": "s"}
