"""Toolkit-agnostic "add a job by hand" pipeline (no scraper, no Bright Data).

The dashboard's manual-entry form (qt/manual_add_dialog.py) collects a job URL,
title, company and pasted description, then hands the input here. This module
is pure Python with no Qt dependency, so the widget stays a thin shell and the
logic is unit-testable.

The user already chose this job, so there is no scoring step (SP5/MA-1): it is
saved and tailored at once.

    parse  -> build a job record (master-CSV schema, source="manual")
    fetch  -> optional free HTTP GET for a URL with no pasted JD (NEVER Bright Data)
    tailor -> the existing résumé engine (resume_tailor.run.tailor)
    append -> jobsdata.append_manual_job -> the master CSV (same dedup as scraped)

`find_duplicate` runs the id check before any of the above, so re-adding the
same posting never spends a fresh tailor call (MA-2); `retailor_existing` is
the "Tailor again" path it offers instead.

Every LLM/HTTP touch point is behind an injectable seam (``tailor_fn``,
``fetch_fn``) so tests mock them exactly the way the existing suite mocks the
tailor, and a real run uses the user's normal setup.
"""
from __future__ import annotations

import hashlib
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
# pipeline/ holds the flat pipeline modules (score_jobs, keypool, run_labels …);
# they are imported by bare name so the same files also run standalone on the VM.
for _p in (str(HERE), str(REPO_ROOT / "pipeline")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# A manually-added job gets a deterministic synthetic id so re-adding the same JD
# de-dupes against itself (and never collides with a real numeric LinkedIn id).
_MANUAL_ID_PREFIX = "manual-"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    """HTML -> plain text (descriptions pasted from a posting are often markup)."""
    if not isinstance(text, str):
        return ""
    if "<" in text and ">" in text:
        text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def manual_job_id(jd_text: str, url: str = "") -> str:
    """A stable id for a manual job: hash of the URL if given, else the JD text.

    Deterministic so the same paste/URL re-added later collides with itself on the
    master's job_posting_id dedup instead of piling up duplicates.
    """
    seed = (url.strip() or _strip_html(jd_text))[:4000]
    digest = hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{_MANUAL_ID_PREFIX}{digest}"


def is_manual_id(job_posting_id: Any) -> bool:
    return isinstance(job_posting_id, str) and job_posting_id.startswith(_MANUAL_ID_PREFIX)


def _guess_title_company(jd_text: str) -> tuple[str, str]:
    """Best-effort title/company from the first lines of a pasted JD.

    Cheap heuristic only (no LLM): the first non-empty line is treated as the
    title, the second as the company. The form lets the user override both, so
    this just spares them typing for a clean copy-paste. Never raises.
    """
    # _strip_html collapses all whitespace, so line structure only survives in
    # the RAW text (the old stripped-then-split branch was dead — audit P2-3).
    lines = [_strip_html(ln) for ln in str(jd_text or "").splitlines()
             if _strip_html(ln)]
    title = lines[0][:120] if lines else ""
    company = lines[1][:120] if len(lines) > 1 else ""
    return title, company


def _pasted_description(jd_text: str = "", fetched_text: str = "") -> str:
    """The exact value `build_job_record` stores in `job_description_formatted`:
    the pasted text trimmed only at the ends (embedded newlines intact), or the
    fetched page text when nothing was pasted. Only `job_summary` gets the
    fuller `_strip_html` whitespace-collapse -- `job_description_formatted`
    does not. Pulled out so a second caller (retailor_existing's prune-blanked
    backfill) can reuse the identical expression instead of a copy that can
    drift from this one, which is exactly how that backfill first shipped
    wrong (it used `_strip_html`, flattening every pasted paragraph)."""
    return (jd_text or "").strip() or (fetched_text or "").strip()


def build_job_record(
    *,
    jd_text: str = "",
    url: str = "",
    company: str = "",
    title: str = "",
    fetched_text: str = "",
) -> Dict[str, str]:
    """Assemble a master-CSV-shaped job record from manual input.

    ``jd_text`` is the pasted description (wins for the JD); ``fetched_text`` is the
    optional free-GET page text used only when nothing was pasted. The returned
    dict carries the keys the scorer (job_description_formatted / job_summary) and
    the tailor (company_name / job_title / url) read, plus source="manual" and a
    deterministic job_posting_id. Raises ValueError when there is no usable JD.
    """
    description = _pasted_description(jd_text, fetched_text)
    plain = _strip_html(description)
    if len(plain) < 40:
        raise ValueError(
            "No usable job description. Paste the job text (a URL fetch is optional "
            "and may be blocked by the site).")

    guess_title, guess_company = _guess_title_company(jd_text or fetched_text)
    title = (title or "").strip() or guess_title or "Role"
    company = (company or "").strip() or guess_company or "Unknown Company"
    url = (url or "").strip()
    jid = manual_job_id(description, url)
    today = date.today().isoformat()

    return {
        "job_posting_id": jid,
        "url": url,
        "job_title": title,
        "company_name": company,
        "job_location": "",
        # Both keys the downstream code reads for the JD: the scorer prefers
        # job_description_formatted, the tailor's _job_description_text does too.
        "job_summary": plain[:1000],
        "job_description_formatted": description,
        "run_label": "manual",
        "extracted_date": today,
        "job_posted_date": today,
        "source": "manual",
        "is_seen": "no",
    }


# ── optional free URL fetch (NEVER Bright Data; best-effort) ───────────────────

# SSRF + robustness guards for the free GET (audit P2-16): the URL is pasted user
# input, so it must not reach loopback/private/link-local/metadata hosts (blind
# SSRF), redirects must be re-validated against the same blocklist (an external
# URL can 302 internal), and the body is streamed with a byte cap so a huge page
# can't balloon the dashboard's memory.
_MAX_FETCH_BYTES = 5 * 1024 * 1024
_MAX_REDIRECTS = 3


def _host_is_private(host: str) -> bool:
    """True when `host` resolves ONLY-or-at-all to a non-public address (private,
    loopback, link-local incl. 169.254.169.254, reserved, multicast, unspecified)
    or cannot be resolved — fail closed."""
    import ipaddress
    import socket
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]))
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def _url_allowed(url: str) -> bool:
    from urllib.parse import urlparse
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    return not _host_is_private(urlparse(url).hostname or "")


def _read_capped(resp) -> str:
    """The response body, streamed up to _MAX_FETCH_BYTES (never unbounded)."""
    chunks: list[str] = []
    total = 0
    iterator = getattr(resp, "iter_content", None)
    if iterator is None:      # plain stub/legacy response — truncate its text
        return (resp.text or "")[:_MAX_FETCH_BYTES]
    for chunk in iterator(chunk_size=65536, decode_unicode=True):
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", "replace")
        chunks.append(chunk)
        total += len(chunk)
        if total >= _MAX_FETCH_BYTES:
            break
    return "".join(chunks)[:_MAX_FETCH_BYTES]


def fetch_url_text(url: str, *, timeout: float = 10.0) -> str:
    """A single lightweight, free HTTP GET of a page's visible text, or "".

    This is the ONLY network path here and it is strictly optional: a job site that
    blocks scraping (most do) just yields "" and the caller falls back to requiring
    a pasted JD. It never uses the paid Bright Data scraper. Any failure (no
    requests lib, network error, non-2xx, blocked host, tiny body) returns "" —
    never raises. Private/metadata hosts are refused, each redirect target is
    re-validated, and the body is streamed with a byte cap (audit P2-16).
    """
    url = (url or "").strip()
    if not _url_allowed(url):
        return ""
    try:
        import requests
    except ImportError:
        return ""
    try:
        body = ""
        for _hop in range(_MAX_REDIRECTS + 1):
            resp = requests.get(
                url, timeout=timeout, stream=True, allow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0 (INployed manual-add)"},
            )
            # stream=True keeps the socket open until the body is consumed OR the
            # response is closed. Every early return below (redirect chain, non-2xx,
            # the byte cap tripping mid-body) used to leak the connection back to
            # the pool unread — audit C6-9. try/finally rather than `with` because
            # the tests stub the response with a plain object; close defensively.
            try:
                status = getattr(resp, "status_code", 599)
                if 300 <= status < 400:
                    target = str((getattr(resp, "headers", {}) or {}).get("Location", ""))
                    from urllib.parse import urljoin
                    url = urljoin(url, target)
                    if not _url_allowed(url):   # a redirect must not pivot internal
                        return ""
                    continue
                if status >= 300 or resp is None:
                    return ""
                body = _read_capped(resp)
            finally:
                closer = getattr(resp, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:       # noqa: BLE001 — cleanup must never mask
                        pass
            break
        else:
            return ""                       # too many redirects
    except Exception:  # noqa: BLE001 - any fetch problem is a non-fatal fallback
        return ""
    # Drop script/style blocks before stripping the remaining tags.
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    text = _strip_html(body)
    return text if len(text) >= 40 else ""


def _run_tailor(
    record: Dict[str, Any],
    tailor_opts: Dict[str, Any],
    tailor_fn: Optional[Callable[..., Path]],
    log: Callable[[str], None],
) -> Optional[Path]:
    """The tailor step shared by a fresh add and a "Tailor again" re-run.

    Best-effort (MA-4): any failure is logged and swallowed here so the
    caller's row is never lost over a tailoring error. Returns the tailored
    output folder, or None.
    """
    try:
        log(f"tailoring résumé for {record.get('job_title')} @ {record.get('company_name')}…")
        fn = tailor_fn
        if fn is None:
            from resume_tailor.run import tailor as fn  # noqa: PLW0127
        out = fn(
            record,
            cover_letter=bool(tailor_opts.get("cover_letter", False)),
            ats_report=bool(tailor_opts.get("ats_report", True)),
            prep_sheet=bool(tailor_opts.get("prep_sheet", False)),
            tone=tailor_opts.get("tone", "professional"),
            on_status=log,
        )
        return Path(out) if out else None
    except Exception as exc:  # noqa: BLE001 - tailoring is best-effort; the row is kept either way
        log(f"tailoring failed ({exc}); the job is still saved. Retry with Tailor résumé.")
        return None


# ── orchestration: parse -> (fetch) -> tailor -> append ───────────────────────

def add_manual_job(
    *,
    jd_text: str = "",
    url: str = "",
    company: str = "",
    title: str = "",
    tailor_opts: Optional[Dict[str, Any]] = None,
    tailor_fn: Optional[Callable[..., Path]] = None,
    fetch_fn: Optional[Callable[[str], str]] = None,
    master_csv: Optional[Path] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Full manual-add flow. Returns {record, resume_dir, appended}.

    record       the job record (source="manual") appended to the master;
                 never scored (SP5/MA-1) -- the user already chose this job
    resume_dir   the tailored-résumé output folder (Path), or None when
                 tailoring failed (the record is still appended -- MA-4)
    appended     True when the record landed in the master CSV (False if a dup)

    Call `find_duplicate` first: this always tailors, so running it again on a
    posting already in the master spends a fresh tailor call for no reason.

    Seams (default to the real implementations, overridden in tests):
      tailor_fn  resume_tailor.run.tailor
      fetch_fn   fetch_url_text (the free, optional URL GET)
    """
    log = on_status or (lambda _m: None)
    tailor_opts = tailor_opts or {}
    fetch_fn = fetch_fn or fetch_url_text

    fetched = ""
    if not (jd_text or "").strip() and (url or "").strip():
        log("fetching page text (free GET; optional)…")
        fetched = fetch_fn(url)
        if not fetched:
            log("fetch returned nothing; a pasted job description is required.")

    log("building job record…")
    record = build_job_record(
        jd_text=jd_text, url=url, company=company, title=title, fetched_text=fetched)

    resume_dir = _run_tailor(record, tailor_opts, tailor_fn, log)
    record["resume"] = str(resume_dir) if resume_dir else ""

    log("appending to the master jobs list…")
    import jobsdata
    appended = jobsdata.append_manual_job(record, master_csv=master_csv)

    log("done.")
    return {"record": record, "resume_dir": resume_dir, "appended": appended}


def find_duplicate(
    jd_text: str = "",
    url: str = "",
    *,
    df: Any = None,
    master_csv: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """The existing row for this job's id, or None (MA-2's duplicate check).

    Computes the same deterministic id `add_manual_job` would use and looks
    for it before any tailoring spend: first in `df` (the rows the dashboard
    already holds in memory), then in the master CSV file itself, so a row
    written by another process, or before `df` was last refreshed, still
    counts.
    """
    jid = manual_job_id(jd_text, url)
    if (df is not None and not getattr(df, "empty", True)
            and "job_posting_id" in getattr(df, "columns", ())):
        import pandas as pd
        hit = df.loc[df["job_posting_id"].astype(str) == jid]
        if not hit.empty:
            row = hit.iloc[0].to_dict()
            return {k: ("" if isinstance(v, float) and pd.isna(v) else v)
                    for k, v in row.items()}
    import jobsdata
    return jobsdata.master_row(jid, master_csv=master_csv)


def duplicate_message(dup: Dict[str, Any]) -> str:
    """MA-2's duplicate-found text: "Already added on <date> as <title> at
    <company>.", or without the date clause when the row carries none."""
    title = str(dup.get("job_title") or "this job")
    company = str(dup.get("company_name") or "this company")
    found = str(dup.get("extracted_date") or "").strip()
    lead = f"Already added on {found}" if found else "Already added"
    return f"{lead} as {title} at {company}."


def retailor_existing(
    record: Dict[str, Any],
    *,
    jd_text: str = "",
    tailor_opts: Optional[Dict[str, Any]] = None,
    tailor_fn: Optional[Callable[..., Path]] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Re-run the tailor step on an ALREADY-SAVED row (MA-2's "Tailor again").

    Never appends a new row (the record already lives in the master), so
    `appended` is always False. Shares the tailor seam and the MA-4 failure
    handling with `add_manual_job` through `_run_tailor`.

    `jd_text` is the description the user just re-pasted into the dialog. The
    stored record often comes from the dashboard's row, and a hand-added
    job's row never carries its description (the gz bridge leaves
    job_description_formatted out), so the record can carry only the
    1000-char job_summary even though the user just pasted the full text again. When
    the stored record's job_description_formatted is blank, this fills it
    with `_pasted_description(jd_text)` -- the SAME expression a fresh add
    stores in that field (trimmed only at the ends, embedded newlines intact;
    NOT `_strip_html`, which is for job_summary only and would flatten a
    multi-paragraph paste onto one line) -- for THIS tailor run only: `record`
    is copied up front, so neither the caller's dict nor (in turn) the master
    row it came from is ever rewritten. A present, non-blank stored
    description always wins over a re-paste.
    """
    log = on_status or (lambda _m: None)
    record = dict(record)
    if jd_text.strip() and not str(record.get("job_description_formatted") or "").strip():
        record["job_description_formatted"] = _pasted_description(jd_text)
    resume_dir = _run_tailor(record, tailor_opts or {}, tailor_fn, log)
    if resume_dir:
        record["resume"] = str(resume_dir)
    log("done.")
    return {"record": record, "resume_dir": resume_dir, "appended": False}
