"""Jev scoring for score_jobs.py (cycle 19: SC-1 to SC-3, JS-4).

Jev (TypeSafe System One) answers typed questions about a state and cannot
write text, so the split is: Jev decides, code composes. `score_jobs.py` calls
`stage1` and `stage2` once per job per stage when `use_jev` says so; each asks
one request about the state `{candidate, resume, job}` and composes the
columns the LLM path writes today. `None` from either means "use the LLM path"
for that job.

The scorer runs as its own process and on the VM, so it decides Jev use by
itself (JS-4): `SCORE_USE_JEV` in the environment, else the dashboard's
`local/config.json` beside the repo when that file exists (`jev_enabled` and
`jev_scoring`, read the way `local/jev_switch.py` reads them), else off. The VM
has neither, so it stays on Gemini by construction.

Importing this module imports neither the TypeSafe SDK nor `local/jev.py`:
`_jev_module()` loads `jev` on first use through a sys.path hop to `local/`.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# --- composition constants (SC-2, SC-3; tuned in SP7) ------------------------------
#
# Stage 1 follows the rubric of the stage 1 prompt (score_jobs.STAGE1_TEMPLATE_RESUME):
# in domain with no experience bar, skills decide 3 to 5; one year or more caps the
# score at 3 and lowers it as the requirement rises; off domain or a hard advanced
# degree gives 1 or 2. `skills_fit` arrives scaled to 0-1 (Jev's level over the top
# level), so 0.75 is the "most of the core tools" level.
SKILLS_FOR_5 = 0.80             # in domain with no bar: a 5 at or above this
SKILLS_FOR_4 = 0.55             # a 4 at or above this, else a 3
LOW_BAND_SKILLS_FOR_2 = 0.55    # off domain or a hard advanced degree: a 2 at or above this, else a 1
# Minimum years code found -> the highest score allowed; the first row reached wins.
YEARS_CAPS: tuple[tuple[int, int], ...] = ((5, 1), (3, 2), (1, 3))
# experience_label -> the highest score allowed (read only when code found no years).
EXPERIENCE_LABEL_CAPS: dict[str, int] = {"one_to_two_years": 3, "three_plus_years": 2,
                                         "senior_title": 2}
PARTIAL_DOMAIN_CAP = 3          # other technical work without data or engineering duties
CLEARANCE_CAP = 1               # a new graduate cannot hold an active clearance
ANALYTICAL_YES = 0.5            # analytical_work at or above this reads as yes
SKILLS_WORDS: tuple[tuple[float, str], ...] = (
    (SKILLS_FOR_5, "strong"), (SKILLS_FOR_4, "good"), (0.30, "partial"), (0.0, "weak"))

# The request (SC-1): the job text is cut from its end until the request fits
# `jev.request_fits` (which keeps its own margin under Jev's limits); a job that
# would have to shrink below MIN_JOB_CHARS goes to the LLM path.
MIN_JOB_CHARS = 1_000
TRIM_EXTRA_CHARS = 200          # cut this many characters past the size estimate's overage

_HERE = Path(__file__).resolve().parent
# The repo keeps this file in pipeline/, with the dashboard's local/ beside it.
# The VM copies the pipeline scripts flat into ~/, where neither exists.
LOCAL_DIR: Path | None = _HERE.parent / "local" if _HERE.name == "pipeline" else None
CONFIG_PATH: Path | None = LOCAL_DIR / "config.json" if LOCAL_DIR is not None else None

ENV_SWITCH = "SCORE_USE_JEV"
MASTER_KEY = "jev_enabled"
AREA_KEY = "jev_scoring"
KEY_ENV = "TYPESAFE_API_KEY"
SDK_MODULE = "typesafe_sdk"
_TRUE = ("1", "true", "yes", "on")

# The reasons `jev_switch.jev_why_off("scoring")` gives, word for word, so the
# dashboard and the scorer name a switched-off Jev the same way.
REASON_SWITCH = "Jev is switched off in Settings"
REASON_AREA = "Jev is switched off for scoring in Settings"
REASON_KEY = "no TypeSafe API key"
REASON_SDK = "typesafe-sdk is not installed"
REASON_MODULE = "local/jev.py could not be imported"
REASON_NO_CONFIG = f"no {ENV_SWITCH} and no dashboard config beside the scorer"


# --- JS-4: does this run use Jev? -------------------------------------------------

def _read_config(path: Path) -> dict[str, Any]:
    """The dashboard config as a dict; {} when it is unreadable or holds no JSON
    object, so every switch reads its default (`jsonutil.read_json_dict`)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sdk_installed() -> bool:
    """Is `typesafe_sdk` importable? A find_spec probe, so nothing is imported."""
    try:
        return importlib.util.find_spec(SDK_MODULE) is not None
    except (ImportError, ValueError):
        return False


def _jev_module() -> Any:
    """`local/jev.py`, imported on first use. local/ goes on the end of sys.path
    so nothing in it can shadow a pipeline module (llm.py's hop, reversed)."""
    if "jev" in sys.modules:
        return sys.modules["jev"]
    if LOCAL_DIR is not None and str(LOCAL_DIR) not in sys.path:
        sys.path.append(str(LOCAL_DIR))
    import jev
    return jev


def use_jev(env: Any = None) -> tuple[bool, str]:
    """(True, where the switch came from) when this run scores with Jev, else
    (False, the reason). Order (JS-4): `SCORE_USE_JEV`, else the dashboard
    config when it exists (`jev_enabled`, `jev_scoring`, both default on),
    else off. When the switch is on but the key, the SDK or `local/jev.py` is
    missing, one warning is printed and the answer is off: the run falls back
    to the LLM path and never exits over it. The key is checked for presence
    only and never printed."""
    env = os.environ if env is None else env
    raw = str(env.get(ENV_SWITCH) or "").strip().lower()
    if raw:
        if raw not in _TRUE:
            return False, f"{ENV_SWITCH}={raw} turns it off"
        source = f"{ENV_SWITCH}={raw}"
    elif CONFIG_PATH is not None and CONFIG_PATH.is_file():
        cfg = _read_config(CONFIG_PATH)
        if cfg.get(MASTER_KEY, True) is False:
            return False, REASON_SWITCH
        if cfg.get(AREA_KEY, True) is False:
            return False, REASON_AREA
        source = "Settings"
    else:
        return False, REASON_NO_CONFIG
    if not str(env.get(KEY_ENV) or "").strip():
        reason = REASON_KEY
    elif not _sdk_installed():
        reason = REASON_SDK
    else:
        try:
            _jev_module()
        except Exception:       # noqa: BLE001  (any import failure reads as Jev off)
            reason = REASON_MODULE
        else:
            return True, source
    print(f"WARNING: Jev scoring is on ({source}) but {reason}; scoring on the LLM path.")
    return False, reason


def make_judge() -> Any:
    """The run's judge: `jev.Guarded(jev.TypeSafeJev())`, whose breaker opens
    after an outage so the rest of the run takes the LLM path (SC-5). None,
    with one warning naming the error's class only, when it cannot be built."""
    try:
        jev = _jev_module()
        return jev.Guarded(jev.TypeSafeJev())
    except Exception as e:      # noqa: BLE001  (None keeps the run on the LLM path)
        print(f"WARNING: the Jev judge could not be built ({type(e).__name__}); "
              "scoring on the LLM path.")
        return None


def usage() -> dict[str, Any]:
    """`jev.usage()`: the live requests this process made and what they cost.
    Zeros when `local/jev.py` cannot be loaded."""
    try:
        return dict(_jev_module().usage())
    except Exception:           # noqa: BLE001  (no jev module means no Jev spend)
        return {"requests": 0, "input_tokens": 0, "usd": 0.0}


# --- the state (SC-1) -----------------------------------------------------------------

# The fixed candidate context the scorer prompts carry (score_jobs.STAGE1_TEMPLATE_RESUME).
CANDIDATE: dict[str, str] = {
    "status": ("New graduate: B.S. in Computer Science (AI/ML concentration, Data Science "
               "minor), graduated May 2026 and available to start now."),
    "experience": ("One data-science internship plus academic and personal projects. No "
                   "full-time work since graduating."),
    "target": "Entry-level and early-career roles.",
    "does_not_count": ("Location, on-site, hybrid or remote terms, relocation, time zone, "
                       "visa sponsorship and work authorization never count for or against "
                       "a job. The candidate will relocate and is authorized to work in the "
                       "U.S. without sponsorship."),
}


def _job_parts(job: Any) -> tuple[str, Mapping[str, Any]]:
    """(the job's markdown, the code facts) from a job given as its markdown or
    as a mapping with `md` (or `job_md`, `description`) and `facts`."""
    if isinstance(job, str):
        return job, {}
    if isinstance(job, Mapping):
        md = job.get("md") or job.get("job_md") or job.get("description") or ""
        facts = job.get("facts")
        return (md if isinstance(md, str) else ""), (facts if isinstance(facts, Mapping) else {})
    return "", {}


def fitted_state(job_md: str, resume: str, questions: Mapping[str, Any]) -> dict | None:
    """The request's state `{candidate, resume, job}`, with the job text cut from
    its end until `jev.request_fits` passes. None when the job would have to
    shrink below MIN_JOB_CHARS (a large résumé, say) or `local/jev.py` is missing."""
    try:
        jev = _jev_module()
    except Exception:           # noqa: BLE001  (no size check means no request)
        return None
    state = {"candidate": CANDIDATE, "resume": resume, "job": job_md}
    for _ in range(8):
        longest, whole = jev.request_size(state, questions)
        over = max(longest - jev.STATE_TOKENS_MAX * jev.SIZE_MARGIN,
                   whole - jev.REQUEST_TOKENS_MAX * jev.SIZE_MARGIN)
        if over <= 0:
            return state
        text = state["job"]
        keep = len(text) - math.ceil(over * jev.CHARS_PER_TOKEN) - TRIM_EXTRA_CHARS
        if keep < MIN_JOB_CHARS:
            return None
        state = dict(state, job=text[:keep])
    return None


# Error classes already reported this run: one line per class, never the message
# (a service error can quote the request, which carries the résumé).
_WARNED: set[str] = set()


def _ask(judge: Any, state: Any, questions: dict[str, dict]) -> Mapping[str, Any] | None:
    """The judge's answers, or None when the request fails. An open breaker
    (`jev.JudgeOutage`) stays quiet here: the run reports the outage once."""
    try:
        answers = judge.judge(state, questions)
    except Exception as e:      # noqa: BLE001  (a failed request sends the job to the LLM path)
        kind = type(e).__name__
        if kind != "JudgeOutage" and kind not in _WARNED:
            _WARNED.add(kind)
            print(f"Jev could not score a job ({kind}); such jobs take the LLM path.")
        return None
    return answers if isinstance(answers, Mapping) else None


def _real(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _noul_read(answer: Any) -> float | None:
    """A noul's yes probability, or None when it is missing or outside 0-1."""
    p = _real(getattr(answer, "noul", None))
    return p if p is not None and 0.0 <= p <= 1.0 else None


def _choice_read(answer: Any, options: Mapping[str, Any]) -> str | None:
    """The option picked, or None when it is missing or one the question never offered."""
    name = getattr(answer, "choice", None)
    return name if isinstance(name, str) and name in options else None


def _score_read(answer: Any, levels: int) -> float | None:
    """A score scaled to 0-1 (the level over the top level), or None when it is
    missing or off the rubric."""
    raw = _real(getattr(answer, "score", None))
    top = levels - 1
    if raw is None or top <= 0 or not -1e-6 <= raw <= top + 1e-6:
        return None
    return min(1.0, max(0.0, raw / top))


# --- SC-2: stage 1 (score 1-5 and reason) -----------------------------------------------

SKILLS_FIT_LEVELS = (
    "The résumé shows almost none of the tools and skills the job lists.",
    "The résumé shows a few of the tools and skills the job lists; most of the core ones "
    "are missing.",
    "The résumé shows about half of the core tools and skills the job lists.",
    "The résumé shows most of the core tools and skills the job lists; one or two are missing.",
    "The résumé shows nearly all of the core tools and skills the job lists.",
)

# option id -> (what Jev reads, what the reason says). data_analytics_bi is in
# domain per the rubric's analyst rule, business-flavoured titles included.
DOMAINS: dict[str, tuple[str, str]] = {
    "data_science_ml": (
        "Data science, machine learning or AI: building models, running experiments, "
        "statistical analysis.", "data science or ML"),
    "data_analytics_bi": (
        "Data analytics or business intelligence: querying and analysing data, reports and "
        "dashboards. Business, product, operations, marketing, research and reporting analyst "
        "roles belong here.", "data analytics"),
    "software_engineering": (
        "Software engineering: writing code for applications, services, data pipelines or "
        "developer tools.", "software engineering"),
    "other_technical": (
        "Other technical work: IT support, networking, systems administration, test "
        "engineering, technical support or another technical field.", "other technical"),
    "hardware_embedded": (
        "Hardware or embedded work: firmware, device drivers, low-level C or C++ on devices, "
        "electrical or chip design.", "hardware or embedded"),
    "non_technical": (
        "Non-technical work: sales, recruiting, customer service, administration, writing, "
        "marketing or operations roles.", "non-technical"),
}
IN_DOMAIN = frozenset({"data_science_ml", "data_analytics_bi", "software_engineering"})
# Fields that count as in domain when the listed duties are data or engineering
# work (analytical_work): without such duties other technical work is a partial
# match and non-technical work is off domain. Hardware stays off domain either way,
# since engineering duties are its norm.
DUTY_RESCUED = frozenset({"other_technical", "non_technical"})
PARTIAL_DOMAIN = frozenset({"other_technical"})
RESCUED_SUFFIX = " with data or engineering duties"

EXPERIENCE_LABELS: dict[str, tuple[str, str]] = {
    "none_stated": (
        "The job states no experience level and no years of experience.",
        "no experience level stated"),
    "entry_level": (
        "The job is labeled entry level, new grad, junior, associate or level I, or welcomes "
        "candidates with no experience.", "entry level (no years stated)"),
    "one_to_two_years": (
        "The job asks for at least one or two years of experience.", "a floor of 1 to 2 years"),
    "three_plus_years": (
        "The job asks for three or more years of experience.", "3 or more years"),
    "senior_title": (
        "The job is a senior, staff, principal, lead, manager or director role.", "senior title"),
}


def stage1_questions() -> dict[str, dict]:
    """The four stage 1 questions, asked in one request (SC-2)."""
    return {
        "skills_fit": {
            "type": "score",
            "instructions": ("How well do the tools and skills shown in `resume` cover the "
                             "tools and skills that `job` asks for? Coursework, projects and "
                             "the internship count as showing a skill."),
            "criteria": list(SKILLS_FIT_LEVELS),
        },
        "domain": {
            "type": "choice",
            "instructions": ("Which field is the work that `job` describes in? Judge by the "
                             "listed duties and the title."),
            "criteria": {k: v[0] for k, v in DOMAINS.items()},
        },
        "experience_label": {
            "type": "choice",
            "instructions": "What experience level does `job` ask for?",
            "criteria": {k: v[0] for k, v in EXPERIENCE_LABELS.items()},
        },
        "analytical_work": {
            "type": "noul",
            "instructions": ("Are the responsibilities listed in `job` querying or analysing "
                             "data, building reports or dashboards, or engineering work?"),
            "criteria": {
                "true": ("Most listed duties are data analysis, reporting, dashboards, "
                         "modeling or building software or systems."),
                "false": ("Most listed duties are selling, recruiting, support, "
                          "administration, writing or other work with no data or "
                          "engineering in it."),
            },
        },
    }


def stage1_reads(answers: Mapping[str, Any], questions: Mapping[str, dict]) -> dict | None:
    """The reads `compose_stage1` takes, or None when any answer is unusable."""
    reads = {
        "skills_fit": _score_read(answers.get("skills_fit"),
                                  len(questions["skills_fit"]["criteria"])),
        "domain": _choice_read(answers.get("domain"), questions["domain"]["criteria"]),
        "experience_label": _choice_read(answers.get("experience_label"),
                                         questions["experience_label"]["criteria"]),
        "analytical_work": _noul_read(answers.get("analytical_work")),
    }
    return None if any(v is None for v in reads.values()) else reads


def _years_cap(years: int) -> int | None:
    return next((cap for floor, cap in YEARS_CAPS if years >= floor), None)


def compose_stage1(facts: Mapping[str, Any], reads: Mapping[str, Any]) -> tuple[int, str]:
    """(score 1-5, reason) from the code facts (`min_years`, `advanced_degree`,
    `clearance`) and Jev's reads (`skills_fit` 0-1, `domain`, `experience_label`,
    `analytical_work` 0-1). Pure: the rules and constants at the top of the module."""
    skills = min(1.0, max(0.0, float(reads["skills_fit"])))
    domain = str(reads["domain"])
    label = str(reads["experience_label"])
    duties = float(reads["analytical_work"]) >= ANALYTICAL_YES
    years = facts.get("min_years")
    degree = bool(facts.get("advanced_degree"))
    clearance = bool(facts.get("clearance"))

    rescued = domain in DUTY_RESCUED and duties
    if domain in IN_DOMAIN or rescued:
        fit = "in"
    elif domain in PARTIAL_DOMAIN:
        fit = "partial"
    else:
        fit = "off"

    if fit == "off" or degree:
        score = 2 if skills >= LOW_BAND_SKILLS_FOR_2 else 1
    elif skills >= SKILLS_FOR_5:
        score = 5
    elif skills >= SKILLS_FOR_4:
        score = 4
    else:
        score = 3
    caps = [PARTIAL_DOMAIN_CAP if fit == "partial" else None,
            CLEARANCE_CAP if clearance else None]
    if years is not None:
        years = int(years)
        caps.append(_years_cap(years))
        experience = "0-year floor" if years <= 0 else f"{years}+ years required"
    else:
        caps.append(EXPERIENCE_LABEL_CAPS.get(label))
        experience = EXPERIENCE_LABELS.get(label, ("", label.replace("_", " ")))[1]
    score = min([score] + [c for c in caps if c is not None])

    word = next(w for floor, w in SKILLS_WORDS if skills >= floor)
    field = DOMAINS.get(domain, ("", domain.replace("_", " ")))[1]
    parts = [f"Skills fit {word} ({skills:.2f})",
             f"domain {field}{RESCUED_SUFFIX if rescued else ''}",
             experience,
             "advanced degree required" if degree else "no degree bar"]
    if clearance:
        parts.append("clearance required")
    return max(1, min(5, score)), "; ".join(parts)


def stage1(judge: Any, job: Any, resume: str) -> dict | None:
    """{"score", "reason"} for one job from one Jev request, or None to score
    that job on the LLM path (no judge, a request that failed or did not fit,
    an answer that could not be read). `job` is the job's markdown or a mapping
    with `md` and `facts` (code facts; see `compose_stage1`)."""
    if judge is None:
        return None
    md, facts = _job_parts(job)
    if not md.strip() or not str(resume or "").strip():
        return None
    questions = stage1_questions()
    state = fitted_state(md, resume, questions)
    if state is None:
        return None
    answers = _ask(judge, state, questions)
    reads = stage1_reads(answers, questions) if answers is not None else None
    if reads is None:
        return None
    score, reason = compose_stage1(facts, reads)
    return {"score": score, "reason": reason}
