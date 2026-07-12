"""Fold locally-pushed outbox files (~/incoming/*) into this host's master + run stats.

run_scraper.sh runs this BEFORE scraper.py, so rows a local machine scraped join the
master (and therefore load_exclude_ids + score_jobs' rescore pass) ahead of the day's
scrape. Per-file problems quarantine to ~/incoming/bad/ and NEVER fail the cron run
(always exit 0); the one loud exception is an existing-but-unreadable master, which
exits 1 so `set -e` stops the run BEFORE the scrape spends money against an exclude
set rebuilt from nothing (mirror of scraper.append_to_master's policy).

Deployed standalone beside scraper.py on the VM — stdlib + pandas only, no local/ imports.
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

HOME = Path(__file__).resolve().parent
INCOMING_DIR = HOME / "incoming"
MASTER_CSV = HOME / "linkedin_jobs_master.csv"
RUN_STATS_CSV = HOME / "run_stats.csv"
STATS_KEY = ["timestamp", "input_csv"]
CHUNK = 2000  # Chunked streaming row count for the master-wins merge (memory bounded)

# Per-file parse/read failures that must quarantine-and-continue rather than
# raise. NOT included: unreadable *master*, which is the one case that must
# abort the whole run (handled separately, outside this tuple).
_BAD_FILE_ERRORS = (OSError, ValueError, pd.errors.ParserError, EOFError, gzip.BadGzipFile)


def _atomic_to_csv(df: pd.DataFrame, path: Path, **kwargs) -> None:
    """Write `df` to `path` atomically: same-dir tempfile + os.replace.

    A crash/kill/OOM mid-write then leaves either the old file (rename never
    happened) or the new one (rename completed) -- never a truncated partial
    write. `path` is only touched by the final os.replace. scraper.py is
    copied standalone to the VM (no local/ package), hence this private copy
    instead of importing local/csv_io.write_csv_gz_atomic.
    """
    fd, tmp = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        df.to_csv(tmp, index=False, encoding="utf-8", **kwargs)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _say(msg: str) -> None:
    print(f"merge_incoming: {msg}")


def _quarantine(path: Path, bad_dir: Path, reason: str) -> None:
    bad_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(bad_dir / path.name))
    _say(f"{path.name} quarantined ({reason})")


def _is_old_enough(path: Path, min_age_seconds: float) -> bool:
    """True once `path` has sat untouched for `min_age_seconds`.

    Guards against folding in a file mid-scp: a local push that's still
    writing looks momentarily corrupt (truncated gzip / partial CSV), and
    without this check it would be quarantined here while the pusher (having
    seen its own scp exit 0) deletes its local copy -- the rows would be
    gone from both sides. Leaving it queued costs nothing; the next cron
    tick picks it up once it's finished landing.
    """
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age >= min_age_seconds


def merge_rows(master_df: pd.DataFrame | None, incoming_df: pd.DataFrame) -> pd.DataFrame:
    """Column-union concat of master (first) + incoming, master wins ties.

    Mirrors scraper.append_to_master: ids cast to str on both sides before
    dedup (an int64 id from a re-read CSV never string-equals a fresh str
    id, so skipping the cast would silently keep duplicate rows), and
    `keep="first"` with master concatenated first so an already-scored VM
    row is never clobbered by a same-id local row that hasn't been scored.
    """
    if master_df is None:
        combined = incoming_df.copy()
    else:
        combined = pd.concat([master_df, incoming_df], ignore_index=True)
    if "job_posting_id" in combined.columns:
        combined["job_posting_id"] = combined["job_posting_id"].astype(str)
        combined = combined.drop_duplicates(subset=["job_posting_id"], keep="first")
    return combined


def merge_stats(existing_df: pd.DataFrame | None, incoming_df: pd.DataFrame) -> pd.DataFrame:
    """Same shape as merge_rows, keyed on STATS_KEY instead of job_posting_id.

    Incoming rows missing either key column can't be deduped against
    history, so they're dropped rather than let through un-keyed (they'd
    never collide with anything, present or future, and would just
    accumulate as junk).
    """
    incoming_df = incoming_df.dropna(subset=STATS_KEY)
    if existing_df is None:
        combined = incoming_df.copy()
    else:
        combined = pd.concat([existing_df, incoming_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=STATS_KEY, keep="first")
    return combined


def _process_row_files(paths: list[Path], bad_dir: Path) -> tuple[list[pd.DataFrame], list[Path]]:
    """Parse+validate each row file; quarantine the bad ones in place.

    Returns the good DataFrames and the paths they came from (so the
    caller can delete only those, only after the master write succeeds).
    """
    good_dfs: list[pd.DataFrame] = []
    good_paths: list[Path] = []
    for path in paths:
        try:
            df = pd.read_csv(path, dtype={"job_posting_id": str})
        except _BAD_FILE_ERRORS as e:
            _quarantine(path, bad_dir, f"unreadable: {e}")
            continue
        if "job_posting_id" not in df.columns:
            _quarantine(path, bad_dir, "missing job_posting_id column")
            continue
        good_dfs.append(df)
        good_paths.append(path)
    return good_dfs, good_paths


def _process_stats_files(paths: list[Path], bad_dir: Path) -> tuple[list[pd.DataFrame], list[Path]]:
    good_dfs: list[pd.DataFrame] = []
    good_paths: list[Path] = []
    for path in paths:
        try:
            df = pd.read_csv(path)
        except _BAD_FILE_ERRORS as e:
            _quarantine(path, bad_dir, f"unreadable: {e}")
            continue
        missing = [c for c in STATS_KEY if c not in df.columns]
        if missing:
            _quarantine(path, bad_dir, f"missing column(s): {', '.join(missing)}")
            continue
        good_dfs.append(df)
        good_paths.append(path)
    return good_dfs, good_paths


def main(
    incoming_dir: Path | None = None,
    master_csv: Path | None = None,
    stats_csv: Path | None = None,
    min_age_seconds: float = 60.0,
) -> int:
    incoming_dir = Path(incoming_dir) if incoming_dir is not None else INCOMING_DIR
    master_csv = Path(master_csv) if master_csv is not None else MASTER_CSV
    stats_csv = Path(stats_csv) if stats_csv is not None else RUN_STATS_CSV
    bad_dir = incoming_dir / "bad"

    incoming_dir.mkdir(parents=True, exist_ok=True)

    row_paths = sorted(
        p for p in incoming_dir.glob("local_rows_*.csv.gz") if _is_old_enough(p, min_age_seconds)
    )
    stats_paths = sorted(
        p for p in incoming_dir.glob("local_stats_*.csv") if _is_old_enough(p, min_age_seconds)
    )

    if not row_paths and not stats_paths:
        return 0

    # ---- Row files: quarantine bad ones first (schema/parse failures are
    # independent of the master's state); only after that do we touch the
    # master, and only once, since an unreadable master must abort loudly
    # before anything else about this run is committed.
    if row_paths:
        good_dfs, good_paths = _process_row_files(row_paths, bad_dir)

        if good_dfs:
            header: list[str] = []
            existing_ids: set[str] = set()
            before_len = 0
            if master_csv.exists():
                # Cheap probe: usecols=["job_posting_id"] parses every row but
                # materializes only that one column, so this never holds the
                # full 92 MB master in memory. It doubles as the readability
                # check (a genuinely corrupt master must still raise here,
                # same contract as the old full pd.read_csv did) and gives us
                # existing_ids + before_len for free before the chunked stream
                # below even starts.
                try:
                    header = pd.read_csv(master_csv, nrows=0).columns.tolist()
                    probe = pd.read_csv(master_csv, usecols=lambda c: c == "job_posting_id",
                                         dtype=str)
                except _BAD_FILE_ERRORS as e:
                    # NEVER treat an unreadable-but-existing master as empty --
                    # that would silently rebuild the exclude set from nothing
                    # and re-bill jobs already scraped. Abort loudly instead;
                    # `set -e` in run_scraper.sh stops the cron run here, before
                    # the scrape spends money. Nothing else has been consumed
                    # this run (bad row files above were quarantined -- that
                    # doesn't depend on the master and is safe to have happened).
                    _say(
                        f"CRITICAL: {master_csv.name} exists but is unreadable ({e}); "
                        f"leaving queued files in place and aborting."
                    )
                    return 1
                before_len = len(probe)
                if "job_posting_id" in probe.columns:
                    existing_ids = set(probe["job_posting_id"].astype(str))
                del probe  # release memory before streaming

            def _not_yet_in_master(rows: pd.DataFrame) -> pd.DataFrame:
                if "job_posting_id" in rows.columns:
                    return rows[~rows["job_posting_id"].isin(existing_ids)]
                return rows

            # Fold incoming files against each other first (small: bounded by
            # the day's pushed files, never the master) so each file's own
            # contribution to the running total can still be reported
            # accurately -- "new rows" means rows that survived dedup, not
            # rows in the source file. Earlier-sorted files win ties over
            # later ones, matching the previous sequential-concat behaviour.
            incoming_running: pd.DataFrame | None = None
            running_total = before_len
            contributions: list[tuple[Path, int]] = []
            for path, df in zip(good_paths, good_dfs):
                incoming_running = merge_rows(incoming_running, df)
                new_total = before_len + len(_not_yet_in_master(incoming_running))
                contributions.append((path, new_total - running_total))
                running_total = new_total

            truly_new = _not_yet_in_master(incoming_running)
            final_total = before_len + len(truly_new)

            if final_total > before_len:
                unified = header + [c for c in incoming_running.columns if c not in header]
                fd, tmp = tempfile.mkstemp(prefix=master_csv.stem + ".", suffix=".tmp",
                                            dir=str(master_csv.parent))
                os.close(fd)
                try:
                    wrote_header = False
                    if master_csv.exists():
                        for chunk in pd.read_csv(master_csv, dtype={"job_posting_id": str}, chunksize=CHUNK):
                            chunk = chunk.reindex(columns=unified)
                            chunk.to_csv(tmp, mode="a", header=not wrote_header, index=False, encoding="utf-8")
                            wrote_header = True
                    truly_new_out = truly_new.reindex(columns=unified)
                    truly_new_out.to_csv(tmp, mode="a", header=not wrote_header, index=False, encoding="utf-8")
                    os.replace(tmp, master_csv)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                _say(f"master updated: {final_total - before_len} new row(s), {final_total} total")
            else:
                _say("no new rows after dedup; master left untouched")

            # Only delete merged files after the write above has landed (or,
            # for the all-duplicate case, after we've established the merge
            # was a safe no-op) -- a crash before this point leaves them
            # queued, and re-merging an already-applied file just dedupes
            # away to nothing on the rerun.
            for path, added in contributions:
                _say(f"{path.name} merged ({added} new rows)")
                # A duplicate contribution path (or a file already swept) would
                # otherwise crash the whole merge on the second unlink — guard it
                # the same way the tmp-file cleanup above does.
                try:
                    path.unlink()
                except OSError:
                    pass

    # ---- Stats files: same shape, but never fatal -- an unreadable existing
    # stats file only warns and leaves incoming stats queued for next time.
    if stats_paths:
        good_dfs, good_paths = _process_stats_files(stats_paths, bad_dir)

        if good_dfs:
            existing_df = None
            if stats_csv.exists():
                try:
                    existing_df = pd.read_csv(stats_csv)
                except _BAD_FILE_ERRORS as e:
                    _say(
                        f"WARNING: {stats_csv.name} exists but is unreadable ({e}); "
                        f"leaving incoming stats files queued."
                    )
                    good_dfs, good_paths = [], []

            if good_dfs:
                combined = existing_df
                before_len = 0 if existing_df is None else len(existing_df)
                running_total = before_len
                contributions: list[tuple[Path, int]] = []
                for path, df in zip(good_paths, good_dfs):
                    combined = merge_stats(combined, df)
                    contributions.append((path, len(combined) - running_total))
                    running_total = len(combined)

                if len(combined) > before_len:
                    _atomic_to_csv(combined, stats_csv)
                    _say(f"{stats_csv.name} updated: {len(combined) - before_len} new row(s), {len(combined)} total")
                else:
                    _say("no new stats rows after dedup; stats file left untouched")

                for path, added in contributions:
                    _say(f"{path.name} merged ({added} new rows)")
                    # A delete failure here (already swept, locked, permissions)
                    # must not propagate: merge runs BEFORE the scrape under set -e,
                    # so an unguarded OSError would kill the day's run over a
                    # bookkeeping delete. Per-file problems never fail the cron --
                    # same guard as the rows path above.
                    try:
                        path.unlink()
                    except OSError:
                        pass

    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incoming", type=Path, default=None, help="Path to the incoming outbox dir")
    parser.add_argument("--master", type=Path, default=None, help="Path to the master jobs CSV")
    parser.add_argument("--stats", type=Path, default=None, help="Path to the run stats CSV")
    parser.add_argument(
        "--min-age",
        type=float,
        default=60.0,
        dest="min_age_seconds",
        help="Skip (leave queued) files younger than this many seconds (default 60)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(
        main(
            incoming_dir=args.incoming,
            master_csv=args.master,
            stats_csv=args.stats,
            min_age_seconds=args.min_age_seconds,
        )
    )
