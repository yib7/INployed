"""The tailor's Jev requests (TL-1 to TL-3).

Jev (`local/jev.py`) answers typed questions about a state and writes no text, so
each helper here asks one kind of question and hands the answers back as data for
code to compose. The LLM still writes every bullet.

  skills_pick     TL-1  one noul per skill in the user's pools and the skill focus
                        (a choice over select's enum), in one request. Each skills
                        line comes back as its pool in probability order;
                        compress_skills cuts it to the line's count and width.
  atom_relevance  TL-2  one noul per atom's `what`, batched to fit: the
                        probabilities select() shortlists its catalog by.
  lead_group      TL-3  one choice per project over its numbered bullet groups: the
                        overview bullet lead_with_overview moves to the front.

Every helper takes `judge=`. Left out, it is `jev_switch.client("tailor")`, which is
None when Jev is off for the tailor. A helper returns None when Jev is off, when
there is nothing to ask, when a request fails or an answer comes back unusable, and
once `jev.Guarded`'s breaker is open; its caller then keeps the LLM path it had
before cycle 19. `run.tailor()` builds one judge per run and hands it to every
step, so one outage moves the rest of that run to the LLM path (JS-3).

`usage_line(step)` is a step's line in the run report: its requests, their tokens
and what those cost. The tokens are estimated (`jev.request_size`): the live client
counts real tokens only for the whole process (`jev.usage()`), and the dashboard
tailors several jobs at once, one worker thread each. The counts here are kept per
thread for the same reason, and `run.tailor()` clears them with `reset_usage()` as
a run starts.

`jev` and `jev_switch` live one directory up, in `local/`; they are imported on
first use, the way `run.py` reaches `jobsdata`.
"""
from __future__ import annotations

import logging
import math
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import assets
from . import skills as _skills

log = logging.getLogger(__name__)

# The steps a run reports, one usage line each.
STEP_SKILLS = "skills"
STEP_SHORTLIST = "shortlist"
STEP_LEAD = "lead"

# What a helper's `judge` is when the caller passes none: `default_judge()`.
_DEFAULT: Any = object()

# How much of the job description a request's state carries. A noul reads the whole
# state, and a long posting ends in benefits and legal text.
JD_CHARS = 12_000

# TL-3: a lead Jev picks with a confidence under this keeps file order.
LEAD_MIN_CONFIDENCE = 0.5

# The judge questions. A backticked path names a part of the request's state;
# `{i}` is the item's index there.
SKILL_QUESTION = "Does `job` ask for `skills[{i}]` or a direct equivalent?"
ATOM_QUESTION = "Does `atoms[{i}]` show experience `job` asks for?"
LEAD_QUESTION = ("Which bullet describes `projects[{i}]` as a whole, saying what the "
                 "project is?")
FOCUS_QUESTION = "Which focus fits the work `job` describes?"
# select()'s skill_focus enum, each value with the description Jev reads.
SKILL_FOCUS = {
    "ml_research": "Machine learning research: training and evaluating models",
    "backend_platform": "Backend and platform engineering: services, APIs and infrastructure",
    "data_analytics": "Data analytics: SQL, dashboards and reporting",
    "general": "General software or data work with no single focus",
}

_RUN = threading.local()


class _NothingToAsk(Exception):
    """The step has no question to send."""


class _Unusable(Exception):
    """An answer is missing or out of range."""


def _jev_modules() -> Tuple[Any, Any]:
    """(jev, jev_switch), with `local/` put on sys.path when it is missing."""
    local_dir = str(Path(__file__).resolve().parents[1])
    if local_dir not in sys.path:
        sys.path.insert(0, local_dir)
    import jev
    import jev_switch
    return jev, jev_switch


def default_judge() -> Any:
    """`jev_switch.client("tailor")`: the tailor's guarded TypeSafe judge, or None
    when Jev is off for the tailor. A client that cannot be built reads as off."""
    try:
        _jev, switch = _jev_modules()
        return switch.client("tailor")
    except Exception as exc:  # noqa: BLE001 - off keeps every caller on its LLM path
        log.warning("jev_assist: the tailor judge could not be built (%s)", type(exc).__name__)
        return None


# ── usage ────────────────────────────────────────────────────────────────────
def reset_usage() -> None:
    """Clear this thread's step counts; `run.tailor()` calls it as a run starts."""
    _RUN.steps = {}


def _step_counts(step: str) -> Dict[str, Any]:
    steps = getattr(_RUN, "steps", None)
    if steps is None:
        steps = _RUN.steps = {}
    return steps.setdefault(step, {"requests": 0, "tokens": 0, "note": ""})


def usage(step: str) -> Dict[str, Any]:
    """{"requests", "tokens", "usd", "note"} for `step` in this thread's run. A
    request counts once the judge answers it; `note` says why the step returned
    None ("nothing to ask", or the failure that sent it back to the LLM path)."""
    jev, _switch = _jev_modules()
    counts = (getattr(_RUN, "steps", None) or {}).get(step) or {
        "requests": 0, "tokens": 0, "note": ""}
    return {"requests": counts["requests"], "tokens": counts["tokens"],
            "usd": jev.usd_for(counts["tokens"]), "note": counts["note"]}


def usage_line(step: str) -> str:
    """The run report's line for `step`, e.g. "jev shortlist: 1 request, 812 tokens
    (estimated), $0.000034", with "; <note>" after it when the step has one."""
    u = usage(step)
    n = u["requests"]
    line = (f"jev {step}: {n} request{'' if n == 1 else 's'}, {u['tokens']} tokens "
            f"(estimated), ${u['usd']:.6f}")
    return f"{line}; {u['note']}" if u["note"] else line


# ── plumbing ─────────────────────────────────────────────────────────────────
def _failure_kind(exc: BaseException) -> str:
    """The failure's class and status. Its message stays out of the log and the
    report: a service error can quote the request, which carries the job."""
    jev, _switch = _jev_modules()
    if isinstance(exc, jev.JudgeOutage):
        return f"JudgeOutage {exc.kind}"
    return jev.error_kind(exc)


def _run_step(step: str, judge: Any, ask: Callable[[Any], Any]) -> Any:
    """`ask(judge)` for one step: None when Jev is off, when there is nothing to
    ask, and on any failure, with the step's note saying which."""
    counts = _step_counts(step)
    counts["note"] = ""
    if judge is _DEFAULT:
        judge = default_judge()
    if judge is None:
        return None
    try:
        return ask(judge)
    except _NothingToAsk:
        counts["note"] = "nothing to ask"
    except _Unusable:
        counts["note"] = "fell back to the LLM path (unusable answer)"
        log.warning("jev_assist: jev %s got an unusable answer; the LLM path runs", step)
    except Exception as exc:  # noqa: BLE001 - any judge failure keeps the LLM path
        kind = _failure_kind(exc)
        counts["note"] = f"fell back to the LLM path ({kind})"
        log.warning("jev_assist: jev %s failed (%s); the LLM path runs", step, kind)
    return None


def _send(step: str, judge: Any, state: Any, questions: Dict[str, dict]) -> Mapping[str, Any]:
    """One request, counted once the judge answers it."""
    jev, _switch = _jev_modules()
    answers = judge.judge(state, questions)
    counts = _step_counts(step)
    counts["requests"] += 1
    counts["tokens"] += jev.request_size(state, questions)[1]
    if not isinstance(answers, Mapping):
        raise _Unusable(step)
    return answers


def _fit_spans(n: int, request: Callable[[int, int], Tuple[Any, Dict[str, dict]]]
               ) -> List[Tuple[int, int]]:
    """Contiguous (lo, hi) spans over `n` items, in order, whose requests fit Jev's
    limits: the whole list when it fits, else halves, halved again until each fits.
    A single item goes as it is."""
    jev, _switch = _jev_modules()
    todo = [(0, n)]
    spans: List[Tuple[int, int]] = []
    while todo:
        lo, hi = todo.pop()
        if hi - lo > 1 and not jev.request_fits(*request(lo, hi)):
            mid = (lo + hi) // 2
            todo += [(mid, hi), (lo, mid)]      # the first half is popped first
            continue
        spans.append((lo, hi))
    return spans


def _noul_prob(answer: Any) -> float:
    p = getattr(answer, "noul", None)
    if (isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p)
            or not 0.0 <= p <= 1.0):
        raise _Unusable("noul")
    return float(p)


def _choice_pick(answer: Any, names: Sequence[str]) -> Tuple[str, float]:
    """(the option picked, its confidence); a missing confidence reads as 0.0."""
    choice = getattr(answer, "choice", None)
    if choice not in names:
        raise _Unusable("choice")
    conf = getattr(answer, "confidence", None)
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf):
        conf = 0.0
    return str(choice), min(1.0, max(0.0, float(conf)))


def _job_state(jd: str, job_title: str) -> Dict[str, str]:
    return {"title": str(job_title or ""), "description": str(jd or "")[:JD_CHARS]}


def _ask_nouls(step: str, judge: Any, job: Dict[str, str], key: str, items: Sequence[str],
               question: str, extra: Optional[Dict[str, dict]] = None
               ) -> Tuple[List[float], Dict[str, Any]]:
    """P(yes) of `question` for each of `items`, in order, plus the answers to
    `extra`, which rides in the first request. The state is the job and the items
    under `key`; past Jev's limits the items go out in several requests."""
    extra = extra or {}

    def request(lo: int, hi: int) -> Tuple[Dict[str, Any], Dict[str, dict]]:
        state = {"job": job, key: list(items[lo:hi])}
        questions = {f"{key}_{i}": {"type": "noul", "instructions": question.format(i=i)}
                     for i in range(hi - lo)}
        if lo == 0:
            questions.update(extra)
        return state, questions

    probs: List[float] = []
    extras: Dict[str, Any] = {}
    for lo, hi in _fit_spans(len(items), request):
        state, questions = request(lo, hi)
        answers = _send(step, judge, state, questions)
        probs += [_noul_prob(answers.get(f"{key}_{i}")) for i in range(hi - lo)]
        if lo == 0:
            extras = {qid: answers.get(qid) for qid in extra}
    return probs, extras


# ── the helpers ──────────────────────────────────────────────────────────────
def skills_pick(jd: str, job_title: str, *, judge: Any = _DEFAULT
                ) -> Optional[Dict[str, Any]]:
    """TL-1: how strongly the job asks for each skill in the user's pools, and the
    skill focus.

    One noul per distinct skill ("Does `job` ask for `skills[i]` or a direct
    equivalent?") and the focus, a choice over select's enum, in one request (split
    only when it would not fit). Returns {"lines": {label: the line's pool, most
    probable first, equal probabilities in pool order}, "skill_focus": one of
    SKILL_FOCUS, "probabilities": {skill: P(yes)}}. Every skill named is one of the
    user's own; None when Jev is off or fails, or the pools are empty."""
    def ask(j: Any) -> Dict[str, Any]:
        pools = {label: [str(s) for s in pool if str(s).strip()]
                 for label, pool in _skills._skill_pools().items()}
        names = list(dict.fromkeys(s for pool in pools.values() for s in pool))
        if not names:
            raise _NothingToAsk
        focus = {"focus": {"type": "choice", "instructions": FOCUS_QUESTION,
                           "criteria": dict(SKILL_FOCUS)}}
        probs, extra = _ask_nouls(STEP_SKILLS, j, _job_state(jd, job_title), "skills",
                                  names, SKILL_QUESTION, focus)
        skill_focus, _conf = _choice_pick(extra.get("focus"), list(SKILL_FOCUS))
        p = dict(zip(names, probs))
        lines = {label: sorted(pool, key=lambda s: -p[s]) for label, pool in pools.items()}
        return {"lines": lines, "skill_focus": skill_focus, "probabilities": p}
    return _run_step(STEP_SKILLS, judge, ask)


def atom_relevance(jd: str, job_title: str, *, judge: Any = _DEFAULT
                   ) -> Optional[Dict[str, float]]:
    """TL-2: {atom id: P(yes)} for every atom in the master, in file order.

    One noul per atom ("Does `atoms[i]` show experience `job` asks for?") over the
    atom's `what`, batched to fit Jev's limits. select() keeps each block's most
    probable atoms and the strongest projects (`selection._shortlist`). None when
    Jev is off or fails, or the master has no atoms."""
    def ask(j: Any) -> Dict[str, float]:
        atoms = assets.atoms_by_id()
        ids = list(atoms)
        if not ids:
            raise _NothingToAsk
        whats = [str(atoms[aid].get("what", "") or "") for aid in ids]
        probs, _extra = _ask_nouls(STEP_SHORTLIST, j, _job_state(jd, job_title), "atoms",
                                   whats, ATOM_QUESTION)
        return dict(zip(ids, probs))
    return _run_step(STEP_SHORTLIST, judge, ask)


def lead_group(projects: Sequence[Mapping[str, Any]], *, judge: Any = _DEFAULT
               ) -> Optional[Dict[str, Tuple[int, float]]]:
    """TL-3: which of each project's bullets describes the whole project.

    `projects` is [{"project": name, "bullets": [summary, ...]}], the bullets in
    their selected order. One choice per project over its numbered bullets ("Which
    bullet describes `projects[k]` as a whole, saying what the project is?"), every
    project in one request. Returns {name: (bullet number counting from 1,
    confidence)}; the caller keeps file order under LEAD_MIN_CONFIDENCE. A project
    with one bullet has nothing to choose and is left out. None when Jev is off or
    fails, or no project has two bullets."""
    def ask(j: Any) -> Dict[str, Tuple[int, float]]:
        asked = [p for p in projects if len(p.get("bullets") or []) >= 2]
        if not asked:
            raise _NothingToAsk
        state = {"projects": [str(p["project"]) for p in asked]}
        questions = {
            f"project_{k}": {
                "type": "choice", "instructions": LEAD_QUESTION.format(i=k),
                "criteria": {str(n): str(text) for n, text in enumerate(p["bullets"], start=1)}}
            for k, p in enumerate(asked)}
        answers = _send(STEP_LEAD, j, state, questions)
        out: Dict[str, Tuple[int, float]] = {}
        for k, p in enumerate(asked):
            qid = f"project_{k}"
            choice, conf = _choice_pick(answers.get(qid), list(questions[qid]["criteria"]))
            out[str(p["project"])] = (int(choice), conf)
        return out
    return _run_step(STEP_LEAD, judge, ask)
