import ast
import asyncio
import bisect
import itertools
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
    _print_line(f"WARNING: {_one_line(msg)}")


def _print_line(text: str) -> None:
    """print() one problem line, falling back to ASCII escapes when stdout cannot
    encode it. cli() switches stdout to UTF-8, but run() can be called without it
    (and the VM's shell may hand over an ASCII stdout), and an UnicodeEncodeError
    here would lose the one line that says what went wrong."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


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
# fresh run -- keeping its id in jobs_to_not_include is pure payload. Every search
# input carries its own skip list (the known jobs its keyword can return, see
# exclude_ids_per_keyword), so a months-long history makes the Bright Data trigger
# POST grow monotonically until it hard-fails the request-size limit and silently
# kills the cron before collection.
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
#
# The budget is per search INPUT, because each input's children copy only that
# input's list. So each search carries its own list: the known jobs whose title
# holds its keyword, plus those a search for it returned before (with
# selective_search on, a search returns nothing else; see exclude_ids_per_keyword).
# Measured 2026-10-13 on the 30-day window at limit_per_input=300: one shared list
# fit about 3,350 of ~20,000 known ids (four or five days), while the per-keyword
# lists ran 130 to 3,060 ids, all inside the budget, except "Software Engineer" at
# 4,714, which keeps its newest ~3,350. Each list is capped on its own, and the
# run log gets one line naming every keyword that was trimmed.
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
    """The trigger was refused as too large (413, or a 4xx whose body is about
    size). See _too_large."""

    def __init__(self, msg: str, status: int):
        super().__init__(msg)
        self.status = status


class TriggerRefused(RuntimeError):
    """Any other refusal of the trigger POST (5xx, 429, ...)."""

    def __init__(self, msg: str, status: int, body: str):
        super().__init__(msg)
        self.status = status
        self.body = body


class TriggerInvalid(TriggerRefused):
    """A 400 whose body says nothing about size: Bright Data refused the request
    as invalid (a keyword or filter it will not take). Retrying will not help."""


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
    """append_to_master could not read the existing master."""


class MasterNotUpdated(RuntimeError):
    """Something failed AFTER the run CSV was written: the master update (an
    unreadable master, or os.replace refused because Excel has the file open) or
    the last-run ids. The jobs are safe in `saved_csv` and the score step picks
    that file up, so the ERROR line must not offer a --snapshot rerun: that would
    only save and score the same jobs again. The cause is __cause__."""

    def __init__(self, msg: str, saved_csv: str, n_jobs: int):
        super().__init__(msg)
        self.saved_csv = saved_csv
        self.n_jobs = n_jobs


class TriggerLost(RuntimeError):
    """The connection broke AFTER the trigger POST was sent (the server hung up,
    the reply was cut off, or the wait timed out). The collection may be running
    and billed, so this must not read as "no network". The cause is __cause__."""

    def __init__(self, msg: str, request_bytes: int):
        super().__init__(msg)
        self.request_bytes = request_bytes


class SnapshotRefused(RuntimeError):
    """Polling or the download got a 4xx that retrying will not fix: 404 (no
    such snapshot, often a mistyped --snapshot id) or 401/403 (the token)."""

    def __init__(self, msg: str, snapshot_id: str, status: int):
        super().__init__(msg)
        self.snapshot_id = snapshot_id
        self.status = status

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
ALL_REMOTE_TYPES = ("On-site", "Hybrid", "Remote")


def remote_filters(remote_types) -> list[str]:
    """The `remote` value of each search a keyword runs. Every type ticked (or none)
    is ONE search with no filter: Bright Data mostly ignores the filter (2026-10-13:
    a keyword's On-site and Hybrid searches returned 85% the same jobs, and each copy
    was billed), so one unfiltered search per keyword costs about a third less per job."""
    types = list(remote_types or [])
    if not types or set(ALL_REMOTE_TYPES) <= set(types):
        return [""]
    return types

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

    The cap still bounds what is sent: max_exclude_ids() (10,000 by default) and
    the byte budget trim each search's list, newest kept, and the run log says so
    when they trim. See cap_exclude_ids and _cap_per_keyword."""
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
# What the skip list reads from a master: the id, its date, and its title for the
# skip list per search keyword (see exclude_ids_per_keyword). One extra column of
# a ~100 MB master is a small cost next to reading the whole file.
_KNOWN_COLUMNS = (*_ID_DATE_COLUMNS, "job_title")
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
    """This host's master as (job_posting_id, extracted_date, job_title) rows (the
    title when the master has one), or None when it is missing, unreadable or not
    a master (the reason is printed). An empty but valid master is an empty frame."""
    if not MASTER_CSV.exists():
        return None
    try:
        df = pd.read_csv(MASTER_CSV, usecols=lambda c: c in _KNOWN_COLUMNS, dtype=str)
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
    extracted_date, job_title) rows, or None when the var is unset, the file is missing or
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
        df = pd.read_csv(path, usecols=lambda c: c in _KNOWN_COLUMNS,
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

# job_title and keywords were added for the skip list per search keyword: with
# selective_search on, a search only returns jobs whose title holds its keyword,
# so a search only needs the known jobs whose title matches it, plus the jobs
# that search is on record as returning (a margin for LinkedIn's own matching).
# keywords is the |-joined, sorted set of the normalized (_norm_text) keywords of
# every search that returned the id, unioned across runs; job_title is the newest
# non-empty title. A ledger written before these columns existed has only the
# first two; it reads as empty titles and keywords and gains the columns on the
# next write. Only the first two are required.
_LEDGER_COLUMNS = ["job_posting_id", "collected_date", "job_title", "keywords"]
_LEDGER_REQUIRED = _LEDGER_COLUMNS[:2]
_KEYWORD_SEP = "|"
_ISO_DAY = r"^\d{4}-\d{2}-\d{2}$"
_RUN_FILE_DAY = re.compile(r"^linkedin_jobs_(\d{4}-\d{2}-\d{2})_")
# Straight, curly and back quotes, written as escapes so the line stays ASCII.
_QUOTES = re.compile("[\"'`\u2018\u2019\u201c\u201d]")
_NON_ALNUM = re.compile(r"[\W_]+")


def _norm_text(value) -> str:
    """A keyword or job title reduced to what a match should compare: lowercase,
    quotes removed (a quoted "exact phrase" keyword and an apostrophe inside a
    word both vanish), every other run of non-alphanumeric characters one space,
    trimmed. None, NaN and anything that is not text come back as "".

    The ledger stores keywords this way, and the skip list per keyword compares
    titles with it, so a search for "Gen-AI Engineer" and a title saying
    "gen ai engineer" read the same."""
    if not isinstance(value, str):
        return ""
    return _NON_ALNUM.sub(" ", _QUOTES.sub("", value.lower())).strip()


def _keyword_set(value) -> set[str]:
    """A ledger keywords cell (or one keyword, or an iterable of them) as a set of
    normalized keywords, empties dropped. A cell is split on the | separator;
    each piece is normalized again, so a hand-edited cell still merges cleanly."""
    if value is None:
        return set()
    if isinstance(value, str):
        pieces = value.split(_KEYWORD_SEP)
    else:
        try:
            pieces = list(value)
        except TypeError:       # NaN or another scalar: no keyword
            return set()
    return {k for k in (_norm_text(p) for p in pieces) if k}


def _join_keywords(keywords: set[str]) -> str:
    return _KEYWORD_SEP.join(sorted(keywords))


# A keyword LinkedIn reads as a boolean search: OR / AND / NOT as whole words in
# capitals, or a parenthesis. What such a search can return is not a title match
# we can predict, so its search skips every known job (see _keyword_rule), except
# the one shape we can: `BASE NOT a NOT "b c"` (see _split_not_keyword).
_BOOLEAN_KEYWORD = re.compile(r"\b(?:OR|AND|NOT)\b|[()]")
# Straight and curly double quotes, written as escapes so the line stays ASCII.
_DOUBLE_QUOTES = "\"\u201c\u201d"


def _keyword_key(keyword) -> str:
    """The key a search keyword's skip list is filed under: the keyword itself when
    it is text. A hand-edited search_config.json can hold a number or a list
    there; that gets a key of its own (its type and repr), so it still gets a
    list and nothing raises before the trigger."""
    if isinstance(keyword, str):
        return keyword
    return f"<{type(keyword).__name__} {keyword!r}>"


# A NOT keyword, `BASE NOT a NOT "b c"` (see _split_not_keyword): the base, then
# one or more " NOT term", where a term is one double-quoted phrase or one bare
# word (no spaces, parentheses or double quotes). The base is lazy, so the terms
# take every trailing NOT; NOT is case-sensitive, LinkedIn's capital operator.
_QUOTED_PHRASE = f"[{_DOUBLE_QUOTES}][^{_DOUBLE_QUOTES}]*[{_DOUBLE_QUOTES}]"
_NOT_TERM = re.compile(rf"\s+NOT\s+({_QUOTED_PHRASE}|[^\s(){_DOUBLE_QUOTES}]+)")
_NOT_KEYWORD = re.compile(rf"(?P<base>.*?)(?P<terms>(?:{_NOT_TERM.pattern})+)", re.DOTALL)
_OPERATORS = frozenset({"OR", "AND", "NOT"})


def _split_not_keyword(keyword) -> tuple[str, list[str]] | None:
    """A NOT keyword split into (its base, its banned terms), or None for every
    other keyword: plain ones (no NOT to split) and every other boolean shape.

    The shape is `BASE NOT a NOT "b c"`, the one the experience level setting
    builds: LinkedIn's NOT reads TITLES only, so `"Data Engineer" NOT Senior`
    returns the "Data Engineer" jobs whose title has no Senior in it, which is a
    title rule we can predict (title_matches, exclude_ids_per_keyword). BASE has
    no OR, AND, NOT or parenthesis; it is a quoted phrase or unquoted words, the
    same as a plain keyword, and one wrapping pair of parentheses around it is
    allowed and dropped ("(Data Scientist) NOT Lead"). Each term is one bare
    word or one double-quoted phrase. NOT must be the capital operator: a
    lower-case "not" is one more word of a plain search, as LinkedIn reads it.

    The base comes back as written (trimmed, the parentheses gone), so
    _keyword_rule still tells a quoted phrase from unquoted words; the terms come
    back normalized (_norm_text), in order: "Sr." is "sr", "Tech Lead" is "tech
    lead". Anything else (an OR or AND anywhere, a term in parentheses, an
    unclosed quote, a base or a term that normalizes to nothing, a lone
    operator as a term) is None, and its search keeps the whole known-id list,
    as every boolean keyword did before."""
    if not isinstance(keyword, str):
        return None
    match = _NOT_KEYWORD.fullmatch(keyword.strip())
    if match is None:
        return None
    base = match["base"].strip()
    if base.startswith("(") and base.endswith(")"):
        base = base[1:-1].strip()
    if _BOOLEAN_KEYWORD.search(base) or not _norm_text(base):
        return None
    terms = _NOT_TERM.findall(match["terms"])
    banned = [_norm_text(t) for t in terms]
    if any(t in _OPERATORS for t in terms) or not all(banned):
        return None
    return base, banned


def _keyword_rule(keyword) -> tuple[str, list[str] | None, list[str]] | None:
    """How a search keyword matches titles, or None when its search must skip
    every known job: a keyword that is not text, blank, or boolean (OR, AND,
    parentheses, a NOT outside the NOT-keyword shape), none of which
    title_matches can predict.

    Otherwise (the keyword normalized with its spaces removed, its words or None,
    its banned terms). The words are given for an UNQUOTED keyword of two or more
    words: LinkedIn searches those as separate words (Settings tells the user to
    quote a phrase), so a title holding every word, in any order, can come back.
    A keyword wrapped in one pair of double quotes is a phrase and has no words
    rule. A NOT keyword (_split_not_keyword) takes the first two from its base,
    and its banned terms (normalized, possibly several words each) are what its
    title must not hold as whole words; a plain keyword has none."""
    split = _split_not_keyword(keyword)
    keyword, banned = split if split is not None else (keyword, [])
    if not isinstance(keyword, str) or _BOOLEAN_KEYWORD.search(keyword):
        return None
    norm = _norm_text(keyword)
    if not norm:
        return None
    raw = keyword.strip()
    quoted = (len(raw) >= 2 and raw[0] in _DOUBLE_QUOTES and raw[-1] in _DOUBLE_QUOTES
              and not any(c in _DOUBLE_QUOTES for c in raw[1:-1]))
    words = norm.split(" ")
    return norm.replace(" ", ""), (None if quoted or len(words) < 2 else words), banned


def _has_banned_word(text: str, banned: list[str]) -> bool:
    """Whether a NORMALIZED title holds any banned term as whole word(s): both are
    padded with a space, so "lead" bans "lead engineer" but not "leadership", and
    "tech lead" bans "senior tech lead" but not "tech team lead"."""
    padded = f" {text} "
    return any(f" {term} " in padded for term in banned)


def title_matches(keyword, title) -> bool:
    """Whether a search for `keyword` can return a job titled `title`, the rule
    the skip list per keyword uses to decide which known jobs a search needs.

    Both sides go through _norm_text, then the keyword must appear in the title as
    a plain SUBSTRING once every space is removed from both, so "GenAI" and
    "Gen AI" find each other (a substring with the spaces in stays one without
    them). No word boundaries on purpose: "quant" has to match "quantitative" and
    "llm" "llms". Measured on 5,440 real (job, keyword) pairs from ten snapshots,
    this missed none; a whole-word rule missed 320. A looser match only puts more
    ids on a list, which is the safe side.

    An unquoted keyword of two or more words also matches a title that holds
    every one of its words, each as a substring, in any order ("Data Scientist"
    unquoted matches "Scientist, Data Platform"). A keyword with no title rule
    (blank, boolean, not text; see _keyword_rule) matches every title. An empty
    title matches no keyword (exclude_ids_per_keyword puts an id with no title
    and no recorded keyword on every list instead).

    A NOT keyword (`"Data Engineer" NOT Senior NOT Sr`, see _split_not_keyword)
    matches a title its base matches, unless the title holds a banned term as
    WHOLE word(s) after _norm_text: "Sr. Data Engineer" is out ("sr"), but
    "Data Engineer, Leadership" stays for NOT Lead and "Seniors" for NOT Senior.
    Whole words, unlike the base's substring rule, because here the strict side
    is the safe one: an id left off a list that LinkedIn would still return is
    collected and billed again, and LinkedIn's own NOT is not a substring ban."""
    rule = _keyword_rule(keyword)
    if rule is None:
        return True
    phrase, words, banned = rule
    text = _norm_text(title)
    if _has_banned_word(text, banned):
        return False
    if phrase in text.replace(" ", ""):
        return True
    return bool(words) and all(w in text for w in words)


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
    """One ledger row per id from `frames`, oldest first by its newest
    collected_date. A row without a YYYY-MM-DD date is dropped: an undated ledger
    row says nothing.

    The id's job_title is the title on its newest dated row that HAS one (on a
    date tie, the later frame wins, so pass the older data first): a later run
    whose row came back without a title must not erase the one already known.
    Its keywords are the union over every row, older ones too, because a search
    that returned the job once is on record as a search that can return it. A
    frame without the title or keywords column (a ledger written before they
    existed, a run file) counts as empty there."""
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=_LEDGER_COLUMNS, dtype=str)
    df = pd.concat([f.reindex(columns=_LEDGER_COLUMNS) for f in frames],
                   ignore_index=True)
    df = df.dropna(subset=_LEDGER_REQUIRED).fillna("").astype(str)
    df = df[df["collected_date"].str.match(_ISO_DAY)]
    out = df.groupby("job_posting_id", as_index=False, sort=False)["collected_date"].max()
    titled = df.assign(job_title=df["job_title"].str.strip())
    titled = titled[titled["job_title"] != ""].sort_values("collected_date", kind="stable")
    titles = titled.groupby("job_posting_id", sort=False)["job_title"].last()
    keyed = df[df["keywords"].str.strip() != ""]
    keywords = keyed.groupby("job_posting_id", sort=False)["keywords"].agg(
        lambda cells: _join_keywords(set().union(*map(_keyword_set, cells))))
    out["job_title"] = out["job_posting_id"].map(titles).fillna("")
    out["keywords"] = out["job_posting_id"].map(keywords).fillna("")
    return out.sort_values("collected_date", kind="stable").reset_index(drop=True)


def _read_ledger() -> pd.DataFrame:
    """LEDGER_CSV as one row per id. Raises OSError/ValueError when the file cannot
    be read or is not a ledger; the callers decide what that costs."""
    df = pd.read_csv(LEDGER_CSV, dtype=str, keep_default_na=False)
    missing = [c for c in _LEDGER_REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"it has no {' or '.join(missing)} column")
    return _newest_per_id([df])


def seed_ledger_from_run_files() -> pd.DataFrame:
    """A ledger rebuilt from the run files under the run-label folders: every
    linkedin_jobs_<date>_<label>*.csv and *_scored.csv.gz, each id with the newest
    date it was collected (the row's extracted_date, else the date in the file
    name) and its newest title. Files older than the ledger keeps
    (_ledger_keep_days) by name are skipped unread. A run file has no keyword
    column (the discovery_input columns are dropped before it is written), so a
    seeded row has no keywords; the skip list per keyword then goes by its title.

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
                df = pd.read_csv(path, dtype=str, usecols=lambda c: (
                    c in _ID_DATE_COLUMNS or c == "job_title"))
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
            frames.append(pd.DataFrame({
                "job_posting_id": df["job_posting_id"], "collected_date": days,
                "job_title": df.get("job_title",
                                    pd.Series("", index=df.index, dtype=object))}))
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
    """The ledger as (job_posting_id, extracted_date, job_title, keywords) rows,
    one per id, oldest first: seeded from the run files (and saved) when the file
    does not exist yet, and from the run files with a WARNING when it cannot be
    read. Never raises.

    load_exclude_ids reads only the id and the date (_parse_dates ignores the
    rest); job_title and keywords (|-joined normalized keywords, "" when none is
    known) are there for the skip list per search keyword."""
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


def record_collected_ids(ids: list[str], day: str,
                         titles: dict[str, str] | None = None,
                         keywords: dict[str, set[str]] | None = None) -> None:
    """Add this run's ids to the ledger with `day` (YYYY-MM-DD): every id Bright
    Data returned, known and new alike, because a known job collected again is the
    one whose master date is wrong. Keeps each id's newest date, prunes rows older
    than _ledger_keep_days(), writes atomically.

    `titles` (id -> job title) and `keywords` (id -> the keywords of the searches
    that returned it this run) are optional; an id missing from either records no
    title or no keyword this time and keeps what the ledger already knows (see
    _newest_per_id). Keywords are stored normalized (_norm_text).

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
        ids = [str(i) for i in ids]
        titles, keywords = titles or {}, keywords or {}
        run = pd.DataFrame({
            "job_posting_id": ids,
            "collected_date": day,
            "job_title": [t.strip() if isinstance(t := titles.get(i), str) else ""
                          for i in ids],
            "keywords": [_join_keywords(_keyword_set(keywords.get(i))) for i in ids],
        }, columns=_LEDGER_COLUMNS)
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
    return _load_known()[0]


_DRIVE_SOURCE = "the Google Drive master"


def _known_sources() -> list[tuple[str, pd.DataFrame | None]]:
    """(name, frame) for every source of known job ids, in the order
    load_exclude_ids combines them (Drive, external, ours, the ledger): the order
    undated ids keep at the front of the list. A source that is absent or
    unreadable is None or an empty frame (the readers say why)."""
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
    return [
        (_DRIVE_SOURCE, _extra_master_frame()),
        (EXTERNAL_EXCLUDE_FILE.name,
         pd.DataFrame({"job_posting_id": load_external_exclude_ids()}, dtype=str)),
        (MASTER_CSV.name, own),
        (LEDGER_CSV.name, _ledger_frame()),
    ]


def _load_known() -> tuple[list[str], list[tuple[str, pd.DataFrame | None]]]:
    """(the windowed ids, oldest first, as load_exclude_ids documents them; the
    sources they came from, for load_known_jobs to read titles off)."""
    window = exclude_window_days()
    sources = _known_sources()
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
        return [], sources
    dated = pd.concat(parts, ignore_index=True)
    try:
        return _window_dated(dated, window), sources
    except (TypeError, ValueError) as e:
        warn(f"could not compare the dates on the skip list ({e}), so every known "
             f"job id is kept on it, however old. That bills nothing again by itself, "
             f"but the list is longer than it needs to be and the cap may leave "
             f"recent jobs off it. Report this if it repeats.")
        return dated["job_posting_id"].unique().tolist(), sources


_KNOWN_JOB_COLUMNS = ["job_posting_id", "job_title", "keywords"]


def _titles_of(df: pd.DataFrame | None) -> pd.Series:
    """id -> its title in one source (the last non-empty one, which in a master is
    the newest row), as a Series indexed by unique id. Empty when the source has
    no job_title column."""
    if (df is None or df.empty or "job_title" not in df.columns
            or "job_posting_id" not in df.columns):
        return pd.Series(dtype=object)
    t = df[["job_posting_id", "job_title"]].dropna()
    t = pd.DataFrame({"job_posting_id": t["job_posting_id"].astype(str),
                      "job_title": t["job_title"].astype(str).str.strip()})
    t = t[t["job_title"] != ""].drop_duplicates("job_posting_id", keep="last")
    return t.set_index("job_posting_id")["job_title"]


def load_known_jobs() -> pd.DataFrame:
    """The ids load_exclude_ids returns, in the same order (oldest first by the
    newest date any source knows), with each id's job_title and its ledger
    keywords: the input to the skip list per search keyword (see
    exclude_ids_per_keyword and build_inputs).

    job_title comes from whichever source has one, the ledger first (it holds the
    newest title), then this host's master, then the Google Drive master; "" when
    none does (the external file and the last-run JSON carry no titles).
    keywords is the ledger's |-joined, normalized keywords of the searches that
    returned the id ("" when none is on record). Never raises more than
    load_exclude_ids does."""
    ids, sources = _load_known()
    if not ids:
        return pd.DataFrame(columns=_KNOWN_JOB_COLUMNS, dtype=object)
    frames = dict(sources)
    id_series = pd.Series(ids, dtype=object)
    titles = pd.Series("", index=id_series.index, dtype=object)
    for name in (LEDGER_CSV.name, MASTER_CSV.name, _DRIVE_SOURCE):
        found = id_series.map(_titles_of(frames.get(name)))
        titles = titles.where(titles != "", found.fillna(""))
    ledger = frames.get(LEDGER_CSV.name)
    keywords = pd.Series("", index=id_series.index, dtype=object)
    if ledger is not None and not ledger.empty and "keywords" in ledger.columns:
        led = ledger[["job_posting_id", "keywords"]].dropna(subset=["job_posting_id"])
        led = led.assign(job_posting_id=led["job_posting_id"].astype(str))
        cells = led.drop_duplicates("job_posting_id", keep="last").set_index(
            "job_posting_id")["keywords"]
        keywords = id_series.map(cells).map(
            lambda c: _join_keywords(_keyword_set(c)) if isinstance(c, str) else "")
    return pd.DataFrame({"job_posting_id": id_series, "job_title": titles,
                         "keywords": keywords}, columns=_KNOWN_JOB_COLUMNS)


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
            f"This run's rows are still saved to the run CSV, which the score step "
            f"picks up; fix or restore {MASTER_CSV} and the next run adds to it."
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


class _Trim(NamedTuple):
    """What _cap_ids did to one list: kept `kept` of `total` ids. `room` is how
    many the byte budget and max_exclude_ids allowed (it can be 0), `budget` what
    the byte budget alone allowed, `cap` max_exclude_ids(). `floor` is True when
    room fell under MIN_EXCLUDE_IDS, the one case that warns."""
    kept: int
    total: int
    room: int
    budget: int
    cap: int
    floor: bool


def _cap_ids(exclude_ids: list[str], limit: int) -> tuple[list[str], _Trim | None]:
    """`exclude_ids` trimmed to what one search input may carry at fan-out `limit`,
    newest kept, and what the trim did (None when the list went through whole and
    above the floor). Prints nothing: cap_exclude_ids reports its one list, and
    _cap_per_keyword one line for every keyword's list together.

    The payload is sized exactly, not from an average width: json.dumps of a
    list is "[" + ", ".join(json.dumps(id) ...) + "]", so the newest k ids take
    2 + (their widths) + 2 x (k - 1) bytes. A running sum of the widths from the
    newest end gives every k's size at once, and the budget is the largest k
    whose size x limit fits MAX_EXCLUDE_PAYLOAD_BYTES. Ids grow over time, so
    the newest (the ones kept) can be wider than the list's average; an
    estimate from the average either overflows or, rounded up to be safe,
    leaves a few hundred ids off for nothing. When the whole list fits, the
    budget counts on past it at the widest id's width, so the floor and the
    "which limit bound" hint still read sensibly for a short list."""
    if not exclude_ids:
        return exclude_ids, None
    total = len(exclude_ids)
    allowed = MAX_EXCLUDE_PAYLOAD_BYTES // limit       # the most bytes one copy may take
    widths = [len(json.dumps(x)) for x in reversed(exclude_ids)]
    # sizes[k - 1] = the json.dumps length of the newest k ids.
    sizes = [2 + run + 2 * k for k, run in enumerate(itertools.accumulate(widths))]
    budget = bisect.bisect_right(sizes, allowed)
    if budget == total:
        budget += (allowed - sizes[-1]) // (max(widths) + 2)
    cap = max_exclude_ids()
    room = min(budget, cap)
    keep = max(1, min(room, total))
    floor = room < MIN_EXCLUDE_IDS
    if not floor and keep == total:
        return exclude_ids, None
    return exclude_ids[-keep:], _Trim(keep, total, room, budget, cap, floor)


def _floor_warning(limit: int, room: int) -> str:
    return (f"limit_per_input={limit} leaves room for only {room} skip-list ids, "
            f"under the {MIN_EXCLUDE_IDS}-id floor, so this run will collect and "
            f"bill again jobs it already has. Lower limit_per_input in "
            f"search_config.json.")


def cap_exclude_ids(exclude_ids: list[str], limit_per_input: int) -> list[str]:
    """Trim `exclude_ids` to what Bright Data will actually accept in one input.

    Each search input fans out to up to `limit_per_input` child inputs that each
    carry a copy of this array, so the budget is spent len(ids) x limit_per_input
    times over (see MAX_EXCLUDE_PAYLOAD_BYTES). Over budget, Bright Data rejects
    every child with `child_input_size_validation` and the collection returns zero
    rows without raising anything. The budget is per INPUT, since every search
    carries its own copy: this caps one list, the one every search shares when
    build_inputs is handed a plain list. main() hands it the known jobs instead,
    and each keyword's own list is capped on its own (_cap_per_keyword).

    Keeps the TAIL, which load_exclude_ids() guarantees is the newest, oldest
    first (see its docstring and _window_dated). With a time_range="Past 24 hours"
    search those are the only ids that can resurface, so trimming the head
    instead would keep precisely the ids that cannot recur.

    The per-id width is MEASURED off the ids in hand, not assumed: the
    BYTES_PER_EXCLUDE_ID constant bakes in a 10-digit LinkedIn posting id, ids are
    at 4.4e9 now, and the first 11-digit id would silently push the real payload
    past the cap with nothing noticing.

    Every id that reaches here is a known job inside the window, so a trim is a
    cost: it goes to the log with the setting that would avoid it. Not a WARNING,
    since both limits are the user's own settings; only a list squeezed under
    MIN_EXCLUDE_IDS warns.
    """
    limit = _positive_int(limit_per_input, LIMIT_PER_INPUT)
    kept, trim = _cap_ids(exclude_ids, limit)
    if trim is None:
        return kept
    if trim.floor:
        # The budget, not the floor, wins here. An exclude set below the floor
        # re-bills postings we already hold, which is bad; a payload over the cap
        # gets the collection REJECTED, which costs the whole run and still burns
        # the trigger. The floor is worth having, but not at the price of the
        # failure the cap exists to prevent -- so say plainly what to change.
        warn(_floor_warning(limit, trim.room))
        return kept
    trimmed = (f"skip list capped at {trim.kept} of {trim.total} known jobs. "
               f"The {trim.total - trim.kept} oldest are left off it, so they may be "
               f"collected and billed again.")
    # Both limits are the user's own settings (a window wider than the cap, a high
    # limit_per_input), so a trim goes to the log only: a WARNING would open the
    # dashboard's problems box on every run. Below the floor (above) still warns.
    if trim.budget > trim.cap:
        print(f"{trimmed} Raise {max_exclude_ids_where()} if Bright Data accepts "
              f"a longer list.")
    else:
        print(f"{trimmed} limit_per_input={limit} leaves room for only {trim.budget}; "
              f"lower limit_per_input in search_config.json to fit more.")
    return kept


def exclude_ids_per_keyword(known: pd.DataFrame, keywords) -> dict[str, list[str]]:
    """keyword -> the known ids its search has to skip, each list in the order of
    `known` (load_known_jobs: oldest first, so a trim keeps the newest). Filed
    under _keyword_key(keyword), which is the keyword itself when it is text.

    With selective_search on, a search only returns jobs whose title holds its
    keyword, so it only needs the known jobs it could return. An id goes on
    keyword K's list when its title matches K (title_matches), or when the ledger
    records a search for K returning it (LinkedIn matches in its own way, and a
    job it returned once it can return again; compared with spaces removed, like
    the title, so "genai" on record counts for a "Gen AI" search). An id with no
    title AND no recorded keyword goes on every list: nothing proves it
    irrelevant. A keyword with no title rule (blank, boolean, not text; see
    _keyword_rule) gets the whole list.

    A NOT keyword (`"Data Engineer" NOT Senior`, see _split_not_keyword) does all
    of that for its BASE (its title match, a recorded search for the base, the
    untitled and unrecorded ids), then drops every id whose title holds one of
    its banned terms as whole word(s), which is what LinkedIn's NOT leaves out
    (the rule title_matches follows). A recorded keyword is the base for a NOT
    search too (_keywords_by_id), so a job found by `"Data Engineer" NOT Senior`
    counts for "Data Engineer" and the other way round. An id with no title has
    no banned word, so it stays.

    Built for the real window (~38,000 ids x ~16 keywords): every title is
    normalized once, then each keyword is one plain substring scan over the titles
    with their spaces removed (plus one scan per word for an unquoted phrase, and
    one per banned term over the titles padded with a space at each end)."""
    ids = known["job_posting_id"].astype(object).astype(str)
    spaced = known["job_title"].astype(object).map(_norm_text)
    titles = spaced.str.replace(" ", "", regex=False)
    padded = " " + spaced + " "
    recorded = known["keywords"].astype(object).map(
        lambda c: _KEYWORD_SEP + _KEYWORD_SEP.join(
            sorted({k.replace(" ", "") for k in _keyword_set(c)})) + _KEYWORD_SEP)
    blank = (titles == "") & (recorded == _KEYWORD_SEP * 2)
    lists: dict[str, list[str]] = {}
    for kw in keywords:
        key = _keyword_key(kw)
        if key in lists:
            continue
        rule = _keyword_rule(kw)
        if rule is None:
            lists[key] = ids.tolist()
            continue
        phrase, words, banned = rule
        mask = (blank
                | titles.str.contains(phrase, regex=False)
                | recorded.str.contains(f"{_KEYWORD_SEP}{phrase}{_KEYWORD_SEP}",
                                        regex=False))
        if words:
            every = spaced.str.contains(words[0], regex=False)
            for word in words[1:]:
                every &= spaced.str.contains(word, regex=False)
            mask |= every
        for term in banned:
            mask &= ~padded.str.contains(f" {term} ", regex=False)
        lists[key] = ids[mask].tolist()
    return lists


def _cap_per_keyword(lists: dict[str, list[str]], limit: int) -> dict[str, list[str]]:
    """Each keyword's list capped on its own (_cap_ids), with ONE log line for the
    run summing up every trim, and at most one WARNING, when the fan-out squeezes
    the lists under MIN_EXCLUDE_IDS."""
    capped: dict[str, list[str]] = {}
    trims: list[tuple[str, _Trim]] = []
    for kw, ids in lists.items():
        capped[kw], trim = _cap_ids(ids, limit)
        if trim is not None:
            trims.append((kw, trim))
    floors = [t for _, t in trims if t.floor]
    if floors:
        warn(_floor_warning(limit, min(t.room for t in floors)))
    trims = [(kw, t) for kw, t in trims if not t.floor]
    if not trims:
        return capped
    each = ", ".join(f"{kw} kept {t.kept:,} of {t.total:,} known jobs" for kw, t in trims)
    left = sum(t.total - t.kept for _, t in trims)
    cost = (f"the {left:,} oldest may be collected and billed again" if len(trims) == 1
            else f"the oldest left off ({left:,} across these lists) may be collected "
                 f"and billed again")
    hints = []
    by_budget = [t.budget for _, t in trims if t.budget <= t.cap]
    if by_budget:
        hints.append(f"limit_per_input={limit} leaves room for {min(by_budget):,}; "
                     f"lower limit_per_input in search_config.json to fit more.")
    if len(by_budget) < len(trims):
        hints.append(f"Raise {max_exclude_ids_where()} if Bright Data accepts a "
                     f"longer list.")
    _print_line(f"skip list capped for {len(trims)} of {len(lists)} keywords: {each}; "
                f"{cost}. {' '.join(hints)}")
    return capped


def build_inputs(exclude: list[str] | pd.DataFrame, max_keywords: int | None = None,
                 limit_per_input: int | None = None) -> list[dict]:
    """One search input per (keyword x remote type); one per keyword when every
    type is wanted (see remote_filters).

    Keywords, remote types, and the base filters come from load_search_config()
    (which falls back to the module constants when no search_config.json exists).
    `max_keywords` caps how many keywords are used (the first N) — a spend guard
    for verification runs so a single scrape can't fan out to every keyword.
    None (the default, used by the VM cron) keeps the full keyword list.

    `exclude` is the known jobs from load_known_jobs (what main() passes) or a
    plain list of ids. Known jobs give each keyword its own skip list
    (exclude_ids_per_keyword), capped on its own, and every input of that keyword
    (one per remote type) carries it. A plain list is one list every search
    carries alike, capped once (cap_exclude_ids).

    `limit_per_input` is the same value that goes into the trigger URL; it is taken
    here only to size the jobs_to_not_include cap, which scales with the fan-out.
    None falls back to the config value, so the cap applies even to a caller that
    doesn't thread it through.
    """
    cfg = load_search_config()
    keywords = (cfg["keywords"] if max_keywords is None
                else cfg["keywords"][:_keyword_cap(max_keywords)])
    remote_types = remote_filters(cfg["remote_types"])
    if limit_per_input is None:
        limit_per_input = cfg["limit_per_input"]
    if isinstance(exclude, pd.DataFrame):
        limit = _positive_int(limit_per_input, LIMIT_PER_INPUT)
        per_keyword = _cap_per_keyword(exclude_ids_per_keyword(exclude, keywords), limit)
    else:
        shared = cap_exclude_ids(exclude, limit_per_input)
        per_keyword = {_keyword_key(kw): shared for kw in keywords}
    base_filters = {
        "location": cfg["location"],
        "country": cfg["country"],
        "time_range": cfg["time_range"],
        "job_type": cfg["job_type"],
        "experience_level": cfg["experience_level"],
        "selective_search": BASE_FILTERS["selective_search"],
    }
    return [
        {**base_filters, "keyword": kw, "remote": remote,
         "jobs_to_not_include": per_keyword[_keyword_key(kw)]}
        for kw in keywords
        for remote in remote_types
    ]


def _skip_list_summary(run_label: str, known: int, inputs: list[dict]) -> str:
    """The run log's "Run: <label> | ..." line: how many known jobs the window
    holds and the shortest and longest skip list a search carries."""
    days = exclude_window_days()
    if not known:
        return f"Run: {run_label} | no known jobs from the last {days} days to skip"
    sizes = [len(i["jobs_to_not_include"]) for i in inputs] or [0]
    low, high = min(sizes), max(sizes)
    span = f"{low:,}" if low == high else f"{low:,} to {high:,}"
    return (f"Run: {run_label} | {known:,} known jobs from the last {days} days; "
            f"each search skips the ones its keyword can return ({span} ids per search)")


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
            if resp.status == 400:
                raise TriggerInvalid(
                    f"{head}\nBright Data refused the request as invalid. Check the "
                    f"keywords and filters in search_config.json.", resp.status, body)
            raise TriggerRefused(head, resp.status, body)
        # text(), not json(): a collection that beat the sync window comes back
        # as NDJSON, which json() cannot read. See _parse_collection_body.
        return _parse_collection_body(await resp.text(), status=resp.status)


# Phrases that mean the request was too big. input_size also covers
# child_input_size (input_size_validation, child_input_size_validation: the codes
# Bright Data gives an oversized input). Never bare "payload" or "size": Bright
# Data calls the request body the payload ('Invalid payload: keyword'), and
# sending that user to shrink the skip list fixes nothing.
_SIZE_WORDS = re.compile(
    r"too large|input_size|size limit|payload too large|entity too large",
    re.IGNORECASE)


def _too_large(status: int, body: str) -> bool:
    """A trigger refusal that means the request was too big: 413, or a 4xx (400
    is what Bright Data has answered an oversized input with) whose body talks
    about size. A 400 that does not is a different problem (TriggerInvalid), and
    calling it too large would send the user to shrink the skip list for nothing."""
    if status == 413:
        return True
    return 400 <= status < 500 and bool(_SIZE_WORDS.search(body or ""))


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
             f"{records} job(s) from the rest. Those jobs will be saved as usual, but the "
             f"rejected searches collected nothing, so their new jobs are missed until "
             f"a later run. {fix}")
        return
    if not records and errors:
        # A plain info line, never a WARNING: this is what a quiet day (and the
        # small test run) looks like, and a WARNING opens the dashboard's
        # problems box. Without error codes there is nothing to say about
        # rejection, so it claims nothing.
        if codes:
            why = (f"({codes}). No search was rejected, so most likely nothing new "
                   f"was posted.")
        else:
            why = ("and Bright Data did not say which, so this run could not check "
                   "for rejected searches. Most likely nothing new was posted.")
        print(f"Collection {snapshot_id} returned 0 jobs with {errors} page error(s) "
              f"{why} If it happens on several runs in a row, check scraper.log and "
              f"that collection in the Bright Data dashboard.", flush=True)


# 4xx answers worth retrying: a request timeout, too early, rate limited.
_RETRYABLE_4XX = frozenset({408, 425, 429})


def _raise_if_permanent(e: BaseException, snapshot_id: str) -> None:
    """Raise SnapshotRefused when `e` is a 4xx that retrying cannot fix: 404 for a
    snapshot Bright Data does not have (a mistyped --snapshot id), 401/403 for the
    token. Retrying those 5 times only delays an answer that will not change."""
    if not isinstance(e, aiohttp.ClientResponseError):
        return
    status = e.status
    if 400 <= status < 500 and status not in _RETRYABLE_4XX:
        raise SnapshotRefused(f"Bright Data answered {status} for snapshot {snapshot_id}: "
                              f"{_exc_text(e)}", snapshot_id, status) from e


async def check_recovered_snapshot(session: aiohttp.ClientSession, snapshot_id: str) -> None:
    """A --snapshot recovery's one GET to /progress (free): the same rejection
    check a fresh run gets in wait_until_ready, so a recovered collection that
    Bright Data partly or wholly rejected says so. A 4xx that will not change
    (404, 401/403) stops the run here with a clear reason; any other failure to
    read it is a WARNING, and the download goes ahead."""
    url = f"https://api.brightdata.com/datasets/v3/progress/{snapshot_id}"
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        _raise_if_permanent(e, snapshot_id)
        warn(f"could not read the progress record of snapshot {snapshot_id} "
             f"({_exc_text(e)}), so this run could not check whether Bright Data "
             f"rejected any of its searches. Its jobs are downloaded as usual.")
        return
    if isinstance(data, dict) and data.get("status") == "ready":
        _assert_collected_something(data, snapshot_id)


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
            _raise_if_permanent(e, snapshot_id)
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
            _raise_if_permanent(e, snapshot_id)
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

    _RUN_CONTEXT.update(snapshot_id=snapshot_id, run_label=run_label)
    raw_rows: Path | None = None
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            if snapshot_id is None:
                # Normal path: trigger a fresh (billed) collection and wait for it.
                # Preflight first so a dead account costs nothing. The recovery path
                # below is deliberately NOT gated: that snapshot is already paid for,
                # and a fail-open probe must never block collecting what you own.
                await preflight(session)
                # Each search skips only the known jobs its keyword can return
                # (exclude_ids_per_keyword), so a 30-day window fits where one
                # shared list could hold only a few days at a high limit_per_input.
                known = load_known_jobs()
                inputs = build_inputs(known, max_keywords=max_keywords,
                                      limit_per_input=limit_per_input)
                _print_line(_skip_list_summary(run_label, len(known), inputs))
                payload = {"input": inputs}
                # Derived from `inputs`, not from the raw flag: build_inputs coerces
                # the flag, and a run log naming a cap the run did not apply is worse
                # than no line at all.
                filters = remote_filters(cfg["remote_types"])
                kw_used = len(inputs) // max(len(filters), 1)
                shape = ("all workplace types in one search each" if filters == [""]
                         else f"x {len(filters)} remote types")
                print(f"Triggering {len(inputs)} searches ({kw_used} keywords {shape}), "
                      f"limit_per_input={limit_per_input} -> up to {len(inputs) * limit_per_input} postings")
                collected = await _trigger_once(session, payload, limit_per_input)
                if collected.rows is not None:
                    # Finished inside Bright Data's synchronous window: the records
                    # arrived WITH the POST and no snapshot was ever created, so
                    # there is nothing to poll and nothing --snapshot could recover
                    # later. This collection is already paid for; it gets used here
                    # or it is lost, so a copy goes to disk before anything else.
                    results = collected.rows
                    print(f"Collected {len(results)} rows synchronously "
                          f"(no snapshot issued, nothing to poll)")
                    if results:
                        raw_rows = _keep_raw_rows(results, run_label)
                    else:
                        # The /progress payload is where input-rejection codes live,
                        # and this branch never fetches one, so say plainly that the
                        # rejection check did not run rather than implying a clean
                        # quiet day. An info line, not a WARNING: this is what a
                        # quiet run looks like. See _assert_collected_something.
                        print("Bright Data finished the collection at once with 0 jobs "
                              "and sent no progress record, so this run could not check "
                              "whether it rejected any searches. Most likely nothing new "
                              "was posted. If it happens on several runs in a row, check "
                              "the Bright Data dashboard.", flush=True)
                else:
                    snapshot_id = collected.snapshot_id
                    _RUN_CONTEXT["snapshot_id"] = snapshot_id
                    print(f"Snapshot: {snapshot_id}")
                    await wait_until_ready(session, snapshot_id)
                    results = await download(session, snapshot_id)
            else:
                # Recovery path: re-download an already-collected snapshot (e.g. one
                # whose run aborted after billing). No trigger -> no extra cost. The
                # one /progress read is free and gives it the rejection check a
                # fresh run gets.
                print(f"Run: {run_label} | Recovering already-collected snapshot {snapshot_id} (no new trigger/billing)")
                await check_recovered_snapshot(session, snapshot_id)
                results = await download(session, snapshot_id)

        try:
            _save_results(results, run_label)
        except BaseException as e:
            if raw_rows is not None:
                if isinstance(e, MasterNotUpdated):
                    # The run CSV holds the jobs; the ERROR line names it.
                    _drop_raw_rows(raw_rows)
                else:
                    _tag(e, raw_rows=f"{run_label}/{raw_rows.name}")
            raise
        if raw_rows is not None:
            _drop_raw_rows(raw_rows)
    except BaseException as e:  # Ctrl+C too: run() names the paid snapshot either way
        # Whatever failed, the ERROR line needs the paid snapshot (if one exists
        # by now) to tell the user how to fetch it again for free. See run().
        _tag(e, snapshot_id=snapshot_id, run_label=run_label)
        raise


# Where main() has got to, for run()'s Ctrl+C line. asyncio.run turns Ctrl+C into
# a cancellation inside main() and then raises a NEW KeyboardInterrupt, which
# carries none of the tags main() put on the cancellation.
_RUN_CONTEXT: dict = {}

# Raised before anything reached Bright Data: no collection exists, nothing billed.
_NOT_SENT_ERRORS = tuple(c for c in (
    getattr(aiohttp, "ClientConnectorError", None),
    getattr(aiohttp, "ConnectionTimeoutError", None),   # aiohttp 3.10+
    socket.gaierror,
) if c is not None)
# Raised after the POST may have gone out.
_MAYBE_SENT_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError,
                      ConnectionError)


async def _trigger_once(session: aiohttp.ClientSession, payload: dict,
                        limit_per_input: int) -> Collection:
    """trigger(), with a broken connection sorted by whether the request may have
    been sent. A failure to connect leaves nothing running ("could not reach
    Bright Data"); a server hang-up, a cut-off reply or a timeout can come after
    Bright Data took the request and started a billed collection, so that one is
    a TriggerLost that tells the user to look before running again."""
    try:
        return await trigger(session, payload, limit_per_input=limit_per_input)
    except _NOT_SENT_ERRORS:
        raise
    except _MAYBE_SENT_ERRORS as e:
        size = len(json.dumps(payload).encode("utf-8"))
        raise TriggerLost(f"Lost the connection after sending the trigger "
                          f"({size} bytes): {_exc_text(e)}", size) from e


def _keep_raw_rows(rows: list[dict], run_label: str) -> Path | None:
    """Write rows that came back with the POST to
    `<label>/linkedin_jobs_<date>_<label>_raw_<HHMMSS>.json` before saving them,
    since that collection has no snapshot to download again. A .json name: no
    run-file reader globs it (they want linkedin_jobs_*.csv or *_scored.csv*).
    main() deletes it once the save works. Never raises: a failed copy is a
    WARNING and the save goes ahead."""
    now = datetime.now()
    try:
        run_dir = OUTPUT_DIR / run_label
        run_dir.mkdir(exist_ok=True)
        path = run_dir / (f"linkedin_jobs_{now:%Y-%m-%d}_{run_label}"
                          f"_raw_{now:%H%M%S}.json")
        _atomic_write_json(path, rows)
        return path
    except Exception as e:  # noqa: BLE001 -- the save below is what matters
        warn(f"could not keep a copy of the {len(rows)} job(s) Bright Data sent back "
             f"({_exc_text(e)}). They are saved as usual next, but if that fails too "
             f"they are lost: this collection has no snapshot to download again.")
        return None


def _drop_raw_rows(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# Where a collected row says which search returned it. Bright Data echoes each
# search's input on its rows as discovery_input; json_normalize flattens a dict
# to discovery_input.keyword, but some snapshots carry the whole input in one
# column instead, as a dict or as its Python-repr / JSON text. input.* is the
# older name for the same echo.
_KEYWORD_COLUMNS = ("discovery_input.keyword", "input.keyword")
_INPUT_COLUMNS = ("discovery_input", "input")


def _input_keyword(value) -> str | None:
    """The keyword inside one discovery_input / input cell (a dict, or its
    Python-repr or JSON text), or None when the cell holds no keyword."""
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("{"):
            return None
        try:
            value = json.loads(text)
        except ValueError:
            try:
                value = ast.literal_eval(text)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                return None
    if isinstance(value, dict):
        kw = value.get("keyword")
        return kw if isinstance(kw, str) else None
    return None


def _keywords_by_id(df: pd.DataFrame) -> dict[str, set[str]]:
    """id -> the normalized keywords of every search that returned it, read from
    the raw rows of a run (BEFORE the dedupe: one job found by three searches is
    three rows, one per keyword, and the dedupe keeps only the first). Ids with
    no keyword on any row are left out.

    A NOT keyword is recorded as its BASE (_split_not_keyword), normalized: the
    banned terms only narrow which titles come back, and every job the NOT search
    returned the base search can return too, so `"Data Engineer" NOT Senior` and
    "Data Engineer" share one record (exclude_ids_per_keyword reads it for both).
    Any other keyword, boolean ones included, is recorded whole, as before."""
    if "job_posting_id" not in df.columns:
        return {}
    ids = df["job_posting_id"].astype(str)
    found: dict[str, set[str]] = {}

    def add(values) -> None:
        for jid, kw in zip(ids, values):
            split = _split_not_keyword(kw)
            norm = _norm_text(split[0] if split is not None else kw)
            if norm:
                found.setdefault(jid, set()).add(norm)

    for col in _KEYWORD_COLUMNS:
        if col in df.columns:
            add(df[col])
    for col in _INPUT_COLUMNS:
        if col in df.columns:
            add(df[col].map(_input_keyword))
    return found


def _titles_by_id(df: pd.DataFrame) -> dict[str, str]:
    """id -> its first non-empty job_title across the raw rows of a run (the row
    the dedupe keeps can be one that came back without a title)."""
    if "job_posting_id" not in df.columns or "job_title" not in df.columns:
        return {}
    titles: dict[str, str] = {}
    for jid, title in zip(df["job_posting_id"].astype(str), df["job_title"]):
        if isinstance(title, str) and title.strip():
            titles.setdefault(jid, title.strip())
    return titles


def _save_results(results: list[dict], run_label: str) -> None:
    """Write a run's rows: the run CSV, the ledger, last-run ids, the master.

    Once the run CSV is on disk the jobs are safe (the score step picks the file
    up), so any later failure is a MasterNotUpdated naming that file, never an
    offer to fetch the snapshot again."""
    df = pd.json_normalize(results)
    if df.empty:
        # Write nothing: a columnless CSV would crash the scoring step, and the
        # rest of run_scraper.sh (master upload) should still proceed.
        print("No new jobs returned this run; nothing to write.")
        return

    collected_titles: dict[str, str] = {}
    collected_keywords: dict[str, set[str]] = {}
    if "job_posting_id" in df.columns:
        before = len(df)
        df["job_posting_id"] = df["job_posting_id"].astype(str)
        # For the ledger, from every raw row before the dedupe below and before
        # drop_unneeded_columns drops discovery_input.*: the title, and the
        # keyword of each search that returned the job (the skip list per keyword
        # reads both). Best effort: a malformed row costs its keyword, not the run.
        try:
            collected_titles = _titles_by_id(df)
            collected_keywords = _keywords_by_id(df)
        except Exception as e:  # noqa: BLE001 -- the ledger dates matter more
            print(f"Could not read the search keywords on this run's rows ({e}); "
                  f"the ledger records their ids and dates without them.")
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
    ids = (set(df["job_posting_id"].astype(str)) if "job_posting_id" in df.columns
           else set())
    csv_path, earlier = _run_csv_path(run_dir, date_str, run_label, ids)

    if earlier == "scored":
        # A --snapshot rerun after a failed save: the same jobs are on disk and
        # scored already. Writing them again would only pay to score them twice.
        saved = f"{run_label}/{csv_path.stem}_scored.csv.gz"
        print(f"This run's {len(df)} jobs are the same as {saved} from an earlier "
              f"attempt today, which is already scored, so no new run file was written.")
    else:
        _atomic_to_csv(df, csv_path)  # atomic: a truncated run CSV would crash the scoring step
        saved = f"{run_label}/{csv_path.name}"
        if earlier == "raw":
            print(f"This run's {len(df)} jobs are the same as {saved} from an earlier "
                  f"attempt today, so that file was written again instead of a new one.")
    # Right after the run CSV, before the master: append_to_master can still raise
    # on an unreadable master, and these jobs are paid for either way.
    record_collected_ids(collected_ids, date_str, titles=collected_titles,
                         keywords=collected_keywords)

    try:
        if "job_posting_id" in df.columns:
            save_current_ids(df["job_posting_id"].astype(str).tolist())
        master_total = append_to_master(df)
    except Exception as e:
        raise MasterNotUpdated(f"{MASTER_CSV.name} was not updated after saving "
                               f"{saved}: {_exc_text(e)}", saved, len(df)) from e
    if earlier is None:
        note = ("" if csv_path.stem == f"linkedin_jobs_{date_str}_{run_label}" else
                f" (an earlier {run_label} run today already used the base name)")
        print(f"Saved {len(df)} jobs -> {saved}{note}")
    print(f"Master CSV now contains {master_total} unique jobs")


def _run_csv_path(run_dir: Path, date_str: str, run_label: str,
                  ids: set[str]) -> tuple[Path, str | None]:
    """Where today's run CSV goes, and whether an earlier attempt already saved
    these exact jobs: (path, None) for a new file (`_free_run_csv_path`), or the
    earlier file's raw path with "raw" (unscored: write it again) or "scored"
    (scored: write nothing).

    The match is on the job_posting_id SET, so a --snapshot rerun after a failed
    save reuses its file instead of adding a `-2` the scorer would pay to score
    again. A run with any other job set gets a new file, as before. A file that
    cannot be read never matches."""
    if ids:
        for name in _taken_run_names(run_dir, date_str, run_label):
            raw = run_dir / f"{name}.csv"
            scored = run_dir / f"{name}_scored.csv.gz"
            if _file_ids(raw if raw.exists() else scored) == ids:
                return raw, ("scored" if scored.exists() else "raw")
    return _free_run_csv_path(run_dir, date_str, run_label), None


def _taken_run_names(run_dir: Path, date_str: str, run_label: str) -> list[str]:
    """Stems of today's run files for this label (base, -2, -3, ...), raw or scored."""
    stem = f"linkedin_jobs_{date_str}_{run_label}"
    pat = re.compile(rf"^{re.escape(stem)}(-[2-9]|-[1-9]\d+)?(?:\.csv|_scored\.csv\.gz)$")
    names = {stem + (m.group(1) or "") for p in run_dir.iterdir()
             if (m := pat.match(p.name))}
    return sorted(names, key=lambda n: (len(n), n))


def _file_ids(path: Path) -> set[str] | None:
    """The job ids in a run file (raw .csv or _scored.csv.gz), None when unreadable."""
    try:
        col = pd.read_csv(path, usecols=["job_posting_id"], dtype=str)["job_posting_id"]
    except Exception:  # noqa: BLE001 -- unreadable means "not the same file"
        return None
    return set(col.dropna().astype(str))


def _free_run_csv_path(run_dir: Path, date_str: str, run_label: str) -> Path:
    """The run CSV path for today: `linkedin_jobs_<date>_<label>.csv`, or `-2`,
    `-3`, ... when an earlier same-day run of this label already took the name.

    A name counts as taken while its raw CSV OR its `_scored.csv.gz` exists: the
    scorer skips any input whose scored sibling exists, so reusing a stem whose
    raw file is gone would still leave the new jobs unscored. Overwriting it lost
    185 jobs on 2026-10-12. Every reader accepts the suffix: they glob
    `linkedin_jobs_*.csv` / `*_scored.csv.gz`, read the date from the start of the
    name, and take the label from the folder or the run_label column.

    Check-then-write, not an atomic reservation: two scrapers saving the same
    label in the same second could pick the same name. Nothing runs two at once
    (the dashboard refuses a second run while one is going, and the VM cron runs
    each label once), and reserving the name with an empty file would leave a
    columnless CSV that crashes the scorer if the write then failed."""
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
    """The plain-English reason a run failed and what to do, for the ERROR line,
    plus where rows that came back with the POST were kept, when they were."""
    msg = _fatal_reason(e)
    kept = getattr(e, "raw_rows", None)
    if kept:
        msg += (f" The jobs Bright Data sent back with the request are kept in {kept}. "
                f"Keep that file: this collection has no snapshot, so it cannot be "
                f"downloaded again.")
    return msg


def _fatal_reason(e: BaseException) -> str:
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
    if isinstance(e, TriggerInvalid):
        return (f"Bright Data refused the request as invalid ({e.status}: "
                f"{_one_line(e.body)[:200]}), so no jobs were collected. Check the "
                f"keywords and filters in Settings (search_config.json) and run again.")
    if isinstance(e, TriggerRefused):
        return (f"Bright Data refused to start the collection ({e.status}: "
                f"{_one_line(e.body)[:200]}), so no jobs were collected. This is usually "
                f"a problem on their side: try again later, and check the Bright Data "
                f"dashboard if it keeps happening.")
    if isinstance(e, TriggerLost):
        return (f"the request to start the collection ({e.request_bytes / 1_000_000:.2f} "
                f"MB) was sent, but the connection to Bright Data broke before it "
                f"answered ({_exc_text(e.__cause__ or e)}). Bright Data may have dropped "
                f"the request because it was too large, or the collection may already "
                f"be running and billed. Before running again, check the snapshot list "
                f"in the Bright Data dashboard. If this run's snapshot is there, run: "
                f"{_rerun_command('<id>', label)} to fetch its jobs without paying "
                f"again. If none is there and this keeps happening, lower "
                f"{max_exclude_ids_where()}.")
    if isinstance(e, SnapshotRefused):
        if e.status == 404:
            return (f"Bright Data has no collection with the id {e.snapshot_id} (404), so "
                    f"nothing was downloaded. Check the snapshot id in the Bright Data "
                    f"dashboard and run again.")
        if e.status in (401, 403):
            then = (f" Once the token works, run: {_rerun_command(sid, label)} to fetch "
                    f"its jobs without paying again." if sid else "")
            return (f"Bright Data refused the API token ({e.status}) while fetching "
                    f"collection {e.snapshot_id}, so its jobs were not downloaded. "
                    f"{TOKEN_HINT}{then}")
        return (f"Bright Data refused to send collection {e.snapshot_id} ({e.status}), "
                f"so its jobs were not downloaded. Check the snapshot id in the Bright "
                f"Data dashboard.{refetch}")
    if isinstance(e, CollectionFailed):
        return (f"Bright Data's collection {e.snapshot_id} failed ({e.detail}), so no "
                f"jobs were collected. Try again later; if it keeps failing, open that "
                f"collection in the Bright Data dashboard.")
    if isinstance(e, SnapshotNotFetched):
        return f"{e.why}, so this run's jobs were not downloaded.{refetch}"
    if isinstance(e, (MasterNotUpdated, MasterUnreadable)):
        # No --snapshot here: the jobs are on disk, and a rerun would only save and
        # score the same jobs again.
        cause = e.__cause__ or e
        if isinstance(cause, MasterUnreadable):
            cause = cause.__cause__ or cause
        n = getattr(e, "n_jobs", None)
        saved = getattr(e, "saved_csv", None) or "this run's file"
        jobs = f"{n} job(s)" if n is not None else "jobs"
        return (f"this run's {jobs} were saved, but {MASTER_CSV.name} was not updated "
                f"({_exc_text(cause)}). The jobs are safe in {saved}, and the score step "
                f"picks that file up (the dashboard offers to score it when it next "
                f"opens). To fix the master, close it if it is open in Excel or another "
                f"program, or restore it from a backup; the next run adds to it as usual.")
    if isinstance(e, _NETWORK_ERRORS):
        return (f"could not reach Bright Data ({_exc_text(e)}), so the run stopped. "
                f"Check the internet connection and try again.{refetch}")
    return (f"the job search stopped unexpectedly: {_exc_text(e, 300)}. The details "
            f"are in the log above.{refetch}")


def _stopped_message(e: BaseException) -> str:
    """The ERROR line for Ctrl+C. The snapshot comes from the exception's tag or,
    when asyncio raised a fresh KeyboardInterrupt, from _RUN_CONTEXT."""
    sid = getattr(e, "snapshot_id", None) or _RUN_CONTEXT.get("snapshot_id")
    label = getattr(e, "run_label", None) or _RUN_CONTEXT.get("run_label")
    msg = "the run was stopped (Ctrl+C) before it finished, so its jobs may not be saved."
    if sid:
        msg += (f" Its collection is already paid for. To fetch its jobs without paying "
                f"again, run: {_rerun_command(sid, label)}")
    return msg


def run(snapshot_id: str | None = None, run_label: str | None = None,
        max_keywords: int | None = None, limit_per_input: int | None = None) -> int:
    """main() with the top-level handler: 0 on success; on a failure, the traceback
    to stderr (it goes to the log), then ONE final "ERROR: <reason>. <what to do>"
    line to stdout, and 1 (130 for Ctrl+C). SystemExit (missing credentials) passes
    through; it has printed its own ERROR line."""
    _RUN_CONTEXT.clear()
    try:
        asyncio.run(main(snapshot_id=snapshot_id, run_label=run_label,
                         max_keywords=max_keywords, limit_per_input=limit_per_input))
    except KeyboardInterrupt as e:
        _print_line(f"ERROR: {_one_line(_stopped_message(e))}")
        return 130
    except Exception as e:  # noqa: BLE001 -- the last line of a failed run
        traceback.print_exc()
        sys.stderr.flush()
        try:
            msg = _fatal_message(e)
        except Exception:  # noqa: BLE001 -- the classifier must never hide the failure
            msg = f"the job search stopped unexpectedly: {_exc_text(e, 300)}"
        _print_line(f"ERROR: {_one_line(msg)}")
        return 1
    return 0


def _utf8_stdio() -> None:
    """Make stdout/stderr UTF-8. Under the dashboard's pipe a Windows child gets
    cp1252, and an arrow or CJK in a job title or Bright Data's answer then raised
    UnicodeEncodeError and lost the WARNING or ERROR line that carried it. The
    dashboard decodes the pipe as UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:  # noqa: BLE001 -- _print_line still falls back to ASCII
            pass


def cli(argv: list[str] | None = None) -> int:
    _utf8_stdio()
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
