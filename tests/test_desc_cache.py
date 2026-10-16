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
import threading
import time
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


def test_the_db_uses_wal_so_lookups_do_not_wait_on_a_store(db, tmp_path):
    src = _src(tmp_path)
    desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1"], job_summary=["s"]))
    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        con.close()


def test_a_lookup_reads_while_a_store_holds_the_write_lock(db, tmp_path):
    # A store of a whole master is one long write transaction. Under a rollback
    # journal the UI thread's lookup waited it out (0.58 s measured, up to the busy
    # timeout); under WAL the reader sees the last committed rows at once.
    src = _src(tmp_path)
    desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1"], job_summary=["s"]))
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("DELETE FROM desc")
    try:
        t0 = time.perf_counter()
        assert desc_cache.lookup("1", str(src)) == {"job_summary": "s"}
        assert time.perf_counter() - t0 < 1.0
    finally:
        holder.rollback()
        holder.close()


def test_lookups_use_a_short_busy_timeout_and_store_the_long_one(db, tmp_path, monkeypatch):
    seen = []
    real = sqlite3.connect

    def spy(*a, **k):
        seen.append(k.get("timeout"))
        return real(*a, **k)
    monkeypatch.setattr(desc_cache.sqlite3, "connect", spy)
    src = _src(tmp_path)
    desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1"], job_summary=["s"]))
    assert seen[-1] == desc_cache._BUSY_TIMEOUT_S
    desc_cache.lookup("1", str(src))
    assert seen[-1] <= 1.0


def test_reset_removes_the_wal_sidecars_too(db):
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(db) + suffix).write_bytes(b"junk")
    desc_cache._reset()
    for suffix in ("", "-journal", "-wal", "-shm"):
        assert not Path(str(db) + suffix).exists(), suffix


def test_a_missing_table_counts_as_broken_and_heals(db, tmp_path):
    # user_version says "current schema" but a table is gone: every query fails
    # with "no such table", which is a broken cache, not contention.
    src = _src(tmp_path)
    desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1"], job_summary=["s"]))
    con = sqlite3.connect(db)
    con.execute("DROP TABLE desc")
    con.commit()
    con.close()
    assert desc_cache.store(str(src), src.stat(),
                            _frame(job_posting_id=["1"], job_summary=["healed"])) is True
    assert desc_cache.lookup("1", str(src)) == {"job_summary": "healed"}


@pytest.mark.parametrize("message, broken", [
    ("no such table: desc", True),
    ("no such column: blob", True),
    ("database disk image is malformed", True),
    ("file is not a database", True),
    ("database is locked", False),
    ("database table is locked", False),
])
def test_is_broken_tells_a_damaged_db_from_contention(message, broken):
    assert desc_cache._is_broken(sqlite3.OperationalError(message)) is broken


def test_a_schema_created_by_another_connection_meanwhile_is_kept(db, tmp_path):
    # Two connections open a fresh file at once. Both read user_version 0; the
    # second must re-check inside its write transaction, not drop the tables
    # (and rows) the first just created.
    a = _src(tmp_path, "a.csv.gz")
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("CREATE TABLE files (source TEXT PRIMARY KEY, size INTEGER NOT NULL,"
                   " mtime_ns INTEGER NOT NULL, stored_ns INTEGER NOT NULL)")
    holder.execute("CREATE TABLE desc (source TEXT NOT NULL, job_posting_id TEXT NOT NULL,"
                   " blob BLOB NOT NULL, PRIMARY KEY (source, job_posting_id))")
    holder.execute("CREATE INDEX desc_by_id ON desc (job_posting_id)")
    holder.execute(f"PRAGMA user_version={desc_cache._SCHEMA_VERSION}")
    result = {}
    b = _src(tmp_path, "b.csv.gz")
    t = threading.Thread(target=lambda: result.setdefault(
        "ok", desc_cache.store(str(b), b.stat(), _frame(job_posting_id=["2"],
                                                        job_summary=["B"]))))
    t.start()
    time.sleep(0.3)               # the store has read user_version 0 by now
    holder.execute("INSERT INTO files VALUES (?, ?, ?, ?)",
                   (str(a), a.stat().st_size, a.stat().st_mtime_ns, 1))
    holder.execute("COMMIT")
    holder.close()
    t.join(15)
    assert result.get("ok") is True
    assert desc_cache.is_current(str(a), a) is True       # the first one's rows survived
    assert desc_cache.lookup("2", str(b)) == {"job_summary": "B"}


def test_restamp_moves_a_current_stamp_to_the_rewritten_file(db, tmp_path):
    src = _src(tmp_path, text="before")
    pre = src.stat()
    desc_cache.store(str(src), pre, _frame(job_posting_id=["1"], job_summary=["s"]))
    src.write_text("after, a different size", encoding="utf-8")
    post = src.stat()
    assert desc_cache.is_current(str(src), src) is False
    assert desc_cache.restamp(str(src), pre, post) is True
    assert desc_cache.is_current(str(src), src) is True
    assert desc_cache.lookup("1", str(src)) == {"job_summary": "s"}


def test_restamp_leaves_a_stamp_it_did_not_start_from(db, tmp_path):
    # compare-and-set: the stored stamp is not `pre` (the cache was stale, or a
    # load stored something newer), so the rewrite's stamp must not be claimed
    src = _src(tmp_path, text="one")
    desc_cache.store(str(src), src.stat(), _frame(job_posting_id=["1"], job_summary=["s"]))
    src.write_text("two two", encoding="utf-8")
    pre = src.stat()
    src.write_text("three three three", encoding="utf-8")
    post = src.stat()
    assert desc_cache.restamp(str(src), pre, post) is False
    assert desc_cache.is_current(str(src), src) is False


def test_restamp_of_an_unknown_source_is_a_quiet_no(db, tmp_path):
    src = _src(tmp_path)
    assert desc_cache.restamp(str(src), src.stat(), src.stat()) is False


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
