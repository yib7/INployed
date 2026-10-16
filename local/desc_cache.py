"""On-disk cache of job description text, so the dashboard frame need not hold it.

The loaded dashboard frame was ~500 MB, ~440 MB of it the four description
columns, and only two readers ever look at them: the job detail card and the
tailor/cover-letter payload, one job at a time. `jobsdata.load_files` (in its
`descriptions_cached` mode) stores each source file's descriptions here, keyed
by (source, job_posting_id) and stamped with the file's (size, mtime_ns), and
loads the frame without them; a source whose stamp still matches is read
without the text columns at all.

This is a pure cache of what the source files already hold. Any database that
cannot be read is deleted and rebuilt; nothing here raises. Every operation
opens its own short-lived connection, because the load runs on a worker thread
while the UI thread and tailor workers look descriptions up. The journal is the
default rollback journal, not WAL: seen.db once lost weeks of writes that lived
only in a WAL file that went missing.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import zlib
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

DESC_COLS = ("job_summary", "job_description_md", "job_description_formatted",
             "job_description")


def _default_db_path() -> Path:
    """`descriptions.db` in the app-data folder (jobsdata.APPDATA)."""
    base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(base) / "linkedin_watcher" / "descriptions.db"


DB_PATH = _default_db_path()

_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_S = 10.0
# SQLite's primary result codes for a file that is damaged or not a database.
_BROKEN_CODES = {getattr(sqlite3, "SQLITE_CORRUPT", 11), getattr(sqlite3, "SQLITE_NOTADB", 26)}


def _is_broken(exc: sqlite3.Error) -> bool:
    """Corruption, not contention: a "database is locked" must never delete the file."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in _BROKEN_CODES
    msg = str(exc).lower()
    return "malformed" in msg or "not a database" in msg


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=_BUSY_TIMEOUT_S)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version != _SCHEMA_VERSION:
            con.execute("DROP TABLE IF EXISTS desc")
            con.execute("DROP TABLE IF EXISTS files")
            con.execute("CREATE TABLE files (source TEXT PRIMARY KEY, size INTEGER NOT NULL,"
                        " mtime_ns INTEGER NOT NULL, stored_ns INTEGER NOT NULL)")
            con.execute("CREATE TABLE desc (source TEXT NOT NULL,"
                        " job_posting_id TEXT NOT NULL, blob BLOB NOT NULL,"
                        " PRIMARY KEY (source, job_posting_id))")
            con.execute("CREATE INDEX desc_by_id ON desc (job_posting_id)")
            con.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            con.commit()
    except BaseException:
        con.close()
        raise
    return con


def _reset() -> None:
    """Delete a broken cache so the next connection starts empty."""
    log.warning("description cache %s is unreadable; rebuilding it", DB_PATH)
    for suffix in ("", "-journal"):
        try:
            os.unlink(str(DB_PATH) + suffix)
        except FileNotFoundError:
            pass


def _run(op, default):
    """`op(connection)`, healing a broken file once; `default` on any failure."""
    for attempt in (1, 2):
        con = None
        try:
            con = _connect()
            return op(con)
        except sqlite3.Error as exc:
            if attempt == 1 and _is_broken(exc):
                if con is not None:
                    con.close()
                    con = None
                try:
                    _reset()
                except OSError:
                    return default
                continue
            log.warning("description cache: %s", exc)
            return default
        except OSError as exc:
            log.warning("description cache: %s", exc)
            return default
        finally:
            if con is not None:
                con.close()
    return default


def is_current(source: str, read_path: Path) -> bool:
    """True when `source`'s descriptions are stored at `read_path`'s current stamp."""
    try:
        st = os.stat(read_path)
    except OSError:
        return False

    def op(con):
        row = con.execute("SELECT size, mtime_ns FROM files WHERE source = ?",
                          (source,)).fetchone()
        return row is not None and tuple(row) == (st.st_size, st.st_mtime_ns)
    return _run(op, False)


def _blobs(df: pd.DataFrame) -> list[tuple[str, bytes]]:
    """(job_posting_id, compressed JSON of its non-empty description fields)."""
    cols = [c for c in DESC_COLS if c in df.columns]
    if not cols or "job_posting_id" not in df.columns:
        return []
    ids = df["job_posting_id"].astype(str).tolist()
    values = {c: df[c].tolist() for c in cols}
    out = []
    for i, jid in enumerate(ids):
        fields = {}
        for c in cols:
            v = values[c][i]
            if v is None or (not isinstance(v, str) and pd.isna(v)):
                continue
            v = str(v)
            if v:
                fields[c] = v
        if fields:
            out.append((jid, zlib.compress(json.dumps(fields, ensure_ascii=False)
                                           .encode("utf-8"), 1)))
    return out


def store(source: str, stat: os.stat_result, df: pd.DataFrame) -> bool:
    """Replace `source`'s stored descriptions with `df`'s, stamped with `stat`
    (taken BEFORE the read, so a file rewritten mid-read is re-read next time).
    One transaction. False when the cache could not be written."""
    rows = _blobs(df)

    def op(con):
        with con:
            con.execute("DELETE FROM desc WHERE source = ?", (source,))
            con.executemany("INSERT OR REPLACE INTO desc (source, job_posting_id, blob)"
                            " VALUES (?, ?, ?)", ((source, jid, b) for jid, b in rows))
            con.execute("INSERT OR REPLACE INTO files (source, size, mtime_ns, stored_ns)"
                        " VALUES (?, ?, ?, ?)",
                        (source, stat.st_size, stat.st_mtime_ns, time.time_ns()))
        return True
    return _run(op, False)


def lookup(job_id: str, source: str) -> dict:
    """The stored description fields for `job_id` from `source`; else from the
    most recently stored other source holding it; else {}."""
    def op(con):
        row = con.execute("SELECT blob FROM desc WHERE source = ? AND job_posting_id = ?",
                          (source, job_id)).fetchone()
        if row is None:
            row = con.execute(
                "SELECT d.blob FROM desc d JOIN files f ON f.source = d.source"
                " WHERE d.job_posting_id = ? AND d.source != ?"
                " ORDER BY f.stored_ns DESC LIMIT 1", (job_id, source)).fetchone()
        return row[0] if row else None
    blob = _run(op, None)
    if blob is None:
        return {}
    try:
        fields = json.loads(zlib.decompress(blob).decode("utf-8"))
    except (zlib.error, ValueError, UnicodeDecodeError):
        return {}
    return fields if isinstance(fields, dict) else {}


def prune(keep: Iterable[str]) -> None:
    """Forget every source not in `keep` (files that are no longer loaded)."""
    keep = set(keep)

    def op(con):
        gone = [s for (s,) in con.execute("SELECT source FROM files") if s not in keep]
        if gone:
            with con:
                for s in gone:
                    con.execute("DELETE FROM desc WHERE source = ?", (s,))
                    con.execute("DELETE FROM files WHERE source = ?", (s,))
        return None
    _run(op, None)
