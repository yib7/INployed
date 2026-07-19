"""Cycle-5 audit sweep (P2-2, P2-6, P2-12, P2-13, P2-30): the behavior-changing
small fixes, each pinned by a regression test.
"""
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402
import outbox  # noqa: E402
from resume_tailor import compose, output  # noqa: E402
from seen_db import SeenRegistry  # noqa: E402


# ── P2-2: a list-shaped skill line keeps the model's ranking ─────────────────

def test_finalize_skill_lines_joins_list_shape():
    out = {"Languages": ["Python", "SQL"], "Frameworks": "",
           "Developer Tools": "", "Libraries": ""}
    lines = compose._finalize_skill_lines(out)
    langs = next(ln["items"] for ln in lines if ln["label"] == "Languages")
    # The model's ranking survives (previously the list coerced to "" and the
    # pool order silently filled instead).
    assert langs.index("Python") < langs.index("SQL")


# ── P2-6: same-day cross-machine status change can win the merge ─────────────

def test_import_same_day_status_change_wins_with_timestamp(tmp_path):
    a = SeenRegistry(tmp_path / "a.db")
    b = SeenRegistry(tmp_path / "b.db")
    try:
        a.set_status("j1", "applied", company="Acme")
        time.sleep(1.1)      # status_ts has second resolution
        b.set_status("j1", "interviewing", company="Acme")
        bak = tmp_path / "b_export.db"
        b.export_to(bak)
        a.import_from(bak)   # same status_date on both sides — ts must decide
        row = next(r for r in a.status_rows() if r["job_posting_id"] == "j1")
        assert row["status"] == "interviewing"
    finally:
        a.close()
        b.close()


def test_import_from_old_backup_without_status_ts(tmp_path):
    """A pre-P2-6 backup (no status_ts column) must still merge."""
    import sqlite3
    bak = tmp_path / "old.db"
    conn = sqlite3.connect(bak)
    conn.execute(
        "CREATE TABLE seen (job_posting_id TEXT PRIMARY KEY, marked_at TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE app_status (job_posting_id TEXT PRIMARY KEY, status TEXT,"
        " status_date TEXT, applied_date TEXT, followed_up_at TEXT,"
        " company TEXT, job_title TEXT, url TEXT)")
    conn.execute(
        "CREATE TABLE resume_paths (job_posting_id TEXT PRIMARY KEY,"
        " path TEXT, created_at TEXT)")
    conn.execute(
        "INSERT INTO app_status VALUES ('j9', 'applied', '2099-01-01', NULL,"
        " NULL, 'Old Co', '', '')")
    conn.commit()
    conn.close()
    reg = SeenRegistry(tmp_path / "cur.db")
    try:
        counts = reg.import_from(bak)
        assert counts["status"] == 1
        row = next(r for r in reg.status_rows() if r["job_posting_id"] == "j9")
        assert row["status"] == "applied"
    finally:
        reg.close()


# ── P2-12: a same-mtime-tick rewrite is still detected (size differs) ────────

def test_new_run_ids_detects_same_mtime_rewrite(tmp_path, monkeypatch):
    d = tmp_path / "evening"
    d.mkdir()
    f = d / "linkedin_jobs_2026-07-18_evening_scored.csv.gz"
    pd.DataFrame({"job_posting_id": ["1"]}).to_csv(f, index=False,
                                                   compression="gzip")
    before = outbox.snapshot_run_files(base=tmp_path)
    st = f.stat()
    pd.DataFrame({"job_posting_id": ["1", "2"]}).to_csv(f, index=False,
                                                        compression="gzip")
    import os
    os.utime(f, (st.st_atime, st.st_mtime))   # force the SAME mtime
    assert outbox.new_run_ids(before, base=tmp_path) == ["1", "2"]


# ── P2-13: >80-char names get a disambiguating hash suffix ───────────────────

def test_sanitize_long_names_do_not_collide():
    a = output.sanitize("A" * 79 + " Consolidated Holdings International Group")
    b = output.sanitize("A" * 79 + " Consolidated Holdings International Trust")
    assert a != b
    assert len(a) <= 80 and len(b) <= 80
    # short names are untouched
    assert output.sanitize("Acme") == "Acme"


# ── P2-30: removed_jobs marker prunes once no source carries the row ─────────

def test_removed_jobs_pruned_when_absent_everywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)   # isolate config.json
    src = tmp_path / "jobs_scored.csv.gz"
    pd.DataFrame({"job_posting_id": ["1", "2"], "job_title": ["A", "B"],
                  "score": ["5", "4"], "is_seen": ["no", "no"]}).to_csv(
        src, index=False, compression="gzip")
    jobsdata._save_removed_jobs({"2", "gone-id"})
    df, _ = jobsdata.load_files([src])
    assert list(df["job_posting_id"]) == ["1"]        # 2 still hidden
    assert jobsdata.load_removed_jobs() == {"2"}      # gone-id pruned
