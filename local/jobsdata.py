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
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from jsonutil import atomic_write_json  # noqa: E402  (needs HERE on sys.path)
from csv_io import read_csv_gz  # noqa: E402
from vm_schedule import RUN_LABELS  # noqa: E402  (shared run-label set)

APPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "linkedin_watcher"
APPDATA.mkdir(parents=True, exist_ok=True)
RELOAD_FLAG = APPDATA / "reload.flag"
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


class _UILock:
    """Single-instance guard for the dashboard window. Same pattern as watcher.py."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self._fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
            self._fh = None


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


def add_extracted_date(df: pd.DataFrame, id_date: dict[str, str]) -> pd.DataFrame:
    """Ensure an 'extracted_date' column (the day a job was scraped).

    Priority, highest first:
      1. a value already stored in the master (scraper.py now writes one),
      2. the date parsed from the per-run filename it first appeared in,
      3. the date portion of job_posted_date (scrape filter is 'Past 24 hours',
         so the posting date is within ~a day of extraction),
      4. blank.
    """
    if df.empty:
        return df
    if "job_posted_date" in df.columns:
        posted = pd.to_datetime(df["job_posted_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    else:
        posted = pd.Series([None] * len(df), index=df.index)
    from_runs = df["job_posting_id"].astype(str).map(id_date)
    if "extracted_date" in df.columns:
        stored = df["extracted_date"].astype(str).str.strip()
        stored = stored.mask(stored.isin(["", "nan", "NaN", "NaT", "None"]))
    else:
        stored = pd.Series([None] * len(df), index=df.index)
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
    combined = add_extracted_date(combined, extraction_dates_from_runs(paths))
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
    is_seen = df["is_seen"].astype(str) if "is_seen" in df.columns else pd.Series(["no"] * len(df))
    mask = (score >= min_score) & (is_seen == "no")
    out = df.loc[mask].copy()
    out["__score_num"] = score[mask]
    out["__deep_num"] = pd.to_numeric(out.get("deep_score", 0), errors="coerce").fillna(0)
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
                    time_: str, reco: str, easy: bool = False,
                    search_column: str | None = None) -> pd.DataFrame:
    """Apply the shared multi-column filters (AND) + default sort to a base set.
    search_column: a column id to restrict the text search to; None/"All" = all."""
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
    if easy and "is_easy_apply" in view.columns:
        view = view.loc[view["is_easy_apply"].astype(str).str.lower().isin(("true", "1", "yes"))]
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
