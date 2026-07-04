"""Durable outbox: full master rows from LOCAL scrapes / manual adds, queued for the VM.

A local run's data used to live only on this PC. Now every successful dashboard scrape
and manual add writes its new rows (full local-master rows, JD included) plus the whole
run_stats.csv into <repo>/outbox/, and the dashboard best-effort-pushes every pending
outbox file to the VM's ~/incoming/ (vm_sync transport). merge_incoming.py on the VM
folds them into the real master before each scheduled scrape. Files are deleted locally
only after a confirmed push (scp exit 0), so a failed push simply retries on the next
scrape/add; re-pushing an already-merged file is a no-op (the VM merge dedups).

Pure pandas + pathlib: no Qt, no gcloud here (push argv/runner live in vm_sync).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
OUTBOX_DIR = REPO_ROOT / "outbox"
MASTER_CSV = REPO_ROOT / "linkedin_jobs_master.csv"
RUN_STATS_CSV = REPO_ROOT / "run_stats.csv"


def _stamp() -> str:
    # Microseconds keep two same-second writes (e.g. rapid manual adds) distinct.
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def snapshot_run_files(base: Path | None = None) -> dict[Path, float]:
    """{scored-run-file: mtime} for every local run file, taken BEFORE a scrape so
    new_run_ids can tell which files the scrape produced or rewrote (a same-day
    re-run overwrites the same filename, so presence alone is not enough)."""
    import jobsdata

    out: dict[Path, float] = {}
    for p in jobsdata.local_run_files(base):
        try:
            out[p] = p.stat().st_mtime
        except OSError:
            pass
    return out


def new_run_ids(before: dict[Path, float], base: Path | None = None) -> list[str]:
    """job_posting_ids from run files that appeared or changed since `before`."""
    import jobsdata

    ids: list[str] = []
    seen: set[str] = set()
    for p in jobsdata.local_run_files(base):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if p in before and before[p] == mtime:
            continue
        try:
            df = pd.read_csv(p, usecols=lambda c: c == "job_posting_id", dtype=str,
                             compression="gzip" if p.suffix == ".gz" else "infer")
        except (OSError, ValueError, pd.errors.ParserError):
            continue
        if "job_posting_id" not in df.columns:
            continue
        for jid in df["job_posting_id"].dropna().astype(str):
            if jid not in seen:
                seen.add(jid)
                ids.append(jid)
    return ids


def write_rows_outbox(ids, master_csv: Path | None = None,
                      outbox_dir: Path | None = None) -> Path | None:
    """Write the full local-master rows for `ids` (JD + score columns included) to
    outbox/local_rows_<stamp>.csv.gz. None when there is nothing to write — no ids,
    master missing/unreadable, or no id matched (best-effort by design: the outbox
    must never fail a scrape)."""
    ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
    if not ids:
        return None
    master = Path(master_csv) if master_csv is not None else MASTER_CSV
    if not master.exists():
        return None
    try:
        df = pd.read_csv(master, dtype={"job_posting_id": str})
    except (OSError, ValueError, pd.errors.ParserError):
        return None
    if "job_posting_id" not in df.columns:
        return None
    rows = df[df["job_posting_id"].astype(str).isin(set(ids))]
    if rows.empty:
        return None
    ob = Path(outbox_dir) if outbox_dir is not None else OUTBOX_DIR
    ob.mkdir(parents=True, exist_ok=True)
    path = ob / f"local_rows_{_stamp()}.csv.gz"
    rows.to_csv(path, index=False, compression="gzip")
    return path


def write_stats_outbox(stats_csv: Path | None = None,
                       outbox_dir: Path | None = None) -> Path | None:
    """Queue the WHOLE local run_stats.csv (idempotent + monotonic, like
    write_external_exclude_ids — the VM-side merge dedups, so no cursor state)."""
    stats = Path(stats_csv) if stats_csv is not None else RUN_STATS_CSV
    if not stats.exists():
        return None
    try:
        data = stats.read_bytes()
    except OSError:
        return None
    if not data.strip():
        return None
    ob = Path(outbox_dir) if outbox_dir is not None else OUTBOX_DIR
    ob.mkdir(parents=True, exist_ok=True)
    path = ob / f"local_stats_{_stamp()}.csv"
    path.write_bytes(data)
    return path


def pending_files(outbox_dir: Path | None = None) -> list[Path]:
    """Every queued outbox file, name-sorted (stamps make that chronological)."""
    ob = Path(outbox_dir) if outbox_dir is not None else OUTBOX_DIR
    if not ob.is_dir():
        return []
    return sorted([*ob.glob("local_rows_*.csv.gz"), *ob.glob("local_stats_*.csv")])
