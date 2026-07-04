"""Regression: a job marked seen must STAY seen across a fresh master.

The local SQLite registry is the source of truth for seen-state; the is_seen
column inside each synced .csv.gz is only a projection. When the VM ships a new
master with every is_seen="no", the dashboard must re-overlay the registry on
load (reload_data -> reconcile_is_seen) so a job the user already triaged does
NOT resurface in the High Score (Unseen) tab. These guard that overlay, and the
invariant that it can only promote no->yes (never un-see a row).

Run:  python -m pytest tests/ -v
"""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from csv_io import reconcile_is_seen  # noqa: E402
from seen_db import SeenRegistry  # noqa: E402


def _fresh_master(seen_default="no"):
    """Mimics a VM-shipped master where the is_seen column was reset."""
    return pd.DataFrame([
        {"job_posting_id": 1, "score": 5, "is_seen": seen_default},
        {"job_posting_id": 2, "score": 4, "is_seen": seen_default},
        {"job_posting_id": 3, "score": 5, "is_seen": seen_default},
    ])


def test_marked_seen_survives_fresh_master(tmp_path):
    reg = SeenRegistry(tmp_path / "seen.db")
    reg.mark(["1", "3"])  # user triaged jobs 1 and 3 in a previous session

    df, n = reconcile_is_seen(_fresh_master(), reg)

    assert n == 2, "both registry-seen rows should have been promoted"
    seen = dict(zip(df["job_posting_id"].astype(str), df["is_seen"]))
    assert seen == {"1": "yes", "2": "no", "3": "yes"}
    reg.close()


def test_empty_registry_never_unsees(tmp_path):
    reg = SeenRegistry(tmp_path / "seen.db")  # nothing marked
    df = _fresh_master()
    df.loc[df["job_posting_id"] == 2, "is_seen"] = "yes"  # already seen on disk

    out, n = reconcile_is_seen(df, reg)

    assert n == 0  # an empty registry must not touch any row
    assert dict(zip(out["job_posting_id"].astype(str), out["is_seen"]))["2"] == "yes"
    reg.close()


def test_reconcile_only_promotes(tmp_path):
    """A row already seen on disk but absent from the registry stays seen."""
    reg = SeenRegistry(tmp_path / "seen.db")
    reg.mark(["1"])
    df = _fresh_master()
    df.loc[df["job_posting_id"] == 2, "is_seen"] = "yes"  # seen on disk only

    out, _ = reconcile_is_seen(df, reg)

    seen = dict(zip(out["job_posting_id"].astype(str), out["is_seen"]))
    assert seen == {"1": "yes", "2": "yes", "3": "no"}
    reg.close()


def test_unmark_reverses_mark(tmp_path):
    """unmark is the inverse of mark: it removes ids so they un-see on reload
    (the 'Undo seen' path), and is a no-op for ids that were never marked."""
    reg = SeenRegistry(tmp_path / "seen.db")
    reg.mark(["1", "2", "3"])
    removed = reg.unmark(["2", "3"])
    assert removed == 2
    assert reg.all_ids() == {"1"}
    assert reg.unmark(["nope"]) == 0  # absent id -> nothing removed
    reg.close()


# ---- export / import (tracker backup & restore) ----------------------------

def _put_status(reg, jid, status, status_date, *, applied_date=None,
                followed_up_at=None, company="", job_title="", url=""):
    """Insert a tracker row with explicit dates (set_status only uses today)."""
    reg._conn.execute(
        "INSERT OR REPLACE INTO app_status (job_posting_id, status, status_date,"
        " applied_date, followed_up_at, company, job_title, url)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(jid), status, status_date, applied_date, followed_up_at,
         company, job_title, url))
    reg._conn.commit()


def test_export_import_roundtrip(tmp_path):
    """export_to makes a self-contained snapshot; importing it into a fresh
    registry reproduces seen + tracker + resume-path rows."""
    src = SeenRegistry(tmp_path / "a.db")
    src.mark(["1", "2"])
    _put_status(src, "1", "applied", "2026-01-10", applied_date="2026-01-10",
                company="Acme", job_title="Eng")
    src.record_resume("1", str(tmp_path / "gen" / "1"))
    dest = tmp_path / "backup.db"
    src.export_to(dest)
    src.close()
    assert dest.exists()

    fresh = SeenRegistry(tmp_path / "b.db")
    counts = fresh.import_from(dest)
    assert counts["seen"] == 2 and counts["status"] == 1 and counts["resume_paths"] == 1
    assert fresh.all_ids() == {"1", "2"}
    rows = {r["job_posting_id"]: r for r in fresh.status_rows()}
    assert rows["1"]["status"] == "applied" and rows["1"]["company"] == "Acme"
    assert fresh.resume_paths() == {"1": str(tmp_path / "gen" / "1")}
    fresh.close()


def test_import_merges_seen_union(tmp_path):
    cur = SeenRegistry(tmp_path / "cur.db")
    cur.mark(["1"])
    bak = SeenRegistry(tmp_path / "bak.db")
    bak.mark(["2", "3"])
    bak.export_to(tmp_path / "bak_snap.db")
    bak.close()
    cur.import_from(tmp_path / "bak_snap.db")
    assert cur.all_ids() == {"1", "2", "3"}
    cur.close()


def test_import_status_newer_date_wins(tmp_path):
    """On an id present in both, the row with the later status_date wins; an
    older incoming row never downgrades a newer local status."""
    cur = SeenRegistry(tmp_path / "cur.db")
    _put_status(cur, "1", "applied", "2026-01-01")      # older local
    _put_status(cur, "9", "offer", "2026-03-01")        # newer local, older incoming
    bak = SeenRegistry(tmp_path / "bak.db")
    _put_status(bak, "1", "interviewing", "2026-02-01")  # newer incoming -> wins
    _put_status(bak, "9", "applied", "2026-01-01")       # older incoming -> ignored
    bak.export_to(tmp_path / "snap.db")
    bak.close()

    cur.import_from(tmp_path / "snap.db")
    rows = {r["job_posting_id"]: r for r in cur.status_rows()}
    assert rows["1"]["status"] == "interviewing"  # newer incoming won
    assert rows["9"]["status"] == "offer"         # newer local kept
    cur.close()


def test_import_applied_date_earliest_kept(tmp_path):
    """applied_date is the earliest non-null across both copies (a re-import can't
    lose the original applied date)."""
    cur = SeenRegistry(tmp_path / "cur.db")
    _put_status(cur, "1", "rejected", "2026-05-01", applied_date=None)
    bak = SeenRegistry(tmp_path / "bak.db")
    _put_status(bak, "1", "applied", "2026-01-15", applied_date="2026-01-15")
    bak.export_to(tmp_path / "snap.db")
    bak.close()

    cur.import_from(tmp_path / "snap.db")
    rows = {r["job_posting_id"]: r for r in cur.status_rows()}
    # newer local status_date (2026-05-01) keeps status=rejected, but the
    # incoming applied_date is preserved since local had none.
    assert rows["1"]["status"] == "rejected"
    assert rows["1"]["applied_date"] == "2026-01-15"
    cur.close()


def test_clear_resume_path_removes_row(tmp_path):
    """Deleting a job must not leak its resume_paths row: set, clear, gone."""
    reg = SeenRegistry(tmp_path / "seen.db")
    reg.record_resume("1", "C:/gen/Acme/Engineer")
    assert reg.resume_path("1") == "C:/gen/Acme/Engineer"
    reg.clear_resume_path("1")
    assert reg.resume_path("1") is None
    assert reg.resume_paths() == {}
    reg.close()


def test_clear_resume_path_missing_id_is_noop(tmp_path):
    reg = SeenRegistry(tmp_path / "seen.db")
    reg.record_resume("1", "C:/gen/1")
    reg.clear_resume_path("never-recorded")   # absent id -> no error, nothing lost
    assert reg.resume_paths() == {"1": "C:/gen/1"}
    reg.close()


def test_import_resume_paths_keep_existing(tmp_path):
    cur = SeenRegistry(tmp_path / "cur.db")
    cur.record_resume("1", "C:/cur/1")
    bak = SeenRegistry(tmp_path / "bak.db")
    bak.record_resume("1", "C:/bak/1")   # conflict -> current kept
    bak.record_resume("2", "C:/bak/2")   # new -> taken
    bak.export_to(tmp_path / "snap.db")
    bak.close()

    cur.import_from(tmp_path / "snap.db")
    assert cur.resume_paths() == {"1": "C:/cur/1", "2": "C:/bak/2"}
    cur.close()


if __name__ == "__main__":
    import tempfile

    for fn in (test_marked_seen_survives_fresh_master,
               test_empty_registry_never_unsees, test_reconcile_only_promotes,
               test_unmark_reverses_mark):
        fn(Path(tempfile.mkdtemp()))
    print("SEEN RECONCILE TESTS OK")
