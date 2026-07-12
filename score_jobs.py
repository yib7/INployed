"""Score jobs from the latest scraper run against the resume.

Runs on the VM after scraper.py via run_scraper.sh.
Stage 1: STAGE1_MODEL (default gemini-3.1-flash-lite) scores every surviving job 1-5 with a short reason.
Stage 2: STAGE2_MODEL (default gemini-3.5-flash) gives deep analysis for jobs scoring >= STAGE2_THRESHOLD.
After the fresh batch, master rows whose scoring previously failed (transient
Vertex errors) are retried, capped at RESCORE_CAP per run.
Output: ~/<morning|evening>/linkedin_jobs_<date>_<label>_scored.csv.gz

Auth: uses Vertex AI via Application Default Credentials so usage bills to the
linked Google Cloud project (and draws down the $300 trial credit) instead of a
standalone AI Studio key. Set GOOGLE_CLOUD_PROJECT (and optionally
GOOGLE_CLOUD_LOCATION) in the environment, e.g. in run_scraper.sh. On a GCE VM,
attach a service account with the "Vertex AI User" role and ADC is picked up
automatically. On a non-GCE host, set GOOGLE_APPLICATION_CREDENTIALS to a
service-account key file.
"""
import argparse
import asyncio
import csv
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from google.genai import types
from markdownify import markdownify

from keypool import KeyPool, PoolError
from run_labels import RUN_LABELS

# Optional: load a local .env so credentials work for manual/local runs. The VM
# path exports these via run_scraper.sh, so a missing python-dotenv is fine.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

OUTPUT_DIR = Path(__file__).parent
RESUME_PATH = OUTPUT_DIR / "resume.md"

# Root-level scoring_config.json lets a local user (or the dashboard's Settings
# tab) retune the scorer without editing this file. Precedence is
# env > config-file > built-in default: an env var (exported by run_scraper.sh
# on the VM) always wins, then the file, then today's constant. The VM runs with
# NO such file, so absent it the scorer behaves exactly as before.
SCORING_CONFIG_FILE = "scoring_config.json"

# Built-in defaults, keyed by config name -> (env var name, default value, kind).
# kind drives coercion: "str" leaves the value alone, "int" casts via int().
_SCORING_DEFAULTS: dict[str, tuple[str, object, str]] = {
    "provider": ("SCORE_PROVIDER", "gemini", "str"),
    "stage1_model": ("SCORE_STAGE1_MODEL", "gemini-3.1-flash-lite", "str"),
    "stage2_model": ("SCORE_STAGE2_MODEL", "gemini-3.5-flash", "str"),
    "stage1_model_claude": ("SCORE_STAGE1_MODEL_CLAUDE", "claude-haiku-4-5", "str"),
    "stage2_model_claude": ("SCORE_STAGE2_MODEL_CLAUDE", "claude-sonnet-5", "str"),
    "stage1_concurrency": ("SCORE_STAGE1_CONCURRENCY", 6, "int"),
    "stage2_concurrency": ("SCORE_STAGE2_CONCURRENCY", 4, "int"),
    "stage2_threshold": ("SCORE_STAGE2_THRESHOLD", 4, "int"),
    # Spend guards: cap LLM calls per run so a keyword change or scrape anomaly
    # can't fire thousands of calls unattended. Overflow rows keep score=NaN and
    # are picked up by the rescore pass on later runs.
    "max_scored_per_run": ("SCORE_MAX_PER_RUN", 800, "int"),
    "rescore_cap": ("SCORE_RESCORE_CAP", 200, "int"),
    # The seniority cutoff: roles requiring >= this many years are filtered out.
    "min_filter_years": ("SCORE_MIN_FILTER_YEARS", 1, "int"),
}


def load_scoring_config() -> dict:
    """Effective scoring config with env > scoring_config.json > built-in default.

    Reads OUTPUT_DIR / scoring_config.json (or {} when absent/unreadable) and, for
    each key, returns the env var if set, else the file value, else the constant.
    """
    path = OUTPUT_DIR / SCORING_CONFIG_FILE
    raw: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data
        except (OSError, ValueError) as e:
            print(f"Could not read {SCORING_CONFIG_FILE} ({e}); using built-in defaults")
    cfg: dict = {}
    for key, (env_var, default, kind) in _SCORING_DEFAULTS.items():
        env_val = os.environ.get(env_var)
        if env_val is not None:
            value = env_val
        elif key in raw:
            value = raw[key]
        else:
            value = default
        cfg[key] = int(value) if kind == "int" else value
    return cfg


def _active_scoring(cfg: dict) -> tuple[str, str, str]:
    """(provider, stage1_model, stage2_model) resolved from `cfg`.

    Anything but exactly "claude" (after strip/lower) resolves to "gemini"
    with the Gemini stage models -- this pins the VM's behavior, since the
    VM ships no scoring_config.json and provider always defaults "gemini".
    """
    provider = "claude" if str(cfg.get("provider", "")).strip().lower() == "claude" else "gemini"
    if provider == "claude":
        return provider, cfg["stage1_model_claude"], cfg["stage2_model_claude"]
    return provider, cfg["stage1_model"], cfg["stage2_model"]


_SCORING = load_scoring_config()
SCORING_PROVIDER, STAGE1_MODEL, STAGE2_MODEL = _active_scoring(_SCORING)
STAGE1_CONCURRENCY = _SCORING["stage1_concurrency"]
STAGE2_CONCURRENCY = _SCORING["stage2_concurrency"]
STAGE2_THRESHOLD = _SCORING["stage2_threshold"]
MAX_SCORED_PER_RUN = _SCORING["max_scored_per_run"]
RESCORE_CAP = _SCORING["rescore_cap"]

# Per-run metrics appended to run_stats.csv (uploaded to Drive by run_scraper.sh,
# shown in the dashboard's Stats tab). One row per score_jobs.py invocation, so
# cost or volume drift is visible without grepping scraper.log.
RUN_STATS_CSV = OUTPUT_DIR / "run_stats.csv"
RUN_STATS_COLS = [
    "timestamp", "input_csv", "rows_in", "filtered_out", "llm_scored",
    "llm_errors", "stage2_done", "rescore_attempted", "rescore_scored",
    "llm_calls", "prompt_tokens", "output_tokens", "free_calls", "vertex_calls",
]

# Aggregate token spend across both stages and both passes (fresh + rescore).
TOKEN_USAGE = {"calls": 0, "prompt": 0, "output": 0}


def _track_usage(resp: Any) -> None:
    meta = getattr(resp, "usage_metadata", None)
    TOKEN_USAGE["calls"] += 1
    TOKEN_USAGE["prompt"] += getattr(meta, "prompt_token_count", 0) or 0
    TOKEN_USAGE["output"] += getattr(meta, "candidates_token_count", 0) or 0


def append_run_stats(stats: dict) -> None:
    """Append one metrics row; never let stats bookkeeping kill the run.

    Self-heals an older CSV whose header predates added columns by rewriting it
    with the current header (missing columns backfilled with 0) before appending,
    so pandas can always read a uniform-width file.
    """
    try:
        rows: list[dict] = []
        existing_header: list[str] = []
        if RUN_STATS_CSV.exists():
            with open(RUN_STATS_CSV, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                existing_header = reader.fieldnames or []
                rows = list(reader)
        new_row = {c: stats.get(c, 0) for c in RUN_STATS_COLS}

        if not RUN_STATS_CSV.exists() or existing_header != RUN_STATS_COLS:
            # Fresh file, or an older/narrower header -- rewrite with the current
            # columns, backfilling anything the old rows lack.
            with open(RUN_STATS_CSV, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=RUN_STATS_COLS, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow({c: r.get(c, 0) for c in RUN_STATS_COLS})
                w.writerow(new_row)
        else:
            with open(RUN_STATS_CSV, "a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=RUN_STATS_COLS, extrasaction="ignore")
                w.writerow(new_row)
        print(f"Run stats appended -> {RUN_STATS_CSV.name}: {stats}")
    except OSError as e:
        print(f"Could not append run stats ({e}) -- continuing")

JUNK_TITLE_PATTERNS = [
    re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|vp|vice president|chief|architect)\b", re.I),
    re.compile(r"\b(iii|iv|level\s*[3-9])\b", re.I),
    re.compile(r"\bii\b", re.I),
]

JUNK_DESC_PATTERNS = [
    re.compile(r"\b(senior|staff|principal|lead|manager|director|vp|vice president)\s+(level|role|position|engineer|developer|scientist|analyst)\b", re.I),
]
# Capture "<n>", "<n>+", or a range "<n>-<m>" / "<n> to <m>" before years/yrs.
#   group 1 = lower number (the experience floor)
#   group 2 = connector ("+", "to", or a dash variant) if any
#   group 3 = upper number of a range if any
#   group 4 = trailing "+" if any
# The dash class covers hyphen, en/em dash, minus sign, and common mojibake.
YEARS_RE = re.compile(
    r"(\d{1,2})\s*(\+|to|[-‐‑‒–—―−�])?\s*(\d{1,2})?\s*(\+)?\s*(?:years?|yrs?)",
    re.I,
)
# A BARE single number ("5 years") counts as a requirement only with a cue
# nearby; a range ("1-3 years") or open-ended "N+ years" is a requirement on
# sight (these are virtually never marketing copy).
REQ_CUES = ("experien", "minimum", "at least", "require", "must have", "background", "track record", "proven")
# Marketing / tenure wrappers that must NOT be treated as a requirement even in
# range / "N+" form ("20+ years of excellence", "30+ years in business",
# "doubled over the past 5 years", "founded 30 years ago", "5 years of service").
NONREQ_CTX = ("founded", "founding", " ago", "of service", "sabbatical",
              "anniversary", "years in business", "over the past",
              "been the leading", "of excellence", "year history", "of heritage")
# Minimum required years at or above which the role is scrapped. The user only
# wants roles a 0-experience applicant can clear: a 0-floor range ("0-2 years")
# stays, but "1+", "1-2", or anything requiring >= 1 year is filtered out.
# Sourced via env > scoring_config.json > default 1 (load_scoring_config()).
MIN_FILTER_YEARS = _SCORING["min_filter_years"]

# --- security-clearance requirement -------------------------------------------
# A new grad provably cannot hold an active US clearance, so any genuine
# clearance requirement is a hard drop. The negation guard keeps "no clearance
# required" / "clearance is not required" postings (precision bias: keep on doubt).
# Note: a bare mention of a clearance LEVEL ("Secret clearance shop", "team holds
# an active clearance") is treated as a drop -- such roles effectively require
# clearance. Only clearly-non-requiring phrasings ("no clearance required",
# "clearance holders") are kept via _CLEARANCE_NEG.
CLEARANCE_PATTERNS = [
    re.compile(r"\b(active|current)?\s*(secret|top[\s-]*secret|ts/sci|ts-sci)\b[^.\n]{0,40}\bclearance\b", re.I),
    re.compile(r"\bclearance\b[^.\n]{0,25}\b(is\s+)?required\b", re.I),
    re.compile(r"\brequires?\b[^.\n]{0,30}\bclearance\b", re.I),
    re.compile(r"\bmust\b[^.\n]{0,40}\b(have|possess|obtain|hold|maintain)\b[^.\n]{0,30}\bclearance\b", re.I),
    re.compile(r"\bability to obtain\b[^.\n]{0,30}\bclearance\b", re.I),
    re.compile(r"\bpolygraph\b", re.I),
]
# Keeps postings whose only clearance/polygraph signal is negated ("no clearance
# required", "no polygraph required") or merely describes cleared colleagues
# ("clearance holders"). Precision bias: a suppressor can only ever KEEP a job.
_CLEARANCE_NEG = re.compile(
    r"\b(no|not|without|does not|do not|don'?t|doesn'?t)\b[^.\n]{0,30}\bclearance\b"
    r"|\bclearance\b[^.\n]{0,30}\bnot\s+(required|needed)\b"
    r"|\b(no|not|without|does not|do not|don'?t|doesn'?t)\b[^.\n]{0,20}\bpolygraph\b"
    r"|\bpolygraph\b[^.\n]{0,20}\bnot\s+(required|needed)\b"
    r"|\bclearance\s+holders?\b",
    re.I,
)

# --- hard advanced-degree requirement -----------------------------------------
# Fires only when an advanced-degree token co-occurs with a REQUIRE cue and NO
# softener in the same window. The required-vs-preferred distinction is the whole
# game, so softeners (preferred / a plus / or equivalent / bachelor's-or...) keep
# the job. A bachelor's requirement is NEVER filtered (the candidate has one);
# "MS"/"M.S." only counts as a degree when followed by "degree" or "in <field>"
# so unit tokens like "5 ms latency" never match.
_DEGREE_TOKEN = re.compile(
    r"\b(ph\.?\s?d|doctorate|doctoral degree|graduate degree|advanced degree"
    r"|master's(?:\s+degree)?|master of (?:science|engineering|arts)"
    r"|m\.?s\.?\s+(?:degree|in\b)|m\.?eng\b"
    r"|mba\b(?![\s-]+(?:students?|alumni|alumnus|network|track|program|candidates?|grads?)))\b",
    re.I,
)
_DEGREE_REQ_CUE = ("requir", "must have", "must possess", "must hold", "minimum")
_DEGREE_SOFTENER = (
    "preferred", "a plus", "nice to have", "a bonus", "or equivalent",
    "equivalent experience", "or related experience", "bachelor",
    "undergraduate", "desired", "ideally", "not required",
)


STAGE1_SYSTEM = "You honestly evaluate how well a new-grad candidate fits early-career roles. Return JSON only."

# Split at the resume/job boundary so the Claude lane can send the resume half
# (stable across every job in a run) via --system-prompt and the job half
# (volatile per job) via stdin -- see the module-level caching note and
# score_stage1/score_stage2 below. RESUME + JOB is a plain string
# concatenation of the two literals below, so on the gemini path
# STAGE1_TEMPLATE.format(...) is byte-identical to before the split.
STAGE1_TEMPLATE_RESUME = """\
Rate how well this job matches the resume below, on a 1-5 scale.

CANDIDATE CONTEXT (read this before scoring):
TODAY'S DATE IS {today}. Judge every date in the resume and the job posting relative to that date, NOT relative to your training data. In particular, the candidate's May 2026 graduation is already in the PAST: the degree is COMPLETED and they are available to start immediately. Never treat the candidate as a current student or the degree as pending/"expected," and never lower the score because the graduation date is recent or looks like a future date to you.

This candidate is a new graduate (B.S. Computer Science, AI/ML concentration, Data Science minor, graduated May 2026 — available to start immediately) with one strong data-science internship plus substantial, advanced personal and academic projects. They are actively targeting ENTRY-LEVEL and EARLY-CAREER roles. Score with that in mind:

GEOGRAPHY / LOCATION / WORK AUTHORIZATION — IGNORE COMPLETELY: Do not factor in geography, location, onsite / hybrid / remote requirements, relocation, time zone, or work authorization at all. This job has already been vetted against the candidate's geographic preferences — regardless of where they currently live they are 100% willing to relocate, and they are authorized to work in the U.S. without sponsorship. Never raise or lower the score for location, onsite/hybrid/remote requirements, relocation, time zone, or work authorization / visa sponsorship; those have already been consented to by the candidate.

The candidate has essentially no full-time post-graduation experience yet (one internship plus strong projects) and is targeting roles a 0-experience applicant can clear. Apply this required-experience bar strictly:
  * 0 years required, OR a range with a floor of 0 ("0-2 years"), OR labeled entry-level / junior / new-grad / associate / university-grad / level "I", OR no stated experience requirement -> judge purely on SKILLS, STACK, and DOMAIN fit; a good skills match here is a 4 or 5.
  * Requires 1 or more years ("1+ years", "1-2 years", "2 years", "3+ years", etc.) -> the candidate does NOT clear the bar; this is a real gap. Cap the score at 3, and lower it toward 1-2 as the requirement or seniority rises (5+ years, OR senior/staff/principal/lead/manager/director titles -> 1-2).
  For a RANGE, use the LOWER bound: "0-2 years" clears the bar, "1-2 years" does not.
- Also score 1-2 for a hard advanced-degree requirement the candidate lacks ("Master's/PhD required"), or a genuine domain/stack mismatch where the candidate's skills do not map: low-level C/C++ kernel/embedded/firmware, hardware/electrical, or roles with NO data, analysis, or engineering component (e.g. pure quota-carrying sales, recruiting, manual non-technical QA, copywriting). Do NOT use this clause for data / analytics / BI / analyst roles — those are in-domain (see ADJACENT ANALYTICAL ROLES below).

ADJACENT ANALYTICAL ROLES ARE IN-DOMAIN (read carefully — this is a common mistake):
Treat data-analytical roles as a DOMAIN MATCH even when the title is business-flavored: Data Analyst, Business Analyst, Business Intelligence / BI Analyst, Reporting Analyst, Analytics Analyst, Product Analyst, Operations Analyst, Marketing / Research Analyst, and similar. These map directly to the candidate's SQL + Python + statistics + data-visualization / dashboarding skills (Tableau, Power BI, Looker Studio), their data-science internship, and their stakeholder / customer-facing experience. Judge such roles ONLY on whether the candidate can perform the listed RESPONSIBILITIES (querying and analyzing data, building reports/dashboards, drawing insights, communicating findings to stakeholders). Do NOT lower the score because the candidate lacks a business / finance / economics degree, because their prior experience or projects are "technical" rather than "business," or for any "career trajectory" / "career path" reason. A degree-field or job-title-history mismatch is NOT a disqualifier when the responsibilities are analytical — score these on skills like any other in-domain role (a good skills match with a 0-year floor is a 4 or 5).

Scale:
5 = Strong match - skills/domain align well AND no real experience bar (0 years / entry-level)
4 = Good match - skills align and the role has a 0-year floor / is entry-level; clearly worth applying
3 = Borderline - a real gap (requires >= 1 year, or only partial skills/domain alignment)
2 = Weak match - significant domain/stack mismatch, or 3+ years / senior seniority required
1 = No match - wrong field, or hard requirements the candidate cannot meet

Be honest and specific. Do not inflate roles that require professional experience (>= 1 year) or are off-domain. But do NOT lower the score of an otherwise-good entry-level skills fit (0-year floor) just because the candidate only graduated in May 2026 — they are a graduate, available immediately.

Resume:
---
{resume}
---

"""

STAGE1_TEMPLATE_JOB = """\
Job description:
---
{job}
---
"""

STAGE1_TEMPLATE = STAGE1_TEMPLATE_RESUME + STAGE1_TEMPLATE_JOB

STAGE2_SYSTEM = "You provide candid, detailed job-fit analysis. Return JSON only."

STAGE2_TEMPLATE_RESUME = """\
This job passed Stage 1 as a strong/good match for the candidate. Give an in-depth fit analysis: deep score 1-10, key strengths, gaps, and a recommendation.

TODAY'S DATE IS {today}. Judge every date in the resume and the job posting relative to that date, NOT relative to your training data: the candidate's May 2026 graduation is in the past and the degree is COMPLETED. Never list graduation timing, "degree in progress," or "has not graduated yet" as a gap.

Be specific. Tie strengths and gaps to concrete resume bullets and job requirements. Recommendation: "apply" (clear fit, prioritize), "consider" (mixed, depends on candidate's other options), "skip" (gaps too large despite the Stage 1 score).

When listing GAPS, name only concrete, stated requirements the candidate cannot meet: specific tools / technologies they lack, a hard credential (e.g. a required security clearance or an explicitly required advanced degree), or required years of experience. For analytical roles (Data Analyst, Business Analyst, BI / Reporting / Analytics Analyst, Product / Operations Analyst, Data Scientist), do NOT list "career trajectory," "career path," "lacks a business background/degree," "experience is technical rather than business," or similar title/degree-history mismatches as gaps — the candidate's SQL, Python, statistics, dashboarding (Tableau / Power BI / Looker), internship, and stakeholder / customer-facing experience transfer directly. Treat a title or degree-field difference as a non-issue when the candidate can do the listed work. NEVER list location, onsite / hybrid / remote, relocation, time zone, or work authorization / visa sponsorship as a gap — the candidate is fully willing to relocate and is authorized to work in the U.S. without sponsorship, so these are not gaps regardless of what the job states.

Resume:
---
{resume}
---

"""

STAGE2_TEMPLATE_JOB = """\
Job description:
---
{job}
---
"""

STAGE2_TEMPLATE = STAGE2_TEMPLATE_RESUME + STAGE2_TEMPLATE_JOB

STAGE1_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
}

STAGE2_SCHEMA = {
    "type": "object",
    "properties": {
        "deep_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "string", "enum": ["apply", "consider", "skip"]},
    },
    "required": ["deep_score", "strengths", "gaps", "recommendation"],
}


def today_str() -> str:
    """Current date for the scoring prompts, e.g. 'July 3, 2026'. The models'
    training data predates the resume's May 2026 graduation, so without an
    explicit 'today' they judge it as upcoming and dock the score."""
    now = datetime.now()
    return f"{now:%B} {now.day}, {now.year}"


def make_pool():
    """Build the scoring pool for SCORING_PROVIDER.

    "claude" tries the local `claude_cli.ClaudePool` first; a missing
    claude_cli.py (not shipped to the VM) or a missing `claude` CLI on PATH
    prints a warning and falls through to Gemini -- this branch must NEVER
    sys.exit, so a local-only provider choice pushed to the VM via
    scoring_config.json can't brick an unattended run. The final fallback
    (KeyPool.from_env, GEMINI_API_KEYS + Vertex) is unchanged from before and
    is the only branch that may exit, exactly as today.
    """
    if SCORING_PROVIDER == "claude":
        try:
            import claude_cli  # lazy: file absent on the VM; branch unreachable there
        except ImportError:
            print("Scoring provider is 'claude' but claude_cli.py is missing "
                  "(VM/standalone install?) -- falling back to Gemini.")
        else:
            if claude_cli.find_claude() is not None:
                timeout_s = int(os.environ.get("SCORE_CLAUDE_TIMEOUT_S", "240"))
                return claude_cli.ClaudePool(
                    timeout_s=timeout_s,
                    max_procs=max(STAGE1_CONCURRENCY, STAGE2_CONCURRENCY))
            print("Scoring provider is 'claude' but the `claude` CLI is not on "
                  "PATH -- falling back to Gemini.")
    try:
        return KeyPool.from_env(state_path=OUTPUT_DIR / "score_state.json")
    except PoolError as e:
        sys.exit(str(e))


def latest_input_csv() -> Path | None:
    """Newest unscored input CSV across ALL run-label dirs, or None.

    Scanning every run-label dir (instead of recomputing the run label at scoring
    time) avoids the label flipping when a run is triggered manually. Inputs whose
    _scored.csv.gz output already exists are skipped so a no-new-jobs run never
    rescores an old file.
    """
    candidates: list[Path] = []
    for label in RUN_LABELS:
        run_dir = OUTPUT_DIR / label
        if not run_dir.is_dir():
            continue
        for p in run_dir.glob("linkedin_jobs_*.csv"):
            if "_scored" in p.name:
                continue
            if p.with_name(p.stem + "_scored.csv.gz").exists():
                continue
            candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv", nargs="?", help="CSV to score (default: auto-discover latest in morning/ or evening/)")
    return p.parse_args()


def is_junk_title(title: Any) -> bool:
    if not isinstance(title, str):
        return False
    return any(p.search(title) for p in JUNK_TITLE_PATTERNS)

def is_junk_desc(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    return any(p.search(text) for p in JUNK_DESC_PATTERNS)

def requires_clearance(text: Any) -> bool:
    """True when the JD genuinely requires a US security clearance / polygraph.

    Suppressed by an explicit negation ("no clearance required") so such postings
    survive (precision bias favors keeping a job on doubt).
    """
    if not isinstance(text, str):
        return False
    if not any(p.search(text) for p in CLEARANCE_PATTERNS):
        return False
    return not bool(_CLEARANCE_NEG.search(text))


def requires_advanced_degree(text: Any) -> bool:
    """True when the JD HARD-requires a Master's/PhD-level degree.

    Proximity rule: for each advanced-degree token, look in a +-60 char window;
    the job is filtered only if that window has a require-cue and no softener. A
    bachelor's requirement never trips this. Errs toward keeping the job.
    """
    if not isinstance(text, str):
        return False
    low = text.lower().replace("'", "'")  # normalize curly apostrophe
    for m in _DEGREE_TOKEN.finditer(low):
        lo = max(0, m.start() - 60)
        hi = min(len(low), m.end() + 60)
        ctx = low[lo:hi]
        if any(s in ctx for s in _DEGREE_SOFTENER):
            continue
        if any(c in ctx for c in _DEGREE_REQ_CUE):
            return True
    return False


def min_required_years(text: Any) -> int | None:
    """Smallest experience-requirement minimum in the text, or None.

    A range ("1-3 years", "1 to 3 years") or open-ended "N+ years" is taken as a
    requirement on sight and contributes its LOWER bound, so Ford's "1 to 3
    years ... experience" is caught even when "experience" is far from the
    number. A BARE single number ("5 years") counts only with a requirement cue
    nearby, so company-age / tenure / benefits phrases ("for 90 years", "5 years
    of service") are ignored. Marketing wrappers around a range / "N+" form
    ("20+ years of excellence") are skipped too. Combined with MIN_FILTER_YEARS,
    only roles with a 0-year floor (or no detected requirement) survive.
    """
    if not isinstance(text, str):
        return None
    mins = []
    for m in YEARS_RE.finditer(text):
        conn = (m.group(2) or "").lower()
        is_range = bool(m.group(3)) and conn not in ("", "+")
        is_plus = conn == "+" or bool(m.group(4))
        lo = max(0, m.start() - 40)
        hi = min(len(text), m.end() + 45)
        ctx = text[lo:hi].lower()
        if any(w in ctx for w in NONREQ_CTX):
            continue
        if is_range or is_plus or any(cue in ctx for cue in REQ_CUES):
            mins.append(int(m.group(1)))
    return min(mins) if mins else None


def has_too_many_years(text: Any) -> bool:
    m = min_required_years(text)
    return m is not None and m >= MIN_FILTER_YEARS


def html_to_md(html: Any) -> str:
    if not isinstance(html, str) or not html.strip():
        return ""
    return markdownify(html, heading_style="ATX").strip()


def pick_col(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    return next((c for c in candidates if c in df.columns), None)


async def score_stage1(pool, sem: asyncio.Semaphore, resume: str, job_id: str, job_md: str) -> dict:
    async with sem:
        today = today_str()
        if SCORING_PROVIDER == "claude":
            # Cache-friendly split: the resume half (stable across every job
            # in a run) rides system_instruction (the CLI's cache
            # breakpoint), the job half (volatile) rides contents/stdin.
            system_instruction = STAGE1_SYSTEM + STAGE1_TEMPLATE_RESUME.format(
                resume=resume, today=today)
            contents = STAGE1_TEMPLATE_JOB.format(job=job_md)
        else:
            system_instruction = STAGE1_SYSTEM
            contents = STAGE1_TEMPLATE.format(resume=resume, job=job_md, today=today)
        try:
            resp = await pool.generate(
                model=STAGE1_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema=STAGE1_SCHEMA,
                ),
            )
            _track_usage(resp)
            data = json.loads(resp.text)
            return {"job_posting_id": job_id, "score": int(data["score"]), "reason": data["reason"]}
        except Exception as e:  # noqa: BLE001
            return {"job_posting_id": job_id, "score": None,
                    "reason": f"ERROR: {type(e).__name__}: {e}"[:200]}


async def score_stage2(pool, sem: asyncio.Semaphore, resume: str, job_id: str, job_md: str) -> dict:
    async with sem:
        today = today_str()
        if SCORING_PROVIDER == "claude":
            system_instruction = STAGE2_SYSTEM + STAGE2_TEMPLATE_RESUME.format(
                resume=resume, today=today)
            contents = STAGE2_TEMPLATE_JOB.format(job=job_md)
        else:
            system_instruction = STAGE2_SYSTEM
            contents = STAGE2_TEMPLATE.format(resume=resume, job=job_md, today=today)
        try:
            resp = await pool.generate(
                model=STAGE2_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=STAGE2_SCHEMA,
                ),
            )
            _track_usage(resp)
            data = json.loads(resp.text)
            return {
                "job_posting_id": job_id,
                "deep_score": int(data["deep_score"]),
                "strengths": " | ".join(data["strengths"]),
                "gaps": " | ".join(data["gaps"]),
                "recommendation": data["recommendation"],
            }
        except Exception as e:  # noqa: BLE001
            return {
                "job_posting_id": job_id, "deep_score": None,
                "strengths": "", "gaps": "",
                "recommendation": f"ERROR: {type(e).__name__}: {e}"[:200],
            }


CHUNK = 2000  # Chunked streaming row count for update_master_scores (memory bounded)

# Columns produced by scoring that should be carried into the master CSV so it
# is not just raw scrape data. (job_posting_id is the merge key, kept separate.)
SCORE_COLS = [
    "score", "reason", "deep_score", "strengths", "gaps", "recommendation",
    "filter_junk_title", "filter_junk_desc", "filter_too_many_years",
    "filter_clearance", "filter_degree", "filtered_out", "is_seen",
]
MASTER_CSV = OUTPUT_DIR / "linkedin_jobs_master.csv"


def _atomic_to_csv(df: pd.DataFrame, path: Path, **kwargs) -> None:
    """Write `df` to `path` atomically: same-dir tempfile + os.replace.

    A crash/kill/OOM mid-write then leaves either the old file (rename never
    happened) or the new one (rename completed) -- never a truncated partial
    write. score_jobs.py is copied standalone to the VM (no local/ package),
    hence this private copy instead of importing local/csv_io.write_csv_gz_atomic.
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


def update_master_scores(scored: pd.DataFrame) -> None:
    """Merge this run's score columns into the cumulative master CSV.

    The master is otherwise raw scrape data (scraper.py owns it); this folds in
    score / recommendation / the filter columns so the master is a complete
    record. Uses DataFrame.update, so jobs scored on a previous run that are not
    in this run keep their existing scores, and jobs in this run get refreshed.

    is_seen is deliberately excluded from the merge: it is local triage state
    owned by the dashboard's sticky registry reconcile, not by scoring. Folding
    it in here would let a routine re-score (fresh scrape or rescore pass) reset
    an already-"yes" master row back to "no". `scored` (e.g. the per-run scored
    CSV) may still carry the column for its own output -- it is simply never
    read out of it here.
    """
    if not MASTER_CSV.exists() or "job_posting_id" not in scored.columns:
        return
    cols = [c for c in SCORE_COLS if c in scored.columns and c != "is_seen"]
    if not cols:
        return
    s = scored[["job_posting_id"] + cols].copy()
    s["job_posting_id"] = s["job_posting_id"].astype(str)
    s = s.drop_duplicates(subset=["job_posting_id"], keep="last").set_index("job_posting_id")

    header = pd.read_csv(MASTER_CSV, nrows=0).columns.tolist()
    if "job_posting_id" not in header:
        return
    add_cols = [c for c in cols if c not in header]
    fd, tmp = tempfile.mkstemp(prefix=MASTER_CSV.stem + ".", suffix=".tmp",
                               dir=str(MASTER_CSV.parent))
    os.close(fd)
    wrote_header = False
    try:
        for chunk in pd.read_csv(MASTER_CSV, dtype={"job_posting_id": str}, chunksize=CHUNK):
            chunk["job_posting_id"] = chunk["job_posting_id"].astype(str)
            for c in add_cols:
                chunk[c] = pd.NA
            chunk = chunk.set_index("job_posting_id")
            # Per-chunk dtype inference (not whole-file) means a column that is
            # all-empty within this chunk's rows reads back as float64.
            # DataFrame.update() on pandas >= 3 raises TypeError rather than
            # silently upcasting when it would write an incompatible value into
            # such a column, so widen just those columns first. Columns that are
            # numeric on both sides are left alone so their on-disk formatting
            # (e.g. "5.0") is unchanged. bool is deliberately treated as
            # non-numeric here: is_numeric_dtype(bool) is True, but update()
            # still refuses to write True/False into a float64 block, so the
            # boolean filter columns (filter_*, filtered_out) landing on an
            # all-empty float64 master chunk must widen too.
            def _is_num(x):
                return (pd.api.types.is_numeric_dtype(x)
                        and not pd.api.types.is_bool_dtype(x))
            for c in cols:
                if c in chunk.columns and _is_num(chunk[c]) and not _is_num(s[c]):
                    chunk[c] = chunk[c].astype(object)
            chunk.update(s)  # aligns on index: only this chunk's ids that appear in s change
            chunk.reset_index().to_csv(tmp, mode="a", header=not wrote_header,
                                       index=False, encoding="utf-8")
            wrote_header = True
        os.replace(tmp, MASTER_CSV)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def save_output(df: pd.DataFrame, input_csv: Path) -> Path:
    out_path = input_csv.with_name(input_csv.stem + "_scored.csv.gz")
    df = df.drop(columns=[c for c in ("job_description_formatted",) if c in df.columns])
    df["is_seen"] = "no"
    df.to_csv(out_path, index=False, encoding="utf-8", compression="gzip")
    update_master_scores(df)
    return out_path


def add_filter_columns(df: pd.DataFrame, desc_col: str, title_col: str | None) -> pd.DataFrame:
    """Add job_description_md + the mechanical-filter columns."""
    df["job_description_md"] = df[desc_col].apply(html_to_md)
    df["filter_junk_title"] = df[title_col].apply(is_junk_title) if title_col else False
    df["filter_junk_desc"] = df["job_description_md"].apply(is_junk_desc)
    df["filter_too_many_years"] = df["job_description_md"].apply(has_too_many_years)
    df["filter_clearance"] = df["job_description_md"].apply(requires_clearance)
    df["filter_degree"] = df["job_description_md"].apply(requires_advanced_degree)
    df["filtered_out"] = (
        df["filter_junk_title"] | df["filter_junk_desc"] | df["filter_too_many_years"]
        | df["filter_clearance"] | df["filter_degree"]
    )
    # An unscoreable (empty/missing) description would otherwise be retried by
    # the rescore pass forever — park it as filtered.
    no_desc = df["job_description_md"].str.len() < 40
    df.loc[no_desc, "filtered_out"] = True
    return df


async def run_scoring(pool, resume: str, df: pd.DataFrame) -> pd.DataFrame:
    """Stage 1 + Stage 2 over the unfiltered rows of df; returns df with score columns merged.

    df must carry job_posting_id (str), job_description_md, and the filter columns.
    """
    to_score = df[~df["filtered_out"]].copy()
    print(f"Mechanical filter: {len(df)} -> {len(to_score)} to score")
    if len(to_score) > MAX_SCORED_PER_RUN:
        print(f"Spend guard: capping at {MAX_SCORED_PER_RUN} of {len(to_score)} jobs "
              "(rest stays unscored; the rescore pass picks them up on later runs)")
        to_score = to_score.head(MAX_SCORED_PER_RUN)

    if to_score.empty:
        df = df.copy()
        df["score"] = None
        df["reason"] = "filtered_out"
        df["deep_score"] = None
        df["strengths"] = ""
        df["gaps"] = ""
        df["recommendation"] = ""
        return df

    sem1 = asyncio.Semaphore(STAGE1_CONCURRENCY)
    print(f"Stage 1: scoring {len(to_score)} jobs with {STAGE1_MODEL}")
    s1_tasks = [
        score_stage1(pool, sem1, resume, r.job_posting_id, r.job_description_md)
        for r in to_score.itertuples(index=False)
    ]
    s1_results = await asyncio.gather(*s1_tasks)
    s1_df = pd.DataFrame(s1_results)

    s2 = s1_df[s1_df["score"].fillna(0) >= STAGE2_THRESHOLD].sort_values(
        "score", ascending=False, kind="stable"
    )
    s2_ids = s2["job_posting_id"].tolist()
    print(f"Stage 2: {len(s2_ids)} jobs at threshold >= {STAGE2_THRESHOLD}")

    if s2_ids:
        sem2 = asyncio.Semaphore(STAGE2_CONCURRENCY)
        # Dispatch highest Stage-1 score first so the scarce free flash budget
        # goes to the best-fit jobs; the overflow tail spills to Vertex.
        rank = {jid: i for i, jid in enumerate(s2_ids)}
        s2_input = to_score[to_score["job_posting_id"].isin(s2_ids)].copy()
        s2_input["_rank"] = s2_input["job_posting_id"].map(rank)
        s2_input = s2_input.sort_values("_rank", kind="stable").drop(columns="_rank")
        s2_tasks = [
            score_stage2(pool, sem2, resume, r.job_posting_id, r.job_description_md)
            for r in s2_input.itertuples(index=False)
        ]
        s2_results = await asyncio.gather(*s2_tasks)
        s2_df = pd.DataFrame(s2_results)
    else:
        s2_df = pd.DataFrame(columns=["job_posting_id", "deep_score", "strengths", "gaps", "recommendation"])

    merged = df.merge(s1_df, on="job_posting_id", how="left").merge(s2_df, on="job_posting_id", how="left")
    merged.loc[merged["filtered_out"], "reason"] = merged.loc[merged["filtered_out"], "reason"].fillna("filtered_out")
    return merged


def rows_needing_rescore(master: pd.DataFrame) -> pd.DataFrame:
    """Master rows whose scoring previously failed or never happened.

    score NaN + not mechanically filtered = never scored (crash, spend cap, or
    a swallowed Stage-1 exception); reason/recommendation starting with ERROR:
    = an explicit failed call. Without this, one transient 429 permanently
    hides a job from the High-Score tab.
    """
    if "score" in master.columns:
        score = pd.to_numeric(master["score"], errors="coerce")
    else:
        score = pd.Series(float("nan"), index=master.index)
    filtered = (
        master.get("filtered_out", pd.Series(False, index=master.index))
        .fillna(False).astype(str).str.lower().isin(("true", "1", "yes"))
    )
    reason = master.get("reason", pd.Series("", index=master.index)).fillna("").astype(str)
    reco = master.get("recommendation", pd.Series("", index=master.index)).fillna("").astype(str)
    err = reason.str.startswith("ERROR:") | reco.str.startswith("ERROR:")
    return master[(score.isna() & ~filtered) | err]


def _load_rows_by_id(master_csv, ids) -> pd.DataFrame:
    """Chunked by-id load: scan `master_csv` in CHUNK-row pieces, keeping only
    rows whose job_posting_id is in `ids`. Used to pull the (<=RESCORE_CAP)
    full rows needed for rescoring without ever holding the whole master
    (with its two ~90 MB text columns) in memory at once."""
    want = set(map(str, ids))
    parts = []
    for chunk in pd.read_csv(master_csv, dtype={"job_posting_id": str}, chunksize=CHUNK):
        chunk["job_posting_id"] = chunk["job_posting_id"].astype(str)
        hit = chunk[chunk["job_posting_id"].isin(want)]
        if not hit.empty:
            parts.append(hit)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


async def rescore_master_failures(pool, resume: str) -> tuple[int, int]:
    """Retry failed/missing master rows. Returns (attempted, newly_scored)."""
    if not MASTER_CSV.exists():
        return 0, 0
    header = pd.read_csv(MASTER_CSV, nrows=0).columns
    light_cols = [c for c in ("job_posting_id", "score", "filtered_out", "reason",
                              "recommendation") if c in header]
    # Candidate-finding never needs the two ~90 MB text columns.
    light = pd.read_csv(MASTER_CSV, usecols=light_cols, dtype={"job_posting_id": str})
    if "job_posting_id" not in light.columns or light.empty:
        return 0, 0
    todo_ids = rows_needing_rescore(light)["job_posting_id"].astype(str)
    if todo_ids.empty:
        return 0, 0
    todo_ids = todo_ids.tail(RESCORE_CAP).tolist()  # newest-first cap, same as before
    print(f"Rescore pass: retrying {len(todo_ids)} master row(s) with missing/failed scores")

    master = _load_rows_by_id(MASTER_CSV, todo_ids)  # <= RESCORE_CAP full rows only
    desc_col = pick_col(master, ("job_description_formatted", "job_description"))
    if not desc_col or master.empty:
        return 0, 0
    todo = master.copy()
    todo["job_posting_id"] = todo["job_posting_id"].astype(str)
    todo = todo.drop(columns=[c for c in SCORE_COLS if c in todo.columns], errors="ignore")
    title_col = pick_col(master, ("job_title", "job_posting_title", "title"))
    todo = add_filter_columns(todo, desc_col, title_col)
    merged = await run_scoring(pool, resume, todo)
    # Fold back WITHOUT is_seen so locally-triaged state is never reset here.
    update_master_scores(merged.drop(columns=["is_seen"], errors="ignore"))
    n = int(pd.to_numeric(merged["score"], errors="coerce").notna().sum())
    print(f"Rescore pass: {n} of {len(todo)} rows now scored")
    return len(todo), n


def load_resume() -> str:
    """Read resume.md, or exit with a friendly message instead of a raw traceback."""
    if not RESUME_PATH.exists():
        sys.exit("resume.md not found - generate it from the dashboard's Resume Data tab")
    return RESUME_PATH.read_text(encoding="utf-8")


async def main() -> None:
    args = parse_args()
    resume = load_resume()
    pool = make_pool()

    stats = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "input_csv": "",
    }

    if args.csv:
        csv_path = Path(args.csv).resolve()
        if not csv_path.exists():
            sys.exit(f"CSV not found: {csv_path}")
    else:
        csv_path = latest_input_csv()
        if csv_path is None:
            print("No unscored input CSVs found — skipping fresh scoring.")

    if csv_path is not None:
        print(f"Scoring {csv_path}")
        stats["input_csv"] = csv_path.name
        try:
            df = pd.read_csv(csv_path, dtype={"job_posting_id": str})
        except pd.errors.EmptyDataError:
            df = pd.DataFrame()
        if df.empty:
            print("Input CSV is empty — nothing to score.")
        else:
            # Make scoring idempotent: drop any prior scoring output so re-scoring an
            # already-scored input (e.g. the master, which now carries score columns)
            # doesn't collide on the Stage-1/Stage-2 merge (reason_x/reason_y, etc.).
            df = df.drop(columns=[c for c in SCORE_COLS if c in df.columns], errors="ignore")

            desc_col = pick_col(df, ("job_description_formatted", "job_description"))
            if not desc_col:
                sys.exit("No job description column found")
            title_col = pick_col(df, ("job_title", "job_posting_title", "title"))
            id_col = pick_col(df, ("job_posting_id", "job_id"))
            if not id_col:
                sys.exit("No job_posting_id column found")
            df[id_col] = df[id_col].astype(str)
            if id_col != "job_posting_id":
                df = df.rename(columns={id_col: "job_posting_id"})

            df = add_filter_columns(df, desc_col, title_col)
            merged = await run_scoring(pool, resume, df)
            out = save_output(merged, csv_path)
            n_scored = merged["score"].notna().sum()
            n_deep = merged["deep_score"].notna().sum()
            print(f"Saved -> {out}")
            print(f"  Stage 1 scored: {n_scored}, Stage 2 deep-analyzed: {n_deep}")
            stats["rows_in"] = len(merged)
            stats["filtered_out"] = int(merged["filtered_out"].sum())
            stats["llm_scored"] = int(n_scored)
            stats["llm_errors"] = int(
                merged["reason"].fillna("").astype(str).str.startswith("ERROR:").sum()
            )
            stats["stage2_done"] = int(n_deep)

    rescore_attempted, rescore_scored = await rescore_master_failures(pool, resume)
    stats["rescore_attempted"] = rescore_attempted
    stats["rescore_scored"] = rescore_scored
    stats["llm_calls"] = TOKEN_USAGE["calls"]
    stats["prompt_tokens"] = TOKEN_USAGE["prompt"]
    stats["output_tokens"] = TOKEN_USAGE["output"]
    stats["free_calls"] = pool.stats()["free_calls"]
    stats["vertex_calls"] = pool.stats()["vertex_calls"]
    append_run_stats(stats)


if __name__ == "__main__":
    asyncio.run(main())
