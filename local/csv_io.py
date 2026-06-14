"""Shared CSV.gz read/reconcile/write helpers used by watcher.py and ui.py.

Reconcile = re-apply the local seen registry onto a freshly-synced CSV
so the is_seen column always reflects locally-tracked state.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd

from seen_db import SeenRegistry


def read_csv_gz(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, compression="gzip", dtype={"job_posting_id": str})
    if "is_seen" not in df.columns:
        df["is_seen"] = "no"
    return df


def write_csv_gz_atomic(df: pd.DataFrame, path: Path) -> None:
    """Atomic in-place rewrite of a gzipped CSV — same-volume tempfile + os.replace."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.stem + ".",
        suffix=".tmp.gz",
        dir=str(path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        df.to_csv(tmp_path, index=False, encoding="utf-8", compression="gzip")
        os.replace(tmp_path, path)
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


def reconcile_file(path: Path, registry: SeenRegistry) -> int:
    """Read + reconcile + write back. Returns rows changed (0 if no rewrite needed)."""
    df = read_csv_gz(path)
    df, n = reconcile_is_seen(df, registry)
    if n:
        write_csv_gz_atomic(df, path)
    return n
