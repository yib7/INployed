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
