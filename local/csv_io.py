"""Shared CSV.gz read/reconcile/write helpers used by watcher.py and the dashboard (local/qt).

Reconcile = re-apply the local seen registry onto a freshly-synced CSV
so the is_seen column always reflects locally-tracked state.
"""
from __future__ import annotations

import gzip
import logging
import os
import tempfile
from pathlib import Path

import pandas as pd

from jsonutil import replace_with_retry
from seen_db import SeenRegistry

log = logging.getLogger(__name__)


def read_csv_gz(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, compression="gzip", dtype={"job_posting_id": str})
    if "is_seen" not in df.columns:
        df["is_seen"] = "no"
    else:
        # A fresh row from scraper.py's pd.concat([existing, df]) column-union
        # (append_to_master) lands with is_seen=NaN when the master already has
        # the column -- nothing downstream ever stamps it (update_master_scores
        # deliberately excludes is_seen; see its docstring). Every consumer here
        # tests astype(str) == "no", and NaN stringifies to "nan", not "no", so an
        # un-normalized blank silently hides the row from every high-score view.
        n_nan = int(df["is_seen"].isna().sum())
        if n_nan:
            # Expected for fresh append-to-master rows, but a warn makes an
            # unexpected upstream schema/merge blank visible instead of masked.
            log.warning("read_csv_gz: is_seen had %d NaN value(s) in %s; "
                        "defaulting them to 'no'", n_nan, path)
        df["is_seen"] = df["is_seen"].fillna("no")
    return df


def write_csv_gz_atomic(df: pd.DataFrame, path: Path, *, compression: str | None = "gzip") -> None:
    """Atomic in-place rewrite of a CSV -- same-volume tempfile + os.replace.

    `compression` defaults to "gzip" (the original behavior, for the .csv.gz
    stores this was written for); pass compression=None for a plain CSV so
    local/jobsdata.py's master-CSV writers (_drop_ids_from_csv, _append_dedup_csv)
    can reuse the same atomic tmp+replace helper instead of a naked to_csv.

    The replace goes through jsonutil.replace_with_retry so a lock-free concurrent
    reader on Windows can't fail a master rewrite with a transient PermissionError
    (the master CSV is read lock-free by load_files/master_row/reconcile_file).
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.stem + ".",
        suffix=".tmp.gz" if compression == "gzip" else ".tmp",
        dir=str(path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        df.to_csv(tmp_path, index=False, encoding="utf-8", compression=compression)
        replace_with_retry(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def reconcile_is_seen(df: pd.DataFrame, registry: SeenRegistry) -> tuple[pd.DataFrame, int]:
    """Apply the registry to the dataframe. Returns (df, n_changed)."""
    if "job_posting_id" not in df.columns:
        return df, 0
    seen_ids = registry.all_ids()
    if not seen_ids:
        return df, 0
    df["job_posting_id"] = df["job_posting_id"].astype(str)
    mask = df["job_posting_id"].isin(seen_ids) & (df["is_seen"] != "yes")
    n = int(mask.sum())
    if n:
        df.loc[mask, "is_seen"] = "yes"
    return df, n


# Rows per chunk for reconcile_file's streaming rewrite (test-patched).
_RECONCILE_CHUNK = 5000


def reconcile_file(path: Path, registry: SeenRegistry) -> int:
    """Read + reconcile + write back. Returns rows changed (0 if no rewrite needed).

    Streams the file in bounded chunks (audit P2-21): the watcher runs this
    against the ~90 MB decompressed master on every fire, and the old full
    read + full rewrite held the whole frame in memory. The rewrite lands via
    tmp + retrying replace, and is skipped entirely when nothing changed."""
    seen_ids = registry.all_ids()
    compression = "gzip" if str(path).endswith(".gz") else None
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".reconcile.tmp",
                                    dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    total = 0
    try:
        opener = (lambda: gzip.open(tmp_path, "wt", encoding="utf-8", newline="")
                  ) if compression else (
                  lambda: open(tmp_path, "w", encoding="utf-8", newline=""))
        with opener() as out:
            wrote_header = False
            for chunk in pd.read_csv(path, dtype={"job_posting_id": str},
                                     chunksize=_RECONCILE_CHUNK):
                if "is_seen" in chunk.columns:
                    chunk["is_seen"] = chunk["is_seen"].fillna("no")
                if seen_ids and "job_posting_id" in chunk.columns:
                    chunk, n = reconcile_is_seen(chunk, registry)
                    total += n
                chunk.to_csv(out, index=False, header=not wrote_header)
                wrote_header = True
        if total:
            replace_with_retry(tmp_path, path)
        return total
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
