"""SQLite registry of seen job_posting_ids + the local application tracker.

The registry is the local source of truth for whether a job has been
triaged. The is_seen column inside each synced .csv.gz is a denormalized
projection — when the VM overwrites the master CSV (all is_seen="no"),
the watcher rebuilds the column from this registry.

Two more tables extend it into an application tracker:
  app_status   — one row per job the user acted on (applied / interviewing /
                 rejected / offer), with dates and a company/title/url snapshot
                 so the row survives the job aging out of the master CSV.
  resume_paths — job_posting_id -> the folder the tailored resume landed in,
                 so re-finding a generated PDF is one click from the UI.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

APP_STATUSES = ("applied", "interviewing", "rejected", "offer")


def _default_db_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / "linkedin_watcher"
    d.mkdir(parents=True, exist_ok=True)
    return d / "seen.db"


class SeenRegistry:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.path = Path(db_path) if db_path else _default_db_path()
        self._conn = sqlite3.connect(self.path)
        # WAL: readers (dashboard views) and the writer (watcher reconcile) no
        # longer block each other, and a crash mid-write can't corrupt the DB.
        # Best-effort — an in-memory or exotic-FS path may reject it.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            "  job_posting_id TEXT PRIMARY KEY,"
            "  marked_at      TEXT NOT NULL"
            ")"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS app_status ("
            "  job_posting_id TEXT PRIMARY KEY,"
            "  status         TEXT NOT NULL,"
            "  status_date    TEXT NOT NULL,"   # ISO date of the last status change
            "  applied_date   TEXT,"            # ISO date first marked applied
            "  followed_up_at TEXT,"            # ISO date a follow-up nudge was sent
            "  company        TEXT DEFAULT ''," # snapshot — survives master turnover
            "  job_title      TEXT DEFAULT '',"
            "  url            TEXT DEFAULT ''"
            ")"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS resume_paths ("
            "  job_posting_id TEXT PRIMARY KEY,"
            "  path           TEXT NOT NULL,"
            "  created_at     TEXT NOT NULL"
            ")"
        )
        self._conn.commit()

    # ---- seen ----

    def mark(self, job_posting_ids: list[str]) -> int:
        if not job_posting_ids:
            return 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [(str(i), now) for i in job_posting_ids]
        cur = self._conn.executemany(
            "INSERT OR IGNORE INTO seen (job_posting_id, marked_at) VALUES (?, ?)",
            rows,
        )
        self._conn.commit()
        return cur.rowcount

    def unmark(self, job_posting_ids: list[str]) -> int:
        """Remove ids from the seen set (the inverse of mark). Used to undo a
        mark-seen click: the job returns to the unseen views on the next reload."""
        if not job_posting_ids:
            return 0
        cur = self._conn.executemany(
            "DELETE FROM seen WHERE job_posting_id = ?",
            [(str(i),) for i in job_posting_ids],
        )
        self._conn.commit()
        return cur.rowcount

    def all_ids(self) -> set[str]:
        cur = self._conn.execute("SELECT job_posting_id FROM seen")
        return {row[0] for row in cur.fetchall()}

    # ---- application tracker ----

    def set_status(self, job_posting_id: str, status: str, *,
                   company: str = "", job_title: str = "", url: str = "") -> None:
        """Upsert a tracker row. applied_date is set the first time the status
        becomes 'applied' and never overwritten; snapshot fields only fill in
        when non-empty so a later status change can't blank them."""
        if status not in APP_STATUSES:
            raise ValueError(f"status must be one of {APP_STATUSES}, got {status!r}")
        today = date.today().isoformat()
        applied = today if status == "applied" else None
        self._conn.execute(
            "INSERT INTO app_status (job_posting_id, status, status_date, applied_date,"
            "                        company, job_title, url)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(job_posting_id) DO UPDATE SET"
            "   status      = excluded.status,"
            "   status_date = excluded.status_date,"
            "   applied_date = COALESCE(app_status.applied_date, excluded.applied_date),"
            "   company   = CASE WHEN excluded.company   != '' THEN excluded.company   ELSE app_status.company   END,"
            "   job_title = CASE WHEN excluded.job_title != '' THEN excluded.job_title ELSE app_status.job_title END,"
            "   url       = CASE WHEN excluded.url       != '' THEN excluded.url       ELSE app_status.url       END",
            (str(job_posting_id), status, today, applied, company, job_title, url),
        )
        self._conn.commit()

    def clear_status(self, job_posting_id: str) -> None:
        self._conn.execute(
            "DELETE FROM app_status WHERE job_posting_id = ?", (str(job_posting_id),)
        )
        self._conn.commit()

    def mark_followed_up(self, job_posting_ids: list[str]) -> None:
        today = date.today().isoformat()
        self._conn.executemany(
            "UPDATE app_status SET followed_up_at = ? WHERE job_posting_id = ?",
            [(today, str(i)) for i in job_posting_ids],
        )
        self._conn.commit()

    def status_rows(self) -> list[dict]:
        """All tracker rows as dicts, newest status change first."""
        cur = self._conn.execute(
            "SELECT job_posting_id, status, status_date, applied_date, followed_up_at,"
            "       company, job_title, url"
            " FROM app_status ORDER BY status_date DESC, job_posting_id"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ---- tailored-resume locations ----

    def record_resume(self, job_posting_id: str, path: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT INTO resume_paths (job_posting_id, path, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(job_posting_id) DO UPDATE SET"
            "   path = excluded.path, created_at = excluded.created_at",
            (str(job_posting_id), str(path), now),
        )
        self._conn.commit()

    def resume_path(self, job_posting_id: str) -> str | None:
        cur = self._conn.execute(
            "SELECT path FROM resume_paths WHERE job_posting_id = ?",
            (str(job_posting_id),),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def resume_paths(self) -> dict[str, str]:
        cur = self._conn.execute("SELECT job_posting_id, path FROM resume_paths")
        return {row[0]: row[1] for row in cur.fetchall()}

    def clear_resume_path(self, job_posting_id: str) -> None:
        """Drop the résumé-folder link for a deleted job (no-op if absent) so
        the row doesn't outlive the folder it points at."""
        self._conn.execute(
            "DELETE FROM resume_paths WHERE job_posting_id = ?", (str(job_posting_id),)
        )
        self._conn.commit()

    # ---- backup / restore ----------------------------------------------------

    def export_to(self, dest: Path | str) -> Path:
        """Write a self-contained snapshot of the whole registry to `dest`.

        Uses SQLite `VACUUM INTO`, which makes a transactionally-consistent copy
        straight off the live connection (no need to close it). `VACUUM INTO`
        refuses to overwrite, so an existing destination is removed first."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        self._conn.commit()  # no VACUUM inside an open transaction
        self._conn.execute("VACUUM INTO ?", (str(dest),))
        return dest

    def import_from(self, src: Path | str, *, mode: str = "merge") -> dict[str, int]:
        """Merge a backup created by `export_to` into this registry.

        Never replaces — a restore can't wipe newer local progress:
          seen          : set union (INSERT OR IGNORE).
          app_status    : per id, the row with the later `status_date` wins;
                          `applied_date` is the earliest non-null of the two;
                          `followed_up_at` the latest non-null; snapshot fields
                          keep the existing value when set, else take the backup's.
          resume_paths  : keep the current path if present, else take the backup's.
        Returns {"seen": n, "status": n, "resume_paths": n} rows added/updated."""
        if mode != "merge":
            raise ValueError(f"unsupported import mode {mode!r} (only 'merge')")
        src = Path(src)
        if not src.exists():
            raise FileNotFoundError(src)
        counts = {"seen": 0, "status": 0, "resume_paths": 0}
        conn = self._conn
        conn.execute("ATTACH DATABASE ? AS bak", (str(src),))
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO seen (job_posting_id, marked_at)"
                " SELECT job_posting_id, marked_at FROM bak.seen"
            )
            counts["seen"] = cur.rowcount

            bak_rows = conn.execute(
                "SELECT job_posting_id, status, status_date, applied_date,"
                "       followed_up_at, company, job_title, url FROM bak.app_status"
            ).fetchall()
            for (jid, status, sdate, applied, follow, company, title, url) in bak_rows:
                ex = conn.execute(
                    "SELECT status, status_date, applied_date, followed_up_at,"
                    "       company, job_title, url FROM app_status"
                    " WHERE job_posting_id = ?", (jid,)
                ).fetchone()
                if ex is None:
                    conn.execute(
                        "INSERT INTO app_status (job_posting_id, status, status_date,"
                        " applied_date, followed_up_at, company, job_title, url)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (jid, status, sdate, applied, follow, company, title, url),
                    )
                else:
                    ex_status, ex_sdate, ex_applied, ex_follow, ex_co, ex_title, ex_url = ex
                    if (sdate or "") > (ex_sdate or ""):
                        new_status, new_sdate = status, sdate
                    else:
                        new_status, new_sdate = ex_status, ex_sdate
                    applied_opts = [d for d in (applied, ex_applied) if d]
                    new_applied = min(applied_opts) if applied_opts else None
                    follow_opts = [d for d in (follow, ex_follow) if d]
                    new_follow = max(follow_opts) if follow_opts else None
                    conn.execute(
                        "UPDATE app_status SET status=?, status_date=?, applied_date=?,"
                        " followed_up_at=?, company=?, job_title=?, url=?"
                        " WHERE job_posting_id=?",
                        (new_status, new_sdate, new_applied, new_follow,
                         ex_co or company, ex_title or title, ex_url or url, jid),
                    )
                counts["status"] += 1

            cur = conn.execute(
                "INSERT OR IGNORE INTO resume_paths (job_posting_id, path, created_at)"
                " SELECT job_posting_id, path, created_at FROM bak.resume_paths"
            )
            counts["resume_paths"] = cur.rowcount
            conn.commit()
        except Exception:
            # A mid-merge failure (e.g. a malformed backup row) must not leave a
            # half-applied transaction live: roll it back so the registry is
            # exactly as it was before the import, then re-raise.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.execute("DETACH DATABASE bak")
        return counts

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SeenRegistry":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
