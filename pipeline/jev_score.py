"""Jev scoring for score_jobs.py (cycle 20: the scorer mirrors the LLM prompts).

Jev (TypeSafe System One) answers typed questions about a state and cannot
write text, so the split is: Jev decides, code composes. `score_jobs.py` calls
`stage1` and `stage2` once per job per stage when `use_jev` says so; each asks
one request about the state `{candidate, resume, job}` and composes the
columns the LLM path writes today. `None` from either means "use the LLM path"
for that job.

Cycle 20 rebuilt stage 1 and stage 2 to ask Jev the same rubric the LLM stage
prompts use, on Jev's own answer types (a Score for the 1-5 and the deep
score, a Choice for the main factor). Cycle 19's narrow reads and fitted
weights are gone. SP3 tuned the deep score's map against Gemini's own deep
score and moved the recommendation to a code read of the composed deep
score (`recommend`): Jev no longer picks it as a Choice. See
`docs/superpowers/specs/2026-09-28-jev-scorer-mirror-design.md` for the
rubric text this module copies verbatim.

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
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- composition constants (cycle 20: the scorer mirrors the LLM prompts) -----------
#
# Stage 1 asks Jev the stage 1 rubric directly (`fit`, a five-level Score) plus a
# `main_factor` Choice for the reason. `fit`'s value (Jev's probability-weighted
# level, 0-4) maps to the 1-5 score by `floor(value + 0.5) + 1`; code caps then
# apply the hard rules the rubric states (years, an advanced degree, a security
# clearance), as cycle 19 did. The cycle 19 calibration history that used to sit
# here (skills-fit thresholds, domain weighting) no longer applies: this module
# no longer builds the score from those narrow reads.
YEARS_CAPS: tuple[tuple[int, int], ...] = ((5, 1), (3, 2), (1, 3))  # min_years -> score cap
CLEARANCE_CAP = 1               # a new graduate cannot hold an active clearance
ADVANCED_DEGREE_CAP = 2         # a hard master's/PhD requirement the candidate lacks
SCORE_LABELS: dict[int, str] = {1: "No match", 2: "Weak match", 3: "Borderline",
                                4: "Good match", 5: "Strong match"}

# Stage 2 asks Jev the stage 2 rubric directly (`deep_fit`, a five-level Score);
# the recommendation is no longer a Choice Jev picks, it is read off the
# composed deep score (see RECOMMEND_APPLY, RECOMMEND_CONSIDER and `recommend`
# below). `deep_fit`'s value maps to 1-10 by `floor(DEEP_BASE + DEEP_SPAN *
# value / 4 + 0.5)`. Tuned 2026-09-28 against Gemini's deep score on 321
# stage 2 jobs (two samples), mean gap 0.53 / 0.54; Jev's `deep_fit` reads a
# mixed fit where Gemini gives about 8 of 10, so the map starts high. With
# these values a stage 2 deep score lands on 7 to 10.
DEEP_BASE = 6.5
DEEP_SPAN = 3.5
# The recommendation is read off the composed deep score: apply at
# RECOMMEND_APPLY or more, consider at RECOMMEND_CONSIDER or more, skip
# below; 83.2% / 81.9% agreement with Gemini, where Jev's own apply /
# consider / skip Choice agreed on 29.2% / 30.6%.
RECOMMEND_APPLY = 8
RECOMMEND_CONSIDER = 6
MIN_PREFERRED_LINES = 3         # requirement_lines(): below this many heading bullets,
                                 # other sections' bullets fill in too (unrelated to any
                                 # stage 2 minimum: stage 2 now runs on 0 to 30 lines)
MAX_REQUIREMENT_LINES = 30
MET_YES = 0.5                   # req_{i}_met at or above this counts the line as met
MUST_YES = 0.5                  # req_{i}_must at or above this makes the line a must-have
LIST_MAX = 5                    # strengths and gaps keep at most this many lines each
LINE_CHARS = 90                 # a strength or gap is cut to this length
REQ_TEXT_CHARS = 300            # a requirement line is cut to this length in its question
PLAIN_LINE_CHARS = 250          # an unbulleted line under a requirement heading, at most

# The request (SC-1): the job text is cut from its end until the request fits
# `jev.request_fits` (which keeps its own margin under Jev's limits); a job that
# would have to shrink below MIN_JOB_CHARS goes to the LLM path.
MIN_JOB_CHARS = 1_000
TRIM_STEP_CHARS = 200           # the cut job lands within this many characters of the longest fit

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
# How a stored switch value reads: a copy of `settings.switch_on` (local/settings.py),
# the rule the Settings checkbox and `jev_switch` use. The VM copies this file flat
# into ~/ with no local/, so it cannot import that module; a test holds the two equal.
SWITCH_ON_WORDS = frozenset(("true", "yes", "on", "1"))

# The reasons `jev_switch.jev_why_off("scoring")` gives, word for word, so the
# scorer and `jev_switch` name a switched-off Jev the same way.
REASON_SWITCH = "Jev is switched off in Settings"
REASON_AREA = "Jev is switched off for scoring in Settings"
REASON_KEY = "no TypeSafe API key"
REASON_SDK = "typesafe-sdk is not installed"
REASON_MODULE = "local/jev.py could not be imported"
REASON_NO_CONFIG = f"no {ENV_SWITCH} and no dashboard config beside the scorer"
# The reasons `use_jev` prints its own warning line for (the switch is on).
WARNED_REASONS = (REASON_KEY, REASON_SDK, REASON_MODULE)


# --- JS-4: does this run use Jev? -------------------------------------------------

def _read_config(path: Path) -> dict[str, Any]:
    """The dashboard config as a dict; {} when it is unreadable or holds no JSON
    object, so every switch reads its default (`jsonutil.read_json_dict`)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def switch_on(value: Any) -> bool:
    """Is a stored Jev switch `value` on? A bool is itself; any other value is
    on only as one of SWITCH_ON_WORDS once stripped and lower-cased, so a
    hand-edited null, 0, "" or "false" reads off (`settings.switch_on`)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in SWITCH_ON_WORDS


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
        if not switch_on(cfg.get(MASTER_KEY, True)):
            return False, REASON_SWITCH
        if not switch_on(cfg.get(AREA_KEY, True)):
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
    """The run's judge: a `jev.Guarded` TypeSafe judge with the short retries
    `jev_switch.client` gives the tailor (`jev.QUICK_RETRY_DELAYS_S`), so an
    outage reaches the LLM path in seconds, and whose breaker opens after it so
    the rest of the run takes that path (SC-5). The scorer builds it here: it
    reads its own switch (`use_jev`) and also runs on the VM, which has no
    `jev_switch` and no dashboard config. None, with one warning
    naming the error's class only, when it cannot be built."""
    try:
        jev = _jev_module()
        return jev.Guarded(jev.TypeSafeJev(), delays=jev.QUICK_RETRY_DELAYS_S)
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
    its end until `jev.request_fits` passes: a halving search for the longest
    start of the job that fits, to within TRIM_STEP_CHARS. None when even the
    first MIN_JOB_CHARS do not fit (a large résumé, say) or `local/jev.py` is missing."""
    try:
        jev = _jev_module()
    except Exception:           # noqa: BLE001  (no size check means no request)
        return None
    state = {"candidate": CANDIDATE, "resume": resume, "job": job_md}
    if jev.request_fits(state, questions):
        return state
    fits, over = MIN_JOB_CHARS, len(job_md)     # job_md[:fits] fits; job_md[:over] does not
    if fits >= over or not jev.request_fits(dict(state, job=job_md[:fits]), questions):
        return None
    while over - fits > TRIM_STEP_CHARS:
        mid = (fits + over) // 2
        if jev.request_fits(dict(state, job=job_md[:mid]), questions):
            fits = mid
        else:
            over = mid
    return dict(state, job=job_md[:fits])


# Error classes already reported this run, one line per class. The message itself
# is never printed: a service error can quote the request, which carries the résumé.
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


# --- stage 1 (score 1-5 and reason) ------------------------------------------------------

# Levels index 0 (no match) to 4 (strong match), the stage 1 prompt's own wording.
FIT_LEVELS = (
    "No match: the job is in another field (sales, recruiting, hardware, electrical, embedded "
    "or firmware work, or other work with no data, analysis or software in it), or it has a "
    "hard requirement the candidate cannot meet, such as a required master's degree or PhD or "
    "an active security clearance.",
    "Weak match: the job needs 3 or more years of experience or is a senior, staff, principal, "
    "lead or manager role, or the candidate's skills and tools mostly do not carry over to it.",
    "Borderline: the job asks for 1 or more years of experience, or only part of the "
    "candidate's skills, tools and field line up with it.",
    "Good match: the job has no experience bar, and the candidate's skills, tools and field "
    "line up with most of what it asks.",
    "Strong match: the job has no experience bar, and the candidate's skills, tools and field "
    "line up with nearly everything it asks.",
)

FIT_INSTRUCTIONS: dict[str, str] = {
    "question": ("How well does the job in `job` fit the candidate described in `candidate` "
                "and `resume`?"),
    "experience_bar": ("Use the lower bound of any experience range. A range starting at 0, "
                       "an entry-level, junior, new-grad, associate or level I label, or no "
                       "stated requirement all count as no experience bar."),
    "analyst_roles": ("Data, business, BI, reporting, analytics, product, operations, "
                      "marketing and research analyst roles are in the candidate's field: "
                      "judge them on whether the candidate can do the listed analytical work, "
                      "whatever the title or degree field."),
    "ignore": ("Location, on-site, hybrid or remote terms, relocation, time zone, visa "
              "sponsorship and work authorization never count for or against a job."),
}

MAIN_FACTOR_INSTRUCTIONS = ("What most decides how well the job in `job` fits the candidate "
                            "in `candidate` and `resume`?")

# option id -> what Jev reads.
MAIN_FACTOR_OPTIONS: dict[str, str] = {
    "skills_fit": ("The job has no experience bar, and the candidate's skills, tools and field "
                  "line up with it."),
    "partial_skills": ("The job is in the candidate's field, and several of the main tools or "
                       "skills it asks for are missing from the résumé."),
    "years_1_2": "The job asks for one or two years of experience.",
    "years_3_plus": "The job asks for three or more years of experience.",
    "senior": "The job is a senior, staff, principal, lead, manager or director role.",
    "different_field": ("The job is in a field the candidate did not train or work in: sales, "
                        "recruiting, hardware, embedded work, or work with no data, analysis "
                        "or software in it."),
    "degree": "The job requires a master's degree or PhD.",
    "clearance": "The job requires an active security clearance.",
}

# option id -> what the reason says.
FACTOR_TEXT: dict[str, str] = {
    "skills_fit": "no experience bar, and the skills, tools and field line up",
    "partial_skills": "in field; several main tools or skills are missing from the résumé",
    "years_1_2": "asks for 1 to 2 years of experience",
    "years_3_plus": "asks for 3 or more years of experience",
    "senior": "a senior, lead or manager role",
    "different_field": "a field outside data, analytics and software",
    "degree": "requires a master's degree or PhD",
    "clearance": "requires an active security clearance",
}


def stage1_questions() -> dict[str, dict]:
    """The two stage 1 questions, asked in one request: `fit` (a Score on
    FIT_LEVELS) and `main_factor` (a Choice among MAIN_FACTOR_OPTIONS)."""
    return {
        "fit": {
            "type": "score",
            "instructions": dict(FIT_INSTRUCTIONS),
            "criteria": list(FIT_LEVELS),
        },
        "main_factor": {
            "type": "choice",
            "instructions": MAIN_FACTOR_INSTRUCTIONS,
            "criteria": dict(MAIN_FACTOR_OPTIONS),
        },
    }


def stage1_reads(answers: Mapping[str, Any], questions: Mapping[str, dict]) -> dict | None:
    """The reads `compose_stage1` takes, or None when any answer is unusable."""
    reads = {
        "fit": _score_read(answers.get("fit"), len(questions["fit"]["criteria"])),
        "main_factor": _choice_read(answers.get("main_factor"),
                                    questions["main_factor"]["criteria"]),
    }
    return None if any(v is None for v in reads.values()) else reads


def _years_cap(years: int) -> int | None:
    return next((cap for floor, cap in YEARS_CAPS if years >= floor), None)


def compose_stage1(facts: Mapping[str, Any], reads: Mapping[str, Any]) -> tuple[int, str]:
    """(score 1-5, reason) from Jev's `fit` level and `main_factor` choice, and
    the code facts (`min_years`, `advanced_degree`, `clearance`). `fit` is
    scaled 0-1 (the level over the top level); its raw level (0-4) maps to the
    score by `floor(value + 0.5) + 1`. When a code fact caps the score below
    what `fit` gave, the reason names that fact in place of `main_factor` (the
    lowest cap's fact when several caps apply). Pure: the rules and constants
    at the top of the module."""
    frac = min(1.0, max(0.0, float(reads["fit"])))
    value = frac * (len(FIT_LEVELS) - 1)
    score = max(1, min(5, math.floor(value + 0.5) + 1))
    main_factor = str(reads["main_factor"])

    years = facts.get("min_years")
    degree = bool(facts.get("advanced_degree"))
    clearance = bool(facts.get("clearance"))

    cap_facts: list[tuple[int, str]] = []
    if years is not None:
        years = int(years)
        cap = _years_cap(years)
        if cap is not None:
            cap_facts.append((cap, f"the posting asks for {years}+ years of experience"))
    if clearance:
        cap_facts.append((CLEARANCE_CAP, "the posting requires a security clearance"))
    if degree:
        cap_facts.append((ADVANCED_DEGREE_CAP, "the posting requires a master's degree or PhD"))

    final, cap_text = score, None
    if cap_facts:
        lowest_cap, lowest_text = min(cap_facts, key=lambda ct: ct[0])
        if lowest_cap < score:
            final, cap_text = lowest_cap, lowest_text

    label = SCORE_LABELS[final]
    text = cap_text if cap_text is not None else FACTOR_TEXT.get(main_factor, main_factor)
    return final, f"{label}: {text}."


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


# --- SC-3: requirement lines -------------------------------------------------------------

@dataclass(frozen=True)
class Req:
    """One requirement line of a job description. `must` is the code cue: True
    under a required heading, False for a preferred cue (the heading's or the
    line's own), None when Jev decides."""
    text: str
    must: bool | None = None


_ATX_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*#*\s*$")
_BOLD_LINE_RE = re.compile(r"^\s*(?:\*\*|__)(?P<text>[^*_]+?)(?:\*\*|__)\s*:?\s*$")
_COLON_LINE_RE = re.compile(r"^\s*(?P<text>[^\s:*#\d\-+][^:]{1,59}):\s*$")
_BULLET_RE = re.compile(r"^\s{0,8}(?:(?:[-*+]|\(?\d{1,2}[.)])\s+"
                        r"|[•·▪●◦‣⁃–]\s*)(?P<text>\S.*)$")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~<])")

# Section headings, read in this order: a skipped section's lines never count; a
# heading that names both kinds ("Required and Preferred") leaves its lines to Jev;
# a preferred heading marks its lines nice to have; a requirement heading marks them
# required when it says so (required, minimum, basic, must) and leaves the rest to
# Jev, as an ideal-candidate heading does.
_SKIP_HEAD_RE = re.compile(
    r"benefit|perk|we offer|compensation|salary|\bpay\b|total rewards|about us"
    r"|about the company|who we are|equal (?:employment )?opportunit|\beeo\b|our values"
    r"|culture|why join|why you'll love|physical|work environment|working conditions"
    r"|schedule|location|travel|how to apply|disclaimer|accommodation")
_NICE_HEAD_RE = re.compile(
    r"prefer|nice[- ]to[- ]have|good[- ]to[- ]have|bonus|\bplus(?:es)?\b|desir"
    r"|extra credit|stand out|set you apart")
_REQ_HEAD_RE = re.compile(
    r"requir|qualif|\bmust\b|minimum|basic|what you(?:'ll)? (?:need|bring|have)"
    r"|you(?:'ll)? (?:need|bring|have)|who you are|about you|skills|experience|competenc"
    r"|knowledge|education|looking for|expertise|background|\bideal\b")
_MUST_HEAD_RE = re.compile(r"requir|minimum|basic|\bmust\b")
# A line's own preferred cue beats its heading.
_NICE_LINE_RE = re.compile(
    r"\b(?:preferred|preferably|a plus|nice[- ]to[- ]have|good[- ]to[- ]have|bonus|desired"
    r"|desirable|ideally|an advantage|advantageous)\b", re.I)
# Lines the stage 2 prompt forbids as gaps: where and when the work happens
# (location, on-site, hybrid or remote work, relocation, time zone, travel, hours,
# shifts) and eligibility lines that name no skill or tool (visa, sponsorship, work
# authorization, citizenship, checks, age, lifting). A word with a second sense
# (hybrid, remote, location, travel, visa, sponsor, citizen, shift, commute) drops a
# line only in its work-arrangement or eligibility wording, so "hybrid cloud",
# "remote sensing", "Visa and Mastercard payment data" and "project sponsorship"
# stay.
_ARRANGED = r"(?:hybrid|remote(?:ly)?)"
_DROP_PARTS = (
    # where the work happens
    r"(?:on[- ]?site|in[- ]office|in[- ]person|time[- ]?zones?)\b|relocat\w*",
    r"commut(?:e|es|ing|able)\b",
    r"located (?:in|within|near|at)\b|locations?\s*:|(?:office|work|job) locations?\b",
    rf"^{_ARRANGED}\s*(?:$|[(:,;]|[-–]\s|\d|/?\s*(?:us|u\.s\.?|usa|united states)(?!\w))",
    rf"{_ARRANGED}[- ](?:first|friendly|eligible|only)\b",
    rf"{_ARRANGED}\s+(?:within|in|from|across|anywhere|work(?:ing|place)?|role|position|job"
    r"|schedule|setting|environment|capacity|basis|option|opportunit\w*|arrangement|policy"
    r"|days?|candidates|applicants)\b",
    rf"(?:work\w*|fully|100%|partially|partly|primarily|mostly|is|be)\s+{_ARRANGED}\b",
    rf"{_ARRANGED}\s*(?:\bor\b|\band\b|/)\s*(?:hybrid|remote|on[- ]?site|in[- ]office)",
    # when the work happens
    r"(?:weekends?|overtime)\b",
    r"(?:night|day|evening|morning|overnight|weekend|rotating|early|late|split|flexible"
    r"|on[- ]call|first|second|third|any|\d+[- ]hour) shifts?\b",
    r"shifts?\s+(?:work|schedule|rotations?|differential)\b|work\w*\s+(?:in\s+)?shifts\b",
    r"(?:willing(?:ness)?|able|ability|availab\w*|required|expected|need) to travel\b",
    r"^travel\w*\s*(?:$|[(:,]|[-–]\s|\d)",
    r"travel requirements?\b.{0,40}?(?:\d+\s*%|\bup to\b|\bwilling)"
    r"|willing\w*\b.{0,40}?travel requirements?\b",
    r"travel\w*\s+(?:is\s+)?(?:required|expected|as needed|up to|\d)",
    r"travel\w*\s+to\s+(?:\w+\s+)?(?:client|customer|field|office|site)s?\b",
    r"\d+\s*%\s*(?:of the time\s*)?travel",
    r"(?:overnight|occasional|frequent|domestic|international|minimal|limited|extensive"
    r"|some) travel\b",
    # eligibility
    r"(?:not|cannot|to) sponsor\b|[’']t sponsor\b",
    r"(?:visa|immigration|employment|employer|work|h-?1-?b)\s+sponsorship\b",
    r"sponsorship\s+(?:for|of)\s+(?:an?\s+)?(?:employment|work|visas?|immigration|h-?1-?b)\b",
    r"sponsorship\s+(?:is\s+|will\s+)?(?:not\s+)?(?:available|provided|offered)\b",
    r"(?:requir\w*|need\w*|without|no|offer\w*|provid\w*)\s+(?:an?\s+|any\s+)?sponsorship\b",
    r"h-?1-?b\b|(?:work|employment|immigration|student)\s+visas?\b",
    r"visas?\s+(?:status|holders?|support|transfers?|requirements?|required|needed"
    r"|restrictions?)\b",
    r"(?:requir\w*|need\w*|without|hold\w*|obtain\w*|valid)\s+(?:an?\s+|any\s+)?visas?\b",
    r"sponsor(?:s|ed)?\s+(?:visas?|employment|work|candidates|applicants)\b",
    r"(?:work(?:ing)? authori[sz]ation|authori[sz]ed to work|eligib\w* to work"
    r"|right to work)\b",
    r"citizenship\b|(?:u\.?\s?s\.?|united states|american)\s+citizens?\b",
    r"(?:be|are|is)\s+(?:an?\s+)?citizens?\b|citizens?\s+(?:of|or)\b",
    r"background (?:check|screen\w*|investigation)|drug (?:test\w*|screen\w*)",
    r"driver[’']?s licen[cs]e|lift(?:ing)? (?:up to )?\d+\s*(?:lbs?|pounds)",
    r"18 years (?:of age|old|or older)|age of 18\b|18 or older",
)
_DROP_RE = re.compile(r"\b(?:" + "|".join(_DROP_PARTS) + ")", re.I)


def _plain(text: str) -> str:
    """A line with its markdown taken out: links, escapes, emphasis, spacing."""
    t = _LINK_RE.sub(r"\1", text)
    t = _ESCAPE_RE.sub(r"\1", t)
    t = t.replace("**", "").replace("__", "").replace("*", "").replace("`", "")
    return " ".join(t.split()).strip(" ;,:.")


def _cut(text: str, limit: int) -> str:
    """`text` cut to `limit` characters at a word boundary, marked with "..."."""
    if len(text) <= limit:
        return text
    head = text[:limit - 3]
    space = head.rfind(" ")
    if space >= limit // 2:
        head = head[:space]
    return head.rstrip(" ,;:.") + "..."


def _heading_kind(heading: str) -> tuple[str, bool | None]:
    """("skip" | "req" | "other", the heading's must cue)."""
    h = _plain(heading).lower().replace("’", "'")
    if _SKIP_HEAD_RE.search(h):
        return "skip", None
    must = _MUST_HEAD_RE.search(h) is not None
    if _NICE_HEAD_RE.search(h):
        return "req", (None if must else False)
    if _REQ_HEAD_RE.search(h):
        return "req", (True if must else None)
    return "other", None


def _sections(desc: str) -> list[tuple[str, bool | None, list[str], list[str]]]:
    """The description cut at its headings: (kind, must cue, bullet lines, plain lines)."""
    sections: list[tuple[str, bool | None, list[str], list[str]]] = [("other", None, [], [])]
    for line in desc.splitlines():
        if not line.strip():
            continue
        head = _ATX_RE.match(line) or _BOLD_LINE_RE.match(line)
        bullet = None if head else _BULLET_RE.match(line)
        if head is None and bullet is None:
            head = _COLON_LINE_RE.match(line)
        if head is not None:
            sections.append((*_heading_kind(head.group("text")), [], []))
        elif bullet is not None:
            sections[-1][2].append(bullet.group("text"))
        else:
            sections[-1][3].append(line.strip())
    return sections


def requirement_lines(desc: Any, limit: int = MAX_REQUIREMENT_LINES) -> list[Req]:
    """Up to `limit` requirement lines from a job description's markdown (SC-3).

    Bullet and numbered lines under requirement or qualification headings come
    first (a heading with no bullets lends its short plain lines); when those
    are fewer than MIN_PREFERRED_LINES, bullets from the other sections follow
    in document order. Benefit, company and similar sections never count, and
    lines about where or when the work happens (location, on-site, hybrid or
    remote work, relocation, time zone, travel, shifts) or about eligibility
    (visa, sponsorship, work authorization, a background check) are dropped; a
    skill that shares a word with them ("hybrid cloud", "remote sensing") stays.
    A bullet that ends in a colon introduces a list and is dropped too;
    duplicates keep their first place."""
    if not isinstance(desc, str) or not desc.strip():
        return []
    preferred: list[Req] = []
    others: list[Req] = []
    seen: set[str] = set()
    for kind, must, bullets, plains in _sections(desc):
        if kind == "skip":
            continue
        if kind == "req":
            lines = bullets or [p for p in plains if len(p) <= PLAIN_LINE_CHARS]
        else:
            lines = bullets
        for raw in lines:
            if raw.rstrip(" *_").endswith(":"):
                continue
            text = _plain(raw)
            key = text.casefold()
            if sum(c.isalpha() for c in text) < 2 or key in seen or _DROP_RE.search(text):
                continue
            seen.add(key)
            cue = False if _NICE_LINE_RE.search(text) else must
            (preferred if kind == "req" else others).append(Req(_cut(text, REQ_TEXT_CHARS), cue))
    picked = preferred if len(preferred) >= MIN_PREFERRED_LINES else preferred + others
    return picked[:max(0, int(limit))]


# --- stage 2 (deep score 1-10, strengths, gaps, recommendation) ---------------------------

MET_CRITERIA = {
    "true": ("The résumé or the candidate context shows it: the skill, tool, degree or "
             "experience appears in the degree, the internship, a project or coursework."),
    "false": "Nothing in the résumé or the candidate context shows it.",
}
MUST_CRITERIA = {
    "true": "The job lists it as required: a must-have, minimum or basic qualification.",
    "false": "The job lists it as preferred, a plus, a bonus or nice to have.",
}

# Levels index 0 (poor fit) to 4 (excellent fit), the stage 2 prompt's own wording.
DEEP_FIT_LEVELS = (
    "Poor fit: the candidate lacks most of the job's must-have requirements, or the "
    "day-to-day work is a kind they have not done.",
    "Weak fit: the candidate meets some must-have requirements; key tools, skills or "
    "experience the job needs are missing.",
    "Mixed fit: the candidate meets several core requirements and misses others; they could "
    "do the job after some ramp-up.",
    "Good fit: the candidate meets the core requirements; what is missing is nice-to-have or "
    "a tool they could pick up quickly.",
    "Excellent fit: the candidate meets nearly every requirement, has done this kind of work "
    "in the internship or projects, and the role is aimed at new graduates.",
)

DEEP_FIT_INSTRUCTIONS: dict[str, str] = {
    "question": ("How well does the candidate in `candidate` and `resume` fit the job in "
                "`job`, judged on its stated requirements and day-to-day work?"),
    "not_gaps": ("Location, on-site, hybrid or remote terms, relocation, time zone, visa "
                "sponsorship, work authorization and graduation timing are never gaps. For "
                "analytical roles, career path, business background, degree field and "
                "job-title history are never gaps."),
    "ignore": "Company descriptions, benefits and application instructions.",
}


def stage2_questions(reqs: Sequence[Req]) -> dict[str, dict]:
    """A met noul per requirement line, a must noul for each line the code gives
    no cue, and `deep_fit` (a Score on DEEP_FIT_LEVELS), asked in one request.
    `reqs` may be empty: `deep_fit` needs no requirement lines. The
    recommendation is no longer asked of Jev as a Choice; `compose_stage2`
    reads it off the composed deep score (`recommend`)."""
    qs: dict[str, dict] = {}
    for i, req in enumerate(reqs):
        text = _cut(req.text, REQ_TEXT_CHARS)
        qs[f"req_{i}_met"] = {
            "type": "noul",
            "instructions": ("Do `resume` and `candidate` show that the candidate meets this "
                             f"requirement of the job? Requirement: {text}"),
            "criteria": dict(MET_CRITERIA),
        }
        if req.must is None:
            qs[f"req_{i}_must"] = {
                "type": "noul",
                "instructions": ("Does `job` present this requirement as a must-have? "
                                 f"Requirement: {text}"),
                "criteria": dict(MUST_CRITERIA),
            }
    qs["deep_fit"] = {
        "type": "score",
        "instructions": dict(DEEP_FIT_INSTRUCTIONS),
        "criteria": list(DEEP_FIT_LEVELS),
    }
    return qs


def stage2_reads(answers: Mapping[str, Any], questions: Mapping[str, dict],
                 lines: int) -> dict | None:
    """The reads `compose_stage2` takes, or None when any answer is unusable.
    A line's must read is taken only when `questions` asked it. No
    `recommendation` read: `compose_stage2` derives it from the composed
    deep score."""
    reads: dict[str, Any] = {}
    for i in range(lines):
        for part in ("met", "must"):
            qid = f"req_{i}_{part}"
            if part == "met" or qid in questions:
                reads[qid] = _noul_read(answers.get(qid))
    reads["deep_fit"] = _score_read(answers.get("deep_fit"), len(questions["deep_fit"]["criteria"]))
    return None if any(v is None for v in reads.values()) else reads


def _joined(lines: list[str]) -> str:
    """Up to LIST_MAX lines, each cut to LINE_CHARS, joined with " | " as the LLM path joins them."""
    return " | ".join(_cut(t.replace("|", "/"), LINE_CHARS) for t in lines[:LIST_MAX])


def recommend(deep: int) -> str:
    """The recommendation read off a composed deep score: apply at
    RECOMMEND_APPLY or more, consider at RECOMMEND_CONSIDER or more, skip
    below. Pure: the constants at the top of the module."""
    if deep >= RECOMMEND_APPLY:
        return "apply"
    if deep >= RECOMMEND_CONSIDER:
        return "consider"
    return "skip"


def compose_stage2(reqs: Sequence[Req], reads: Mapping[str, Any]) -> dict:
    """{"deep_score", "strengths", "gaps", "recommendation", "findings"} from the
    requirement lines and Jev's reads (`req_{i}_met`, `req_{i}_must` 0-1 and
    `deep_fit` 0-1). A line's code cue decides whether it is a must-have and
    beats any `req_{i}_must` read; a line with no cue and no read counts as
    one. `deep_score` maps `deep_fit`'s value (0-4) to 1-10 by
    `floor(DEEP_BASE + DEEP_SPAN * value / 4 + 0.5)`; since `deep_fit` already
    arrives scaled to 0-1 (value / 4), that is `floor(DEEP_BASE + DEEP_SPAN *
    deep_fit + 0.5)`. `recommendation` is `recommend(deep_score)`: apply,
    consider or skip by where the composed deep score falls, no longer a
    Choice Jev picks. `findings` carries the full, unjoined line texts (met =
    must-haves first, as `strengths` orders them) for the writer (SP2). Pure:
    the rules and constants at the top of the module."""
    lines = []
    for i, req in enumerate(reqs):
        met = min(1.0, max(0.0, float(reads[f"req_{i}_met"])))
        must = req.must if req.must is not None else (
            float(reads.get(f"req_{i}_must", 1.0)) >= MUST_YES)
        lines.append((req.text, met, must))
    met_must = [t for t, met, must in lines if must and met >= MET_YES]
    met_nice = [t for t, met, must in lines if not must and met >= MET_YES]
    gaps_must = [t for t, met, must in lines if must and met < MET_YES]
    gaps_nice = [t for t, met, must in lines if not must and met < MET_YES]
    strengths = met_must + met_nice

    fit = min(1.0, max(0.0, float(reads["deep_fit"])))
    deep = max(1, min(10, math.floor(DEEP_BASE + DEEP_SPAN * fit + 0.5)))
    findings = {"met": list(strengths), "unmet_must": list(gaps_must),
               "unmet_nice": list(gaps_nice)}
    return {"deep_score": deep, "strengths": _joined(strengths), "gaps": _joined(gaps_must),
            "recommendation": recommend(deep), "findings": findings}


def stage2(judge: Any, job: Any, resume: str) -> dict | None:
    """{"deep_score", "strengths", "gaps", "recommendation", "findings"} for one
    job from one Jev request, or None to run that job's stage 2 on the LLM
    path: no judge, a request that failed or did not fit, an answer that could
    not be read. `requirement_lines()` may find zero lines (0 to
    MAX_REQUIREMENT_LINES); zero lines means no per-line questions and empty
    strengths and gaps, but `deep_fit` still runs, and `recommendation` is
    read off its composed deep score. `job` is as for `stage1`."""
    if judge is None:
        return None
    md, _facts = _job_parts(job)
    if not md.strip() or not str(resume or "").strip():
        return None
    reqs = requirement_lines(md)
    questions = stage2_questions(reqs)
    state = fitted_state(md, resume, questions)
    if state is None:
        return None
    answers = _ask(judge, state, questions)
    reads = stage2_reads(answers, questions, len(reqs)) if answers is not None else None
    if reads is None:
        return None
    return compose_stage2(reqs, reads)
