"""SeenRegistry self-heals a corrupt seen.db instead of crash-looping.

The 2026-06/07 tracker wipes had two ingredients: (a) a non-hermetic pytest
suite corrupted the live seen.db (fixed in conftest — see test_hermetic_appdata),
and (b) once corrupt, the whole app_status tracker was gone because it had no
second store anywhere. This is the defense-in-depth layer for (b): opening the
registry on a malformed db must NOT raise (the scheduled watcher was crashing on
every run), and app_status must survive via the auto-backup written beside the db.
"""
import sqlite3

from seen_db import SeenRegistry


def _corrupt(path):
    """Overwrite a db file with non-SQLite bytes — quick_check raises
    DatabaseError on it, the same signal a genuinely malformed db gives."""
    path.write_bytes(b"this is not a sqlite database " * 8)


def test_healthy_db_open_does_not_report_recovery(tmp_path):
    db = tmp_path / "seen.db"
    r = SeenRegistry(db)
    try:
        assert r.recovered is False
    finally:
        r.close()
    assert not list(tmp_path.glob("seen.db.corrupt-*"))


def test_corrupt_db_is_quarantined_and_recreated(tmp_path):
    db = tmp_path / "seen.db"
    _corrupt(db)
    r = SeenRegistry(db)   # must not raise
    try:
        assert r.recovered is True
        # the corrupt bytes are kept for forensics, not silently deleted
        assert list(tmp_path.glob("seen.db.corrupt-*"))
        # and the registry is fully usable on the fresh db
        r.set_status("J1", "applied", company="Acme")
        assert [row["job_posting_id"] for row in r.status_rows()] == ["J1"]
    finally:
        r.close()


def test_status_write_creates_a_backup(tmp_path):
    db = tmp_path / "seen.db"
    r = SeenRegistry(db)
    try:
        r.set_status("J1", "applied", company="Acme")
    finally:
        r.close()
    bak = tmp_path / "seen.db.backup"
    assert bak.exists()
    con = sqlite3.connect(bak)
    try:
        got = con.execute("SELECT job_posting_id, status FROM app_status").fetchall()
    finally:
        con.close()
    assert got == [("J1", "applied")]
    # the temp used for the atomic write must not be left behind
    assert not list(tmp_path.glob("seen.db.backup.tmp"))


def test_recovery_restores_app_status_from_backup(tmp_path):
    db = tmp_path / "seen.db"
    r = SeenRegistry(db)
    try:
        r.set_status("J1", "applied", company="Acme", job_title="Eng")
        r.set_status("J2", "interviewing", company="Beta")
    finally:
        r.close()
    # the exact failure we saw: main db malformed, backup intact
    _corrupt(db)
    r2 = SeenRegistry(db)
    try:
        assert r2.recovered is True
        rows = {row["job_posting_id"]: row["status"] for row in r2.status_rows()}
        assert rows == {"J1": "applied", "J2": "interviewing"}
    finally:
        r2.close()


def test_recovery_without_a_backup_is_empty_but_usable(tmp_path):
    db = tmp_path / "seen.db"
    _corrupt(db)
    r = SeenRegistry(db)
    try:
        assert r.recovered is True
        assert r.status_rows() == []
        r.set_status("J1", "offer")
        assert len(r.status_rows()) == 1
    finally:
        r.close()


def _main_file_ids(path):
    """Read the seen ids from the MAIN db file only: the WAL is deliberately
    hidden by copying the file somewhere the -wal sidecar does not follow."""
    import shutil
    alone = path.with_name("alone.db")
    shutil.copyfile(path, alone)
    conn = sqlite3.connect(alone)
    try:
        return {row[0] for row in conn.execute("SELECT job_posting_id FROM seen")}
    finally:
        conn.close()
        alone.unlink()


def test_every_write_lands_in_the_main_file_without_a_clean_close(tmp_path):
    """The dashboard is usually killed, never closed, so SQLite's close-time
    checkpoint never runs and the writes are too small for the auto-checkpoint.
    On 2026-09-18 the main file was a seven-week-old snapshot and every mark,
    tracker row and resume link since lived only in seen.db-wal; losing that one
    file silently reverted the registry. Each writer must checkpoint itself."""
    db = tmp_path / "seen.db"
    r = SeenRegistry(db)          # never closed: the kill case
    r.mark(["J1", "J2"])
    assert _main_file_ids(db) == {"J1", "J2"}
    r.unmark(["J2"])
    assert _main_file_ids(db) == {"J1"}
    r.set_status("J3", "applied", company="Acme")
    r.record_resume("J3", str(tmp_path / "out"))
    alone = tmp_path / "alone2.db"
    import shutil
    shutil.copyfile(db, alone)
    conn = sqlite3.connect(alone)
    try:
        assert conn.execute("SELECT count(*) FROM app_status").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM resume_paths").fetchone()[0] == 1
    finally:
        conn.close()


def test_seen_marks_refresh_the_backup(tmp_path):
    """The auto-backup used to follow tracker writes only, so a registry restored
    from it came back without the seen marks made since the last status change."""
    db = tmp_path / "seen.db"
    r = SeenRegistry(db)
    try:
        r.mark(["J1"])
        bak = tmp_path / "seen.db.backup"
        assert bak.exists()
        conn = sqlite3.connect(bak)
        try:
            assert {row[0] for row in conn.execute("SELECT job_posting_id FROM seen")} == {"J1"}
        finally:
            conn.close()
    finally:
        r.close()
