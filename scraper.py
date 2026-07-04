import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import aiohttp
import pandas as pd

from run_labels import RUN_LABELS, label_for_hour

# Optional: load a local .env so credentials live outside the repo. The VM path
# sets these via run_scraper.sh exports, so a missing python-dotenv is fine.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
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

OUTPUT_DIR = Path(__file__).parent
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
            for line in BLOCKLIST_FILE.read_text(encoding="utf-8").splitlines():
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
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data
        except (OSError, ValueError) as e:
            print(f"Could not read {SEARCH_CONFIG_FILE} ({e}); using built-in defaults")
    return {
        "keywords": raw.get("keywords", KEYWORDS),
        "remote_types": raw.get("remote_types", REMOTE_TYPES),
        "limit_per_input": raw.get("limit_per_input", LIMIT_PER_INPUT),
        "location": raw.get("location", BASE_FILTERS["location"]),
        "country": raw.get("country", BASE_FILTERS["country"]),
        "time_range": raw.get("time_range", BASE_FILTERS["time_range"]),
        "job_type": raw.get("job_type", BASE_FILTERS["job_type"]),
        "experience_level": raw.get("experience_level", BASE_FILTERS["experience_level"]),
    }


def get_run_label() -> str:
    return label_for_hour(datetime.now().hour)


def load_previous_ids() -> list[str]:
    if not PREVIOUS_IDS_FILE.exists():
        return []
    try:
        with open(PREVIOUS_IDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"Could not read {PREVIOUS_IDS_FILE.name} ({e}); ignoring last-run ids")
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _master_ids() -> list[str]:
    """Every job id recorded in this host's master (or the last-run JSON if the
    master is missing/unreadable)."""
    if MASTER_CSV.exists():
        try:
            df = pd.read_csv(
                MASTER_CSV,
                usecols=lambda c: c == "job_posting_id",
                dtype=str,
            )
            if "job_posting_id" in df.columns and not df.empty:
                ids = df["job_posting_id"].dropna().astype(str).unique().tolist()
                if ids:
                    return ids
        except (OSError, ValueError, pd.errors.ParserError) as e:
            print(f"Could not read master for exclusions ({e}); using last-run ids")
    return load_previous_ids()


def load_external_exclude_ids() -> list[str]:
    """Ids pushed from another machine (EXTERNAL_EXCLUDE_FILE), [] if absent/unreadable."""
    if not EXTERNAL_EXCLUDE_FILE.exists():
        return []
    try:
        with open(EXTERNAL_EXCLUDE_FILE, "r", encoding="utf-8") as f:
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
        df = pd.read_csv(path, usecols=lambda c: c == "job_posting_id", dtype=str,
                         compression=comp)
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"Could not read extra master {path.name} for exclusions ({e}); ignoring")
        return []
    if "job_posting_id" not in df.columns or df.empty:
        return []
    return df["job_posting_id"].dropna().astype(str).unique().tolist()


def load_exclude_ids() -> list[str]:
    """Every job id this host knows to skip — a hard no-repeat guard. Bright Data
    bills per collection, so re-fetching a posting we already have is pure wasted
    spend; and a listing open long enough to fall outside any recent-only window is
    usually stale anyway, so there's no upside to re-collecting it. We exclude the
    whole master, UNIONED with the extra master named by $LINKEDIN_EXTRA_MASTER (the
    synced Drive master, so a LOCAL run skips what the VM already collected) and with
    external_exclude_ids.json (ids collected on another machine — e.g. a manual local
    run — and pushed here so a scheduled VM run skips them). Falls back to the last-run
    JSON when this host's master is missing/unreadable."""
    ids = list(_master_ids())
    seen = set(ids)
    for jid in load_extra_master_ids() + load_external_exclude_ids():
        if jid not in seen:
            ids.append(jid)
            seen.add(jid)
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
    with open(target, "w", encoding="utf-8") as f:
        json.dump(load_exclude_ids(), f)
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


def drop_blocklisted_companies(df: pd.DataFrame) -> pd.DataFrame:
    """Remove rows whose company name matches the blocklist (substring, case-insensitive)."""
    col = next((c for c in ("company_name", "company") if c in df.columns), None)
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
    new = df.copy()
    if "job_posting_id" in new.columns:
        new["job_posting_id"] = new["job_posting_id"].astype(str)
        new = new.drop_duplicates(subset=["job_posting_id"], keep="first")
    new = drop_blocklisted_companies(new)

    if not MASTER_CSV.exists():
        _atomic_to_csv(new, MASTER_CSV)
        return len(new)

    # Validate readability up front so a corrupt-but-present master still raises
    # loudly (never silently treated as empty — same contract as before). The
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
                               dir=str(MASTER_CSV.parent)); os.close(fd)
    total = 0; wrote_header = False
    try:
        for chunk in pd.read_csv(MASTER_CSV, dtype={"job_posting_id": str}, chunksize=CHUNK):
            chunk = drop_blocklisted_companies(chunk)             # retroactive re-filter, preserved
            chunk = chunk.reindex(columns=unified)
            chunk.to_csv(tmp, mode="a", header=not wrote_header, index=False, encoding="utf-8")
            wrote_header = True; total += len(chunk)
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
            try: os.unlink(tmp)
            except OSError: pass
    return total


def build_inputs(exclude_ids: list[str], max_keywords: int | None = None) -> list[dict]:
    """One search input per (keyword x remote type).

    Keywords, remote types, and the base filters come from load_search_config()
    (which falls back to the module constants when no search_config.json exists).
    `max_keywords` caps how many keywords are used (the first N) — a spend guard
    for verification runs so a single scrape can't fan out to every keyword.
    None (the default, used by the VM cron) keeps the full keyword list.
    """
    cfg = load_search_config()
    keywords = cfg["keywords"] if max_keywords is None else cfg["keywords"][:max_keywords]
    remote_types = cfg["remote_types"]
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


async def trigger(session: aiohttp.ClientSession, payload: dict,
                  limit_per_input: int = LIMIT_PER_INPUT) -> str:
    url = (
        "https://api.brightdata.com/datasets/v3/scrape"
        f"?dataset_id={DATASET_ID}"
        f"&type=discover_new&discover_by=keyword&limit_per_input={limit_per_input}"
    )
    async with session.post(url, json=payload) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"Trigger failed {resp.status}: {body}")
        return (await resp.json())["snapshot_id"]


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
                raise RuntimeError(f"Progress polling failed {failures}x in a row: {e}")
            print(f"  poll error ({e}); retrying ({failures}/{MAX_POLL_FAILURES})")
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(f"Timeout after {MAX_WAIT_MINUTES} min while polling")
            await asyncio.sleep(POLL_INTERVAL)
            continue
        status = data.get("status")
        print(f"  status: {status}")
        if status == "ready":
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
                raise RuntimeError(f"Snapshot download failed {failures}x in a row: {e}")
            print(f"  download error ({e}); retrying ({failures}/{MAX_POLL_FAILURES})")
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(f"Timeout after {MAX_WAIT_MINUTES} min downloading snapshot")
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
            exclude_ids = load_exclude_ids()
            print(f"Run: {run_label} | Excluding {len(exclude_ids)} already-scraped job IDs")
            inputs = build_inputs(exclude_ids, max_keywords=max_keywords)
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

    df.to_csv(csv_path, index=False, encoding="utf-8")

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
