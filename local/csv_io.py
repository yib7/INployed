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
    # A frame that predates the is_seen column (a first-day master, or one
    # rebuilt from a source without it) used to raise KeyError here — and
    # _needs_reconcile deliberately routes exactly that shape into this
    # function. Default it to "no" and let the registry flip what it owns.
    if "is_seen" not in df.columns:
        df["is_seen"] = "no"
    mask = df["job_posting_id"].isin(seen_ids) & (df["is_seen"] != "yes")
    n = int(mask.sum())
    if n:
        df.loc[mask, "is_seen"] = "yes"
    return df, n


# Rows per chunk for reconcile_file's streaming rewrite (test-patched).
_RECONCILE_CHUNK = 5000


def _needs_reconcile(path: Path, seen_ids: set) -> bool:
    """True if any row in `path` is in `seen_ids` but not already is_seen=yes.

    Reads only the two columns it needs, in chunks, and short-circuits on the
    first hit — so the common no-op case never touches the wide text columns.
    Any read problem returns True so the real pass runs and reports the error
    the way it always did (fail toward doing the work, not toward skipping it).
    """
    if not seen_ids:
        return False
    try:
        for chunk in pd.read_csv(path, usecols=lambda c: c in ("job_posting_id", "is_seen"),
                                 dtype=str, keep_default_na=False,
                                 chunksize=_RECONCILE_CHUNK):
            if "job_posting_id" not in chunk.columns or "is_seen" not in chunk.columns:
                return True
            if (chunk["job_posting_id"].isin(seen_ids) & (chunk["is_seen"] != "yes")).any():
                return True
    except (OSError, ValueError, pd.errors.ParserError, pd.errors.EmptyDataError):
        return True
    return False


def reconcile_file(path: Path, registry: SeenRegistry) -> int:
    """Read + reconcile + write back. Returns rows changed (0 if no rewrite needed).

    Streams the file in bounded chunks (audit P2-21): the watcher runs this
    against the ~90 MB decompressed master on every fire, and the old full
    read + full rewrite held the whole frame in memory. The rewrite lands via
    tmp + retrying replace, and is skipped entirely when nothing changed."""
    seen_ids = registry.all_ids()

    # Cheap two-column probe FIRST (audit C6-3). The rewrite below was already
    # skipped when nothing changed — but only the os.replace was: the full
    # decompress + re-serialize + gzip of a ~90 MB master still ran on every
    # watcher fire, and "nothing changed" is the overwhelmingly common case.
    # Reading just job_posting_id + is_seen costs a fraction of that, and lets
    # the no-op path return without allocating a temp file at all.
    if not _needs_reconcile(path, seen_ids):
        return 0

    compression = "gzip" if str(path).endswith(".gz") else None
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".reconcile.tmp",
                                    dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    total = 0
    normalized = 0
    try:
        opener = (lambda: gzip.open(tmp_path, "wt", encoding="utf-8", newline="")
                  ) if compression else (
                  lambda: open(tmp_path, "w", encoding="utf-8", newline=""))
        with opener() as out:
            wrote_header = False
            # dtype=str + keep_default_na=False (audit C6-1, extending P2-26):
            # the watcher reconciles on every fire, so inferred per-chunk dtypes
            # would rewrite the whole master's formatting (score 5 -> 5.0) on a
            # pass that is supposed to touch only is_seen. keep_default_na=False
            # means a blank cell arrives as "" rather than NaN, so the is_seen
            # default below tests for the empty string instead of using fillna.
            for chunk in pd.read_csv(path, dtype=str, keep_default_na=False,
                                     chunksize=_RECONCILE_CHUNK):
                if "is_seen" not in chunk.columns:
                    # _needs_reconcile returns True for a master with no is_seen
                    # column at all; adding it is the work it asked for.
                    chunk["is_seen"] = "no"
                    normalized += len(chunk)
                else:
                    blank = chunk["is_seen"].isin(["", "nan", "NaN", "None"])
                    normalized += int(blank.sum())
                    chunk["is_seen"] = chunk["is_seen"].mask(blank, "no")
                if seen_ids and "job_posting_id" in chunk.columns:
                    chunk, n = reconcile_is_seen(chunk, registry)
                    total += n
                chunk.to_csv(out, index=False, header=not wrote_header)
                wrote_header = True
        # `or normalized` (audit P2-3): the pass also rewrites literal
        # ""/"nan"/"None" in is_seen to "no", and _needs_reconcile enters it on
        # a read error or a missing column too, not only on a pending seen flag.
        # Gating the replace on `total` alone built that normalized file and
        # then deleted it in the finally, so the same decompress + re-serialize
        # of a ~90 MB master repeated on every watcher fire. Persisting it also
        # matters beyond this module: read_csv_gz re-fills blanks on every read,
        # but prune_master.py and merge_incoming.py read the master with plain
        # pd.read_csv and would see the literal "nan".
        if total or normalized:
            replace_with_retry(tmp_path, path)
        return total
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
