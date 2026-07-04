"""Toolkit-agnostic data + config logic for the dashboard.

Everything here is pure Python / pandas with no Tk or Qt dependency, so any UI can
build on it: loading and de-duplicating the scored run files, the per-table column
metadata, config.json access, the local company blocklist, and the high-score
filter. Extracted from the old `ui.py` so it survives the UI toolkit swap.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from jsonutil import atomic_write_json  # noqa: E402  (needs HERE on sys.path)
from csv_io import read_csv_gz, write_csv_gz_atomic  # noqa: E402
from locks import SingleInstance  # noqa: E402  (shared single-instance lock)
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
    "applicants": "Applicants", "is_seen": "Seen", "extracted_date": "Scraped",
    "run_label": "Run", "job_title": "Title", "company_name": "Company",
    "job_location": "Location", "url": "URL", "job_posted_date": "Posted",
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

HIGH_SCORE_COLUMNS = [
    ("score", 50),
    ("deep_score", 70),
    ("recommendation", 100),
    ("applicants", 80),
    ("extracted_date", 105),
    ("run_label", 80),
    ("job_title", 250),
    ("company_name", 170),
    ("job_location", 140),
    ("url", 240),
]

ALL_COLUMNS = [
    ("score", 50),
    ("deep_score", 70),
    ("recommendation", 100),
    ("is_seen", 60),
    ("extracted_date", 105),
    ("run_label", 80),
    ("job_title", 240),
    ("company_name", 170),
    ("job_location", 140),
    ("url", 220),
    ("job_posted_date", 120),
]

TRACKER_COLUMNS = [
    ("status", 90),
    ("status_date", 95),
    ("applied_date", 95),
    ("days", 46),
    ("follow_up", 80),
    ("score", 50),
    ("deep_score", 70),
    ("job_title", 240),
    ("company_name", 170),
    ("url", 220),
    ("resume", 60),
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


_ENGINE_LABELS = {"vertex": "Engine: Vertex Gemini", "api_key": "Engine: Gemini API key"}
_LABEL_TO_AUTH = {v: k for k, v in _ENGINE_LABELS.items()}


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


def load_files(paths: list[Path]) -> tuple[pd.DataFrame, dict[str, Path]]:
    """Load and concatenate CSVs; return (df, id_to_source_path)."""
    frames: list[pd.DataFrame] = []
    id_to_path: dict[str, Path] = {}
    for p in paths:
        if not p.exists():
            continue
        try:
            df = read_csv_gz(p)
        except (OSError, ValueError):
            continue
        if "job_posting_id" not in df.columns:
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
        combined = combined[~combined["job_posting_id"].astype(str).isin(removed)]
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


# Where manually-added jobs (dashboard "Add job by hand") are persisted, both as
# the canonical master row and as a dashboard-loadable scored gz so the job shows
# up immediately and survives a restart — the same bridge local_run_files() gives
# a local scrape, since there is no "manual" RUN_LABEL folder for it to land in.
MASTER_CSV = REPO_ROOT / "linkedin_jobs_master.csv"
MANUAL_DIR = REPO_ROOT / "manual"
MANUAL_SCORED = MANUAL_DIR / "manual_jobs_scored.csv.gz"


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


# Score/scrape columns that should land in the persisted manual row so it carries
# the same signal a scraped+scored row does. (job_posting_id is the dedup key.)
_MANUAL_PERSIST_COLS = [
    "url", "job_title", "company_name", "job_location", "job_summary",
    "job_posted_date", "run_label", "extracted_date", "source",
    "score", "reason", "deep_score", "strengths", "gaps", "recommendation",
    "filter_junk_title", "filter_junk_desc", "filter_too_many_years",
    "filter_clearance", "filter_degree", "filtered_out", "is_seen",
]


def _append_dedup_csv(record: dict, path: Path, *, compression=None) -> bool:
    """Append one job record to a CSV, deduping on job_posting_id (keep="first").

    Mirrors scraper.append_to_master: cast the id to str before deduping so a
    re-read int id never silently keeps a duplicate. Returns True when the record
    was newly added (False when an existing row with the same id already won).
    """
    jid = str(record.get("job_posting_id", "")).strip()
    if not jid:
        return False
    new_df = pd.DataFrame([record])
    new_df["job_posting_id"] = new_df["job_posting_id"].astype(str)
    if path.exists():
        try:
            existing = pd.read_csv(path, dtype={"job_posting_id": str},
                                   compression=compression)
        except (OSError, ValueError) as e:
            # NEVER treat an unreadable-but-existing store as empty -- overwriting
            # it would silently destroy the cumulative master.
            raise OSError(f"cannot append to {path.name}: existing file unreadable ({e})") from e
    else:
        existing = pd.DataFrame()
    already = (not existing.empty and "job_posting_id" in existing.columns
               and jid in set(existing["job_posting_id"].astype(str)))
    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    combined["job_posting_id"] = combined["job_posting_id"].astype(str)
    combined = combined.drop_duplicates(subset=["job_posting_id"], keep="first")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_csv_gz_atomic(combined, path, compression=compression)
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
    added = _append_dedup_csv(row, master, compression=None)
    # The gz copy never carries the raw JD (the scored run files don't either).
    gz_row = {k: v for k, v in row.items() if k != "job_description_formatted"}
    manual_gz = (master.parent / "manual" / "manual_jobs_scored.csv.gz")
    try:
        _append_dedup_csv(gz_row, manual_gz, compression="gzip")
    except OSError:
        pass  # the canonical master append is what matters; the gz is a convenience
    return added


def _drop_ids_from_csv(path: Path, ids: set[str]) -> None:
    """Rewrite a (optionally gz) CSV dropping rows whose job_posting_id is in `ids`.
    No-op when the file is missing, unreadable, idless, or unaffected."""
    if not path.exists() or not ids:
        return
    compression = "gzip" if path.suffix == ".gz" else None
    try:
        df = pd.read_csv(path, dtype={"job_posting_id": str}, compression=compression)
    except (OSError, ValueError):
        return
    if "job_posting_id" not in df.columns:
        return
    keep = df[~df["job_posting_id"].astype(str).isin(ids)]
    if len(keep) == len(df):
        return  # this file held none of the targets
    write_csv_gz_atomic(keep, path, compression=compression)


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

    Rewrites the local master + manual gz + local run files dropping these ids, then
    records them in config.json 'removed_jobs' so a Drive-only copy also disappears
    from the UI. Tracker status is cleared by the caller (the UI's seen registry).
    Returns the count of distinct ids targeted."""
    ids = {str(i).strip() for i in (ids or []) if str(i).strip()}
    if not ids:
        return 0
    master = Path(master_csv) if master_csv is not None else MASTER_CSV
    for p in [master, *local_run_files(master.parent)]:
        _drop_ids_from_csv(p, ids)
    _save_removed_jobs(load_removed_jobs() | ids)
    return len(ids)


def master_row(jid, *, master_csv: Path | None = None) -> dict | None:
    """The full master-CSV row for a job id (incl. job_description_formatted) or None.
    Used to prefill the edit dialog with the stored fields + JD."""
    master = Path(master_csv) if master_csv is not None else MASTER_CSV
    if not master.exists():
        return None
    try:
        df = pd.read_csv(master, dtype={"job_posting_id": str})
    except (OSError, ValueError):
        return None
    if "job_posting_id" not in df.columns:
        return None
    hit = df[df["job_posting_id"].astype(str) == str(jid)]
    if hit.empty:
        return None
    row = hit.iloc[0].to_dict()
    return {k: ("" if (isinstance(v, float) and pd.isna(v)) else v) for k, v in row.items()}


def update_manual_job(record: dict, *, old_id=None, master_csv: Path | None = None) -> bool:
    """Field-fix an existing manual job: drop the old row(s) everywhere, then
    re-append the edited record (since `_append_dedup_csv` keeps 'first'). Editing
    also un-removes a previously-deleted id. Does NOT re-score/re-tailor — those stay
    on the existing buttons. Returns append result (True when it lands fresh)."""
    master = Path(master_csv) if master_csv is not None else MASTER_CSV
    drop = {str(d).strip() for d in (old_id, record.get("job_posting_id")) if str(d or "").strip()}
    if drop:
        for p in [master, *local_run_files(master.parent)]:
            _drop_ids_from_csv(p, drop)
        rem = load_removed_jobs() - drop  # editing resurrects a previously-removed id
        if rem != load_removed_jobs():
            _save_removed_jobs(rem)
    return append_manual_job(record, master_csv=master_csv)


def _load_cfg() -> dict:
    """config.json (shared with the watcher), {} when unreadable."""
    try:
        return json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cfg(updates: dict) -> None:
    """Merge updates into local/config.json (best-effort; never crash the UI)."""
    cfg = _load_cfg()
    cfg.update(updates)
    try:
        atomic_write_json(HERE / "config.json", cfg)
    except OSError:
        pass


def _engine_credential_warnings(auth: str, project: str, has_api_key: bool) -> list[str]:
    """Warn when the chosen résumé-tailor engine is missing the credential it needs.

    'api_key' needs a Gemini API key; 'vertex' needs a Google Cloud project. Pure
    (no I/O) so it can be unit-tested; returns [] when the engine has what it needs.
    """
    if auth == "api_key" and not has_api_key:
        return ["Resume tailor engine is 'api_key' but no Gemini API key is saved "
                "(Settings -> Credentials -> Gemini API key (resume tailor))."]
    if auth == "vertex" and not str(project).strip():
        return ["Resume tailor engine is 'vertex' but no Google Cloud project is set "
                "(Settings -> Connection & paths -> Google Cloud project ID)."]
    return []


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


def visible_columns(all_cols: list[str], hidden) -> list[str]:
    """Display order with `hidden` column ids removed. Never empty — if every
    column is hidden it falls back to showing all (a blank table is never useful)."""
    hidden = set(hidden)
    vis = [c for c in all_cols if c not in hidden]
    return vis or list(all_cols)


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


def load_collapsed_sections() -> list[str]:
    """Settings sections the user has collapsed, persisted in config.json under
    'settings_collapsed'. Shape-checked so a stale/hand-edited config can't crash."""
    raw = _load_cfg().get("settings_collapsed", [])
    return [str(s) for s in raw] if isinstance(raw, list) else []


def save_collapsed_sections(sections) -> None:
    """Persist the collapsed Settings sections (best-effort; never crashes the UI)."""
    _save_cfg({"settings_collapsed": [str(s) for s in sections]})


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


def job_detail_segments(row, snapshot: dict | None = None) -> list[tuple[str, str]]:
    """The score-preview content as (text, style) segments — style in
    {'h','muted','good','bad',''}. `row` is a pandas Series (or None); `snapshot`
    is the tracker row dict shown when the job is no longer in the loaded data.
    Toolkit-agnostic so the Qt preview pane and tests share one source of truth."""
    def cell(col: str) -> str:
        if row is None:
            return ""
        v = row.get(col, "")
        return "" if pd.isna(v) else str(v)

    if row is None:
        if snapshot:
            return [
                (f"{snapshot.get('job_title') or '?'} — {snapshot.get('company') or '?'}\n", "h"),
                ("No longer in the loaded data (tracker snapshot only).\n", "muted"),
                (str(snapshot.get("url") or ""), "muted"),
            ]
        return []

    segs: list[tuple[str, str]] = []
    title = cell("job_title") or "?"
    company = cell("company_name") or "?"
    loc = cell("job_location")
    segs.append((f"{title} — {company}" + (f"  ({loc})" if loc else "") + "\n", "h"))

    meta: list[str] = []
    for label, col in (("score", "score"), ("deep", "deep_score"),
                       ("reco", "recommendation"), ("applicants", "applicants"),
                       ("posted", "job_posted_date"), ("salary", "job_base_pay_range")):
        v = cell(col).strip()
        if col == "job_posted_date" and v:
            v = v[:10]
        if v:
            meta.append(f"{label}: {v}")
    if meta:
        segs.append(("  ·  ".join(meta) + "\n\n", "muted"))

    reason = cell("reason").strip()
    if reason:
        segs.append(("Reason  ", "h"))
        segs.append((reason + "\n", ""))
    strengths = [s.strip() for s in cell("strengths").split("|") if s.strip()]
    if strengths:
        segs.append(("Strengths\n", "h"))
        segs.extend((f"  + {s}\n", "good") for s in strengths)
    gaps = [g.strip() for g in cell("gaps").split("|") if g.strip()]
    if gaps:
        segs.append(("Gaps\n", "h"))
        segs.extend((f"  - {g}\n", "bad") for g in gaps)

    jd = cell("job_summary").strip()
    if len(jd) < 40:
        raw = cell("job_description_formatted")
        jd = re.sub(r"<[^>]+>", " ", raw)
        jd = re.sub(r"\s+", " ", jd).strip()
    if jd:
        segs.append(("\nJD snippet  ", "h"))
        segs.append((jd[:700] + ("…" if len(jd) > 700 else ""), "muted"))
    return segs
