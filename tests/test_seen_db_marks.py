"""SeenRegistry.mark / marked_at_all: the seen table's write and full-table read.

`marked_at_all` backs the dashboard's repost-suppression window (SP5): the
window needs every marked id's timestamp, not just the id set `all_ids()`
already returns.
"""
from datetime import datetime, timezone

from seen_db import SeenRegistry


def test_mark_records_an_aware_iso_timestamp(tmp_path):
    reg = SeenRegistry(tmp_path / "seen.db")
    try:
        reg.mark(["J1"])
        marks = reg.marked_at_all()
        assert set(marks) == {"J1"}
        ts = datetime.fromisoformat(marks["J1"])
        assert ts.tzinfo is not None
        assert abs((datetime.now(timezone.utc) - ts).total_seconds()) < 30
    finally:
        reg.close()


def test_marked_at_all_returns_every_marked_id(tmp_path):
    reg = SeenRegistry(tmp_path / "seen.db")
    try:
        reg.mark(["J1", "J2"])
        marks = reg.marked_at_all()
        assert set(marks) == {"J1", "J2"}
        assert all(isinstance(v, str) and v for v in marks.values())
    finally:
        reg.close()


def test_marked_at_all_on_an_empty_registry_returns_an_empty_dict(tmp_path):
    reg = SeenRegistry(tmp_path / "seen.db")
    try:
        assert reg.marked_at_all() == {}
    finally:
        reg.close()


def test_marked_at_all_reflects_an_explicit_stored_date(tmp_path):
    reg = SeenRegistry(tmp_path / "seen.db")
    try:
        reg._conn.execute(
            "INSERT INTO seen (job_posting_id, marked_at) VALUES (?, ?)",
            ("OLD", "2026-01-01T00:00:00+00:00"),
        )
        reg._conn.commit()
        assert reg.marked_at_all()["OLD"] == "2026-01-01T00:00:00+00:00"
    finally:
        reg.close()


def test_unmark_removes_the_id_from_marked_at_all(tmp_path):
    reg = SeenRegistry(tmp_path / "seen.db")
    try:
        reg.mark(["J1", "J2"])
        reg.unmark(["J1"])
        assert set(reg.marked_at_all()) == {"J2"}
    finally:
        reg.close()
