import asyncio
import json
import os
import re
import socket
import sys
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

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


def warn(msg: str) -> None:
    """Print a non-fatal problem as ONE line starting "WARNING: ".

    The dashboard reads the run's output for these lines and shows them to the
    user after Find new jobs, so each one says what happened, what it costs and
    what to do, and a stray newline inside it would split it in two."""
    print(f"WARNING: {_one_line(msg)}", flush=True)


_WARNED: set[str] = set()


def warn_once(msg: str, key: str | None = None) -> None:
    """warn(), but once per process for the same `key` (default: the message).
    For checks inside a helper the run calls many times."""
    k = key or msg
    if k in _WARNED:
        return
    _WARNED.add(k)
    warn(msg)


def _one_line(text) -> str:
    """`text` with every run of whitespace (newlines included) collapsed to a space."""
    return " ".join(str(text).split())


def require_credentials() -> None:
    """Exit with a helpful message if Bright Data credentials are missing.
    Called at the start of a run, not at import time."""
    if not API_TOKEN or not DATASET_ID:
        # stdout, like every other ERROR line: the dashboard reads it from there.
        print("ERROR: the Bright Data credentials are missing, so no jobs can be "
              "collected. Set BRIGHT_DATA_API_TOKEN and BRIGHT_DATA_DATASET_ID in "
              "Settings, your environment or a local .env file (see .env.example).",
              flush=True)
        sys.exit(1)
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
# Set (to 1) by a caller that EXPECTS a Drive master: the dashboard sets it on a
# local run. Unset LINKEDIN_EXTRA_MASTER is normal on the VM and quiet there, but
# on a local run it means Google Drive was not available, so the skip list lacks
# every job the VM collected; with this set, the run says so in a WARNING. A
# LINKEDIN_EXTRA_MASTER that is set but names a missing file always warns.
EXPECT_DRIVE_MASTER_ENV = "INPLOYED_EXPECT_DRIVE_MASTER"

# Every id each run got back from Bright Data, with the LAST day it was collected.
# The masters cannot answer that: append_to_master keeps the existing row for a
# known id, so a master's extracted_date is the day the job was FIRST found. A job
# first found three weeks ago and collected again yesterday then looked three weeks
# old to the skip-list window, fell off jobs_to_not_include, and was collected and
# billed again on every run (replaying the 2026-10-05..13 runs, that rule skipped
# ~7 of 2,320 repeats). load_exclude_ids windows on the newest date ANY source knows,
# this file included. One row per id (its newest date); rows older than
# LEDGER_KEEP_DAYS are pruned on every write, because a "Past 24 hours" search does
# not bring back a job nobody has seen for two months. A missing ledger is seeded
# once from the run files in the run-label folders (seed_ledger_from_run_files).
LEDGER_CSV = OUTPUT_DIR / "collected_ids_ledger.csv"
LEDGER_KEEP_DAYS = 60

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
# Bright Data does not publish the threshold, so it has been measured directly
# against the LinkedIn jobs dataset, one search at a time:
#   August, limit_per_input=150:
#     2,000 ids -> 4,239,000 bytes of children -> ACCEPTED (collected 128 jobs)
#     2,679 ids -> 5,262,000 bytes of children -> REJECTED, 100% of inputs
#   2026-10-13, limit_per_input=100:
#     10,000 ids -> a 140,279-byte input, x100 = 14,027,900 bytes -> ACCEPTED
# The October probe is far past the August rejection, so the limit has moved (or
# was never a plain byte count) since August. The budget below is the October
# figure, so 10,000 real-width ids fit at limit_per_input=100 and proportionally
# fewer fit as the fan-out grows (about 6,700 at 150). Two things are untested:
# 150 x 6,700, and a full trigger whose ~40 inputs each carry 10,000 ids. If
# Bright Data rejects either, the child_input_size_validation error names
# max_exclude_ids in search_config.json as the setting to lower.
#
# The cap used to be 2,000, about two runs' worth, on the theory that only those
# can resurface in a time_range="Past 24 hours" search. A replay of the
# 2026-10-05..13 runs showed jobs coming back from weeks earlier, re-collected and
# re-billed because they had fallen off the list. Re-collecting a few duplicates
# costs a little; a rejected collection costs the entire run and still burns the
# trigger, which is why the budget stays on the measured-good side.
MAX_EXCLUDE_PAYLOAD_BYTES = 14_100_000  # ~ 10,000 ids x 14 bytes x 100 children
# The width that budget was derived from: '"4444097977", ', the shape a 10-digit
# posting id takes once aiohttp's json.dumps has written it. cap_exclude_ids does
# not USE this -- it measures the ids actually in hand, so an 11-digit id can
# never quietly overflow the budget -- but the number is what MAX_EXCLUDE_PAYLOAD
# _BYTES above was computed from, so it stays as the record of that arithmetic.
BYTES_PER_EXCLUDE_ID = 14
MIN_EXCLUDE_IDS = 50           # never exclude nothing: that re-bills every posting
# The default target, the most ids sent when the byte budget allows it. Overridable
# by max_exclude_ids in search_config.json or the MAX_EXCLUDE_IDS env var (env >
# file > default, see max_exclude_ids()); no Settings row, it is set by hand.
MAX_EXCLUDE_IDS = 10_000
MAX_EXCLUDE_IDS_ENV = "MAX_EXCLUDE_IDS"

# Bright Data error codes that mean "we refused your input", as opposed to the
# per-page codes (dead_page, page_too_big) that show up on healthy runs. Any of
# these means part or all of the search never ran: with no rows at all the run
# fails, and with some rows it keeps them and warns. See _assert_collected_something.
INPUT_REJECTION_CODES = frozenset({
    "child_input_size_validation",
    "input_size_validation",
    "invalid_input",
})
# The subset that means "too large": the skip list, almost always, so the fix the
# messages name is max_exclude_ids.
SIZE_REJECTION_CODES = frozenset({"child_input_size_validation", "input_size_validation"})

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
            warn_once(f"could not read {BLOCKLIST_FILE.name} ({e}), so only the built-in "
                      f"blocklist is used and jobs from companies you blocked are kept "
                      f"this run. Close any program that has the file open and run again.",
                      key=f"blocklist:{BLOCKLIST_FILE}")
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
    "permissions (not read-only) at https://brightdata.com/cp/setting/users, "
    "then put it in .env as BRIGHT_DATA_API_TOKEN."
)


# --- known ways a run fails --------------------------------------------------
# Each is a RuntimeError (or, for the master, an OSError) so existing handlers and
# tests keep working; the type is what _fatal_message reads to tell the user the
# plain reason and what to do. main() also tags any failure with the snapshot id
# and run label it had reached (see _tag), so a paid collection can be fetched
# again with --snapshot.

class TokenRejected(RuntimeError):
    """Bright Data answered 401/403: the token is wrong, expired or not allowed
    to start a collection (the status check or the trigger)."""

    def __init__(self, msg: str, status: int):
        super().__init__(msg)
        self.status = status


class RequestTooLarge(RuntimeError):
    """The trigger was refused as too large (413, or 400 / a body about size)."""

    def __init__(self, msg: str, status: int):
        super().__init__(msg)
        self.status = status


class TriggerRefused(RuntimeError):
    """Any other refusal of the trigger POST (5xx, 429, ...)."""

    def __init__(self, msg: str, status: int, body: str):
        super().__init__(msg)
        self.status = status
        self.body = body


class InputsRejected(RuntimeError):
    """Bright Data refused every search input and collected nothing."""

    def __init__(self, msg: str, snapshot_id: str, codes: dict):
        super().__init__(msg)
        self.snapshot_id = snapshot_id
        self.codes = codes


class CollectionFailed(RuntimeError):
    """Bright Data reported the collection itself as failed."""

    def __init__(self, msg: str, snapshot_id: str, detail: str):
        super().__init__(msg)
        self.snapshot_id = snapshot_id
        self.detail = detail


class SnapshotNotFetched(RuntimeError):
    """Polling or the download gave up (retries spent or the wait ran out) on a
    collection that is already paid for. `why` is the plain-English reason."""

    def __init__(self, msg: str, snapshot_id: str, why: str):
        super().__init__(msg)
        self.snapshot_id = snapshot_id
        self.why = why


class MasterUnreadable(OSError):
    """append_to_master could not read the existing master. main() adds the run
    CSV the rows were saved to (saved_csv) and how many there were (n_jobs)."""

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


def _keyword_cap(value: int) -> int:
    """`max_keywords` as a slice length, with a negative collapsed to 1.

    build_inputs spends this as `keywords[:n]`, and Python reads a negative slice
    bound as "all but n" -- `keywords[:-1]` is every keyword except the last. So
    --max-keywords -1, the obvious way to write "no limit", would announce a cap
    and then fire all-but-one search: a spend guard that names itself and lifts
    itself, which is the one failure mode a spend guard must not have. 1 is the
    tightest real cap, so it is the fail-closed direction. 0 is left alone --
    `keywords[:0]` is empty and already spends nothing.
    """
    if value < 0:
        warn(f"--max-keywords={value} is negative, which would use every keyword "
             f"except the last and cap nothing, so using 1. Pass a positive number.")
        return 1
    return value


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
            # Once per process: build_inputs, the cap and the window all read the
            # config, and one broken file is one problem.
            warn_once(f"could not read {SEARCH_CONFIG_FILE} ({e}), so this run uses the "
                      f"built-in search settings (keywords, filters, limit_per_input "
                      f"and the skip-list size) instead of yours. Fix the file or save "
                      f"Settings again.", key=f"search_config:{path}:{e}")
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
        "max_exclude_ids": _positive_int(raw.get("max_exclude_ids"), MAX_EXCLUDE_IDS),
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
        warn(f"could not read {PREVIOUS_IDS_FILE.name} ({e}), so the job ids from the "
             f"last run are not on the skip list and may be collected and billed again. "
             f"The next run that collects jobs rewrites the file.")
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def exclude_window_days() -> int:
    """Recency window (in days) for pruning the exclude-id set.

    Resolution order is env > search_config.json > DEFAULT_EXCLUDE_WINDOW_DAYS,
    matching every other override in this module. `exclude_window_days` has no
    Settings row: it is set by hand in search_config.json, and without the file
    leg a user who set 14 would still get the 90-day default and ship their whole master
    in every trigger POST.

    A missing, non-integer, or non-positive value falls THROUGH to the next leg,
    rather than skipping to the built-in default: an env var someone fat-fingered
    must not silently override a window the user set in the dashboard. Whatever
    the file holds, _positive_int() collapses junk/0/negative there too, so the
    walk always terminates on DEFAULT_EXCLUDE_WINDOW_DAYS. 0 or a negative window
    would empty the exclude set and re-collect (and re-bill) every posting -- the
    exact failure this guard prevents -- so we always fail toward the safe default
    rather than an empty window.

    The cap still bounds what is sent: max_exclude_ids() (10,000 by default) trims
    whatever the window keeps, newest kept, and says so when it trims. See
    cap_exclude_ids."""
    raw = os.environ.get(EXCLUDE_WINDOW_DAYS_ENV, "").strip()
    if not raw:
        # _positive_int() already collapses junk/0/negative to the default.
        return load_search_config()["exclude_window_days"]
    try:
        val = int(raw)
    except ValueError:
        val = 0
    if val <= 0:
        warn_once(f"{EXCLUDE_WINDOW_DAYS_ENV}={raw!r} is not a whole number above 0, so "
                  f"it is ignored and exclude_window_days comes from search_config.json "
                  f"or the built-in {DEFAULT_EXCLUDE_WINDOW_DAYS}. Fix or remove the "
                  f"variable.")
        return load_search_config()["exclude_window_days"]
    return val


def _parse_dates(df: pd.DataFrame) -> pd.DataFrame:
    """`df`'s job ids (as str, null ids dropped, row order kept) with extracted_date
    parsed to a UTC timestamp, NaT where it is missing or unparseable.

    A frame with no extracted_date column comes back all-NaT, and an undated row
    is KEPT by the window (see _window_dated), so a source we cannot date degrades
    to the pre-window keep-all behavior instead of silently emptying the set.

    utc=True, and a UTC cutoff to match. The master this host writes is plain
    YYYY-MM-DD, but merge_incoming folds rows from other machines and a Drive
    master can carry an ISO offset. Without utc=True pandas 3 returns a tz-AWARE
    column for those, and comparing it to a tz-naive Timestamp.now() raises
    "Invalid comparison between dtype=datetime64[us, UTC] and Timestamp" -- or,
    for a column mixing offsets, "Mixed timezones detected". Normalising both
    sides costs nothing on naive input and removes the whole class. Parse each
    SOURCE on its own before combining: pandas infers one date format per column,
    so a plain date and an ISO offset in one column would turn one of them to NaT.
    format="ISO8601" goes further: it reads each value on its own as ISO 8601, so
    a date, a "T" timestamp with an offset and a space-separated time can share
    one source (a hand-edited row, a merge_incoming fold) without any of them
    turning to NaT.
    """
    if "extracted_date" in df.columns:
        dates = pd.to_datetime(df["extracted_date"], errors="coerce", utc=True,
                               format="ISO8601")
    else:
        dates = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    out = pd.DataFrame({"job_posting_id": df["job_posting_id"], "_extracted": dates})
    out = out[out["job_posting_id"].notna()]
    return out.assign(job_posting_id=out["job_posting_id"].astype(str))


def _max_exclude_ids_setting() -> tuple[int, bool]:
    """(the most exclude ids one search input may carry, whether the env var set it).

    Resolution order is env > search_config.json > MAX_EXCLUDE_IDS, the same walk
    as exclude_window_days(): a missing, non-integer or non-positive env value
    falls THROUGH to the file, and _positive_int() collapses junk in the file to
    the default. A positive value under MIN_EXCLUDE_IDS is raised to it with a
    WARNING: a skip list that short collects and bills almost everything again.
    """
    raw = os.environ.get(MAX_EXCLUDE_IDS_ENV, "").strip()
    val = None
    if raw:
        try:
            val = int(raw)
        except ValueError:
            pass
        if val is None or val <= 0:
            warn_once(f"{MAX_EXCLUDE_IDS_ENV}={raw!r} is not a whole number above 0, so "
                      f"it is ignored and max_exclude_ids comes from search_config.json "
                      f"or the built-in {MAX_EXCLUDE_IDS}. Fix or remove the variable.")
            val = None
    from_env = val is not None
    if val is None:
        val = load_search_config()["max_exclude_ids"]
    if val < MIN_EXCLUDE_IDS:
        warn_once(f"max_exclude_ids={val} is under the {MIN_EXCLUDE_IDS}-id floor, so "
                  f"using {MIN_EXCLUDE_IDS}. A shorter skip list would collect and bill "
                  f"jobs you already have.")
        val = MIN_EXCLUDE_IDS
    return val, from_env


def max_exclude_ids() -> int:
    """The most exclude ids one search input may carry (before the byte budget).
    See _max_exclude_ids_setting for where the number comes from."""
    return _max_exclude_ids_setting()[0]


def max_exclude_ids_where() -> str:
    """Where the user changes max_exclude_ids, with its current value, for a hint:
    the env var when that is what set it (editing the file would change nothing),
    else search_config.json."""
    val, from_env = _max_exclude_ids_setting()
    if from_env:
        return f"the {MAX_EXCLUDE_IDS_ENV} environment variable (now {val})"
    return f"max_exclude_ids in search_config.json (now {val})"


def _window_dated(dated: pd.DataFrame, window_days: int) -> list[str]:
    """Unique ids from a _parse_dates frame, keeping those dated within the last
    `window_days` days, OLDEST FIRST by each id's newest date.

    The order is part of the contract: cap_exclude_ids() evicts from the front, so
    this has to be sorted by date, not left in row order.

    Fail toward a SUPERSET -- an exclude set that is too SMALL re-bills already-
    collected jobs, so when recency can't be determined we KEEP the id: a row with
    no parseable date (NaT) is treated as recent.
    """
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=window_days)
    keep = dated["_extracted"].isna() | (dated["_extracted"] >= cutoff)  # NaT rows kept
    kept = dated.loc[keep]
    # Collapse a repeated id to its NEWEST date before sorting. The same job shows
    # up more than once by design: the master holds the day it was first found, the
    # ledger the day it was last collected, the Drive master the VM's copy, and a
    # merge_incoming fold or a hand-added row can repeat it inside one master. Sorted
    # at the position of its OLDEST copy it would be evicted early, the opposite of
    # what the sort is for. max() skips NaT, so an id is undated only when no source
    # dates it inside the window. sort=False keeps first-appearance order, so ids
    # sharing a date come back in the order the sources were combined.
    kept = kept.groupby("job_posting_id", as_index=False, sort=False)["_extracted"].max()
    # Oldest date FIRST, so cap_exclude_ids' tail slice evicts by date rather than by
    # row order. A merge_incoming fold on the VM (or a hand-added row) can land an
    # older posting after a newer one, and evicting on row order would then drop a
    # recent id and keep a stale one -- backwards for a "Past 24 hours" search.
    # Undated rows sort first, so they are evicted first: the window keeps them so
    # the set never silently shrinks, but a date we cannot prove must not outrank
    # one we can.
    kept = kept.sort_values("_extracted", na_position="first", kind="stable")
    return kept["job_posting_id"].tolist()


_ID_DATE_COLUMNS = ("job_posting_id", "extracted_date")
# What reading a CSV (plain or gzipped) can raise when the file is locked, damaged
# or half-synced: a truncated .csv.gz ends in EOFError, a corrupt deflate stream
# in zlib.error, neither of which is an OSError or a ValueError.
# (gzip.BadGzipFile is an OSError, UnicodeDecodeError and ParserError ValueErrors.)
_READ_ERRORS = (OSError, ValueError, EOFError, zlib.error)


def _warn_undated(df: pd.DataFrame, what: str) -> None:
    """WARNING when a non-empty id source carries no extracted_date column."""
    if df.empty or "extracted_date" in df.columns:
        return
    warn(f"{what} has no extracted_date column, so its {len(df)} job(s) cannot be "
         f"dated. They stay on the skip list, but they are the first left off when "
         f"the list is longer than the cap, so some may be collected and billed "
         f"again. Restore the file from a backup that has the column.")


def _own_master_frame() -> pd.DataFrame | None:
    """This host's master as (job_posting_id, extracted_date) rows, or None when it
    is missing, unreadable or not a master (the reason is printed). An empty but
    valid master is an empty frame."""
    if not MASTER_CSV.exists():
        return None
    try:
        df = pd.read_csv(MASTER_CSV, usecols=lambda c: c in _ID_DATE_COLUMNS, dtype=str)
    except _READ_ERRORS as e:
        warn(f"could not read {MASTER_CSV.name} for the skip list ({e}). Using the job "
             f"ids from the last run instead, so jobs collected before that may be "
             f"collected and billed again. Close any program that has the file open, "
             f"or restore it from a backup, and run again.")
        return None
    if "job_posting_id" not in df.columns:
        # Parsed, but not a master: a truncated write, or the wrong CSV copied
        # in. Say so. Falling through silently means an empty exclude set,
        # which re-collects and re-bills every posting already held -- and on
        # the VM the only trace would be the ABSENCE of a line in scraper.log.
        # An empty-but-valid master is a different thing and stays quiet.
        warn(f"{MASTER_CSV.name} has no job_posting_id column (found: "
             f"{list(df.columns) or 'no columns'}), so it cannot be used. Using the "
             f"job ids from the last run instead, so jobs collected before that may "
             f"be collected and billed again. Restore the master from a backup.")
        return None
    _warn_undated(df, MASTER_CSV.name)
    return df


def load_external_exclude_ids() -> list[str]:
    """Ids pushed from another machine (EXTERNAL_EXCLUDE_FILE), [] if absent/unreadable."""
    if not EXTERNAL_EXCLUDE_FILE.exists():
        return []
    try:
        with open(EXTERNAL_EXCLUDE_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return [str(x) for x in data] if isinstance(data, list) else []
    except (OSError, ValueError) as e:
        warn(f"could not read {EXTERNAL_EXCLUDE_FILE.name} ({e}). It lists jobs collected "
             f"on another computer, so some of them may be collected and billed again. "
             f"The next push from the dashboard replaces the file.")
        return []


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _extra_master_frame() -> pd.DataFrame | None:
    """The additional master named by $LINKEDIN_EXTRA_MASTER as (job_posting_id,
    extracted_date) rows, or None when the var is unset, the file is missing or
    unreadable, or it holds no ids. Accepts a plain or gzipped CSV.

    Never raises: the Drive master is a synced copy this host does not write, and
    a half-synced or damaged one must cost a WARNING, not the run. Unset is quiet
    unless the caller set EXPECT_DRIVE_MASTER_ENV (the dashboard, on a local run);
    set-but-missing always warns."""
    cost = ("so the skip list lacks the jobs already collected on the VM, and some "
            "of them may be collected and billed again.")
    raw = os.environ.get(EXTRA_MASTER_ENV, "").strip()
    if not raw:
        if _truthy_env(EXPECT_DRIVE_MASTER_ENV):
            warn(f"no Google Drive master was found, {cost} Start Google Drive and "
                 f"let it finish syncing, then run again.")
        return None
    path = Path(raw)
    if not path.exists():
        warn(f"the Google Drive master {path} does not exist, {cost} Start Google "
             f"Drive (or check that the file was not moved) and run again.")
        return None
    try:
        comp = "gzip" if path.suffix == ".gz" else "infer"
        df = pd.read_csv(path, usecols=lambda c: c in _ID_DATE_COLUMNS,
                         dtype=str, compression=comp)
    except _READ_ERRORS as e:
        warn(f"could not read the Google Drive master {path.name} "
             f"({type(e).__name__}: {e}), {cost} If Google Drive is still syncing, "
             f"wait for it to finish and run again; otherwise the file is damaged.")
        return None
    if "job_posting_id" not in df.columns:
        warn(f"the Google Drive master {path.name} has no job_posting_id column, "
             f"{cost} Check that {EXTRA_MASTER_ENV} points at the right file.")
        return None
    if df.empty:
        return None
    _warn_undated(df, f"the Google Drive master {path.name}")
    return df


# --- the collection ledger (see LEDGER_CSV) -----------------------------------

_LEDGER_COLUMNS = ["job_posting_id", "collected_date"]
_ISO_DAY = r"^\d{4}-\d{2}-\d{2}$"
_RUN_FILE_DAY = re.compile(r"^linkedin_jobs_(\d{4}-\d{2}-\d{2})_")


def _ledger_keep_days() -> int:
    """How many days of collected dates the ledger keeps: LEDGER_KEEP_DAYS, or the
    skip-list window when that is longer. Pruning inside the window would drop a
    date the window still wants, and that job would fall back to its master
    first-found date and be collected and billed again."""
    return max(LEDGER_KEEP_DAYS, exclude_window_days())


def _ledger_cutoff() -> str:
    """The oldest collected_date the ledger keeps, as YYYY-MM-DD (which sorts as text)."""
    return (datetime.now() - timedelta(days=_ledger_keep_days())).strftime("%Y-%m-%d")


def _newest_per_id(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """One ledger row per id from `frames`, its newest collected_date, oldest first.
    A row without a YYYY-MM-DD date is dropped: an undated ledger row says nothing."""
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=_LEDGER_COLUMNS, dtype=str)
    df = pd.concat([f[_LEDGER_COLUMNS] for f in frames], ignore_index=True).dropna()
    df = df.astype(str)
    df = df[df["collected_date"].str.match(_ISO_DAY)]
    df = df.groupby("job_posting_id", as_index=False, sort=False)["collected_date"].max()
    return df.sort_values("collected_date", kind="stable").reset_index(drop=True)


def _read_ledger() -> pd.DataFrame:
    """LEDGER_CSV as one row per id. Raises OSError/ValueError when the file cannot
    be read or is not a ledger; the callers decide what that costs."""
    df = pd.read_csv(LEDGER_CSV, dtype=str, keep_default_na=False)
    missing = [c for c in _LEDGER_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"it has no {' or '.join(missing)} column")
    return _newest_per_id([df])


def seed_ledger_from_run_files() -> pd.DataFrame:
    """A ledger rebuilt from the run files under the run-label folders: every
    linkedin_jobs_<date>_<label>*.csv and *_scored.csv.gz, each id with the newest
    date it was collected (the row's extracted_date, else the date in the file
    name). Files older than the ledger keeps (_ledger_keep_days) by name are
    skipped unread.

    Used once, when the ledger does not exist yet (the first run after this was
    added) or cannot be read. A run file that cannot be read is skipped with a
    WARNING: one bad file must not cost the rest of the seed.
    """
    cutoff = _ledger_cutoff()
    frames = []
    for label in RUN_LABELS:
        folder = OUTPUT_DIR / label
        if not folder.is_dir():
            continue
        files = sorted(folder.glob("linkedin_jobs_*.csv")) + sorted(
            folder.glob("linkedin_jobs_*_scored.csv.gz"))
        for path in files:
            m = _RUN_FILE_DAY.match(path.name)
            file_day = m.group(1) if m else None
            if file_day and file_day < cutoff:
                continue
            try:
                df = pd.read_csv(path, usecols=lambda c: c in _ID_DATE_COLUMNS, dtype=str)
            except _READ_ERRORS as e:
                warn(f"could not read {label}/{path.name} while rebuilding "
                     f"{LEDGER_CSV.name} ({e}). The jobs in that run may be collected "
                     f"and billed again. The file is probably damaged; the rest of "
                     f"the run files are still used.")
                continue
            if "job_posting_id" not in df.columns or df.empty:
                continue
            if "extracted_date" in df.columns:
                days = pd.to_datetime(df["extracted_date"], errors="coerce", utc=True,
                                      format="ISO8601").dt.strftime("%Y-%m-%d")
            else:
                days = pd.Series(None, index=df.index, dtype=object)
            if file_day:
                days = days.fillna(file_day)
            frames.append(pd.DataFrame({"job_posting_id": df["job_posting_id"],
                                        "collected_date": days}))
    seeded = _newest_per_id(frames)
    return seeded[seeded["collected_date"] >= cutoff].reset_index(drop=True)


def _save_seeded_ledger(led: pd.DataFrame) -> None:
    """Write a ledger just rebuilt from the run files, so the next call (and the
    next run) reads one small file instead of every run file again. Best effort:
    the rebuilt rows are already in hand, so a failed write costs a WARNING."""
    try:
        _atomic_to_csv(led[_LEDGER_COLUMNS], LEDGER_CSV)
    except Exception as e:  # noqa: BLE001 -- the skip list is already built
        warn(f"could not save the rebuilt {LEDGER_CSV.name} ({e}). The skip list "
             f"is still complete for this run, but each run reads every saved run "
             f"file again until the file can be written. Check that the folder is "
             f"writable and the disk is not full.")


def _ledger_frame() -> pd.DataFrame:
    """The ledger as (job_posting_id, extracted_date) rows for load_exclude_ids:
    seeded from the run files (and saved) when the file does not exist yet, and
    from the run files with a WARNING when it cannot be read. Never raises."""
    if not LEDGER_CSV.exists():
        led = seed_ledger_from_run_files()
        _save_seeded_ledger(led)
    else:
        try:
            led = _read_ledger()
        except (OSError, ValueError) as e:
            warn(f"could not read {LEDGER_CSV.name} ({e}). It holds the "
                 f"last day each job was collected, so without it jobs collected "
                 f"recently may be left off the skip list and billed again. Using "
                 f"the saved run files instead; this run rewrites the file.")
            led = seed_ledger_from_run_files()
    return led.rename(columns={"collected_date": "extracted_date"})


def record_collected_ids(ids: list[str], day: str) -> None:
    """Add this run's ids to the ledger with `day` (YYYY-MM-DD): every id Bright
    Data returned, known and new alike, because a known job collected again is the
    one whose master date is wrong. Keeps each id's newest date, prunes rows older
    than _ledger_keep_days(), writes atomically.

    Never raises. The jobs are already collected and saved by the time this runs,
    so a ledger problem costs a WARNING and possibly a re-billed job, never the run.
    """
    try:
        if LEDGER_CSV.exists():
            try:
                led = _read_ledger()
            except (OSError, ValueError) as e:
                warn(f"could not read {LEDGER_CSV.name} ({e}). Rebuilding "
                     f"it from the saved run files, so jobs collected only in runs "
                     f"whose files are gone may be collected and billed again.")
                led = seed_ledger_from_run_files()
        else:
            led = seed_ledger_from_run_files()
        run = pd.DataFrame({"job_posting_id": [str(i) for i in ids],
                            "collected_date": day}, columns=_LEDGER_COLUMNS)
        merged = _newest_per_id([led, run])
        merged = merged[merged["collected_date"] >= _ledger_cutoff()]
        _atomic_to_csv(merged, LEDGER_CSV)
    except Exception as e:  # noqa: BLE001 -- the rows are saved; this must not end the run
        warn(f"could not save the ids of this run's {len(ids)} job(s) to "
             f"{LEDGER_CSV.name} ({e}). The next run may leave them off the skip "
             f"list, so they may be collected and billed again. Check that the "
             f"file is not open in another program and that the disk is not full.")


def load_exclude_ids() -> list[str]:
    """Every job id this host knows to skip — a hard no-repeat guard. Bright Data
    bills per collection, so re-fetching a posting we already have is pure wasted
    spend. The sources, combined:
      - this host's master (or the last-run JSON when it is missing/unreadable);
      - the extra master named by $LINKEDIN_EXTRA_MASTER (the synced Drive master,
        so a LOCAL run skips what the VM already collected);
      - external_exclude_ids.json (ids collected on another machine and pushed
        here so a scheduled VM run skips them; they carry no date);
      - the collection ledger (LEDGER_CSV): the last day each id was collected.

    Each id is dated by the NEWEST date any source gives it and windowed to the
    last exclude_window_days on that date. The search is 'Past 24 hours', so an
    id nobody has collected in weeks cannot reappear and is only trigger-POST
    bloat; but the masters hold the day a job was FIRST found, and windowing on
    that dropped a job collected yesterday and re-billed it every run. Undated ids
    (the external file, a master without dates) are kept.

    ORDER IS A CONTRACT, not an accident: cap_exclude_ids() keeps the TAIL, so the
    list is oldest first by that newest date, undated ids first. Every source is
    ranked on the same clock: a job the VM collected yesterday can resurface just
    like one of ours, so it outranks an id of ours last seen a week ago. Undated
    ids tie at the front in source order (Drive, external, ours), so on the VM,
    where the pushed external file routinely carries more ids than the cap, the
    foreign undated ids are evicted before any of this host's own.
    """
    window = exclude_window_days()
    own = _own_master_frame()
    if own is None or own.empty:
        previous = load_previous_ids()
        # An unreadable master has already said so. A missing or emptied master
        # with ids from a last run means the master went away; a first run with
        # neither is normal and stays quiet.
        if previous and (own is not None or not MASTER_CSV.exists()):
            state = "has no jobs" if own is not None else "is missing"
            warn(f"{MASTER_CSV.name} {state}, so the skip list uses the "
                 f"{len(previous)} job ids from the last run instead. Jobs collected "
                 f"before that may be collected and billed again. Restore the master "
                 f"from a backup if it was moved, deleted or emptied.")
        own = pd.DataFrame({"job_posting_id": previous}, dtype=str)
    sources = [
        ("the Google Drive master", _extra_master_frame()),
        (EXTERNAL_EXCLUDE_FILE.name,
         pd.DataFrame({"job_posting_id": load_external_exclude_ids()}, dtype=str)),
        (MASTER_CSV.name, own),
        (LEDGER_CSV.name, _ledger_frame()),
    ]
    parts = []
    for name, df in sources:
        if df is None or df.empty or "job_posting_id" not in df.columns:
            continue
        try:
            parts.append(_parse_dates(df))
        except (TypeError, ValueError) as e:
            # Degrade that one source to undated (kept), never the whole run: a
            # superset never re-bills.
            warn(f"could not read the dates in {name} ({e}), so its jobs stay on the "
                 f"skip list without dates. They are the first left off when the list "
                 f"is longer than the cap, so some may be collected and billed again. "
                 f"Report this if it repeats.")
            parts.append(_parse_dates(df.drop(columns=["extracted_date"], errors="ignore")))
    if not parts:
        return []
    dated = pd.concat(parts, ignore_index=True)
    try:
        return _window_dated(dated, window)
    except (TypeError, ValueError) as e:
        warn(f"could not compare the dates on the skip list ({e}), so every known "
             f"job id is kept on it, however old. That bills nothing again by itself, "
             f"but the list is longer than it needs to be and the cap may leave "
             f"recent jobs off it. Report this if it repeats.")
        return dated["job_posting_id"].unique().tolist()


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
        raise MasterUnreadable(
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
        # dtype=str + keep_default_na=False: byte-stable master
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
    oldest first (see its docstring and _window_dated). With a
    time_range="Past 24 hours" search those are the only ids that can resurface,
    so trimming the head instead would keep precisely the ids that cannot recur.

    The per-id width is MEASURED off the ids in hand, not assumed: the
    BYTES_PER_EXCLUDE_ID constant bakes in a 10-digit LinkedIn posting id, ids are
    at 4.4e9 now, and the first 11-digit id would silently push the real payload
    past the cap with nothing noticing.

    Every id that reaches here is a known job inside the window, so a trim is a
    cost, and it is reported as a WARNING naming the setting that would avoid it.
    """
    limit = _positive_int(limit_per_input, LIMIT_PER_INPUT)
    if not exclude_ids:
        return exclude_ids
    per_id = max(1, len(json.dumps(exclude_ids)) // len(exclude_ids))
    budget = MAX_EXCLUDE_PAYLOAD_BYTES // (per_id * limit)
    cap = max_exclude_ids()
    keep = min(budget, cap)
    if keep < MIN_EXCLUDE_IDS:
        # The budget, not the floor, wins here. An exclude set below the floor
        # re-bills postings we already hold, which is bad; a payload over the cap
        # gets the collection REJECTED, which costs the whole run and still burns
        # the trigger. The floor is worth having, but not at the price of the
        # failure the cap exists to prevent -- so say plainly what to change.
        warn(f"limit_per_input={limit} leaves room for only {keep} skip-list ids, "
             f"under the {MIN_EXCLUDE_IDS}-id floor, so this run will collect and "
             f"bill again jobs it already has. Lower limit_per_input in "
             f"search_config.json.")
        keep = max(keep, 1)
        return exclude_ids[-keep:]
    if len(exclude_ids) <= keep:
        return exclude_ids
    if budget > cap:
        fix = f"Raise {max_exclude_ids_where()} if Bright Data accepts a longer list."
    else:
        fix = (f"limit_per_input={limit} leaves room for only {budget}; lower "
               f"limit_per_input in search_config.json to fit more.")
    warn(f"skip list capped at {keep} of {len(exclude_ids)} known jobs. "
         f"The {len(exclude_ids) - keep} oldest are left off it, so they may be "
         f"collected and billed again. {fix}")
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
    keywords = (cfg["keywords"] if max_keywords is None
                else cfg["keywords"][:_keyword_cap(max_keywords)])
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
    run. A timeout, a 5xx or anything else prints a WARNING and continues. This
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
        warn(f"could not reach Bright Data's account check ({_exc_text(e)}), so the "
             f"token was not checked. The run went ahead anyway; if it fails next, "
             f"check the internet connection.")
        return
    if status in (401, 403):
        raise TokenRejected(
            f"Bright Data rejected the API token ({status}: {body}).\n{TOKEN_HINT}", status)
    if not 200 <= status < 300:
        warn(f"Bright Data's account check answered {status}, so the token could not "
             f"be checked. The run went ahead anyway; if the collection fails next, "
             f"try again later.")


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


class Collection(NamedTuple):
    """What a collection POST came back with. Exactly one side is populated.

    `rows` -- Bright Data finished inside its synchronous window and handed the
    records straight back with the POST. There is no snapshot behind them.
    `snapshot_id` -- it ran long, answered 202, and is still collecting.
    """
    snapshot_id: str | None
    rows: list[dict] | None


# How much of a response body a refusal message quotes. The message becomes one
# line of scraper.log, the console and the dashboard's crash dialog, and a body
# is untrusted bytes from a billed endpoint: the head of it is the diagnosis,
# the whole of it is a wall. The other arms of _parse_collection_body already
# quote 200 characters; this is the bound for the two that quote an envelope.
BODY_QUOTE_CHARS = 2000


def _quoted(text: str) -> str:
    """`text` cut to BODY_QUOTE_CHARS, saying so when it was longer."""
    text = str(text)
    if len(text) <= BODY_QUOTE_CHARS:
        return text
    return f"{text[:BODY_QUOTE_CHARS]}... [{len(text) - BODY_QUOTE_CHARS} more chars]"


def _looks_like_an_envelope(data: dict) -> bool:
    """True for a status/error wrapper rather than a collected job record.

    Only used to keep a lone envelope from being filed as one job. A real record
    carries neither key; `download()` handles the same wrappers on its own path.
    """
    if "error" in data:
        return True
    return "status" in data and "message" in data


def _records(rows: list, text: str) -> Collection:
    """`rows` as a record set, refusing anything that is not a JSON object.

    pd.json_normalize over a scalar is a traceback, and a scalar here means the
    body was never a collection: name it instead of filing it.
    """
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Collection body item {n} is not a record but {type(row).__name__} "
                f"({row!r:.80}); first 200 chars of the body: {text[:200]!r}")
    return Collection(None, rows)


def _parse_collection_body(body: str, status: int | None = None) -> Collection:
    """Read a /datasets/v3/scrape response body, whichever shape it took.

    That endpoint is SYNCHRONOUS with an asynchronous escape hatch: it collects
    for up to a minute and, only if the run is still going, answers 202 with a
    {"snapshot_id": ...} to poll instead. Every production run so far has been
    slow enough to take the escape hatch -- 40 searches at limit_per_input=150
    never finished in a minute -- which is the only reason reading the body as a
    single JSON object and reaching for ["snapshot_id"] ever worked.

    A run that FINISHES in time returns the records themselves, one JSON object
    per line, and json() then dies on the second line with "Extra data: line 2
    column 1". That body is not corrupt: it is the collection, already billed,
    and reading it as an envelope threw it away with no snapshot to recover it
    from. Which branch you get is a property of how long the scrape took, so it
    flips on its own -- and flips toward THIS one as the exclude set grows and
    the runs get smaller.

    All three shapes are accepted here rather than pinned with a `format`
    parameter, because the request that produces them is the billed one and is
    not worth experimenting on to find out.

    `status` is the HTTP status when the caller has one. 202 Accepted is Bright
    Data saying the collection is still running, so its body is a handoff and
    never the records: a 202 whose body carries no snapshot id is refused rather
    than filed as a one-record collection (a `{"status": "running"}` body would
    otherwise become one bogus row with no job_posting_id, and the real
    collection would never be polled).
    """
    text = (body or "").strip()
    if not text:
        # Returning an empty collection here would print "No new jobs returned
        # this run" and exit 0 -- the silent-success shape that
        # _assert_collected_something exists to stop.
        raise RuntimeError(
            "Bright Data returned an empty body for a collection request: no "
            "rows and no snapshot id, so there is nothing to poll or download.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        pass                      # not one JSON value -- fall through to NDJSON
    else:
        if isinstance(data, dict) and data.get("snapshot_id"):
            return Collection(str(data["snapshot_id"]), None)
        if isinstance(data, dict) and _looks_like_an_envelope(data):
            raise RuntimeError(f"Collection request refused: {_quoted(data)}")
        if status == 202:
            raise RuntimeError(
                f"Bright Data answered 202 Accepted (collection still running) "
                f"but the body carries no snapshot_id to poll: {text[:200]!r}")
        if isinstance(data, list):
            return _records(data, text)
        if isinstance(data, dict):
            return Collection(None, [data])       # a one-record NDJSON body
        raise RuntimeError(
            f"Collection body item 1 is not a record but {type(data).__name__} "
            f"({data!r:.80}); first 200 chars of the body: {text[:200]!r}")

    if status == 202:
        raise RuntimeError(
            f"Bright Data answered 202 Accepted (collection still running) "
            f"but the body carries no snapshot_id to poll: {text[:200]!r}")
    rows: list = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            # Name the line: a body that is NDJSON for 200 records and then
            # truncated is a different problem from one that was never NDJSON.
            raise RuntimeError(
                f"Collection body is not readable as JSON or NDJSON; line {n} "
                f"failed to parse ({e}). First 200 chars: {text[:200]!r}") from e
    return _records(rows, text)


async def trigger(session: aiohttp.ClientSession, payload: dict,
                  limit_per_input: int = LIMIT_PER_INPUT) -> Collection:
    # quote() every interpolated value (dataset_id and the limit): a malformed
    # value then fails as a clean API error and never rewrites the query
    # string of a billed collection.
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
            head = f"Trigger failed {resp.status}: {_quoted(body)}"
            # Backstop for when preflight failed open (probe down) but the account
            # is the real problem: 401/403 here means the same thing it does there.
            if resp.status in (401, 403):
                raise TokenRejected(f"{head}\n{TOKEN_HINT}", resp.status)
            if _too_large(resp.status, body):
                raise RequestTooLarge(
                    f"{head}\nThe request was too large. The skip list "
                    f"(jobs_to_not_include) sent with every search is the usual cause: "
                    f"lower {max_exclude_ids_where()}.", resp.status)
            raise TriggerRefused(head, resp.status, body)
        # text(), not json(): a collection that beat the sync window comes back
        # as NDJSON, which json() cannot read. See _parse_collection_body.
        return _parse_collection_body(await resp.text(), status=resp.status)


def _too_large(status: int, body: str) -> bool:
    """A trigger refusal that means the request was too big: 413, a 400 (what
    Bright Data has answered an oversized input with), or another 4xx whose body
    talks about size."""
    if status in (400, 413):
        return True
    return status < 500 and bool(re.search(r"too large|size", body or "", re.IGNORECASE))


def _assert_collected_something(progress: dict, snapshot_id: str) -> None:
    """Refuse to call a 100%-rejected collection a successful empty run.

    When Bright Data refuses every input, the snapshot still finishes with
    status="ready" and just carries records=0 alongside a non-zero error count.
    download() returns [], and without this guard main() prints "No new jobs
    returned this run" and exits 0, so the VM cron logs a clean success twice a
    day while collecting nothing at all. A run that collected NOTHING while reporting
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

    So: an input-rejection code with NO rows is fatal (InputsRejected). With rows,
    those rows are already paid for, so they are downloaded and saved like any
    others and the rejection is a WARNING: raising there threw the paid rows away
    and skipped scoring. Zero rows with only ordinary page errors is a WARNING too.

    Pure ASCII: this text lands in the Windows console and the dashboard dialog.
    """
    records = progress.get("records") or 0
    errors = progress.get("errors") or 0
    codes = progress.get("error_codes") or {}
    rejected = sorted(c for c in codes if c in INPUT_REJECTION_CODES)
    if rejected:
        size = any(c in SIZE_REJECTION_CODES for c in rejected)
        if not records:
            hint = ""
            if size:
                hint = ("\nThat error means the request itself was too large. It is almost "
                        "always the exclude list (jobs_to_not_include), which is copied onto "
                        "every one of the up-to-limit_per_input child fetches each search "
                        f"spawns. Lower {max_exclude_ids_where()}, or limit_per_input.")
            raise InputsRejected(
                f"Collection {snapshot_id} was rejected by Bright Data: {codes}. "
                f"It collected 0 rows.{hint}", snapshot_id, codes)
        if size:
            fix = (f"The rejection code means those searches' skip list was too large. "
                   f"Lower {max_exclude_ids_where()} so every search fits.")
        else:
            fix = "Check the keywords and filters in Settings (search_config.json)."
        warn(f"Bright Data rejected some of this run's searches ({codes}) and collected "
             f"{records} job(s) from the rest. Those jobs are saved as usual, but the "
             f"rejected searches collected nothing, so their new jobs are missed until "
             f"a later run. {fix}")
        return
    if not records and errors:
        warn(f"collection {snapshot_id} returned 0 jobs with {errors} page error(s) "
             f"({codes}). No search was rejected, so most likely nothing new was "
             f"posted. If it happens on several runs in a row, check scraper.log and "
             f"that collection in the Bright Data dashboard.")


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
                raise SnapshotNotFetched(
                    f"Progress polling failed {failures}x in a row: {e}", snapshot_id,
                    f"Bright Data's progress check failed {failures} times in a row "
                    f"({_exc_text(e)})") from e
            print(f"  poll error ({e}); retrying ({failures}/{MAX_POLL_FAILURES})")
            if asyncio.get_event_loop().time() > deadline:
                raise SnapshotNotFetched(
                    f"Timeout after {MAX_WAIT_MINUTES} min while polling", snapshot_id,
                    f"Bright Data's progress check was still failing after "
                    f"{MAX_WAIT_MINUTES} minutes ({_exc_text(e)})") from e
            await asyncio.sleep(POLL_INTERVAL)
            continue
        status = data.get("status")
        print(f"  status: {status}")
        if status == "ready":
            # "ready" is not the same as "worked" -- see _assert_collected_something.
            _assert_collected_something(data, snapshot_id)
            return
        if status == "failed":
            raise CollectionFailed(f"Collection failed: {data}", snapshot_id,
                                   _failure_detail(data))
        if asyncio.get_event_loop().time() > deadline:
            raise SnapshotNotFetched(
                f"Timeout after {MAX_WAIT_MINUTES} min, last status: {status}", snapshot_id,
                f"the collection was still running after {MAX_WAIT_MINUTES} minutes "
                f"(last status: {status})")
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
                raise SnapshotNotFetched(
                    f"Snapshot download failed {failures}x in a row: {e}", snapshot_id,
                    f"the download failed {failures} times in a row "
                    f"({_exc_text(e)})") from e
            print(f"  download error ({e}); retrying ({failures}/{MAX_POLL_FAILURES})")
            if asyncio.get_event_loop().time() > deadline:
                raise SnapshotNotFetched(
                    f"Timeout after {MAX_WAIT_MINUTES} min downloading snapshot",
                    snapshot_id, f"the download was still failing after "
                    f"{MAX_WAIT_MINUTES} minutes ({_exc_text(e)})") from e
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
            raise CollectionFailed(f"Collection failed during download: {data}",
                                   snapshot_id, _failure_detail(data))
        not_ready = status.lower() in {"building", "running", "pending", "collecting", "scheduled"} \
            or "not ready" in message.lower() or "try again" in message.lower()
        if not not_ready:
            # Genuinely unexpected payload — surface it rather than loop forever.
            raise RuntimeError(f"Unexpected snapshot response shape: {data}")
        if asyncio.get_event_loop().time() > deadline:
            raise SnapshotNotFetched(
                f"Timeout after {MAX_WAIT_MINUTES} min; snapshot still "
                f"'{status or 'building'}'", snapshot_id,
                f"the collection was still not ready to download after "
                f"{MAX_WAIT_MINUTES} minutes")
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
    else:
        # trigger() coerces this again before it reaches the billed URL, but ONLY
        # there -- so an explicit --limit=0 or --limit=-5 printed one number and
        # billed against another, and the run log named a cap that was never in
        # force. Coerce once, here, and say so.
        capped = _positive_int(limit_per_input, cfg["limit_per_input"])
        if capped != limit_per_input:
            warn(f"--limit={limit_per_input} is not a positive count, so using "
                 f"{capped}. A bad value never lifts a spend guard.")
        limit_per_input = capped

    try:
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
                # Derived from `inputs`, not from the raw flag: build_inputs coerces
                # the flag, and a run log naming a cap the run did not apply is worse
                # than no line at all.
                kw_used = len(inputs) // max(len(cfg["remote_types"]), 1)
                print(f"Triggering {len(inputs)} searches ({kw_used} keywords x {len(cfg['remote_types'])} remote types), "
                      f"limit_per_input={limit_per_input} -> up to {len(inputs) * limit_per_input} postings")
                collected = await trigger(session, payload, limit_per_input=limit_per_input)
                if collected.rows is not None:
                    # Finished inside Bright Data's synchronous window: the records
                    # arrived WITH the POST and no snapshot was ever created, so
                    # there is nothing to poll and nothing --snapshot could recover
                    # later. This collection is already paid for; it gets used here
                    # or it is lost.
                    results = collected.rows
                    print(f"Collected {len(results)} rows synchronously "
                          f"(no snapshot issued, nothing to poll)")
                    if not results:
                        # The /progress payload is where input-rejection codes live,
                        # and this branch never fetches one, so say plainly that the
                        # rejection check did not run rather than implying a clean
                        # quiet day. See _assert_collected_something.
                        warn("Bright Data finished the collection at once with 0 jobs "
                             "and sent no progress record, so this run could not check "
                             "whether it rejected any searches. Most likely nothing new "
                             "was posted. If it happens on several runs in a row, check "
                             "the Bright Data dashboard.")
                else:
                    snapshot_id = collected.snapshot_id
                    print(f"Snapshot: {snapshot_id}")
                    await wait_until_ready(session, snapshot_id)
                    results = await download(session, snapshot_id)
            else:
                # Recovery path: re-download an already-collected snapshot (e.g. one
                # whose run aborted after billing). No trigger -> no extra cost.
                print(f"Run: {run_label} | Recovering already-collected snapshot {snapshot_id} (no new trigger/billing)")
                results = await download(session, snapshot_id)

        _save_results(results, run_label)
    except Exception as e:
        # Whatever failed, the ERROR line needs the paid snapshot (if one exists
        # by now) to tell the user how to fetch it again for free. See run().
        _tag(e, snapshot_id=snapshot_id, run_label=run_label)
        raise


def _save_results(results: list[dict], run_label: str) -> None:
    """Write a run's rows: the run CSV, the ledger, last-run ids, the master."""
    df = pd.json_normalize(results)
    if df.empty:
        # Write nothing: a columnless CSV would crash the scoring step, and the
        # rest of run_scraper.sh (master upload) should still proceed.
        print("No new jobs returned this run; nothing to write.")
        return

    if "job_posting_id" in df.columns:
        before = len(df)
        df["job_posting_id"] = df["job_posting_id"].astype(str)
        df = df.drop_duplicates(subset=["job_posting_id"])
        print(f"Deduped: {before} -> {len(df)} unique jobs")
    # Taken before the blocklist drop: a blocked company's job was billed all the
    # same, and leaving it out of the ledger would collect and bill it every run.
    collected_ids = (df["job_posting_id"].dropna().astype(str).tolist()
                     if "job_posting_id" in df.columns else [])

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
    csv_path = _free_run_csv_path(run_dir, date_str, run_label)

    _atomic_to_csv(df, csv_path)  # atomic: a truncated run CSV would crash the scoring step
    # Right after the run CSV, before the master: append_to_master can still raise
    # on an unreadable master, and these jobs are paid for either way.
    record_collected_ids(collected_ids, date_str)

    if "job_posting_id" in df.columns:
        save_current_ids(df["job_posting_id"].astype(str).tolist())

    try:
        master_total = append_to_master(df)
    except MasterUnreadable as e:
        e.saved_csv = f"{run_label}/{csv_path.name}"
        e.n_jobs = len(df)
        raise
    note = ("" if csv_path.stem == f"linkedin_jobs_{date_str}_{run_label}" else
            f" (an earlier {run_label} run today already used the base name)")
    print(f"Saved {len(df)} jobs -> {run_label}/{csv_path.name}{note}")
    print(f"Master CSV now contains {master_total} unique jobs")


def _free_run_csv_path(run_dir: Path, date_str: str, run_label: str) -> Path:
    """The run CSV path for today: `linkedin_jobs_<date>_<label>.csv`, or `-2`,
    `-3`, ... when an earlier same-day run of this label already took the name.

    A name counts as taken while its raw CSV OR its `_scored.csv.gz` exists: the
    scorer skips any input whose scored sibling exists, so reusing a stem whose
    raw file is gone would still leave the new jobs unscored. Overwriting it lost
    185 jobs on 2026-10-12. Every reader accepts the suffix: they glob
    `linkedin_jobs_*.csv` / `*_scored.csv.gz`, read the date from the start of the
    name, and take the label from the folder or the run_label column."""
    stem = f"linkedin_jobs_{date_str}_{run_label}"
    n = 1
    while True:
        name = stem if n == 1 else f"{stem}-{n}"
        if not ((run_dir / f"{name}.csv").exists()
                or (run_dir / f"{name}_scored.csv.gz").exists()):
            return run_dir / f"{name}.csv"
        n += 1


# --- the one ERROR line a failed run ends with ---------------------------------

def _tag(exc: BaseException, **attrs) -> None:
    """Attach context (snapshot_id, run_label) to `exc` unless it already has it.
    Best effort: an exception type that refuses attributes just goes without."""
    for name, value in attrs.items():
        if value is not None and getattr(exc, name, None) is None:
            try:
                setattr(exc, name, value)
            except (AttributeError, TypeError):
                pass


def _exc_text(e: BaseException, limit: int = 200) -> str:
    """'Type: message' on one line, bounded; just the type when the message is empty."""
    text = _one_line(e)
    if len(text) > limit:
        text = text[:limit] + "..."
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _failure_detail(data) -> str:
    """The part of a failed-collection payload worth quoting to the user."""
    if isinstance(data, dict):
        for key in ("error", "message", "error_message", "reason"):
            if data.get(key):
                return _one_line(data[key])[:300]
    return _one_line(data)[:300]


def _rerun_command(snapshot_id: str, run_label: str | None) -> str:
    """The command that fetches an already-paid snapshot without a new trigger. In
    the repo the script is pipeline/scraper.py; on the VM it sits flat in ~/."""
    script = "pipeline/scraper.py" if _HERE.name == "pipeline" else "~/scraper.py"
    cmd = f"python {script} --snapshot {snapshot_id}"
    return f"{cmd} --label {run_label}" if run_label else cmd


_NETWORK_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError,
                   ConnectionError, socket.gaierror)


def _fatal_message(e: BaseException) -> str:
    """The plain-English reason a run failed and what to do, for the ERROR line."""
    sid = getattr(e, "snapshot_id", None)
    label = getattr(e, "run_label", None)
    refetch = (f" This collection is already paid for. To fetch its jobs without paying "
               f"again, run: {_rerun_command(sid, label)}" if sid else "")
    if isinstance(e, TokenRejected):
        return (f"Bright Data refused the API token ({e.status}), so no jobs were "
                f"collected. The token is wrong, expired, or not allowed to start a "
                f"collection. {TOKEN_HINT}")
    if isinstance(e, InputsRejected):
        codes = ", ".join(f"{k}: {v}" for k, v in sorted(e.codes.items()))
        if any(c in SIZE_REJECTION_CODES for c in e.codes):
            return (f"Bright Data rejected every search ({codes}) because the skip list "
                    f"sent with each one was too large, so nothing was collected. Lower "
                    f"{max_exclude_ids_where()} and run again.")
        return (f"Bright Data rejected every search as invalid ({codes}), so nothing was "
                f"collected. Check the keywords and filters in Settings "
                f"(search_config.json) and run again.")
    if isinstance(e, RequestTooLarge):
        return (f"Bright Data refused to start the collection because the request was "
                f"too large ({e.status}), so no jobs were collected. The skip list sent "
                f"with every search is the usual cause. Lower {max_exclude_ids_where()} "
                f"and run again.")
    if isinstance(e, TriggerRefused):
        return (f"Bright Data refused to start the collection ({e.status}: "
                f"{_one_line(e.body)[:200]}), so no jobs were collected. This is usually "
                f"a problem on their side: try again later, and check the Bright Data "
                f"dashboard if it keeps happening.")
    if isinstance(e, CollectionFailed):
        return (f"Bright Data's collection {e.snapshot_id} failed ({e.detail}), so no "
                f"jobs were collected. Try again later; if it keeps failing, open that "
                f"collection in the Bright Data dashboard.")
    if isinstance(e, SnapshotNotFetched):
        return f"{e.why}, so this run's jobs were not downloaded.{refetch}"
    if isinstance(e, MasterUnreadable):
        saved = getattr(e, "saved_csv", None)
        n = getattr(e, "n_jobs", None)
        where = (f"The {n} job(s) were saved to {saved}." if saved
                 else "The jobs were saved to this run's file.")
        if sid:
            then = (f" Restore or fix the master, then run {_rerun_command(sid, label)} "
                    f"to add them to it without paying again.")
        else:
            then = (" Restore or fix the master from a backup; the saved file keeps the "
                    "jobs, and the dashboard's score step picks it up.")
        return (f"could not add this run's jobs to {MASTER_CSV.name} because the file "
                f"cannot be read ({_exc_text(e.__cause__ or e)}). {where}{then}")
    if isinstance(e, _NETWORK_ERRORS):
        return (f"could not reach Bright Data ({_exc_text(e)}), so the run stopped. "
                f"Check the internet connection and try again.{refetch}")
    return (f"the job search stopped unexpectedly: {_exc_text(e, 300)}. The details "
            f"are in the log above.{refetch}")


def run(snapshot_id: str | None = None, run_label: str | None = None,
        max_keywords: int | None = None, limit_per_input: int | None = None) -> int:
    """main() with the top-level handler: 0 on success; on a failure, the traceback
    to stderr (it goes to the log), then ONE final "ERROR: <reason>. <what to do>"
    line to stdout, and 1. SystemExit (missing credentials) passes through; it has
    printed its own ERROR line."""
    try:
        asyncio.run(main(snapshot_id=snapshot_id, run_label=run_label,
                         max_keywords=max_keywords, limit_per_input=limit_per_input))
    except Exception as e:  # noqa: BLE001 -- the last line of a failed run
        traceback.print_exc()
        sys.stderr.flush()
        try:
            msg = _fatal_message(e)
        except Exception:  # noqa: BLE001 -- the classifier must never hide the failure
            msg = f"the job search stopped unexpectedly: {_exc_text(e, 300)}"
        print(f"ERROR: {_one_line(msg)}", flush=True)
        return 1
    return 0


def cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Scrape LinkedIn jobs via Bright Data, or recover an already-collected snapshot."
    )
    parser.add_argument(
        "--snapshot",
        help="Recover this already-collected snapshot id; no new (billed) collection is triggered.",
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
    args = parser.parse_args(argv)
    return run(
        snapshot_id=args.snapshot,
        run_label=args.label,
        max_keywords=args.max_keywords,
        limit_per_input=args.limit,
    )


if __name__ == "__main__":
    sys.exit(cli())
