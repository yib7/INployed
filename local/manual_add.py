"""Toolkit-agnostic "add a job by hand" pipeline (no scraper, no Bright Data).

The dashboard's manual-entry form (qt/manual_add_dialog.py) collects either a
pasted job description (plus optional link/company/title) or a job URL, then hands
the input here. This module is pure Python with no Qt dependency, so the widget
stays a thin shell and the logic is unit-testable.

A manually-added job flows through the SAME pipeline as a scraped one:

    parse  -> build a job record (master-CSV schema, source="manual")
    fetch  -> optional free HTTP GET for a URL with no pasted JD (NEVER Bright Data)
    score  -> the existing two-stage Gemini scorer (score_jobs.run_scoring)
    tailor -> the existing résumé engine (resume_tailor.run.tailor)
    append -> jobsdata.append_manual_job -> the master CSV (same dedup as scraped)

Every LLM/HTTP touch point is behind an injectable seam (``pool_factory``,
``tailor_fn``, ``fetch_fn``) so tests mock them exactly the way the existing suite
mocks the scorer/tailor, and a real run uses the user's normal Gemini setup.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
for _p in (str(HERE), str(REPO_ROOT)):
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
    lines = [ln.strip() for ln in _strip_html(jd_text).split("\n") if ln.strip()]
    # _strip_html collapses newlines, so fall back to splitting the raw text.
    if len(lines) < 2:
        lines = [ln.strip() for ln in str(jd_text or "").splitlines() if ln.strip()]
    title = lines[0][:120] if lines else ""
    company = lines[1][:120] if len(lines) > 1 else ""
    return title, company


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
    description = (jd_text or "").strip() or (fetched_text or "").strip()
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

def fetch_url_text(url: str, *, timeout: float = 10.0) -> str:
    """A single lightweight, free HTTP GET of a page's visible text, or "".

    This is the ONLY network path here and it is strictly optional: a job site that
    blocks scraping (most do) just yields "" and the caller falls back to requiring
    a pasted JD. It never uses the paid Bright Data scraper. Any failure (no
    requests lib, network error, non-2xx, tiny body) returns "" — never raises.
    """
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return ""
    try:
        import requests
    except ImportError:
        return ""
    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (INployed manual-add)"},
        )
        if resp.status_code >= 300:
            return ""
        body = resp.text or ""
    except Exception:  # noqa: BLE001 - any fetch problem is a non-fatal fallback
        return ""
    # Drop script/style blocks before stripping the remaining tags.
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    text = _strip_html(body)
    return text if len(text) >= 40 else ""


# ── scoring via the EXISTING two-stage pipeline ───────────────────────────────

def _default_resume() -> str:
    """The résumé text the scorer matches against (score_jobs.RESUME_PATH)."""
    import score_jobs as sj
    try:
        return sj.RESUME_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def score_record(
    record: Dict[str, Any],
    *,
    pool: Any = None,
    pool_factory: Optional[Callable[[], Any]] = None,
    resume: Optional[str] = None,
) -> Dict[str, Any]:
    """Score one record through score_jobs' real two-stage pipeline.

    Reuses ``score_jobs.add_filter_columns`` + ``run_scoring`` (the exact code the
    scraper feeds) over a one-row DataFrame, then folds the resulting score columns
    back into ``record``. The Gemini client is the injected ``pool`` (or one from
    ``pool_factory``), kept behind score_jobs' own pool seam so it is mockable and a
    real run uses ``score_jobs.make_pool`` (the user's normal key pool / Vertex).
    """
    import pandas as pd

    import score_jobs as sj

    if pool is None:
        pool = (pool_factory or sj.make_pool)()
    if resume is None:
        resume = _default_resume()

    df = pd.DataFrame([{
        "job_posting_id": str(record["job_posting_id"]),
        "job_title": record.get("job_title", ""),
        "job_description_formatted": record.get("job_description_formatted", ""),
    }])
    df = sj.add_filter_columns(df, "job_description_formatted", "job_title")
    merged = asyncio.run(sj.run_scoring(pool, resume, df))

    row = merged.iloc[0].to_dict()
    out = dict(record)
    for col in sj.SCORE_COLS:
        if col in row:
            val = row[col]
            out[col] = "" if val is None or (isinstance(val, float) and pd.isna(val)) else val
    return out


# ── orchestration: parse -> (fetch) -> score -> tailor -> append ──────────────

def add_manual_job(
    *,
    jd_text: str = "",
    url: str = "",
    company: str = "",
    title: str = "",
    do_tailor: bool = True,
    tailor_opts: Optional[Dict[str, Any]] = None,
    pool: Any = None,
    pool_factory: Optional[Callable[[], Any]] = None,
    resume: Optional[str] = None,
    tailor_fn: Optional[Callable[..., Path]] = None,
    fetch_fn: Optional[Callable[[str], str]] = None,
    master_csv: Optional[Path] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Full manual-add flow. Returns {record, resume_dir, appended}.

    record       the scored job record (source="manual") appended to the master
    resume_dir   the tailored-résumé output folder (Path), or None when tailoring
                 was skipped (do_tailor=False) or failed
    appended     True when the record landed in the master CSV (False if a dup)

    `do_tailor` False = "just score": score the résumé against the job and append it
    to the dataset, skipping the tailor pass entirely (so it never fails on a thin
    record, and no cover letter is generated).

    Seams (all default to the real implementations, overridden in tests):
      pool / pool_factory  the Gemini scoring client (score_jobs pool)
      tailor_fn            resume_tailor.run.tailor
      fetch_fn             fetch_url_text (the free, optional URL GET)
    """
    log = on_status or (lambda _m: None)
    tailor_opts = tailor_opts or {}
    fetch_fn = fetch_fn or fetch_url_text

    fetched = ""
    if not (jd_text or "").strip() and (url or "").strip():
        log("fetching page text (free GET; optional)…")
        fetched = fetch_fn(url)
        if not fetched:
            log("fetch returned nothing — a pasted job description is required.")

    log("building job record…")
    record = build_job_record(
        jd_text=jd_text, url=url, company=company, title=title, fetched_text=fetched)

    log("scoring through the two-stage pipeline…")
    record = score_record(record, pool=pool, pool_factory=pool_factory, resume=resume)

    resume_dir: Optional[Path] = None
    if not do_tailor:
        log("scored only (tailoring skipped).")
    else:
        try:
            log(f"tailoring résumé for {record.get('job_title')} @ {record.get('company_name')}…")
            if tailor_fn is None:
                from resume_tailor.run import tailor as tailor_fn  # noqa: PLW0127
            out = tailor_fn(
                record,
                cover_letter=bool(tailor_opts.get("cover_letter", False)),
                ats_report=bool(tailor_opts.get("ats_report", True)),
                prep_sheet=bool(tailor_opts.get("prep_sheet", False)),
                tone=tailor_opts.get("tone", "professional"),
                on_status=log,
            )
            resume_dir = Path(out) if out else None
            record["resume"] = str(resume_dir) if resume_dir else ""
        except Exception as exc:  # noqa: BLE001 - tailoring is best-effort; the job is still added
            log(f"tailoring failed ({exc}); the job is still added — tailor it later.")

    log("appending to the master jobs list…")
    import jobsdata
    appended = jobsdata.append_manual_job(record, master_csv=master_csv)

    log("done.")
    return {"record": record, "resume_dir": resume_dir, "appended": appended}
