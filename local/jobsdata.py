"""Toolkit-agnostic data + config logic for the dashboard.

Everything here is pure Python / pandas with no Tk or Qt dependency, so any UI can
build on it: loading and de-duplicating the scored run files, the per-table column
metadata, config.json access, the local company blocklist, and the high-score
filter. Extracted from the old `ui.py` so it survives the UI toolkit swap.
"""
from __future__ import annotations

import gzip
import html
import logging
import os
import re
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import errmsg  # noqa: E402  (needs HERE on sys.path; user-facing message rendering)

from jsonutil import (  # noqa: E402  (needs HERE on sys.path)
    read_json_dict, replace_with_retry, update_json_locked,
)
from csv_io import read_csv_gz, write_csv_gz_atomic  # noqa: E402
from locks import FileLockTimeout, SingleInstance  # noqa: E402  (shared file locks)
from vm_schedule import RUN_LABELS  # noqa: E402  (shared run-label set)

# Repo root: scraper.py / score_jobs.py write their outputs here (one level above
# local/). A LOCAL "Run scraper" lands in <REPO_ROOT>/<label>/, NOT the synced
# Drive folder the dashboard normally reads — local_run_files() bridges that.
REPO_ROOT = HERE.parent

APPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "linkedin_watcher"
APPDATA.mkdir(parents=True, exist_ok=True)
UI_LOCK = APPDATA / "ui.lock"


# Human-friendly column headings (display only — the underlying column ids that
# every filter/sort/populate path keys on are unchanged). Keeps the grid looking
# polished instead of exposing raw snake_case field names.
COLUMN_LABELS = {
    "score": "Score", "deep_score": "Deep", "recommendation": "Reco",
    "applicants": "Applicants", "is_seen": "Seen", "extracted_date": "Found",
    "run_label": "Run", "job_title": "Title", "company_name": "Company",
    "job_location": "Location", "url": "Link", "job_posted_date": "Posted",
    "status": "Status", "status_date": "Updated", "applied_date": "Applied",
    "days": "Days", "follow_up": "Follow-up", "resume": "Resume",
    "source": "Source",
    "timestamp": "When", "input_csv": "Input file", "rows_in": "Rows",
    "filtered_out": "Filtered", "llm_scored": "Scored", "llm_errors": "Errors",
    "stage2_done": "Stage 2", "rescore_attempted": "Rescore try",
    "rescore_scored": "Rescored", "llm_calls": "Calls",
    "prompt_tokens": "In tok", "output_tokens": "Out tok",
}
LABEL_TO_COLUMN = {v: k for k, v in COLUMN_LABELS.items()}

# Initial column widths @100% (JobsTab scales them by the live interface scale).
# `recommendation` is 115 because the widest reco pill, "Don't consider", needs
# 107px at 100% — at the old 100 it elided to "Don't consi…", which is the one
# place the row's meaning is carried by text rather than by its colour.
HIGH_SCORE_COLUMNS = [
    ("score", 50),
    ("deep_score", 70),
    ("recommendation", 115),
    ("applicants", 80),
    ("extracted_date", 105),
    ("run_label", 80),
    ("job_title", 250),
    ("company_name", 170),
    ("job_location", 140),
    ("url", 90),
]

ALL_COLUMNS = [
    ("score", 50),
    ("deep_score", 70),
    ("recommendation", 115),
    ("is_seen", 60),
    ("extracted_date", 105),
    ("run_label", 80),
    ("job_title", 240),
    ("company_name", 170),
    ("job_location", 140),
    ("url", 90),
    ("job_posted_date", 120),
]

TRACKER_COLUMNS = [
    ("status", 110),
    ("status_date", 95),
    ("applied_date", 95),
    ("days", 46),
    ("follow_up", 80),
    ("score", 50),
    ("deep_score", 70),
    ("job_title", 240),
    ("company_name", 170),
    ("url", 90),
    ("resume", 80),
]

STATS_COLUMNS = [
    ("timestamp", 145),
    ("input_csv", 225),
    ("rows_in", 65),
    ("filtered_out", 80),
    ("llm_scored", 78),
    ("llm_errors", 75),
    ("stage2_done", 85),
    ("rescore_attempted", 110),
    ("rescore_scored", 100),
    ("llm_calls", 70),
    ("prompt_tokens", 95),
    ("output_tokens", 95),
]


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Which job ids a run file contains never changes (only its is_seen column gets
# rewritten), so cache per path — reload_data fires on every refresh/mark-seen
# and rescanning every historical gz gets slow as runs accumulate.
_RUN_FILE_IDS: dict[str, list[str]] = {}


# Backwards-compatible alias: this was a jobsdata-local class (_UILock) before
# it was extracted to locks.SingleInstance (shared with watcher.py) since the
# two were byte-for-byte the same lock logic. Kept so local/app.py's `from
# jobsdata import _UILock` and its test monkeypatches keep working unchanged.
_UILock = SingleInstance


def extraction_dates_from_runs(paths: list[Path]) -> dict[str, str]:
    """Map job_posting_id -> the day it was scraped, read from the per-run files.

    The scraper writes each run to morning/ or evening/ with the date baked into
    the filename (linkedin_jobs_<YYYY-MM-DD>_<label>_scored.csv.gz). We scan those
    sibling folders of the loaded master and take the EARLIEST date a job appears
    in (its first scrape = when it was extracted). Cheap: a few small gz files.
    """
    id_date: dict[str, str] = {}
    seen_dirs: set[Path] = set()
    for p in paths:
        parent = Path(p).resolve().parent
        for sub in RUN_LABELS:
            d = parent / sub
            if d in seen_dirs or not d.exists():
                continue
            seen_dirs.add(d)
            for f in sorted(d.glob("*.csv.gz")):
                m = _DATE_RE.search(f.name)
                if not m:
                    continue
                day = m.group(1)
                key = str(f)
                ids = _RUN_FILE_IDS.get(key)
                if ids is None:
                    try:
                        rdf = read_csv_gz(f)
                    except (OSError, ValueError):
                        continue
                    if "job_posting_id" not in rdf.columns:
                        continue
                    ids = rdf["job_posting_id"].astype(str).tolist()
                    _RUN_FILE_IDS[key] = ids
                for jid in ids:
                    prev = id_date.get(jid)
                    if prev is None or day < prev:
                        id_date[jid] = day
    return id_date


def add_extracted_date(df: pd.DataFrame,
                       id_date_provider: Callable[[], dict[str, str]]) -> pd.DataFrame:
    """Ensure an 'extracted_date' column (the day a job was scraped).

    Priority, highest first:
      1. a value already stored in the master (scraper.py now writes one),
      2. the date parsed from the per-run filename it first appeared in,
      3. the date portion of job_posted_date (scrape filter is 'Past 24 hours',
         so the posting date is within ~a day of extraction),
      4. blank.

    `id_date_provider` is a ZERO-ARG callable returning the {id: day} map from the
    per-run files. It is deliberately lazy: that map only ever fills rows with no
    stored date (priority 2 sits *below* the master's own value), so when every
    row already carries one we skip calling it. The provider walks + reads sibling
    run folders that may live on Google Drive File Stream, where the walk can block
    for minutes on a cold mount -- and load_files runs on the UI thread during
    window construction, so paying for it needlessly froze the dashboard on launch.
    """
    if df.empty:
        return df
    if "job_posted_date" in df.columns:
        posted = pd.to_datetime(df["job_posted_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    else:
        posted = pd.Series([None] * len(df), index=df.index)
    if "extracted_date" in df.columns:
        stored = df["extracted_date"].astype(str).str.strip()
        stored = stored.mask(stored.isin(["", "nan", "NaN", "NaT", "None"]))
    else:
        stored = pd.Series([None] * len(df), index=df.index)
    # from_runs is a fallback for blank-stored rows only. No blanks -> the whole
    # (possibly Drive-backed, minutes-long) per-run scan is pure waste; skip it.
    if stored.isna().any():
        id_date = id_date_provider() or {}
        from_runs = df["job_posting_id"].astype(str).map(id_date)
    else:
        from_runs = pd.Series([None] * len(df), index=df.index)
    df["extracted_date"] = stored.fillna(from_runs).fillna(posted).fillna("")
    return df


def load_files(paths: list[Path], *,
               problems: list[tuple[Path, str]] | None = None,
               ) -> tuple[pd.DataFrame, dict[str, Path]]:
    """Load and concatenate CSVs; return (df, id_to_source_path).

    Skipping a source must never stop the others loading, so a bad file is
    dropped rather than raised. But a silent drop is its own failure: a master
    that Drive delivered half-synced, or a truncated .gz, produced an empty frame
    indistinguishable from a fresh install, and the dashboard told a user with
    15,700 collected jobs "No jobs yet" and offered to set them up from scratch.
    Pass `problems` to collect `(path, reason)` for every source that EXISTS and
    was skipped anyway, so the caller can say which file and why. A path that is
    simply absent is not a problem — that is the normal first-run state.
    """
    frames: list[pd.DataFrame] = []
    id_to_path: dict[str, Path] = {}
    for p in paths:
        if not p.exists():
            continue
        try:
            df = read_csv_gz(p)
        except Exception as exc:  # noqa: BLE001 - see below; one bad file, not all of them
            # Deliberately broad. This used to be `(OSError, ValueError)`, which
            # misses the two commonest ways a synced .csv.gz actually breaks: a
            # half-written gzip raises `zlib.error` and a cut-short one raises
            # `EOFError`, and NEITHER is an OSError or a ValueError. So the
            # exception escaped the per-file skip, unwound the whole loop, and
            # took every other source down with it — a truncated Drive master
            # meant the local scrape files did not load either. The entire point
            # of this loop is that one unreadable source costs only that source.
            if problems is not None:
                # errmsg.for_user, not str(exc): the caller renders this beside
                # `Path(p).name` in the dashboard's empty panel, and an OSError
                # carries the offending path in its OWN message -- so the panel
                # carefully said "master.csv.gz" and then printed
                # "[Errno 13] Permission denied: 'C:\\Users\\<name>\\...'" right
                # after it. A locked master (Excel has it open, an AV scanner is
                # mid-scan) is the ordinary way to see this, and the string ends
                # up in screenshots and bug reports.
                problems.append((p, errmsg.for_user(exc, with_type=True)))
            continue
        if "job_posting_id" not in df.columns:
            if problems is not None:
                problems.append((p, "no job_posting_id column"))
            continue
        df["job_posting_id"] = df["job_posting_id"].astype(str)
        df["_source"] = str(p)
        frames.append(df)
        for jid in df["job_posting_id"]:
            id_to_path.setdefault(jid, p)
    if not frames:
        return pd.DataFrame(), {}
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["job_posting_id"], keep="last")
    removed = load_removed_jobs()  # user-deleted ids stay hidden even if Drive still has them
    if removed:
        present = set(combined["job_posting_id"].astype(str))
        combined = combined[~combined["job_posting_id"].astype(str).isin(removed)]
        # Prune marker ids no source still carries (audit P2-30): once the Drive
        # sync has physically dropped the row everywhere, the hide-marker has done
        # its job — without this, config.json's removed_jobs only ever grows.
        stale = removed - present
        if stale:
            _save_removed_jobs(removed - stale)
    combined = add_extracted_date(combined, lambda: extraction_dates_from_runs(paths))
    # Display-friendly applicant count (Bright Data's job_num_applicants): used
    # to prioritize the apply window — fewer applicants = better odds.
    if "job_num_applicants" in combined.columns:
        n = pd.to_numeric(combined["job_num_applicants"], errors="coerce")
        combined["applicants"] = [("" if pd.isna(v) else str(int(v))) for v in n]
    else:
        combined["applicants"] = ""
    # Precomputed lowercase search haystack so per-keystroke filtering matches
    # against one ready column instead of rebuilding it across the whole frame.
    scols = [c for c in ("job_title", "company_name", "url") if c in combined.columns]
    combined["_search"] = (
        combined[scols].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
        if scols else ""
    )
    return combined, id_to_path


# The canonical cumulative store scraper.py / score_jobs.py own. The manual-add
# bridge (a dashboard-loadable manual/manual_jobs_scored.csv.gz beside whichever
# master is in play) is derived inside append_manual_job, which takes the master
# path as an argument.
MASTER_CSV = REPO_ROOT / "linkedin_jobs_master.csv"


def local_run_files(base: Path | None = None) -> list[Path]:
    """Scored run files produced by a LOCAL scrape, newest-last per label.

    scraper.py / score_jobs.py write to the repo dir (`<root>/<label>/*_scored.csv.gz`),
    not the synced Drive folder the dashboard reads — on the VM, rclone bridges that
    gap; locally nothing does. The dashboard merges these into its sources so a
    local "Run scraper" shows up immediately and survives a restart, with or
    without a VM/Drive setup. `load_files` dedupes by job_posting_id, so a job that
    is also in the Drive master is not double-counted. Manually-added jobs live in a
    sibling `manual/` file (no RUN_LABEL folder fits them) and are merged in too.
    """
    base = Path(base) if base is not None else REPO_ROOT
    out: list[Path] = []
    for label in RUN_LABELS:
        d = base / label
        if d.is_dir():
            out.extend(sorted(d.glob("*_scored.csv.gz")))
    manual = base / "manual" / "manual_jobs_scored.csv.gz"
    if manual.exists():
        out.append(manual)
    return out


def unscored_run_csvs(base: Path | None = None) -> list[Path]:
    """Run-dir input CSVs with no `*_scored.csv.gz` sibling, oldest-first.

    These are what an interrupted local scrape leaves behind: the scrape
    pipeline (scraper -> scorer -> refresh) runs inside the dashboard process,
    so closing the dashboard mid-run orphans it — the collected `<label>/<run>.csv`
    survives on disk but the scoring step never ran, and unscored CSVs are
    invisible to the dashboard. `MainWindow.offer_unscored_recovery()` uses this
    on startup to offer running the missed scoring step (score_jobs.py scores the
    newest one per invocation).
    """
    base = Path(base) if base is not None else REPO_ROOT
    out: list[Path] = []
    for label in RUN_LABELS:
        d = base / label
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.csv")):
            if not p.with_name(p.stem + "_scored.csv.gz").exists():
                out.append(p)
    return out


# Score/scrape columns that should land in the persisted manual row so it carries
# the same signal a scraped+scored row does. (job_posting_id is the dedup key.)
_MANUAL_PERSIST_COLS = [
    "url", "job_title", "company_name", "job_location", "job_summary",
    "job_posted_date", "run_label", "extracted_date", "source",
    "score", "reason", "deep_score", "strengths", "gaps", "recommendation",
    "filter_junk_title", "filter_junk_desc", "filter_too_many_years",
    "filter_clearance", "filter_degree", "filtered_out", "is_seen",
]


log = logging.getLogger(__name__)

# Serializes every read-modify-write of the LOCAL master CSV (and its manual-gz
# sibling). The dashboard deletes on a background write queue while manual-add
# runs on its own worker thread, so without this two writers can both read the
# master at N rows and the second atomic replace silently drops the first
# writer's change (audit P2-25). RLock because update_manual_job holds it across
# its drop + re-append sequence, which re-enters the helpers below.
_MASTER_WRITE_LOCK = threading.RLock()


def _append_dedup_csv(record: dict, path: Path, *, compression=None) -> bool:
    """Append one job record to a CSV, deduping on job_posting_id (keep="first").

    Mirrors scraper.append_to_master: cast the id to str before deduping so a
    re-read int id never silently keeps a duplicate. Returns True when the record
    was newly added (False when an existing row with the same id already won).
    """
    jid = str(record.get("job_posting_id", "")).strip()
    if not jid:
        return False
    with _MASTER_WRITE_LOCK:
        return _append_dedup_csv_locked(record, jid, path, compression)


# Rows per chunk for the streaming master rewrites (test-patched).
_RW_CHUNK = 5000


def _open_text_writer(tmp_path: Path, compression):
    if compression == "gzip":
        return gzip.open(tmp_path, "wt", encoding="utf-8", newline="")
    return open(tmp_path, "w", encoding="utf-8", newline="")


def _append_dedup_csv_locked(record: dict, jid: str, path: Path, compression) -> bool:
    """Streaming append (audit P2-21/BACKLOG): the master is copied chunk-by-chunk
    to a tempfile (dedup keep="first" on job_posting_id, columns unified with the
    new record), the new row lands at the end unless its id already exists, and
    the tempfile atomically replaces the master. Only an id set is held in
    memory — never the master's ~90 MB of text columns."""
    new_df = pd.DataFrame([record])
    new_df["job_posting_id"] = new_df["job_posting_id"].astype(str)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_csv_gz_atomic(new_df, path, compression=compression)
        return True
    try:
        header = pd.read_csv(path, nrows=0, compression=compression)
    except (OSError, ValueError) as e:
        # NEVER treat an unreadable-but-existing store as empty -- overwriting
        # it would silently destroy the cumulative master.
        raise OSError(f"cannot append to {path.name}: existing file unreadable ({e})") from e
    unified = list(header.columns) + [c for c in new_df.columns
                                      if c not in header.columns]
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".append.tmp",
                                    dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    already = False
    seen_ids: set[str] = set()
    try:
        try:
            with _open_text_writer(tmp_path, compression) as out:
                wrote_header = False
                # dtype=object + keep_default_na=False (audit C6-1, extending
                # P2-26): with inferred per-chunk dtypes every manual add
                # reformats untouched rows of the master -- score 5 comes back
                # as 5.0, and a chunk boundary can even infer two dtypes for one
                # column. Reading every cell as the literal string keeps the
                # rewrite byte-stable for rows we aren't changing.
                for chunk in pd.read_csv(path, dtype=object, keep_default_na=False,
                                         compression=compression,
                                         chunksize=_RW_CHUNK):
                    if "job_posting_id" in chunk.columns:
                        jids = chunk["job_posting_id"].astype(str)
                        keep = ~jids.isin(seen_ids)   # dedup keep="first"
                        chunk = chunk[keep]
                        kept_ids = set(jids[keep])
                        seen_ids |= kept_ids
                        if jid in kept_ids:
                            already = True            # existing row wins
                    chunk = chunk.reindex(columns=unified)
                    chunk.to_csv(out, index=False, header=not wrote_header)
                    wrote_header = True
                if not already:
                    new_df.reindex(columns=unified).to_csv(
                        out, index=False, header=not wrote_header)
        except (OSError, ValueError) as e:
            raise OSError(f"cannot append to {path.name}: existing file "
                          f"unreadable ({e})") from e
        replace_with_retry(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return not already


def append_manual_job(record: dict, *, master_csv: Path | None = None) -> bool:
    """Persist a manually-added (already-scored) job, same schema/dedup as scraped.

    Writes two places, both deduped on job_posting_id so re-adding the same job is a
    no-op:
      1. the canonical master store (`linkedin_jobs_master.csv`) — the cumulative
         record scraper.py / score_jobs.py own, so the manual job is a first-class
         master row marked source="manual";
      2. a dashboard-loadable `manual/manual_jobs_scored.csv.gz` (picked up by
         `local_run_files()`) so the job appears in the UI immediately and across
         restarts, the same bridge a local scrape gets.
    Returns True when the job was newly added to the master (False if a duplicate).
    """
    row = {c: record.get(c, "") for c in (["job_posting_id"] + _MANUAL_PERSIST_COLS)
           if c in record or c == "job_posting_id"}
    # Carry the full JD into the master so re-scoring/tailoring works off it later.
    if record.get("job_description_formatted"):
        row["job_description_formatted"] = record["job_description_formatted"]
    master = Path(master_csv) if master_csv is not None else MASTER_CSV
    with _MASTER_WRITE_LOCK:   # master + gz land as one unit vs. other writers
        added = _append_dedup_csv(row, master, compression=None)
        # The gz copy never carries the raw JD (the scored run files don't either).
        gz_row = {k: v for k, v in row.items() if k != "job_description_formatted"}
        manual_gz = (master.parent / "manual" / "manual_jobs_scored.csv.gz")
        try:
            _append_dedup_csv(gz_row, manual_gz, compression="gzip")
        except OSError as e:
            # The canonical master append is what matters, but a silently missing
            # gz row makes the master and the dashboard bridge diverge — say so.
            log.warning("manual job %s: gz bridge append failed (%s) — master and "
                        "manual/manual_jobs_scored.csv.gz now differ", row.get(
                            "job_posting_id", "?"), e)
    return added


def _drop_ids_from_csv(path: Path, ids: set[str]) -> None:
    """Rewrite a (optionally gz) CSV dropping rows whose job_posting_id is in `ids`.
    No-op when the file is missing, unreadable, idless, or unaffected."""
    if not path.exists() or not ids:
        return
    compression = "gzip" if path.suffix == ".gz" else None
    with _MASTER_WRITE_LOCK:
        # Streaming rewrite (audit P2-21/BACKLOG): chunked copy to a tempfile,
        # atomically swapped in only when a target row was actually dropped.
        fd, tmp_name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".drop.tmp",
                                        dir=str(path.parent))
        os.close(fd)
        tmp_path = Path(tmp_name)
        total = kept = 0
        try:
            try:
                with _open_text_writer(tmp_path, compression) as out:
                    wrote_header = False
                    # dtype=str + keep_default_na=False: see the note in
                    # _append_dedup_csv_locked (audit C6-1). A delete must not
                    # silently reformat the rows it keeps.
                    for chunk in pd.read_csv(path, dtype=str, keep_default_na=False,
                                             compression=compression,
                                             chunksize=_RW_CHUNK):
                        if "job_posting_id" not in chunk.columns:
                            return
                        total += len(chunk)
                        chunk = chunk[~chunk["job_posting_id"].astype(str).isin(ids)]
                        kept += len(chunk)
                        chunk.to_csv(out, index=False, header=not wrote_header)
                        wrote_header = True
            except (OSError, ValueError):
                return
            if kept == total:
                return  # this file held none of the targets
            replace_with_retry(tmp_path, path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def load_removed_jobs() -> set[str]:
    """Job ids the user deleted from the dashboard (config.json 'removed_jobs').

    `load_files` filters these out so a row that still lives in the Drive-synced
    master (which the dashboard can't rewrite) stays gone in the UI until the next
    sync physically removes it. Local writable stores are also rewritten on delete."""
    val = _load_cfg().get("removed_jobs")
    return {str(x) for x in val} if isinstance(val, (list, tuple, set)) else set()


def _save_removed_jobs(ids: set[str]) -> None:
    _save_cfg({"removed_jobs": sorted(str(i) for i in ids)})


def delete_jobs(ids, *, master_csv: Path | None = None) -> int:
    """Remove jobs from the LOCAL writable stores and remember them as removed.

    Records them in config.json 'removed_jobs' (so a Drive-only copy also disappears
    from the UI), THEN rewrites the local master + manual gz + local run files
    dropping these ids. Tracker status is cleared by the caller (the UI's seen
    registry). Returns the count of distinct ids targeted."""
    ids = {str(i).strip() for i in (ids or []) if str(i).strip()}
    if not ids:
        return 0
    master = Path(master_csv) if master_csv is not None else MASTER_CSV
    # Record the hide-marker FIRST, before the slow multi-file rewrite. load_files
    # filters removed_jobs, so this makes the deletion durable immediately: a reload
    # racing the rewrite (the dashboard deletes on a background queue while the UI
    # stays live) still hides the row instead of resurrecting it, and a row stays
    # hidden even if a later rewrite fails. The CSV drop is then just space reclaim.
    _save_removed_jobs(load_removed_jobs() | ids)
    with _MASTER_WRITE_LOCK:   # the multi-file drop is one unit vs. other writers
        for p in [master, *local_run_files(master.parent)]:
            _drop_ids_from_csv(p, ids)
    return len(ids)


# Rows per chunk for master_row's streaming scan (test-patched to force multi-chunk).
_MASTER_ROW_CHUNK = 2000


def master_row(jid, *, master_csv: Path | None = None) -> dict | None:
    """The full master-CSV row for a job id (incl. job_description_formatted) or None.
    Used to prefill the edit dialog with the stored fields + JD.

    Callers sit on the UI thread, so this streams the master in bounded chunks and
    stops at the first hit (same idiom as score_jobs._load_rows_by_id) instead of
    parsing the whole file — a full-column read of the master froze the window."""
    master = Path(master_csv) if master_csv is not None else MASTER_CSV
    if not master.exists():
        return None
    want = str(jid)
    try:
        for chunk in pd.read_csv(master, dtype={"job_posting_id": str},
                                 chunksize=_MASTER_ROW_CHUNK):
            if "job_posting_id" not in chunk.columns:
                return None
            hit = chunk[chunk["job_posting_id"].astype(str) == want]
            if hit.empty:
                continue
            row = hit.iloc[0].to_dict()
            return {k: ("" if (isinstance(v, float) and pd.isna(v)) else v)
                    for k, v in row.items()}
    except (OSError, ValueError):
        return None
    return None


def update_manual_job(record: dict, *, old_id=None, master_csv: Path | None = None) -> bool:
    """Field-fix an existing manual job: drop the old row(s) everywhere, then
    re-append the edited record (since `_append_dedup_csv` keeps 'first'). Editing
    also un-removes a previously-deleted id. Does NOT re-score/re-tailor — those stay
    on the existing buttons. Returns append result (True when it lands fresh)."""
    master = Path(master_csv) if master_csv is not None else MASTER_CSV
    drop = {str(d).strip() for d in (old_id, record.get("job_posting_id")) if str(d or "").strip()}
    with _MASTER_WRITE_LOCK:   # drop + re-append is one unit vs. other writers
        if drop:
            for p in [master, *local_run_files(master.parent)]:
                _drop_ids_from_csv(p, drop)
            rem = load_removed_jobs() - drop  # editing resurrects a previously-removed id
            if rem != load_removed_jobs():
                _save_removed_jobs(rem)
        return append_manual_job(record, master_csv=master_csv)


def _cfg_path() -> Path:
    return HERE / "config.json"


def _load_cfg() -> dict:
    """config.json (shared with the watcher), {} when unreadable."""
    return read_json_dict(_cfg_path())


def _save_cfg(updates: dict) -> None:
    """Merge updates into local/config.json (best-effort; never crash the UI).

    Locked, not just atomic: deletes run on the dashboard's background
    SerialTaskQueue while every Settings save, column-hide and layout tweak
    runs on the UI thread, and the watcher writes gdrive_root from another
    process entirely. A lock-free read-modify-write there reverts whichever
    writer read first — a deleted job comes back, or a page of settings does.
    """
    try:
        update_json_locked(_cfg_path(), updates)
    except (OSError, FileLockTimeout):
        pass


def load_min_score(default: int = 4) -> int:
    try:
        return int(_load_cfg().get("min_score", default))
    except (TypeError, ValueError):
        return default


def load_followup_days(default: int = 5) -> int:
    """Days after 'applied' before a follow-up nudge shows in the tracker."""
    try:
        return int(_load_cfg().get("followup_days", default))
    except (TypeError, ValueError):
        return default


def live_resume_ids(resume_paths) -> set[str]:
    """Job ids whose recorded tailored-résumé folder still EXISTS on disk.

    The blue "tailored" row tint and the tracker's résumé ✓ derive from this, so
    deleting a folder by hand clears them on the next reload. Non-destructive: the
    registry row is left intact, so the tint returns if the folder reappears (e.g.
    a remounted drive). `resume_paths` is the registry's {job_posting_id: folder}
    map; "exists" means the recorded path is a directory right now.
    """
    items = resume_paths.items() if isinstance(resume_paths, dict) else []
    live: set[str] = set()
    for jid, path in items:
        try:
            if path and Path(str(path)).is_dir():
                live.add(str(jid))
        except OSError:
            pass
    return live


def load_hidden_columns() -> dict[str, list[str]]:
    """Per-table hidden column ids, persisted in config.json under 'hidden_columns'.
    Keyed by table ('high' / 'all' / 'tracker'). Shape-checked so a hand-edited or
    stale config can never crash the UI."""
    raw = _load_cfg().get("hidden_columns", {})
    if not isinstance(raw, dict):
        return {}
    return {str(k): [str(c) for c in v]
            for k, v in raw.items() if isinstance(v, list)}


def save_hidden_columns(hidden: dict[str, list[str]]) -> None:
    """Persist the per-table hidden-column map (best-effort; never crashes the UI)."""
    _save_cfg({"hidden_columns": hidden})


def load_vm_schedule_times() -> list[str]:
    """The VM run times last PUSHED from the dashboard (config.json
    'vm_schedule_times'). The VM panel never reads live VM state, so this is what
    seeds its schedule editor on open; [] when never pushed (or hand-mangled)."""
    raw = _load_cfg().get("vm_schedule_times")
    return [str(t) for t in raw] if isinstance(raw, list) else []


def save_vm_schedule_times(times) -> None:
    """Record the VM run times just pushed (best-effort; never crashes the UI)."""
    _save_cfg({"vm_schedule_times": [str(t) for t in times]})


def load_collapsed_sections() -> list[str]:
    """Settings sections the user has collapsed, persisted in config.json under
    'settings_collapsed'. Shape-checked so a stale/hand-edited config can't crash."""
    raw = _load_cfg().get("settings_collapsed", [])
    return [str(s) for s in raw] if isinstance(raw, list) else []


def save_collapsed_sections(sections) -> None:
    """Persist the collapsed Settings sections (best-effort; never crashes the UI)."""
    _save_cfg({"settings_collapsed": [str(s) for s in sections]})


def load_show_advanced() -> bool:
    """Is the Settings tab's "Show advanced settings" disclosure ticked?
    (config.json 'settings_show_advanced', default False.)

    Strict `is True` rather than truthiness: a hand-edited `"false"` is a truthy
    STRING, so `bool()` would turn disclosure on for someone editing the file to
    turn it off. Anything that isn't a real JSON `true` falls back to the shipped
    default."""
    return _load_cfg().get("settings_show_advanced") is True


def save_show_advanced(show: bool) -> None:
    """Persist the advanced-settings disclosure (best-effort; never crashes the UI)."""
    _save_cfg({"settings_show_advanced": bool(show)})


def load_ui_scale_pct() -> int:
    """The saved interface scale percent (config.json `ui_scale_pct`), default 100,
    clamped to the supported 75-150 range so a stale/hand-edited value can't break it."""
    try:
        pct = int(round(float(_load_cfg().get("ui_scale_pct", 100) or 100)))
    except (TypeError, ValueError):
        pct = 100
    return max(75, min(150, pct))


def save_ui_scale_pct(pct: int) -> None:
    """Persist the interface scale percent (clamped 75-150; best-effort)."""
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        pct = 100
    _save_cfg({"ui_scale_pct": max(75, min(150, pct))})


# --- resume layout (per-bullet line targets, edited from the Resume Data tab) ----
# Two maps in config.json, both read by resume_tailor/config.py:
#   resume_layout  : {section_block: {"line_targets": [int, ...]}}  (experience/leadership)
#   project_layout : {project_name:  {"line_targets": [int, ...]}}
# `resume_layout_enabled` is the master on/off so the user can A/B test the custom
# layout against the engine defaults WITHOUT deleting the saved targets.

def load_resume_layout_enabled() -> bool:
    """Master toggle for the custom bullet layout (default True when absent)."""
    return _load_cfg().get("resume_layout_enabled", True) is not False


def save_resume_layout_enabled(enabled: bool) -> None:
    _save_cfg({"resume_layout_enabled": bool(enabled)})


def load_resume_layout() -> dict:
    """{block: {'line_targets': [...]}} from config.json ({} when absent/bad)."""
    val = _load_cfg().get("resume_layout")
    return val if isinstance(val, dict) else {}


def save_resume_layout(layout: dict) -> None:
    _save_cfg({"resume_layout": dict(layout)})


def load_project_layout() -> dict:
    """{project: {'line_targets': [...]}} from config.json ({} when absent/bad)."""
    val = _load_cfg().get("project_layout")
    return val if isinstance(val, dict) else {}


def save_project_layout(layout: dict) -> None:
    _save_cfg({"project_layout": dict(layout)})


def load_verbatim_blocks() -> dict:
    """{block_name: [bullet, ...]} from config.json — blocks the user marked
    'don't tailor; use my exact bullets'. A non-empty list means that block renders
    verbatim (the résumé engine bypasses the LLM for it). {} when absent/bad."""
    val = _load_cfg().get("verbatim_blocks")
    return val if isinstance(val, dict) else {}


def save_verbatim_blocks(blocks: dict) -> None:
    _save_cfg({"verbatim_blocks": dict(blocks)})


# How many projects the tailored resume lists, and whether that's a ceiling or an
# exact target. Read by resume_tailor/config.py (projects_max() / projects_mode()).
# 6 mirrors resume_tailor.config.PROJECTS_MAX_LIMIT (the resume is one page).
_PROJECTS_MAX_LIMIT = 6


def load_projects_count() -> tuple[int, str]:
    """(count, mode) from config.json. count clamped 1.._PROJECTS_MAX_LIMIT
    (default 3); mode is 'max' or 'exact' (default 'max')."""
    cfg = _load_cfg()
    try:
        n = int(cfg.get("projects_max"))
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(_PROJECTS_MAX_LIMIT, n))
    mode = cfg.get("projects_mode")
    mode = mode if mode in ("max", "exact") else "max"
    return n, mode


def save_projects_count(n: int, mode: str) -> None:
    """Persist the project count cap + mode (clamped/normalized; best-effort)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(_PROJECTS_MAX_LIMIT, n))
    mode = "exact" if str(mode).lower() == "exact" else "max"
    _save_cfg({"projects_max": n, "projects_mode": mode})


def load_project_bullet_tiers() -> list[dict]:
    """[{'projects': int, 'bullets': int}, ...] from config.json ([] when absent/bad).
    Tiered, rank-based project bullet counts read by the résumé engine
    (resume_tailor/config.py:project_bullet_tiers); an empty list means flat allotment."""
    val = _load_cfg().get("project_bullet_tiers")
    if not isinstance(val, list):
        return []
    return [{"projects": t["projects"], "bullets": t["bullets"]}
            for t in val if isinstance(t, dict) and "projects" in t and "bullets" in t]


def save_project_bullet_tiers(tiers: list) -> None:
    """Persist tiered project bullet counts (merged into config.json, so it never wipes
    the per-name project_layout). Each tier is sanitized to {'projects': N>=1,
    'bullets': 1..5}; malformed rows are dropped. An empty list disables tiering."""
    out: list[dict] = []
    for t in tiers or []:
        try:
            p = max(1, int(t["projects"]))
            b = max(1, min(5, int(t["bullets"])))
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"projects": p, "bullets": b})
    _save_cfg({"project_bullet_tiers": out})


def gdrive_root_dir(csv_paths: list[Path]) -> Path | None:
    """The synced LinkedInJobs folder: config.json's gdrive_root, else inferred
    from the loaded files' location (run files sit one level deeper)."""
    root = str(_load_cfg().get("gdrive_root", "") or "")
    if root and Path(root).exists():
        return Path(root)
    for p in csv_paths:
        parent = Path(p).resolve().parent
        if parent.name in RUN_LABELS:
            parent = parent.parent
        if parent.exists():
            return parent
    return None


def blocklist_path(csv_paths: list[Path]) -> Path | None:
    root = gdrive_root_dir(csv_paths)
    return (root / "company_blocklist.txt") if root else None


def run_staleness(newest_run, now, threshold_hours) -> tuple[str, float]:
    """Classify how fresh the latest pipeline run is.

    `newest_run` is the most recent run's datetime (None when nothing has run
    yet), `now` is the current datetime. Returns `(state, age_hours)` where state
    is "fresh" when the newest run is within `threshold_hours` and "stale"
    otherwise (also "stale", with infinite age, when there is no run at all)."""
    if newest_run is None:
        return ("stale", float("inf"))
    age = (now - newest_run).total_seconds() / 3600.0
    return (("fresh" if age <= threshold_hours else "stale"), age)


def load_local_blocklist(csv_paths: list[Path]) -> list[str]:
    """Companies blocked from the UI. The file lives in the synced Drive folder
    so run_scraper.sh pulls it down for scraper.py on every VM run."""
    p = blocklist_path(csv_paths)
    if not p or not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def append_to_blocklist(csv_paths: list[Path], company: str) -> Path:
    p = blocklist_path(csv_paths)
    if not p:
        raise OSError("could not resolve the synced LinkedInJobs folder")
    existing = {b.lower() for b in load_local_blocklist(csv_paths)}
    if company.lower() not in existing:
        with open(p, "a", encoding="utf-8") as f:
            f.write(company + "\n")
    return p


def drop_blocklisted(df: pd.DataFrame, names: list[str]) -> pd.DataFrame:
    """Mirror scraper.py's substring/case-insensitive company filter locally so
    a UI block takes effect immediately, not just on the next VM run."""
    if df.empty or not names or "company_name" not in df.columns:
        return df
    hay = df["company_name"].fillna("").astype(str).str.lower()
    mask = pd.Series(False, index=df.index)
    for bad in names:
        mask = mask | hay.str.contains(bad.lower(), na=False, regex=False)
    return df[~mask]


def filter_high_unseen(df: pd.DataFrame, min_score: int = 4) -> pd.DataFrame:
    if df.empty or "score" not in df.columns:
        return df.iloc[0:0]
    score = pd.to_numeric(df["score"], errors="coerce").fillna(0)
    is_seen = (df["is_seen"].astype(str) if "is_seen" in df.columns
               else pd.Series("no", index=df.index))
    mask = (score >= min_score) & (is_seen == "no")
    out = df.loc[mask].copy()
    out["__score_num"] = score[mask]
    if "deep_score" in out.columns:
        out["__deep_num"] = pd.to_numeric(out["deep_score"], errors="coerce").fillna(0)
    else:
        out["__deep_num"] = 0.0
    # Fewest applicants first within a score band: early applications convert
    # far better, so the freshest apply window floats to the top. Unknown
    # applicant counts sort last.
    if "job_num_applicants" in out.columns:
        out["__appl_num"] = pd.to_numeric(out["job_num_applicants"], errors="coerce").fillna(float("inf"))
    else:
        out["__appl_num"] = float("inf")
    out = out.sort_values(
        ["__score_num", "__appl_num", "__deep_num"], ascending=[False, True, False]
    )
    return out.drop(columns=["__score_num", "__deep_num", "__appl_num"])


def sort_query(view: pd.DataFrame) -> pd.DataFrame:
    """Default listing order: most-recent extracted day first, then highest score,
    then highest deep_score. (A header click in the table re-sorts on top of this.)"""
    if view.empty:
        return view
    keys: list[str] = []
    asc: list[bool] = []
    tmp = view
    if "extracted_date" in tmp.columns:
        tmp = tmp.assign(__d=tmp["extracted_date"].astype(str))
        keys.append("__d")
        asc.append(False)
    if "score" in tmp.columns:
        tmp = tmp.assign(__s=pd.to_numeric(tmp["score"], errors="coerce").fillna(-1))
        keys.append("__s")
        asc.append(False)
    if "deep_score" in tmp.columns:
        tmp = tmp.assign(__ds=pd.to_numeric(tmp["deep_score"], errors="coerce").fillna(-1))
        keys.append("__ds")
        asc.append(False)
    if keys:
        tmp = tmp.sort_values(keys, ascending=asc, kind="stable")
        tmp = tmp.drop(columns=["__d", "__s", "__ds"], errors="ignore")
    return tmp


def filter_and_sort(base: pd.DataFrame, search: str, minscore: str, day: str,
                    time_: str, reco: str, easy: str | bool = "All",
                    search_column: str | None = None) -> pd.DataFrame:
    """Apply the shared multi-column filters (AND) + default sort to a base set.
    search_column: a column id to restrict the text search to; None/"All" = all.
    easy: "All" / "Easy Apply" / "Not Easy Apply" (legacy bools normalize to
    True -> "Easy Apply", False -> "All"). A NaN/blank is_easy_apply cell counts
    as NOT easy apply — it survives "Not Easy Apply" and never matches "Easy Apply"."""
    if isinstance(easy, bool):                     # pre-combo callers
        easy = "Easy Apply" if easy else "All"
    view = base
    if view.empty:
        return view
    if search:
        if search_column and search_column not in ("", "All") and search_column in view.columns:
            hay = view[search_column].fillna("").astype(str).str.lower()
            view = view.loc[hay.str.contains(search, na=False, regex=False)]
        elif "_search" in view.columns:
            view = view.loc[view["_search"].str.contains(search, na=False, regex=False)]
        else:
            cols = [c for c in ("job_title", "company_name", "url") if c in view.columns]
            if cols:
                hay = view[cols].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
                view = view.loc[hay.str.contains(search, na=False, regex=False)]
    if minscore not in ("", "Any") and "score" in view.columns:
        sc = pd.to_numeric(view["score"], errors="coerce")
        view = view.loc[sc >= float(minscore)]
    if day not in ("", "All") and "extracted_date" in view.columns:
        view = view.loc[view["extracted_date"].astype(str) == day]
    if time_ not in ("", "All") and "run_label" in view.columns:
        view = view.loc[view["run_label"].astype(str).str.lower() == time_.lower()]
    if reco not in ("", "All") and "recommendation" in view.columns:
        view = view.loc[view["recommendation"].astype(str).str.lower() == reco.lower()]
    if easy not in ("", "All") and "is_easy_apply" in view.columns:
        truthy = view["is_easy_apply"].astype(str).str.lower().isin(("true", "1", "yes"))
        view = view.loc[truthy] if easy == "Easy Apply" else view.loc[~truthy]
    return sort_query(view)


# Block-level tags whose OPENING or closing form ends a line. The opening form
# has to count too: postings routinely write "Responsibilities:<ul><li>..." with
# no closing tag in between, and stripping that <ul> to nothing would weld the
# lead-in onto the first bullet.
_BLOCK_TAG_RE = re.compile(
    r"</?(?:br|p|div|h[1-6]|ul|ol|tr|table|section|article|blockquote|hr)\b[^>]*>",
    re.I,
)
_LI_OPEN_RE = re.compile(r"<li\b[^>]*>", re.I)
# A `</li>` with only whitespace before the next `<li>` ends the line without
# the blank line an ordinary `</li>` + `<li>` pair would leave, so bullets
# inside one list read as consecutive lines.
_LI_TIGHT_RE = re.compile(r"</li\s*>\s*(?=<li\b)", re.I)
_LI_CLOSE_RE = re.compile(r"</li\s*>", re.I)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
# The `</li>`-adjacent-`<li>` case above only covers bullets that share one
# list. Plenty of postings give every bullet its OWN `<ul>`, and there the
# `</ul>` and the next `<ul>` each break the line, so the blank line survives.
# Closing it needs the RENDERED text: a blank run whose neighbours are both
# bullet lines. A gap next to a paragraph is left alone — that is what sets a
# list off from the prose around it.
_BULLET_GAP_RE = re.compile(r"^(• .*)\n\n+(?=• )", re.M)
# An `<li>` whose content is wrapped in a block tag (`<li><p>text</p></li>`)
# breaks the line right after the marker, orphaning it from its OWN text — 41
# of the 59 stray markers in the master. Rejoin those; only then is a marker
# with nothing after it genuinely an EMPTY `<li>`, which drops out entirely.
# The gap is the tell: a block tag between the marker and the next text emits a
# second newline, so an ADJACENT line is the item's own content, and only a
# non-bullet one (`(?=[^\s•])`) is text rather than the next marker.
_ORPHAN_BULLET_RE = re.compile(r"^•\n(?=[^\s•])", re.M)
_BARE_BULLET_RE = re.compile(r"^•(?:\n|$)", re.M)

# Elements that carry no posting prose — their TEXT goes with the tags. LinkedIn
# wraps every posting in `<section class="show-more-less-html">` whose "Show
# more" / "Show less" buttons otherwise land at the bottom of the description.
# Each alternative spans an opener to its OWN closing tag, and the span may not
# contain another opener of the same name: `(?<!/)` rejects a self-closing
# `<icon/>` and `(?!<tag\b)` stops an unclosed opener from reaching a later
# sibling's closer. Both cases fall through to the `_ANY_TAG_RE` strip, which
# leaks a word of chrome at worst — the alternative is silently deleting the
# prose in between, which the reader cannot even notice.
_DROP_ELEMENTS = ("script", "style", "button", "icon", "svg", "nav", "header",
                  "footer", "noscript", "form", "select")
_DROP_EL_RE = re.compile(
    "|".join(rf"<{tag}\b[^>]*(?<!/)>(?:(?!<{tag}\b).)*?</{tag}\s*>"
             for tag in _DROP_ELEMENTS),
    re.I | re.S,
)


def html_to_text(raw: str) -> str:
    """HTML -> readable plain text, preserving the posting's line structure.

    Block tags become newlines and `<li>` becomes a bullet marker, so a scraped
    `job_description_formatted` reads as paragraphs and lists instead of one
    run-on block. Deliberately hand-rolled rather than delegating to
    `markdownify` (which the résumé tailor uses): markdown syntax is noise in a
    plain-text viewer, and this module carries no soft dependencies.

    Non-content elements (page chrome such as LinkedIn's "Show more" button)
    are dropped WITH their text before anything else runs.

    Plain text passes through essentially unchanged.
    """
    if not raw:
        return ""
    # First: chrome goes with its contents, so its words never reach the strip.
    text = _DROP_EL_RE.sub("", raw)
    text = _LI_TIGHT_RE.sub("\n", text)
    text = _LI_CLOSE_RE.sub("\n", text)
    text = _LI_OPEN_RE.sub("• ", text)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub("", text)
    # Unescape LAST: an escaped `&lt;b&gt;` in the posting's own prose is text,
    # and unescaping before the strip would turn it into a live tag.
    text = html.unescape(text)
    # Strip both ends: HTML source indentation otherwise leaks through as a
    # gutter on every line the posting's markup happened to indent. A `<pre>`
    # block would lose its own indentation with it — postings do not use one.
    text = "\n".join(line.strip() for line in text.splitlines())
    # Now that the lines are final: reunite a marker with its own text, then
    # drop the markers that turn out to have no text at all.
    text = _ORPHAN_BULLET_RE.sub("• ", text)
    text = _BARE_BULLET_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # LAST, on the rendered lines: close the gap between two bullets that the
    # markup structure alone cannot see (one `<ul>` per bullet).
    return _BULLET_GAP_RE.sub(r"\1\n", text)


def job_detail_fields(row, snapshot: dict | None = None) -> dict:
    """The job-detail-card content as a flat dict.

    The structured source the Qt `JobDetailCard` lays out natively. `row` is a
    pandas Series (or None);
    `snapshot` is the tracker row dict used when the job is no longer in the
    loaded data. Returns {} when there is nothing to show.

    Keys: title, company, location, url, score, deep_score, recommendation,
    applicants, salary, posted, reason, strengths (list), gaps (list), jd (the
    FULL description as plain text, uncapped), snapshot_only (bool), note
    (snapshot-only hint)."""
    def cell(col: str) -> str:
        if row is None:
            return ""
        v = row.get(col, "")
        return "" if pd.isna(v) else str(v)

    if row is None:
        if snapshot:
            return {
                "title": str(snapshot.get("job_title") or "?"),
                "company": str(snapshot.get("company") or "?"),
                "location": "",
                "url": str(snapshot.get("url") or ""),
                "score": "", "deep_score": "", "recommendation": "",
                "applicants": "", "salary": "", "posted": "",
                "reason": "", "strengths": [], "gaps": [], "jd": "",
                "snapshot_only": True,
                "note": "No longer in the loaded data (tracker snapshot only).",
            }
        return {}

    posted = cell("job_posted_date").strip()
    # Richest JD text first, summary last — same precedence (and same 40-char
    # bar) as resume_tailor.run._job_description_text: LinkedIn's job_summary is
    # frequently a truncated stub, so preferring it hides most of the posting.
    jd = ""
    for col in ("job_description_formatted", "job_description", "job_summary"):
        text = html_to_text(cell(col))
        if len(text) >= 40:
            jd = text
            break
    return {
        "title": cell("job_title") or "?",
        "company": cell("company_name") or "?",
        "location": cell("job_location").strip(),
        "url": cell("url").strip(),
        "score": cell("score").strip(),
        "deep_score": cell("deep_score").strip(),
        "recommendation": cell("recommendation").strip(),
        "applicants": (cell("applicants") or cell("job_num_applicants")).strip(),
        "salary": cell("job_base_pay_range").strip(),
        "posted": posted[:10] if posted else "",
        "reason": cell("reason").strip(),
        "strengths": [s.strip() for s in cell("strengths").split("|") if s.strip()],
        "gaps": [g.strip() for g in cell("gaps").split("|") if g.strip()],
        "jd": jd,
        "snapshot_only": False,
        "note": "",
    }
