import asyncio
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import aiohttp
import pandas as pd

from run_labels import RUN_LABELS, label_for_hour

# Data root: the directory holding .env, the master CSV and the per-run-label
# output dirs. In the repo this script lives in pipeline/ and the data root is
# the repo root one level up; on the VM the pipeline scripts are scp'd FLAT
# into ~/ and the data root is that same directory, so the parent hop only
# applies while the script is still inside pipeline/.
_HERE = Path(__file__).resolve().parent
DATA_ROOT = _HERE.parent if _HERE.name == "pipeline" else _HERE

# Optional: load a local .env so credentials live outside the repo. The VM path
# sets these via run_scraper.sh exports, so a missing python-dotenv is fine.
#
# INPLOYED_NO_DOTENV=1 skips the file. This script bills Bright Data on a
# successful run, and without the opt-out there is no way to exercise its
# missing-credential path: clearing BRIGHT_DATA_API_TOKEN in the environment
# does nothing, because this runs at import and puts the real token back before
# require_credentials() ever looks.
#
# Case-folded and generous about spelling: this is typed by hand at a shell, and
# an INPLOYED_NO_DOTENV=TRUE that silently re-arms a billed script is the worst
# possible way to be strict. Note it is read at IMPORT, so setting it inside a
# process that already imported this module (the dashboard, which loads .env at
# startup -- local/settings.py:108) does nothing.
if os.environ.get("INPLOYED_NO_DOTENV", "").strip().lower() not in ("1", "true", "yes", "on"):
    try:
        from dotenv import load_dotenv

        load_dotenv(DATA_ROOT / ".env")
    except ImportError:
        pass

# Bright Data credentials — supplied via environment (.env locally, exported on
# the VM). Never hardcode the token in the repo; see .env.example. The presence
# check is deferred to run time (require_credentials) so importing this module
# never aborts a credential-less process (e.g. the test suite or reuse).
API_TOKEN = os.environ.get("BRIGHT_DATA_API_TOKEN", "")
DATASET_ID = os.environ.get("BRIGHT_DATA_DATASET_ID", "")


def require_credentials() -> None:
    """Exit with a helpful message if Bright Data credentials are missing.
    Called at the start of a run, not at import time."""
    if not API_TOKEN or not DATASET_ID:
        sys.exit(
            "Missing Bright Data credentials. Set BRIGHT_DATA_API_TOKEN and "
            "BRIGHT_DATA_DATASET_ID in your environment or a local .env file "
            "(see .env.example)."
        )
CHUNK = 2000  # Chunked streaming row count for append_to_master (memory bounded)

LIMIT_PER_INPUT = 100
POLL_INTERVAL = 10
MAX_WAIT_MINUTES = 60
MAX_POLL_FAILURES = 5

OUTPUT_DIR = DATA_ROOT
PREVIOUS_IDS_FILE = OUTPUT_DIR / "last_run_job_ids.json"
MASTER_CSV = OUTPUT_DIR / "linkedin_jobs_master.csv"
# Job ids collected on ANOTHER machine and pushed here (e.g. a manual local scrape
# syncs its ids up to the VM) so a scheduled run never re-collects — and re-bills —
# what was just pulled. Unioned into load_exclude_ids() on top of this host's master.
EXTERNAL_EXCLUDE_FILE = OUTPUT_DIR / "external_exclude_ids.json"

# An additional master CSV (path in this env var; .csv or .csv.gz) to exclude from, on
# top of this host's own master. The local dashboard sets it to the synced Drive master
# so a LOCAL "Find new jobs" run also skips — and never re-bills — jobs the VM already
# collected: the local repo master is only a small stub of recent local runs, while the
# Drive master is the VM's full cumulative record. Unset on the VM (whose own master IS
# the full set), so the VM's exclusion is unchanged.
EXTRA_MASTER_ENV = "LINKEDIN_EXTRA_MASTER"

# Cap the exclude-id set to a recency window. The search filter is time_range="Past
# 24 hours", so a posting scraped more than ~60 days ago can never reappear in a
# fresh run -- keeping its id in jobs_to_not_include is pure payload. The full
# cumulative set is embedded once per (keyword x remote) input (~40 inputs), so a
# months-long history makes the Bright Data trigger POST grow monotonically until it
# hard-fails the request-size limit and silently kills the cron before collection.
# Ids whose extracted_date is older than this many days are dropped from the exclude
# set. Overridable via the EXCLUDE_WINDOW_DAYS env var. See .env.example.
EXCLUDE_WINDOW_DAYS_ENV = "EXCLUDE_WINDOW_DAYS"
DEFAULT_EXCLUDE_WINDOW_DAYS = 90

# Hard bound on the exclusion array, and the backstop the window above is NOT.
#
# Bright Data expands ONE search input into up to `limit_per_input` CHILD inputs,
# and every child carries a verbatim copy of the parent's fields -- including
# jobs_to_not_include. The exclusion array is therefore paid for once per CHILD,
# not once per search, so its cost scales as len(ids) x limit_per_input. Past a
# threshold Bright Data rejects every child with `child_input_size_validation`:
# the collection completes "ready" with 0 records, so the run reports "No new jobs
# returned this run" and exits 0. Nothing raises. The pipeline looks healthy and
# silently collects nothing, which is how this went unnoticed for weeks.
#
# Bright Data does not publish the threshold, so it was measured directly against
# the LinkedIn jobs dataset with one search at limit_per_input=150:
#   2,000 ids -> 4,239,000 bytes of children -> ACCEPTED (collected 128 jobs)
#   2,679 ids -> 5,262,000 bytes of children -> REJECTED, 100% of inputs
# 5 MiB (5,242,880) falls between those two, which is almost certainly the real cap.
# The budget below sits on the measured-good side, so 2,000 ids fit at
# limit_per_input=150 and proportionally fewer fit as the fan-out grows.
#
# Windowing alone cannot hold this: at ~500 postings/day even a 14-day window is
# ~7,000 ids. The cap is what actually bounds the request -- the search is
# time_range="Past 24 hours", so only a recently scraped posting can resurface and
# older ids are pure payload. 2,000 covers roughly the last two runs, which is the
# overlap a twice-daily schedule can actually re-collect. Re-collecting a few
# duplicates costs a little; a rejected collection costs the entire run and still
# burns the trigger.
MAX_EXCLUDE_PAYLOAD_BYTES = 4_200_000   # = 2,000 ids x 14 bytes x 150 children
# The width that budget was derived from: '"4444097977", ', the shape a 10-digit
# posting id takes once aiohttp's json.dumps has written it. cap_exclude_ids no
# longer USES this -- it measures the ids actually in hand, so an 11-digit id can
# never quietly overflow the budget -- but the number is what MAX_EXCLUDE_PAYLOAD
# _BYTES above was computed from, so it stays as the record of that arithmetic.
BYTES_PER_EXCLUDE_ID = 14
MIN_EXCLUDE_IDS = 50           # never exclude nothing: that re-bills every posting
MAX_EXCLUDE_IDS = 2000         # the target: ~2 runs' worth, all that can recur

# Bright Data error codes that mean "we refused your input", as opposed to the
# per-page codes (dead_page, page_too_big) that show up on healthy runs. Any of
# these means part or all of the search never ran, so the collection is a failure
# even when some rows came back. See _assert_collected_something.
INPUT_REJECTION_CODES = frozenset({
    "child_input_size_validation",
    "input_size_validation",
    "invalid_input",
})

# Spammy aggregator companies to drop entirely — from every fresh run AND from
# the cumulative master (case-insensitive substring match on company_name).
# Add more names here as needed. The dashboard's right-click "Block company"
# appends to company_blocklist.txt (synced down from Drive by run_scraper.sh);
# both sources are merged by load_blocklist().
COMPANY_BLOCKLIST = ("jobright",)
BLOCKLIST_FILE = OUTPUT_DIR / "company_blocklist.txt"


def load_blocklist() -> tuple[str, ...]:
    """Built-in names plus one company per line from company_blocklist.txt.
    Blank lines and #-comments are ignored; duplicates are dropped."""
    merged = list(COMPANY_BLOCKLIST)
    have = {b.lower() for b in merged}
    if BLOCKLIST_FILE.exists():
        try:
            for line in BLOCKLIST_FILE.read_text(encoding="utf-8-sig").splitlines():
                name = line.strip()
                if name and not name.startswith("#") and name.lower() not in have:
                    merged.append(name)
                    have.add(name.lower())
        except OSError as e:
            print(f"Could not read {BLOCKLIST_FILE.name} ({e}); using built-ins only")
    return tuple(merged)

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
}

# Free, unbilled token probe. NOTE: /status reports PROXY capability, so an account
# that only uses the Web Scraper (datasets/v3) API sits at can_make_requests=false /
# zone_not_found forever, even while collections run perfectly. Never gate a run on
# those fields — this account has scraped for months with zero zones. Only a 401/403
# from /status means anything here, and it means the token itself is dead.
STATUS_URL = "https://api.brightdata.com/status"
PREFLIGHT_TIMEOUT = 15

# Bright Data answers the billed scrape POST with a bare "401: Invalid credentials"
# for a token that authenticates fine but lacks permission to START a collection.
# Read calls (dataset catalog, snapshot list) keep returning 200, so the token looks
# healthy while every run dies. Replacing a key without matching the old one's
# permissions lands here, and the raw message sends you hunting for a typo instead.
# Pure ASCII on purpose: this text is printed to the Windows console and tailed into
# the dashboard's crash dialog, where a non-ASCII dash comes out as a replacement
# character. A message whose only job is to be readable at 2am must not be mojibake.
TOKEN_HINT = (
    "A Bright Data token that can READ (catalog, snapshots) can still be refused "
    "permission to START a billed collection, and the API reports that as 'Invalid "
    "credentials'. If you recently replaced your API key, issue one with full "
    "permissions -- not read-only -- at https://brightdata.com/cp/setting/users, "
    "then put it in .env as BRIGHT_DATA_API_TOKEN."
)

KEYWORDS = [
    '"Data Scientist"',
    '"AI Engineer"',
    '"AI Developer"',
    '"AI Scientist"',
    '"Software Engineer"',
    '"Software Developer"',
    '"Data Analyst"',
    '"Data Engineer"',
    '"LLM"',
    '"Analytics Engineer"',
    '"Decision Scientist"',
    '"Generative AI"',
    '"Gen AI"',
    '"GenAI"',
    '"Quant"',
    '"Implementation Engineer"',
    '"Agentic"',
    '"Applied AI"',
    '"Artificial Intelligence"',
    '"Business Analyst"',
]

REMOTE_TYPES = ["Hybrid", "On-site"]

BASE_FILTERS = {
    "location": "United States",
    "country": "US",
    "time_range": "Past 24 hours",
    "job_type": "Full-time",
    "experience_level": "Entry level",
    "selective_search": True,
}

# Root-level search_config.json lets a local user (or the dashboard's Settings
# tab) override the search inputs without editing this file. The VM runs with NO
# such file, so the loader MUST fall back to the constants above byte-for-byte.
SEARCH_CONFIG_FILE = "search_config.json"


def _positive_int(value, default: int) -> int:
    """Coerce a config value to a positive int, falling back to `default`.

    limit_per_input is interpolated into the trigger URL of a pay-per-collection
    API. Uncoerced, a hand-edited or corrupted search_config.json holding
    "100&limit=5000" rewrites the request that gets billed. Everything the
    scraper spends is bounded by this number, so it is coerced at the boundary
    rather than trusted at the call site.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 1 else default


def load_search_config() -> dict:
    """Effective search config: file values where present, built-in constants else.

    Reads OUTPUT_DIR / search_config.json (or {} when absent/unreadable) and
    returns every externalized key, each falling back to today's module constant
    so the VM's behavior is unchanged with no config file.
    """
    path = OUTPUT_DIR / SEARCH_CONFIG_FILE
    raw: dict = {}
    if path.exists():
        try:
# utf-8-sig, not utf-8: json.loads rejects a leading BOM outright, and the
        # handler below then discards the WHOLE file and falls back to built-ins
        # with only a line in scraper.log to show for it. Notepad writes a BOM,
        # PowerShell 5.1's Set-Content -Encoding UTF8 writes a BOM, and this is a
        # file users hand-edit and the dashboard pushes here. local/jsonutil.py's
        # read_json_dict already reads the same file BOM-tolerantly, so without
        # this the two halves disagree about one file: the dashboard honours it,
        # the VM silently ignores it. utf-8-sig is a superset -- it strips a BOM
        # when there is one and decodes plain UTF-8 identically when there isn't.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                raw = data
        except (OSError, ValueError) as e:
            print(f"Could not read {SEARCH_CONFIG_FILE} ({e}); using built-in defaults")
    return {
        "keywords": raw.get("keywords", KEYWORDS),
        "remote_types": raw.get("remote_types", REMOTE_TYPES),
        "limit_per_input": _positive_int(raw.get("limit_per_input"), LIMIT_PER_INPUT),
        "location": raw.get("location", BASE_FILTERS["location"]),
        "country": raw.get("country", BASE_FILTERS["country"]),
        "time_range": raw.get("time_range", BASE_FILTERS["time_range"]),
        "job_type": raw.get("job_type", BASE_FILTERS["job_type"]),
        "experience_level": raw.get("experience_level", BASE_FILTERS["experience_level"]),
        "exclude_window_days": _positive_int(raw.get("exclude_window_days"),
                                             DEFAULT_EXCLUDE_WINDOW_DAYS),
    }


def get_run_label() -> str:
    return label_for_hour(datetime.now().hour)


def load_previous_ids() -> list[str]:
    if not PREVIOUS_IDS_FILE.exists():
        return []
    try:
        with open(PREVIOUS_IDS_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"Could not read {PREVIOUS_IDS_FILE.name} ({e}); ignoring last-run ids")
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def exclude_window_days() -> int:
    """Recency window (in days) for pruning the exclude-id set.

    Resolution order is env > search_config.json > DEFAULT_EXCLUDE_WINDOW_DAYS,
    matching every other override in this module. The file leg was missing until
    2026-08-26: the dashboard's Settings tab has always written
    `exclude_window_days` into search_config.json, but nothing read it back, so a
    user who set 14 still got the 90-day default and shipped their whole master in
    every trigger POST.

    A missing, non-integer, or non-positive value falls THROUGH to the next leg,
    rather than skipping to the built-in default: an env var someone fat-fingered
    must not silently override a window the user set in the dashboard. Whatever
    the file holds, _positive_int() collapses junk/0/negative there too, so the
    walk always terminates on DEFAULT_EXCLUDE_WINDOW_DAYS. 0 or a negative window
    would empty the exclude set and re-collect (and re-bill) every posting -- the
    exact failure this guard prevents -- so we always fail toward the safe default
    rather than an empty window.

    Note the cap dominates the window in practice: MAX_EXCLUDE_IDS trims whatever
    this returns down to ~2,000 ids, so widening the window past a few days changes
    nothing. See cap_exclude_ids."""
    raw = os.environ.get(EXCLUDE_WINDOW_DAYS_ENV, "").strip()
    if not raw:
        # _positive_int() already collapses junk/0/negative to the default.
        return load_search_config()["exclude_window_days"]
    try:
        val = int(raw)
    except ValueError:
        print(f"Invalid {EXCLUDE_WINDOW_DAYS_ENV}={raw!r}; falling back to "
              f"search_config.json")
        return load_search_config()["exclude_window_days"]
    if val <= 0:
        print(f"{EXCLUDE_WINDOW_DAYS_ENV}={val} is not positive; falling back to "
              f"search_config.json")
        return load_search_config()["exclude_window_days"]
    return val


def _window_ids(df: pd.DataFrame, window_days: int) -> list[str]:
    """Unique job_posting_ids in `df`, keeping only those scraped within the last
    `window_days` days (per the extracted_date column), OLDEST FIRST.

    The order is part of the contract: cap_exclude_ids() evicts from the front, so
    this has to be sorted by date, not left in master row order.

    Fail toward a SUPERSET -- an exclude set that is too SMALL re-bills already-
    collected jobs, so when recency can't be determined we KEEP the id:
      - No extracted_date column, or EVERY date unparseable -> keep ALL ids (degrade
        to the pre-window keep-all behavior; never silently empty the set).
      - A single row with a missing/unparseable extracted_date (NaT) -> KEEP that id
        (an undated posting is treated as recent).
    """
    if "job_posting_id" not in df.columns or df.empty:
        return []
    if "extracted_date" not in df.columns:
        return df["job_posting_id"].dropna().astype(str).unique().tolist()
    # utc=True, and a UTC cutoff to match. The master this host writes is plain
    # YYYY-MM-DD, but merge_incoming folds rows from other machines and a Drive
    # master can carry an ISO offset. Without utc=True pandas 3 returns a
    # tz-AWARE column for those, and comparing it to a tz-naive Timestamp.now()
    # raises "Invalid comparison between dtype=datetime64[us, UTC] and Timestamp"
    # -- or, for a column mixing offsets, "Mixed timezones detected". Normalising
    # both sides costs nothing on naive input and removes the whole class.
    dates = pd.to_datetime(df["extracted_date"], errors="coerce", utc=True)
    if dates.isna().all():
        # No parseable dates at all -> can't window; keep everything.
        return df["job_posting_id"].dropna().astype(str).unique().tolist()
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=window_days)
    keep = dates.isna() | (dates >= cutoff)  # NaT (undated) rows are kept
    kept = df.loc[keep, ["job_posting_id"]].copy()
    kept["_extracted"] = dates[keep]
    # Collapse a repeated id to its NEWEST date before sorting. unique() below keeps
    # first-appearance order, so without this a job that appears twice (an original
    # row plus a re-collected one, which is exactly what a merge_incoming fold or a
    # hand-added row produces) would sort at the position of its OLDEST copy and be
    # evicted early -- the opposite of what the sort is for.
    # sort=False keeps first-appearance (master row) order, so ids sharing a date
    # still come back in the order the master holds them.
    kept = kept.groupby("job_posting_id", as_index=False, sort=False)["_extracted"].max()
    # Oldest date FIRST, so cap_exclude_ids' tail slice evicts by date rather than by
    # master row order. Those usually agree, but a merge_incoming fold on the VM (or a
    # hand-added row) can land an older posting after a newer one, and evicting on row
    # order would then drop a recent id and keep a stale one -- backwards for a
    # "Past 24 hours" search. Undated rows sort first, so they are evicted first: the
    # window keeps them so the set never silently shrinks, but a date we cannot prove
    # must not outrank one we can.
    kept = kept.sort_values("_extracted", na_position="first", kind="stable")
    return kept["job_posting_id"].dropna().astype(str).unique().tolist()


def _master_ids() -> list[str]:
    """Every job id recorded in this host's master, pruned to the recency window
    (exclude_window_days) -- or the last-run JSON if the master is missing/unreadable."""
    if MASTER_CSV.exists():
        try:
            df = pd.read_csv(
                MASTER_CSV,
                usecols=lambda c: c in ("job_posting_id", "extracted_date"),
                dtype=str,
            )
        except (OSError, ValueError, pd.errors.ParserError) as e:
            print(f"Could not read master for exclusions ({e}); using last-run ids")
            return load_previous_ids()
        if "job_posting_id" in df.columns and not df.empty:
            try:
                return _window_ids(df, exclude_window_days())
            except (TypeError, ValueError, pd.errors.ParserError) as e:
                # Windowing is an optimization on top of the exclude set; if it ever
                # fails (e.g. an unexpected extracted_date dtype), degrade to keeping
                # ALL master ids -- a superset never re-bills -- rather than dying.
                print(f"Could not window exclude ids ({e}); keeping all master ids")
                return df["job_posting_id"].dropna().astype(str).unique().tolist()
        if "job_posting_id" not in df.columns:
            # Parsed, but not a master: a truncated write, or the wrong CSV copied
            # in. Say so. Falling through silently means an empty exclude set,
            # which re-collects and re-bills every posting already held -- and on
            # the VM the only trace would be the ABSENCE of a line in scraper.log.
            # An empty-but-valid master is a different thing and stays quiet.
            print(f"WARNING: {MASTER_CSV.name} has no job_posting_id column "
                  f"(found: {list(df.columns) or 'no columns'}); using last-run ids")
    return load_previous_ids()


def load_external_exclude_ids() -> list[str]:
    """Ids pushed from another machine (EXTERNAL_EXCLUDE_FILE), [] if absent/unreadable."""
    if not EXTERNAL_EXCLUDE_FILE.exists():
        return []
    try:
        with open(EXTERNAL_EXCLUDE_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return [str(x) for x in data] if isinstance(data, list) else []
    except (OSError, ValueError) as e:
        print(f"Could not read {EXTERNAL_EXCLUDE_FILE.name} ({e}); ignoring external excludes")
        return []


def load_extra_master_ids() -> list[str]:
    """Job ids from the additional master named by $LINKEDIN_EXTRA_MASTER (the synced
    Drive master, for a local run). [] when the var is unset, the file is missing, or it
    can't be read. Accepts a plain or gzipped CSV. Best-effort: a bad/absent extra master
    never crashes a run — it just means weaker exclusion, not a fabricated one."""
    raw = os.environ.get(EXTRA_MASTER_ENV, "").strip()
    if not raw:
        return []
    path = Path(raw)
    if not path.exists():
        return []
    try:
        comp = "gzip" if path.suffix == ".gz" else "infer"
        df = pd.read_csv(path, usecols=lambda c: c in ("job_posting_id", "extracted_date"),
                         dtype=str, compression=comp)
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"Could not read extra master {path.name} for exclusions ({e}); ignoring")
        return []
    if "job_posting_id" not in df.columns or df.empty:
        return []
    try:
        return _window_ids(df, exclude_window_days())
    except (TypeError, ValueError, pd.errors.ParserError) as e:
        # Same degrade-to-superset path _master_ids has. Without it an unexpected
        # extracted_date dtype in the SYNCED master (which this host does not
        # write, so its shape is not ours to guarantee) took down the whole local
        # run with a raw traceback, while the identical column in our own master
        # degraded quietly one function up.
        print(f"Could not window extra master {path.name} ({e}); keeping all its ids")
        return df["job_posting_id"].dropna().astype(str).unique().tolist()


def load_exclude_ids() -> list[str]:
    """Every job id this host knows to skip — a hard no-repeat guard. Bright Data
    bills per collection, so re-fetching a posting we already have is pure wasted
    spend. We exclude this host's master (pruned to the last exclude_window_days by
    extracted_date — the search is 'Past 24 hours', so an older posting can't reappear
    and its id is only trigger-POST bloat), UNIONED with the extra master named by
    $LINKEDIN_EXTRA_MASTER (the synced Drive master, likewise windowed, so a LOCAL run
    skips what the VM already collected) and with external_exclude_ids.json (ids
    collected on another machine — e.g. a manual local run — and pushed here so a
    scheduled VM run skips them; these are freshly-collected this-session ids and are
    NOT windowed). Falls back to the last-run JSON when this host's master is
    missing/unreadable.

    ORDER IS A CONTRACT, not an accident: cap_exclude_ids() keeps the TAIL, so
    this host's own ids go LAST, newest last. They were appended first until
    2026-08-27, which put the other machines' ids in the tail and evicted our own
    -- and on the VM, where the pushed file routinely carries more than
    MAX_EXCLUDE_IDS entries, the cap then kept 100% foreign ids and 0% of the VM's
    own master, so every run re-collected what it had collected the day before.
    Ours are the ids that can actually resurface in a "Past 24 hours" search, so
    ours are the ones that must survive the cap."""
    own = _master_ids()                       # dated, oldest-first (see _window_ids)
    ids: list[str] = []
    seen = set(own)
    for jid in load_extra_master_ids() + load_external_exclude_ids():
        if jid not in seen:
            ids.append(jid)
            seen.add(jid)
    ids.extend(own)
    return ids


def _atomic_write_json(path: Path, data) -> None:
    """Same-dir tempfile + os.replace for a JSON write (mirrors _atomic_to_csv).
    A crash mid-write leaves either the old file or the new one, never a
    truncated partial write."""
    fd, tmp = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def save_current_ids(ids: list[str]) -> None:
    _atomic_write_json(PREVIOUS_IDS_FILE, ids)


def write_external_exclude_ids(path: Path | None = None) -> Path:
    """Dump this host's full known exclude set (load_exclude_ids) to a JSON file for
    pushing to the VM, whose scraper unions it into its own load_exclude_ids(). Pushing
    the whole set each time is idempotent + monotonic, so a prior run's ids can't slip
    through. Returns the written path."""
    target = path or EXTERNAL_EXCLUDE_FILE
    _atomic_write_json(target, load_exclude_ids())
    return target


DROP_PREFIXES = ("discovery_input.", "input.", "base_salary", "job_poster")
DROP_EXACT = {
    "company_logo",
    "salary_standards",
    "application_availability",
    "timestamp",
    "country_code",
}


def drop_unneeded_columns(df: pd.DataFrame) -> pd.DataFrame:
    cols_to_drop = [
        c for c in df.columns
        if c in DROP_EXACT or any(c.startswith(p) for p in DROP_PREFIXES)
    ]
    return df.drop(columns=cols_to_drop)


def drop_blocklisted_companies(df: pd.DataFrame, blocklist=None) -> pd.DataFrame:
    """Remove rows whose company name matches the blocklist (substring, case-insensitive).

    `blocklist` lets a caller pass a pre-loaded list so the disk read is done
    once, not per chunk in the master-rewrite loop; when None we load it here.
    """
    col = next((c for c in ("company_name", "company") if c in df.columns), None)
    if blocklist is None:
        blocklist = load_blocklist()
    if not col or not blocklist:
        return df
    names = df[col].fillna("").astype(str).str.lower()
    mask = pd.Series(False, index=df.index)
    for bad in blocklist:
        mask = mask | names.str.contains(bad.lower(), na=False, regex=False)
    return df[~mask]


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


def append_to_master(df: pd.DataFrame) -> int:
    # Load the blocklist once per master rewrite and pass it down, so the
    # per-2000-row chunk loop below does not re-read company_blocklist.txt from
    # disk on every chunk.
    blocklist = load_blocklist()
    new = df.copy()
    if "job_posting_id" in new.columns:
        new["job_posting_id"] = new["job_posting_id"].astype(str)
        new = new.drop_duplicates(subset=["job_posting_id"], keep="first")
    new = drop_blocklisted_companies(new, blocklist)

    if not MASTER_CSV.exists():
        _atomic_to_csv(new, MASTER_CSV)
        return len(new)

    # Validate readability up front so a corrupt-but-present master still raises
    # loudly, never silently treated as empty. The
    # probe's full parse also gives us existing_ids for free, before the tempfile
    # stream starts and before blocklist filtering (so a new row colliding with an
    # existing-but-about-to-be-blocklisted row is still correctly excluded below).
    try:
        header = pd.read_csv(MASTER_CSV, nrows=0).columns.tolist()
        probe = pd.read_csv(MASTER_CSV, usecols=lambda c: c == "job_posting_id",
                            dtype=str)
    except (OSError, ValueError, pd.errors.ParserError) as e:
        raise OSError(
            f"cannot update {MASTER_CSV.name}: existing master is unreadable ({e}). "
            f"This run's rows are still saved to the run-dir CSV; fix or restore "
            f"{MASTER_CSV} and rerun with --snapshot to recover them."
        ) from e
    existing_ids: set[str] = set(probe["job_posting_id"].astype(str)) if "job_posting_id" in probe.columns else set()
    del probe  # release memory before streaming

    unified = header + [c for c in new.columns if c not in header]  # column union, order preserved
    fd, tmp = tempfile.mkstemp(prefix=MASTER_CSV.stem + ".", suffix=".tmp",
                               dir=str(MASTER_CSV.parent))
    os.close(fd)
    total = 0
    wrote_header = False
    try:
        # dtype=str + keep_default_na=False (audit P2-26): byte-stable master
        # round-trip — matches prune_master.py's reader.
        for chunk in pd.read_csv(MASTER_CSV, dtype=str, keep_default_na=False,
                                 chunksize=CHUNK):
            chunk = drop_blocklisted_companies(chunk, blocklist)  # retroactive re-filter, preserved
            chunk = chunk.reindex(columns=unified)
            chunk.to_csv(tmp, mode="a", header=not wrote_header, index=False, encoding="utf-8")
            wrote_header = True
            total += len(chunk)
        if "job_posting_id" in new.columns:
            truly_new = new[~new["job_posting_id"].isin(existing_ids)]  # keep="first": existing wins
        else:
            truly_new = new
        truly_new = truly_new.reindex(columns=unified)
        truly_new.to_csv(tmp, mode="a", header=not wrote_header, index=False, encoding="utf-8")
        total += len(truly_new)
        os.replace(tmp, MASTER_CSV)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return total


def cap_exclude_ids(exclude_ids: list[str], limit_per_input: int) -> list[str]:
    """Trim `exclude_ids` to what Bright Data will actually accept in one input.

    Each search input fans out to up to `limit_per_input` child inputs that each
    carry a copy of this array, so the budget is spent len(ids) x limit_per_input
    times over (see MAX_EXCLUDE_PAYLOAD_BYTES). Over budget, Bright Data rejects
    every child with `child_input_size_validation` and the collection returns zero
    rows without raising anything.

    Keeps the TAIL, which load_exclude_ids() guarantees is this host's own master,
    oldest first (see its docstring and _window_ids). With a
    time_range="Past 24 hours" search those are the only ids that can resurface,
    so trimming the head instead would keep precisely the ids that cannot recur.

    The per-id width is MEASURED off the ids in hand, not assumed. It used to be
    the BYTES_PER_EXCLUDE_ID constant, which bakes in today's 10-digit LinkedIn
    posting id; ids are at 4.4e9 now, and the first 11-digit id would silently
    push the real payload past the cap with nothing noticing.
    """
    limit = _positive_int(limit_per_input, LIMIT_PER_INPUT)
    if not exclude_ids:
        return exclude_ids
    per_id = max(1, len(json.dumps(exclude_ids)) // len(exclude_ids))
    budget = MAX_EXCLUDE_PAYLOAD_BYTES // (per_id * limit)
    keep = min(budget, MAX_EXCLUDE_IDS)
    if keep < MIN_EXCLUDE_IDS:
        # The budget, not the floor, wins here. An exclude set below the floor
        # re-bills postings we already hold, which is bad; a payload over the cap
        # gets the collection REJECTED, which costs the whole run and still burns
        # the trigger. The floor is worth having, but not at the price of the
        # failure the cap exists to prevent -- so say plainly what to change.
        print(f"  WARNING: limit_per_input={limit} leaves room for only {keep} "
              f"exclude ids, under the {MIN_EXCLUDE_IDS}-id floor. Lower "
              f"limit_per_input in search_config.json; this run will re-collect "
              f"postings it already has.")
        keep = max(keep, 1)
    if len(exclude_ids) <= keep:
        return exclude_ids
    print(f"  exclude set capped: {len(exclude_ids)} -> {keep} most recent ids "
          f"(limit_per_input={limit}; Bright Data rejects oversized inputs)")
    return exclude_ids[-keep:]


def build_inputs(exclude_ids: list[str], max_keywords: int | None = None,
                 limit_per_input: int | None = None) -> list[dict]:
    """One search input per (keyword x remote type).

    Keywords, remote types, and the base filters come from load_search_config()
    (which falls back to the module constants when no search_config.json exists).
    `max_keywords` caps how many keywords are used (the first N) — a spend guard
    for verification runs so a single scrape can't fan out to every keyword.
    None (the default, used by the VM cron) keeps the full keyword list.

    `limit_per_input` is the same value that goes into the trigger URL; it is taken
    here only to size the jobs_to_not_include cap, which scales with the fan-out.
    None falls back to the config value, so the cap applies even to a caller that
    doesn't thread it through.
    """
    cfg = load_search_config()
    keywords = cfg["keywords"] if max_keywords is None else cfg["keywords"][:max_keywords]
    remote_types = cfg["remote_types"]
    if limit_per_input is None:
        limit_per_input = cfg["limit_per_input"]
    exclude_ids = cap_exclude_ids(exclude_ids, limit_per_input)
    base_filters = {
        "location": cfg["location"],
        "country": cfg["country"],
        "time_range": cfg["time_range"],
        "job_type": cfg["job_type"],
        "experience_level": cfg["experience_level"],
        "selective_search": BASE_FILTERS["selective_search"],
    }
    return [
        {**base_filters, "keyword": kw, "remote": remote, "jobs_to_not_include": exclude_ids}
        for kw in keywords
        for remote in remote_types
    ]


async def preflight(session: aiohttp.ClientSession) -> None:
    """Refuse to spend when the Bright Data account can't run a collection.

    One free GET to /status ahead of the billed trigger POST it guards, so a dead
    account fails in a second with a message naming the cause instead of a false
    "Invalid credentials" charged against a valid token.

    FAIL OPEN: only an outright 401/403 (the token is dead or revoked) aborts the
    run. A timeout, a 5xx or anything else prints a warning and continues. This
    script runs unattended on the VM cron, and a probe that is itself unavailable
    must never become the reason a working scrape doesn't happen.

    Deliberately does NOT read `can_make_requests` / `auth_fail_reason`: those
    describe PROXY zones, and this account has run collections for months while
    reporting can_make_requests=false. Gating on them blocks every run forever.
    """
    try:
        async with session.get(
            STATUS_URL, timeout=aiohttp.ClientTimeout(total=PREFLIGHT_TIMEOUT)
        ) as resp:
            status = resp.status
            body = (await resp.text())[:200]
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"  preflight skipped (token check unavailable: {e})")
        return
    if status in (401, 403):
        raise RuntimeError(
            f"Bright Data rejected the API token ({status}: {body}).\n{TOKEN_HINT}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects, so the API token cannot leave api.brightdata.com.

    urllib copies EVERY request header onto a redirect target -- see
    HTTPRedirectHandler.redirect_request, which drops only content-length and
    content-type -- and it does that even when the redirect crosses to a different
    host. account_problems() sends the Bright Data token in an Authorization
    header, so a 30x from /status to any other origin would hand that token to
    whoever answered. aiohttp already guards this (it calls
    headers.popall(hdrs.AUTHORIZATION) whenever the redirect origin differs), which
    is why the async paths in this module are fine and this one is not.

    Returning None means "this handler cannot deal with it", so the redirect
    surfaces as an HTTPError carrying the 30x code, which account_problems()
    treats as "not 401/403" and reports as no problem. Fail-open, same as every
    other unexpected answer from the probe.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def account_problems(timeout: int = PREFLIGHT_TIMEOUT) -> list[str]:
    """Sync mirror of preflight() for the dashboard's Check setup button.

    Returns human-readable problems (empty when the token is fine) so the user can
    test their Bright Data setup without starting — and paying for — a run. Never
    raises and never bills. Fail-open like preflight(): a probe that can't reach
    Bright Data reports nothing rather than inventing a problem, because "offline"
    and "your token is dead" are not the same finding. Same rule as preflight():
    `can_make_requests` is a proxy-zone field and must not be treated as a problem.

    Goes through a redirect-refusing opener rather than the module-level
    urlopen(): this is the one request in the pipeline that carries a credential
    over urllib instead of aiohttp, and urllib forwards Authorization across
    hosts. See _NoRedirect.
    """
    if not API_TOKEN or not DATASET_ID:
        return ["Bright Data credentials are not set (BRIGHT_DATA_API_TOKEN / "
                "BRIGHT_DATA_DATASET_ID), so finding new jobs can't run."]
    req = urllib.request.Request(
        STATUS_URL, headers={"Authorization": f"Bearer {API_TOKEN}"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return [f"Bright Data rejected the API token ({e.code}). {TOKEN_HINT}"]
        return []
    except Exception:  # noqa: BLE001 — offline/blocked/5xx is not a setup problem
        return []
    return []


async def trigger(session: aiohttp.ClientSession, payload: dict,
                  limit_per_input: int = LIMIT_PER_INPUT) -> str:
    # quote() every interpolated value (audit P2-20 for dataset_id, P2-5 for the
    # limit): a malformed value then fails as a clean API error instead of
    # silently rewriting the query string of a billed collection.
    limit = _positive_int(limit_per_input, LIMIT_PER_INPUT)
    url = (
        "https://api.brightdata.com/datasets/v3/scrape"
        f"?dataset_id={urllib.parse.quote(str(DATASET_ID), safe='')}"
        f"&type=discover_new&discover_by=keyword"
        f"&limit_per_input={urllib.parse.quote(str(limit), safe='')}"
    )
    async with session.post(url, json=payload) as resp:
        if resp.status >= 400:
            body = await resp.text()
            # Backstop for when preflight failed open (probe down) but the account
            # is the real problem: 401/403 here means the same thing it does there.
            hint = f"\n{TOKEN_HINT}" if resp.status in (401, 403) else ""
            raise RuntimeError(f"Trigger failed {resp.status}: {body}{hint}")
        return (await resp.json())["snapshot_id"]


def _assert_collected_something(progress: dict, snapshot_id: str) -> None:
    """Refuse to call a 100%-rejected collection a successful empty run.

    This is the guard that would have caught the 2026-08-26 outage on day one.
    When Bright Data refuses every input, the snapshot still finishes with
    status="ready" and just carries records=0 alongside a non-zero error count.
    download() then returned [], main() printed "No new jobs returned this run"
    and exited 0 -- so the VM cron logged a clean success twice a day for weeks
    while collecting nothing at all. A run that collected NOTHING while reporting
    errors is a failure, and it has to be loud enough for the cron log and the
    dashboard's error dialog to show it.

    Keyed on the error CODES, not on the error count, and that distinction is the
    whole design:

      * A count-only rule raises on a legitimately quiet day. dead_page and
        page_too_big appear on nearly every healthy run (a normal one measured
        08-14: 252 rows, 62 errors), and once the exclude set is doing its job the
        expected steady state is 0 new rows with those same page errors still
        present. run_scraper.sh is `set -e`, so raising there would take scoring,
        the retention prune and the Drive upload down with it: a guard against
        silent failure that manufactures a loud one.
      * A count-only rule also MISSES partial rejection. If Bright Data refuses
        90% of the children and 10% return rows, `records` is truthy and a
        count-only check waves through a badly degraded collection.

    So: an input-rejection code is fatal whether or not rows came back, and zero
    rows with only ordinary page errors is a warning, not an exception.

    Pure ASCII: this text lands in the Windows console and the dashboard dialog.
    """
    records = progress.get("records") or 0
    errors = progress.get("errors") or 0
    codes = progress.get("error_codes") or {}
    rejected = sorted(c for c in codes if c in INPUT_REJECTION_CODES)
    if rejected:
        hint = ""
        if "child_input_size_validation" in rejected:
            hint = ("\nThat error means the request itself was too large. It is almost always "
                    "the exclude list (jobs_to_not_include), which is copied onto every one of "
                    "the up-to-limit_per_input child fetches each search spawns. Lower "
                    f"limit_per_input, or MAX_EXCLUDE_IDS (currently {MAX_EXCLUDE_IDS}).")
        raise RuntimeError(
            f"Collection {snapshot_id} was rejected by Bright Data: {codes}. "
            f"It collected {records} row(s), so the inputs it did accept are not the "
            f"whole search.{hint}")
    if not records and errors:
        print(f"  WARNING: collection {snapshot_id} returned 0 rows with {errors} "
              f"error(s): {codes}. No input was rejected, so this is most likely a "
              f"genuinely quiet 24 hours -- but check scraper.log if it repeats.")


async def wait_until_ready(session: aiohttp.ClientSession, snapshot_id: str) -> None:
    url = f"https://api.brightdata.com/datasets/v3/progress/{snapshot_id}"
    deadline = asyncio.get_event_loop().time() + MAX_WAIT_MINUTES * 60
    failures = 0
    while True:
        try:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
            failures = 0
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # One transient 5xx must not abort a snapshot that's already billed.
            failures += 1
            if failures >= MAX_POLL_FAILURES:
                raise RuntimeError(f"Progress polling failed {failures}x in a row: {e}") from e
            print(f"  poll error ({e}); retrying ({failures}/{MAX_POLL_FAILURES})")
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(
                    f"Timeout after {MAX_WAIT_MINUTES} min while polling") from e
            await asyncio.sleep(POLL_INTERVAL)
            continue
        status = data.get("status")
        print(f"  status: {status}")
        if status == "ready":
            # "ready" is not the same as "worked" -- see _assert_collected_something.
            _assert_collected_something(data, snapshot_id)
            return
        if status == "failed":
            raise RuntimeError(f"Collection failed: {data}")
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError(f"Timeout after {MAX_WAIT_MINUTES} min, last status: {status}")
        await asyncio.sleep(POLL_INTERVAL)


async def download(session: aiohttp.ClientSession, snapshot_id: str) -> list[dict]:
    """Fetch the collected rows for a snapshot.

    Bright Data's /progress endpoint can flip to 'ready' a beat before the
    /snapshot data endpoint is actually servable. The first download then comes
    back as HTTP 200 with a JSON *body* like
        {"status": "building", "message": "Dataset is not ready yet, try again in 30s"}
    instead of the rows array. Because that is a 200 (not a ClientError), the old
    retry loop never caught it: it returned the dict, and main() aborted the
    whole run with "Unexpected response shape". So inspect the body and keep
    polling on a not-ready signal, exactly like wait_until_ready does.
    """
    url = f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json"
    deadline = asyncio.get_event_loop().time() + MAX_WAIT_MINUTES * 60
    failures = 0
    while True:
        try:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
            failures = 0
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # Transient network/5xx errors: retry a bounded number of times.
            failures += 1
            if failures >= MAX_POLL_FAILURES:
                raise RuntimeError(f"Snapshot download failed {failures}x in a row: {e}") from e
            print(f"  download error ({e}); retrying ({failures}/{MAX_POLL_FAILURES})")
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(
                    f"Timeout after {MAX_WAIT_MINUTES} min downloading snapshot") from e
            await asyncio.sleep(POLL_INTERVAL)
            continue

        # The rows arrived — done.
        if isinstance(data, list):
            return data

        # Not a list: Bright Data is still materializing the dataset. Detect the
        # known not-ready shapes and keep waiting instead of mistaking it for data.
        status = (data.get("status") if isinstance(data, dict) else "") or ""
        message = (data.get("message") if isinstance(data, dict) else "") or ""
        if status.lower() == "failed":
            raise RuntimeError(f"Collection failed during download: {data}")
        not_ready = status.lower() in {"building", "running", "pending", "collecting", "scheduled"} \
            or "not ready" in message.lower() or "try again" in message.lower()
        if not not_ready:
            # Genuinely unexpected payload — surface it rather than loop forever.
            raise RuntimeError(f"Unexpected snapshot response shape: {data}")
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError(f"Timeout after {MAX_WAIT_MINUTES} min; snapshot still '{status or 'building'}'")
        print(f"  snapshot not ready yet (status: {status or 'building'}); retrying in {POLL_INTERVAL}s")
        await asyncio.sleep(POLL_INTERVAL)


async def main(snapshot_id: str | None = None, run_label: str | None = None,
               max_keywords: int | None = None,
               limit_per_input: int | None = None) -> None:
    require_credentials()
    run_label = run_label or get_run_label()
    cfg = load_search_config()
    # CLI > config > built-in default: an explicit --limit wins, else the config
    # (which itself falls back to LIMIT_PER_INPUT) drives the per-input cap.
    if limit_per_input is None:
        limit_per_input = cfg["limit_per_input"]

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        if snapshot_id is None:
            # Normal path: trigger a fresh (billed) collection and wait for it.
            # Preflight first so a dead account costs nothing. The recovery path
            # below is deliberately NOT gated: that snapshot is already paid for,
            # and a fail-open probe must never block collecting what you own.
            await preflight(session)
            exclude_ids = load_exclude_ids()
            print(f"Run: {run_label} | Excluding {len(exclude_ids)} already-scraped job IDs")
            inputs = build_inputs(exclude_ids, max_keywords=max_keywords,
                                  limit_per_input=limit_per_input)
            payload = {"input": inputs}
            n_keywords = len(cfg["keywords"])
            kw_used = n_keywords if max_keywords is None else min(max_keywords, n_keywords)
            print(f"Triggering {len(inputs)} searches ({kw_used} keywords x {len(cfg['remote_types'])} remote types), "
                  f"limit_per_input={limit_per_input} -> up to {len(inputs) * limit_per_input} postings")
            snapshot_id = await trigger(session, payload, limit_per_input=limit_per_input)
            print(f"Snapshot: {snapshot_id}")
            await wait_until_ready(session, snapshot_id)
        else:
            # Recovery path: re-download an already-collected snapshot (e.g. one
            # whose run aborted after billing). No trigger -> no extra cost.
            print(f"Run: {run_label} | Recovering already-collected snapshot {snapshot_id} (no new trigger/billing)")
        results = await download(session, snapshot_id)

    df = pd.json_normalize(results)
    if df.empty:
        # Write nothing: a columnless CSV would crash the scoring step, and the
        # rest of run_scraper.sh (master upload) should still proceed.
        print("No new jobs returned this run — nothing to write.")
        return

    if "job_posting_id" in df.columns:
        before = len(df)
        df["job_posting_id"] = df["job_posting_id"].astype(str)
        df = df.drop_duplicates(subset=["job_posting_id"])
        print(f"Deduped: {before} -> {len(df)} unique jobs")

    before = len(df)
    df = drop_blocklisted_companies(df)
    if len(df) != before:
        print(f"Company blocklist: dropped {before - len(df)} -> {len(df)} remain")

    df = drop_unneeded_columns(df)
    df["run_label"] = run_label

    date_str = datetime.now().strftime("%Y-%m-%d")
    df["extracted_date"] = date_str  # the day this job was scraped (shown/sorted in the UI)
    run_dir = OUTPUT_DIR / run_label
    run_dir.mkdir(exist_ok=True)
    csv_path = run_dir / f"linkedin_jobs_{date_str}_{run_label}.csv"

    _atomic_to_csv(df, csv_path)  # atomic: a truncated run CSV would crash the scoring step

    if "job_posting_id" in df.columns:
        save_current_ids(df["job_posting_id"].astype(str).tolist())

    master_total = append_to_master(df)
    print(f"Saved {len(df)} jobs -> {run_label}/{csv_path.name}")
    print(f"Master CSV now contains {master_total} unique jobs")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scrape LinkedIn jobs via Bright Data, or recover an already-collected snapshot."
    )
    parser.add_argument(
        "--snapshot",
        help="Recover this already-collected snapshot id instead of triggering a new (billed) collection.",
    )
    parser.add_argument(
        "--label",
        choices=RUN_LABELS,
        help="Force the run label (default: derived from the current hour).",
    )
    parser.add_argument(
        "--max-keywords",
        type=int,
        default=None,
        help="Spend guard: use only the first N keywords (default: all). "
             "Each keyword fans out to one search per remote type.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Spend guard: max postings collected per search "
             f"(default: search_config.json limit_per_input, else {LIMIT_PER_INPUT}).",
    )
    args = parser.parse_args()
    asyncio.run(main(
        snapshot_id=args.snapshot,
        run_label=args.label,
        max_keywords=args.max_keywords,
        limit_per_input=args.limit,
    ))
