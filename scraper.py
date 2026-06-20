import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import pandas as pd

# Optional: load a local .env so credentials live outside the repo. The VM path
# sets these via run_scraper.sh exports, so a missing python-dotenv is fine.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# Bright Data credentials — supplied via environment (.env locally, exported on
# the VM). Never hardcode the token in the repo; see .env.example.
API_TOKEN = os.environ.get("BRIGHT_DATA_API_TOKEN", "")
DATASET_ID = os.environ.get("BRIGHT_DATA_DATASET_ID", "")
if not API_TOKEN or not DATASET_ID:
    sys.exit(
        "Missing Bright Data credentials. Set BRIGHT_DATA_API_TOKEN and "
        "BRIGHT_DATA_DATASET_ID in your environment or a local .env file "
        "(see .env.example)."
    )
LIMIT_PER_INPUT = 100
POLL_INTERVAL = 10
MAX_WAIT_MINUTES = 30
MAX_POLL_FAILURES = 5
# Bright Data bills at collection, so every re-collected posting costs money.
# Exclude every master id scraped within this window (still-live postings).
EXCLUDE_WINDOW_DAYS = 14

OUTPUT_DIR = Path(__file__).parent
PREVIOUS_IDS_FILE = OUTPUT_DIR / "last_run_job_ids.json"
MASTER_CSV = OUTPUT_DIR / "linkedin_jobs_master.csv"

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


def get_run_label() -> str:
    return "morning" if datetime.now().hour < 14 else "evening"


def load_previous_ids() -> list[str]:
    if not PREVIOUS_IDS_FILE.exists():
        return []
    with open(PREVIOUS_IDS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_exclude_ids() -> list[str]:
    """All master ids scraped in the last EXCLUDE_WINDOW_DAYS (plus legacy rows
    without an extracted_date). Excluding only the previous run's ids meant any
    still-live posting from >= 2 runs ago was re-collected — and re-billed —
    every time it matched a keyword. Falls back to the last-run JSON if the
    master is missing/unreadable."""
    if MASTER_CSV.exists():
        try:
            df = pd.read_csv(
                MASTER_CSV,
                usecols=lambda c: c in ("job_posting_id", "extracted_date"),
                dtype=str,
            )
            if "job_posting_id" in df.columns and not df.empty:
                if "extracted_date" in df.columns:
                    cutoff = (datetime.now() - timedelta(days=EXCLUDE_WINDOW_DAYS)).strftime("%Y-%m-%d")
                    dates = df["extracted_date"].fillna("")
                    df = df[(dates == "") | (dates >= cutoff)]
                ids = df["job_posting_id"].dropna().astype(str).unique().tolist()
                if ids:
                    return ids
        except (OSError, ValueError, pd.errors.ParserError) as e:
            print(f"Could not read master for exclusions ({e}); using last-run ids")
    return load_previous_ids()


def save_current_ids(ids: list[str]) -> None:
    with open(PREVIOUS_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(ids, f)


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


def append_to_master(df: pd.DataFrame) -> int:
    if MASTER_CSV.exists():
        existing = pd.read_csv(MASTER_CSV, dtype={"job_posting_id": str})
        combined = pd.concat([existing, df], ignore_index=True)
    else:
        combined = df
    if "job_posting_id" in combined.columns:
        # Cast before deduping: int64 ids from a re-read master never match the
        # fresh run's string ids, silently keeping duplicates.
        combined["job_posting_id"] = combined["job_posting_id"].astype(str)
        combined = combined.drop_duplicates(subset=["job_posting_id"], keep="first")
    combined = drop_blocklisted_companies(combined)
    combined.to_csv(MASTER_CSV, index=False, encoding="utf-8")
    return len(combined)


def build_inputs(exclude_ids: list[str], max_keywords: int | None = None) -> list[dict]:
    """One search input per (keyword x remote type).

    `max_keywords` caps how many keywords are used (the first N) — a spend guard
    for verification runs so a single scrape can't fan out to every keyword.
    None (the default, used by the VM cron) keeps the full keyword list.
    """
    keywords = KEYWORDS if max_keywords is None else KEYWORDS[:max_keywords]
    return [
        {**BASE_FILTERS, "keyword": kw, "remote": remote, "jobs_to_not_include": exclude_ids}
        for kw in keywords
        for remote in REMOTE_TYPES
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
               limit_per_input: int = LIMIT_PER_INPUT) -> None:
    run_label = run_label or get_run_label()

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        if snapshot_id is None:
            # Normal path: trigger a fresh (billed) collection and wait for it.
            exclude_ids = load_exclude_ids()
            print(f"Run: {run_label} | Excluding {len(exclude_ids)} recently-seen job IDs")
            inputs = build_inputs(exclude_ids, max_keywords=max_keywords)
            payload = {"input": inputs}
            kw_used = len(KEYWORDS) if max_keywords is None else min(max_keywords, len(KEYWORDS))
            print(f"Triggering {len(inputs)} searches ({kw_used} keywords x {len(REMOTE_TYPES)} remote types), "
                  f"limit_per_input={limit_per_input} -> up to {len(inputs) * limit_per_input} postings")
            snapshot_id = await trigger(session, payload, limit_per_input=limit_per_input)
            print(f"Snapshot: {snapshot_id}")
            await wait_until_ready(session, snapshot_id)
        else:
            # Recovery path: re-download an already-collected snapshot (e.g. one
            # whose run aborted after billing). No trigger -> no extra cost.
            print(f"Run: {run_label} | Recovering already-collected snapshot {snapshot_id} (no new trigger/billing)")
        results = await download(session, snapshot_id)

    if not isinstance(results, list):
        print(f"Unexpected response shape: {results}")
        sys.exit(1)

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
        choices=("morning", "evening"),
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
        default=LIMIT_PER_INPUT,
        help=f"Spend guard: max postings collected per search (default: {LIMIT_PER_INPUT}).",
    )
    args = parser.parse_args()
    asyncio.run(main(
        snapshot_id=args.snapshot,
        run_label=args.label,
        max_keywords=args.max_keywords,
        limit_per_input=args.limit,
    ))
