#!/usr/bin/env python3
"""Retention prune: blank job_description_formatted for jobs older than
RETENTION_DAYS. The full HTML description is the master's largest column
(~55% of bytes) and is re-fetchable from each job's LinkedIn url; after a few
days a job is applied-to or abandoned. job_summary is kept (dashboard +
resume-tailor fallback). Chunked so peak memory stays low on a large master.

Standalone (stdlib + pandas only) — copied to the VM next to scraper.py.
"""
from __future__ import annotations
import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd

DESC_COL, SUMMARY_COL = "job_description_formatted", "job_summary"
DATE_COL, FALLBACK_DATE_COL = "extracted_date", "job_posted_date"
CHUNK, RETENTION_DAYS = 2000, 3

def _cutoff_date(days: int, now: datetime | None):
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).date()

def _aged_mask(chunk: pd.DataFrame, cutoff) -> pd.Series:
    dt = pd.to_datetime(chunk.get(DATE_COL), errors="coerce", utc=True)
    if FALLBACK_DATE_COL in chunk.columns:
        dt = dt.fillna(pd.to_datetime(chunk[FALLBACK_DATE_COL], errors="coerce", utc=True))
    # Undatable -> NOT aged (never strip what we can't date).
    cutoff_ts = pd.Timestamp(cutoff, tz="UTC")
    return dt.notna() & (dt < cutoff_ts)

def _needs_rescore(chunk: pd.DataFrame) -> pd.Series:
    score = pd.to_numeric(chunk.get("score"), errors="coerce")
    # Accept the historical float-upcast ("1.0") and trailing-space ("True ")
    # spellings too (a .strip()-normalised, false-family-safe set), else those
    # rows read as NOT filtered and get re-parked/retried forever. Keep IDENTICAL
    # to score_jobs.rows_needing_rescore.
    filtered = (chunk.get("filtered_out", pd.Series(False, index=chunk.index))
                .fillna(False).astype(str).str.strip().str.lower()
                .isin(("true", "1", "1.0", "yes")))
    return score.isna() & ~filtered

def prune(master_csv: Path, *, retention_days=RETENTION_DAYS, now=None,
          strip_summary=False, dry_run=False) -> dict:
    master_csv = Path(master_csv)
    if not master_csv.exists():
        return {"stripped": 0, "parked": 0, "rows": 0}
    cutoff = _cutoff_date(retention_days, now)
    fd, tmp = tempfile.mkstemp(prefix=master_csv.stem + ".", suffix=".tmp",
                               dir=str(master_csv.parent))
    os.close(fd)
    stripped = parked = rows = 0
    wrote_header = False
    try:
        for chunk in pd.read_csv(master_csv, dtype=str, keep_default_na=False,
                                 chunksize=CHUNK):
            rows += len(chunk)
            if DESC_COL in chunk.columns:
                aged = _aged_mask(chunk, cutoff) & (chunk[DESC_COL].fillna("") != "")
                stripped += int(aged.sum())
                chunk.loc[aged, DESC_COL] = ""
                if strip_summary and SUMMARY_COL in chunk.columns:
                    chunk.loc[aged, SUMMARY_COL] = ""
                park = aged & _needs_rescore(chunk)  # can't score an empty desc — park it
                if "filtered_out" in chunk.columns:
                    # Lowercase "true" to match the "true"/"1"/"yes" vocabulary the
                    # _needs_rescore reader (and any future case-sensitive reader)
                    # expects; the .str.lower() at read time already tolerates it,
                    # but keep the written value consistent.
                    chunk.loc[park, "filtered_out"] = "true"
                if "reason" in chunk.columns:
                    chunk.loc[park, "reason"] = "pruned_no_desc"
                parked += int(park.sum())
            if not dry_run:
                chunk.to_csv(tmp, mode="a", header=not wrote_header, index=False,
                             encoding="utf-8")
            wrote_header = True
        if not dry_run:
            os.replace(tmp, master_csv)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return {"stripped": stripped, "parked": parked, "rows": rows}

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", default=str(Path.home() / "linkedin_jobs_master.csv"))
    ap.add_argument("--days", type=int, default=RETENTION_DAYS)
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        r = prune(Path(a.master), retention_days=a.days, strip_summary=a.summary,
                  dry_run=a.dry_run)
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"prune_master: cannot process {a.master}: {e}", file=sys.stderr)
        return 1
    print(f"prune_master: rows={r['rows']} stripped_desc={r['stripped']} "
          f"parked_unscored={r['parked']} days={a.days} dry_run={a.dry_run}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
