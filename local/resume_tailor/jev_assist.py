"""The tailor's Jev requests (TL-1 to TL-9).

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
  faithfulness    TL-4  one request per résumé entry, three questions per bullet
                        against the atoms it was written from: `supported` (a
                        choice), `inflates` and `adds_claim` (nouls). A bullet is
                        flagged by a sure "unsupported" or "contradicted" or by
                        `inflates`; `adds_claim` only joins a flagged bullet's
                        finding (VL-3: the live judge reads it high on nearly
                        every faithful rephrase). A flagged bullet comes back with
                        its finding, which the run hands to one reground call
                        before it reverts or drops the bullet.
  sweep_flags     TL-5  one request per résumé entry, one noul per bullet for each
                        tell of the user's banned patterns (SWEEP_QUESTIONS): the
                        AI-writing sweep calls the model only for an entry with a
                        tell at SWEEP_FLAG or more or a detector finding, and names
                        the tells in that call's payload.
  pick_verb       TL-6  two choices per repeated opening verb: the palette category
                        (the kind of action the bullet describes), then a verb
                        among that category's unused ones. dedupe_leading_verbs
                        swaps the bullet's first word for a sure pick and keeps
                        its reverb call otherwise.
  best_variant    TL-7  one choice per bullet over its rephrase drafts that pass
                        the grounding gate and TL-4 (Settings: "Best of 3 bullet
                        drafts", off by default): the draft the run keeps.
  letter_unsupported
                  TL-8  one noul per cover-letter sentence against the letter's
                        sources, never the job description (Settings: "Jev
                        checks the cover letter's claims", off by default): a
                        flagged sentence goes to the letter's repair call.
  keyword_meaning TL-9  one noul per ATS keyword against the résumé's text
                        (Settings: "ATS report: coverage by meaning (Jev)", off by
                        default): the keywords the report's meaning-level coverage
                        line counts.

Every helper takes `judge=`. Left out, it is `jev_switch.client("tailor")`, which is
None when Jev is off for the tailor. A helper returns None when Jev is off, when
there is nothing to ask, when a request fails or an answer comes back unusable, and
once `jev.Guarded`'s breaker is open; its caller then keeps the LLM path it had
before cycle 19. `run.tailor()` builds one judge per run and hands it to every
step, so one outage moves the rest of that run to the LLM path (JS-3). TL-4 has no
LLM path of its own: without it, the deterministic grounding gate runs alone, as it
did before cycle 19. Without TL-5, the sweep calls the model for every item, as it
did before.

`usage_line(step)` is a step's line in the run report: its requests, their tokens
and what those cost. The tokens are estimated (`jev.request_size`): the live client
counts real tokens only for the whole process (`jev.usage()`), and the dashboard
tailors several jobs at once, one worker thread each. The counts here are kept per
thread for the same reason, and `run.tailor()` clears them with `reset_usage()` as
a run starts. A step can be asked several times in one run (TL-4 after every
rewrite), and its line sums every request.

`jev` and `jev_switch` live one directory up, in `local/`; they are imported on
first use, the way `run.py` reaches `jobsdata`.
"""
from __future__ import annotations

import logging
import math
import sys
import threading
from pathlib import Path
from typing import (Any, Callable, Collection, Dict, List, Mapping, Optional, Sequence,
                    Tuple)

from . import assets
from . import skills as _skills

log = logging.getLogger(__name__)

# The steps a run reports, one usage line each.
STEP_SKILLS = "skills"
STEP_SHORTLIST = "shortlist"
STEP_LEAD = "lead"
STEP_VERB = "verb"
STEP_SWEEP_GATE = "sweep gate"
STEP_FAITHFULNESS = "faithfulness"
STEP_BEST_OF = "best of three"
STEP_LETTER = "letter check"
STEP_ATS_MEANING = "ats meaning"

# What a step's note says its caller fell back to when the step fails.
LLM_PATH = "the LLM path"
GATE_ALONE = "the deterministic gate alone"     # TL-4: no LLM path to fall back to
NOTHING_TO_ASK = "nothing to ask"

# What a helper's `judge` is when the caller passes none: `default_judge()`.
_DEFAULT: Any = object()

# How much of the job description a request's state carries. A noul reads the whole
# state, and a long posting ends in benefits and legal text.
JD_CHARS = 12_000

# TL-3: a lead Jev picks with a confidence under this keeps file order.
LEAD_MIN_CONFIDENCE = 0.5

# TL-6: a verb Jev picks with a confidence under this keeps the LLM `reverb` call.
VERB_MIN_CONFIDENCE = 0.5
# TL-6 stage 1: a category Jev picks with a confidence under this is set aside for
# the repeated verb's own category. active_words.md's categories share verbs (558
# entries, 368 verbs), so an unsure pick is a bullet between two kinds of action,
# and the category the writer's verb sits in is then the safer ground. 0.5 is the
# floor the tailor's other choices use: the pick outweighs every other option
# together.
CATEGORY_MIN_CONFIDENCE = 0.5
# TL-6: a verb longer than this many characters is no verb and is left out of the
# options (the palette's longest is 14). VL-3: one choice over every unused verb,
# about 360, went past the 255 options Jev takes (`jev.CHOICE_OPTIONS_MAX`) and came
# back 400; one category's verbs (43 to 83 in active_words.md) stay well under it.
VERB_ID_MAX = 40

# TL-5: a tell Jev reads at this P(yes) or more flags its bullet, and the AI-writing
# sweep calls the model for the item that holds it.
SWEEP_FLAG = 0.6

# TL-4 (the rule is from VL-3, the live check over 54 real bullets and 8 planted
# ones): a bullet is flagged when Jev picks "unsupported" or "contradicted" at
# SUPPORTED_MIN_CONFIDENCE or more, or `inflates` reaches FAITHFULNESS_FLAG.
# `adds_claim` flags nothing alone: it read 0.5 to 0.95 on nearly every faithful
# rephrase, and a merge of two atoms read 0.82. A flagged bullet's finding still
# names it at FAITHFULNESS_FLAG or more, so the reground hears it. The real
# bullets' "verified" picks ran from 0.23 to 0.99 confidence and their
# "unsupported" picks from 0.24 to 0.59, so neither an unsure "verified" nor an
# unsure other pick flags; every planted inflation or added claim read
# "unsupported" or "contradicted" at 0.73 or more, and `inflates` topped out at
# 0.33 on the real bullets and read 0.95 and 0.96 on the two planted inflations.
SUPPORTED_MIN_CONFIDENCE = 0.6
FAITHFULNESS_FLAG = 0.7

# TL-8: a letter sentence Jev reads at this P(yes) or more claims more than the
# letter's sources state, and goes to the letter's repair call.
LETTER_CLAIM_FLAG = 0.7

# TL-9: a keyword Jev reads at this P(yes) or more counts in the meaning-level line.
KEYWORD_MEANING_MIN = 0.5

# The judge questions. A backticked path names a part of the request's state;
# `{i}` is the item's index there.
SKILL_QUESTION = "Does `job` ask for `skills[{i}]` or a direct equivalent?"
ATOM_QUESTION = "Does `atoms[{i}]` show experience `job` asks for?"
LEAD_QUESTION = ("Which bullet describes `projects[{i}]` as a whole, saying what the "
                 "project is?")
FOCUS_QUESTION = "Which focus fits the work `job` describes?"
# TL-6, asked of a bullet whose opening verb another bullet already uses.
PICK_VERB_QUESTION = "Which verb best names the action in `bullet`?"
# TL-6 stage 1, over the palette's categories.
PICK_CATEGORY_QUESTION = "Which kind of action does `bullet` describe?"
# TL-6 stage 1: each category's description names this many of its verbs.
CATEGORY_SAMPLE_VERBS = 8
# select()'s skill_focus enum, each value with the description Jev reads.
SKILL_FOCUS = {
    "ml_research": "Machine learning research: training and evaluating models",
    "backend_platform": "Backend and platform engineering: services, APIs and infrastructure",
    "data_analytics": "Data analytics: SQL, dashboards and reporting",
    "general": "General software or data work with no single focus",
}
# TL-4, asked of each bullet against the atoms it was written from.
SUPPORTED_QUESTION = "Do `atoms[{i}]` state every claim `bullets[{i}]` makes?"
SUPPORTED_OPTIONS = {
    "verified": "The atoms state every claim the bullet makes.",
    "unsupported": "The bullet makes a claim the atoms leave out.",
    "contradicted": "The bullet makes a claim an atom contradicts.",
}
INFLATES_QUESTION = ('Does `bullets[{i}]` give the candidate a bigger role, scope or result '
                     'than `atoms[{i}]` state, such as "led" for "helped"?')
ADDS_CLAIM_QUESTION = ("Does `bullets[{i}]` state a tool, number, outcome or scope that "
                       "`atoms[{i}]` do not state?")
# TL-5, asked of each bullet before the AI-writing sweep: one question per tell of
# the user's banned patterns, keyed by the name the sweep payload and the report
# give it.
SWEEP_QUESTIONS = {
    "contrast framing": ("Does `bullets[{i}]` use contrast framing, which defines a thing "
                         "by what it is not?"),
    "stacked adjectives": "Does `bullets[{i}]` stack adjectives in front of a noun?",
    "filler or vague impact": "Does `bullets[{i}]` hold filler words or a vague claim of impact?",
    "hype words": "Does `bullets[{i}]` use hype words or self-praise?",
    "padded list of three": "Does `bullets[{i}]` pad a list out to three items?",
}
# TL-7, asked of each bullet's rephrase drafts, each draft as an option.
BEST_DRAFT_QUESTION = "Which draft shows the most of what `job` asks for?"
# TL-8, asked of each cover-letter sentence against the letter's sources.
LETTER_CLAIM_QUESTION = ("Does `sentences[{i}]` claim something about the candidate that "
                         "`sources` do not state?")
# TL-9, asked of each ATS keyword against the résumé's text.
KEYWORD_MEANING_QUESTION = "Does `resume` show `keywords[{i}]` or a direct equivalent?"
# Each tell's question id in a request: `<id>_<i>` for the bullet at index i.
_SWEEP_IDS = {"contrast framing": "contrast", "stacked adjectives": "stacked",
              "filler or vague impact": "filler", "hype words": "hype",
              "padded list of three": "three"}
# What a flagged bullet's finding says, one clause per check that failed, joined
# with "; " in this order. The reground prompt names it, and so does the report.
FINDINGS = {
    "unsupported": "it states something its atoms leave out",
    "contradicted": "it states something its atoms contradict",
    "inflates": "it gives the candidate a bigger role, scope or result than its atoms state",
    "adds_claim": "it states a tool, number, outcome or scope that its atoms do not state",
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
    None ("nothing to ask", or the failure that sent it back to the LLM path). A
    step asked several times keeps its first failure, and "nothing to ask" stands
    only while the step has sent no request."""
    jev, _switch = _jev_modules()
    counts = (getattr(_RUN, "steps", None) or {}).get(step) or {
        "requests": 0, "tokens": 0, "note": ""}
    return {"requests": counts["requests"], "tokens": counts["tokens"],
            "usd": jev.usd_for(counts["tokens"]), "note": counts["note"]}


def breaker_open(step: str, judge: Any, *, fallback: str = LLM_PATH) -> bool:
    """True when `judge`'s breaker is open (`jev.Guarded.down`), for a step whose
    caller pays for work before Jev's first request (TL-7's drafts). The step's note
    then names the outage as a request that met it would, and the caller keeps the
    path it has without Jev (JS-3)."""
    down = getattr(judge, "down", "") if judge is not None else ""
    if not isinstance(down, str) or not down:
        return False
    counts = _step_counts(step)
    counts["note"] = counts["note"] or f"fell back to {fallback} (JudgeOutage {down})"
    log.warning("jev_assist: jev %s skipped, the breaker is open (%s); %s runs",
                step, down, fallback)
    return True


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


def _run_step(step: str, judge: Any, ask: Callable[[Any], Any], *,
              fallback: str = LLM_PATH) -> Any:
    """`ask(judge)` for one step: None when Jev is off, when there is nothing to
    ask, and on any failure, with the step's note saying which. `fallback` names
    what the caller runs without Jev, for the note and the log.

    The note outlives a later call: the first failure stays put, so a line for a
    step asked after every rewrite (TL-4) still says a check fell back when a later
    request went through."""
    counts = _step_counts(step)
    if counts["note"] == NOTHING_TO_ASK:
        counts["note"] = ""
    if judge is _DEFAULT:
        judge = default_judge()
    if judge is None:
        return None
    try:
        return ask(judge)
    except _NothingToAsk:
        if not counts["note"] and not counts["requests"]:
            counts["note"] = NOTHING_TO_ASK
    except _Unusable:
        counts["note"] = counts["note"] or f"fell back to {fallback} (unusable answer)"
        log.warning("jev_assist: jev %s got an unusable answer; %s runs", step, fallback)
    except Exception as exc:  # noqa: BLE001 - any judge failure keeps the LLM path
        kind = _failure_kind(exc)
        counts["note"] = counts["note"] or f"fell back to {fallback} ({kind})"
        log.warning("jev_assist: jev %s failed (%s); %s runs", step, kind, fallback)
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


def _choice_pick(answer: Any, names: Sequence[str], where: str) -> Tuple[str, float]:
    """(the option picked, its confidence). A missing confidence reads as 0.0 and is
    logged with `where` (the step and the question id): a TL-4 "unsupported" pick
    with none would otherwise pass as unsure, unseen."""
    choice = getattr(answer, "choice", None)
    if choice not in names:
        raise _Unusable("choice")
    conf = getattr(answer, "confidence", None)
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf):
        log.warning("jev_assist: jev %s answered %r with no confidence; it reads as 0.0",
                    where, str(choice))
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
        skill_focus, _conf = _choice_pick(extra.get("focus"), list(SKILL_FOCUS),
                                          f"{STEP_SKILLS} focus")
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
            choice, conf = _choice_pick(answers.get(qid), list(questions[qid]["criteria"]),
                                        f"{STEP_LEAD} {qid}")
            out[str(p["project"])] = (int(choice), conf)
        return out
    return _run_step(STEP_LEAD, judge, ask)


def _finding(supported: str, confidence: float, inflates: float, adds_claim: float) -> str:
    """TL-4's finding for one bullet: "" when it passes, else one FINDINGS clause per
    check that failed, joined with "; ". A sure "unsupported" or "contradicted"
    (SUPPORTED_MIN_CONFIDENCE) or `inflates` at FAITHFULNESS_FLAG flags the bullet;
    `adds_claim` at FAITHFULNESS_FLAG is named only beside one of them, since VL-3
    read it high on nearly every faithful rephrase (see SUPPORTED_MIN_CONFIDENCE)."""
    parts: List[str] = []
    if supported != "verified" and confidence >= SUPPORTED_MIN_CONFIDENCE:
        parts.append(FINDINGS[supported])
    if inflates >= FAITHFULNESS_FLAG:
        parts.append(FINDINGS["inflates"])
    if parts and adds_claim >= FAITHFULNESS_FLAG:
        parts.append(FINDINGS["adds_claim"])
    return "; ".join(parts)


def faithfulness(entries: Sequence[Mapping[str, Any]], *, judge: Any = _DEFAULT
                 ) -> Optional[Dict[str, str]]:
    """TL-4: does each bullet say only what the atoms it was written from say?

    `entries` is [{"entry": name, "bullets": [{"gkey", "text", "atoms"}]}], one per
    résumé entry, where `atoms` is the bullet's own atoms as the writer saw them. One
    request per entry (split only when it would not fit), three questions per bullet:
    `supported`, a choice of verified / unsupported / contradicted
    (SUPPORTED_QUESTION); `inflates` (INFLATES_QUESTION) and `adds_claim`
    (ADDS_CLAIM_QUESTION), nouls. A bullet is flagged when `supported` is
    "unsupported" or "contradicted" at SUPPORTED_MIN_CONFIDENCE or more, or
    `inflates` reaches FAITHFULNESS_FLAG. `adds_claim` flags nothing alone; at
    FAITHFULNESS_FLAG it joins the finding of a bullet flagged otherwise, so the
    reground hears it. The rule is VL-3's: over real bullets the judge read
    `adds_claim` high on nearly every faithful rephrase and picked "verified" and
    "unsupported" at low confidence, while the planted inflations and added claims
    read a sure "unsupported" or "contradicted" (see SUPPORTED_MIN_CONFIDENCE).

    Returns {gkey: finding}, "" for a bullet that passes (see `_finding`). The check
    judges and names; it never writes text. None when Jev is off or fails, or no
    entry has a bullet."""
    def ask(j: Any) -> Dict[str, str]:
        asked = [e for e in entries if e.get("bullets")]
        if not asked:
            raise _NothingToAsk
        options = list(SUPPORTED_OPTIONS)
        out: Dict[str, str] = {}
        for entry in asked:
            name = str(entry.get("entry") or "")
            items = list(entry["bullets"])

            def request(lo: int, hi: int, name: str = name, items: List[Any] = items
                        ) -> Tuple[Dict[str, Any], Dict[str, dict]]:
                part = items[lo:hi]
                state = {"entry": name, "bullets": [str(b["text"]) for b in part],
                         "atoms": [b.get("atoms") for b in part]}
                questions: Dict[str, dict] = {}
                for i in range(len(part)):
                    questions[f"supported_{i}"] = {
                        "type": "choice", "instructions": SUPPORTED_QUESTION.format(i=i),
                        "criteria": dict(SUPPORTED_OPTIONS)}
                    questions[f"inflates_{i}"] = {
                        "type": "noul", "instructions": INFLATES_QUESTION.format(i=i)}
                    questions[f"adds_claim_{i}"] = {
                        "type": "noul", "instructions": ADDS_CLAIM_QUESTION.format(i=i)}
                return state, questions

            for lo, hi in _fit_spans(len(items), request):
                state, questions = request(lo, hi)
                answers = _send(STEP_FAITHFULNESS, j, state, questions)
                for i, bullet in enumerate(items[lo:hi]):
                    supported, conf = _choice_pick(answers.get(f"supported_{i}"), options,
                                                   f"{STEP_FAITHFULNESS} supported_{i}")
                    out[str(bullet["gkey"])] = _finding(
                        supported, conf, _noul_prob(answers.get(f"inflates_{i}")),
                        _noul_prob(answers.get(f"adds_claim_{i}")))
        return out
    return _run_step(STEP_FAITHFULNESS, judge, ask, fallback=GATE_ALONE)


def sweep_flags(entries: Sequence[Mapping[str, Any]], *, judge: Any = _DEFAULT
                ) -> Optional[Dict[str, Tuple[str, ...]]]:
    """TL-5: which of the user's banned patterns Jev reads in each bullet, asked
    before the AI-writing sweep.

    `entries` is [{"entry": name, "bullets": [{"gkey", "text"}]}], one per résumé
    entry. One request per entry (split only when it would not fit), one noul per
    tell per bullet (SWEEP_QUESTIONS), bullet by bullet. Returns {gkey: the names of
    the tells at SWEEP_FLAG or more, in SWEEP_QUESTIONS order}, () for a bullet Jev
    reads as clean. The sweep calls the model for an item only when one of its
    bullets has a tell or a detector finding. None when Jev is off or fails, or no
    entry has a bullet."""
    def ask(j: Any) -> Dict[str, Tuple[str, ...]]:
        asked = [e for e in entries if e.get("bullets")]
        if not asked:
            raise _NothingToAsk
        out: Dict[str, Tuple[str, ...]] = {}
        for entry in asked:
            name = str(entry.get("entry") or "")
            items = list(entry["bullets"])

            def request(lo: int, hi: int, name: str = name, items: List[Any] = items
                        ) -> Tuple[Dict[str, Any], Dict[str, dict]]:
                part = items[lo:hi]
                state = {"entry": name, "bullets": [str(b["text"]) for b in part]}
                questions = {f"{_SWEEP_IDS[tell]}_{i}": {"type": "noul",
                                                        "instructions": text.format(i=i)}
                             for i in range(len(part)) for tell, text in SWEEP_QUESTIONS.items()}
                return state, questions

            for lo, hi in _fit_spans(len(items), request):
                state, questions = request(lo, hi)
                answers = _send(STEP_SWEEP_GATE, j, state, questions)
                for i, bullet in enumerate(items[lo:hi]):
                    probs = {tell: _noul_prob(answers.get(f"{_SWEEP_IDS[tell]}_{i}"))
                             for tell in SWEEP_QUESTIONS}
                    out[str(bullet["gkey"])] = tuple(
                        tell for tell, p in probs.items() if p >= SWEEP_FLAG)
        return out
    return _run_step(STEP_SWEEP_GATE, judge, ask)


def _option_id(text: Any) -> str:
    """An option's id: its text with the whitespace collapsed."""
    return " ".join(str(text).split())


def _category_ids(palette: Mapping[str, Sequence[str]]) -> Dict[str, str]:
    """TL-6 stage 1's option ids: {id: the palette's own key}, one per category,
    in palette order, left out when empty or alike (whatever the case) an earlier one."""
    out: Dict[str, str] = {}
    seen: set = set()
    for key in palette:
        cid = _option_id(key)
        if cid and cid.lower() not in seen:
            seen.add(cid.lower())
            out[cid] = key
    return out


def _verb_ids(verbs: Sequence[str], cat: str, taken: Collection[str]) -> Dict[str, str]:
    """TL-6 stage 2's option ids, in palette order: {verb: `cat`} for each verb as its
    trimmed text, left out when empty, longer than VERB_ID_MAX, alike (whatever the
    case) an earlier one, or already an opener (its lowercase in `taken`)."""
    out: Dict[str, str] = {}
    seen: set = set()
    for verb in verbs:
        name = _option_id(verb)
        low = name.lower()
        if not name or len(name) > VERB_ID_MAX or low in seen or low in taken:
            continue
        seen.add(low)
        out[name] = cat
    return out


def _own_category(cats: Mapping[str, str], palette: Mapping[str, Sequence[str]],
                  current: str) -> str:
    """The id of the first category holding `current`, whatever its case; "" when
    none does."""
    low = _option_id(current).lower()
    for cid, key in cats.items():
        if low and any(_option_id(v).lower() == low for v in palette[key]):
            return cid
    return ""


def _pick_category(j: Any, state: Dict[str, str], cats: Mapping[str, str],
                   palette: Mapping[str, Sequence[str]]) -> Tuple[str, str]:
    """TL-6 stage 1: (the category Jev picks for the bullet at CATEGORY_MIN_CONFIDENCE
    or more, else ""; the failure's kind when the request failed, else ""). An
    outage is raised: the second request would meet it too."""
    jev, _switch = _jev_modules()
    criteria = {cid: "Verbs such as " + ", ".join(
        list(dict.fromkeys(_option_id(v) for v in palette[key] if _option_id(v)))
        [:CATEGORY_SAMPLE_VERBS]) for cid, key in cats.items()}
    question = {"category": {"type": "choice", "instructions": PICK_CATEGORY_QUESTION,
                             "criteria": criteria}}
    if not jev.request_fits(state, question):
        return "", ""
    try:
        answers = _send(STEP_VERB, j, state, question)
        cid, conf = _choice_pick(answers.get("category"), list(cats), f"{STEP_VERB} category")
    except jev.JudgeOutage:
        raise
    except Exception as exc:  # noqa: BLE001 - stage 2 asks the verb's own category
        kind = "unusable answer" if isinstance(exc, _Unusable) else _failure_kind(exc)
        log.warning("jev_assist: jev %s category pick failed (%s); the repeated verb's "
                    "own category is asked", STEP_VERB, kind)
        return "", kind
    return (cid if conf >= CATEGORY_MIN_CONFIDENCE else ""), ""


def pick_verb(bullet: str, palette: Mapping[str, Sequence[str]], current: str,
              taken: Collection[str] = frozenset(), *, judge: Any = _DEFAULT
              ) -> Optional[Tuple[str, float]]:
    """TL-6: the verb that best names the action in `bullet`, a bullet whose opening
    verb `current` another bullet already uses.

    `palette` is {category: verbs} (`assets.active_verbs()`, grouped by the kind of
    action each verb expresses) and `taken` the lowercase openers no pick may repeat.
    Two requests, each with the bullet as the state:

    1. one choice over the categories (PICK_CATEGORY_QUESTION, `_pick_category`);
    2. one choice over the picked category's unused verbs (PICK_VERB_QUESTION,
       `_verb_ids`), which stays under Jev's 255 options (VL-3). One question cannot
       be split, so past Jev's limits the options are halved from the end until the
       request fits.

    When stage 1 fails, would not fit or picks under CATEGORY_MIN_CONFIDENCE, or its
    category has no unused verb, stage 2 asks the category holding `current`.
    Returns (verb, confidence); the caller swaps the bullet's first word for the verb
    at VERB_MIN_CONFIDENCE or more and keeps its LLM `reverb` call under it. The verb
    is always one of the palette's, trimmed. None when Jev is off or fails, when no
    category has an unused verb, or when the fallback's category has none."""
    def ask(j: Any) -> Tuple[str, float]:
        jev, _switch = _jev_modules()
        cats = _category_ids(palette)
        pools = {cid: _verb_ids(palette[key], cid, taken) for cid, key in cats.items()}
        if not any(pools.values()):
            raise _NothingToAsk
        state = {"bullet": str(bullet or "")}
        cid, failed = _pick_category(j, state, cats, palette)
        if not pools.get(cid):
            cid = _own_category(cats, palette, current)
        criteria = pools.get(cid) or {}
        if not criteria:
            raise _NothingToAsk
        names = list(criteria)

        def question(n: int) -> Dict[str, dict]:
            return {"verb": {"type": "choice", "instructions": PICK_VERB_QUESTION,
                             "criteria": {v: criteria[v] for v in names[:n]}}}

        n = len(names)
        while n > 1 and not jev.request_fits(state, question(n)):
            n //= 2
        answers = _send(STEP_VERB, j, state, question(n))
        got = _choice_pick(answers.get("verb"), names[:n], f"{STEP_VERB} verb")
        if failed:
            # the pick stands; the note says stage 1 fell back (a stage 2 failure
            # gets `_run_step`'s note instead)
            counts = _step_counts(STEP_VERB)
            counts["note"] = counts["note"] or f"category fell back to the verb's own ({failed})"
        return got
    return _run_step(STEP_VERB, judge, ask)


def best_variant(jd: str, job_title: str, groups: Sequence[Mapping[str, Any]], *,
                 judge: Any = _DEFAULT) -> Optional[Dict[str, Tuple[int, float]]]:
    """TL-7: which of each bullet's drafts shows the most of what the job asks for.

    `groups` is [{"gkey", "drafts": [text, ...]}], the drafts that passed the
    grounding gate and TL-4, in the order the rephrase wrote them. One choice per
    bullet over its numbered drafts (BEST_DRAFT_QUESTION), with the job as the
    state, every bullet in one request (split only when it would not fit). Returns
    {gkey: (draft number counting from 1, confidence)}. A bullet with one draft has
    nothing to choose and is left out. The pick only chooses among texts the
    rephrase wrote. None when Jev is off or fails, or no bullet has two drafts."""
    def ask(j: Any) -> Dict[str, Tuple[int, float]]:
        asked = [g for g in groups if len(g.get("drafts") or []) >= 2]
        if not asked:
            raise _NothingToAsk
        job = _job_state(jd, job_title)

        def request(lo: int, hi: int) -> Tuple[Dict[str, Any], Dict[str, dict]]:
            questions = {
                f"bullet_{k}": {
                    "type": "choice", "instructions": BEST_DRAFT_QUESTION,
                    "criteria": {f"draft {n}": str(text)
                                 for n, text in enumerate(g["drafts"], start=1)}}
                for k, g in enumerate(asked[lo:hi])}
            return {"job": job}, questions

        out: Dict[str, Tuple[int, float]] = {}
        for lo, hi in _fit_spans(len(asked), request):
            state, questions = request(lo, hi)
            answers = _send(STEP_BEST_OF, j, state, questions)
            for k, g in enumerate(asked[lo:hi]):
                qid = f"bullet_{k}"
                choice, conf = _choice_pick(answers.get(qid), list(questions[qid]["criteria"]),
                                            f"{STEP_BEST_OF} {qid}")
                out[str(g["gkey"])] = (int(choice.split()[-1]), conf)
        return out
    return _run_step(STEP_BEST_OF, judge, ask)


def letter_unsupported(sentences: Sequence[str], sources: Mapping[str, Any], *,
                       judge: Any = _DEFAULT) -> Optional[List[str]]:
    """TL-8: the cover-letter sentences that claim more about the candidate than the
    letter's sources state.

    `sources` is what the letter may draw on about the candidate: the résumé
    bullets, the candidate's notes behind them, their own words and their basics.
    The job description and the company research are never sources. One noul per
    sentence (LETTER_CLAIM_QUESTION) with the sources beside them, batched to fit
    Jev's limits. Returns the sentences at LETTER_CLAIM_FLAG or more, in letter
    order; [] when every sentence passes. The check names sentences; the letter's
    repair call rewrites them. None when Jev is off or fails, or there is no
    sentence."""
    def ask(j: Any) -> List[str]:
        items = [str(s) for s in sentences if str(s).strip()]
        if not items:
            raise _NothingToAsk
        src = dict(sources)

        def request(lo: int, hi: int) -> Tuple[Dict[str, Any], Dict[str, dict]]:
            state = {"sentences": items[lo:hi], "sources": src}
            questions = {f"claims_{i}": {"type": "noul",
                                         "instructions": LETTER_CLAIM_QUESTION.format(i=i)}
                         for i in range(hi - lo)}
            return state, questions

        flagged: List[str] = []
        for lo, hi in _fit_spans(len(items), request):
            state, questions = request(lo, hi)
            answers = _send(STEP_LETTER, j, state, questions)
            flagged += [s for i, s in enumerate(items[lo:hi])
                        if _noul_prob(answers.get(f"claims_{i}")) >= LETTER_CLAIM_FLAG]
        return flagged
    return _run_step(STEP_LETTER, judge, ask)


def keyword_meaning(keywords: Sequence[str], resume_text: str, *,
                    judge: Any = _DEFAULT) -> Optional[List[str]]:
    """TL-9: the ATS keywords the résumé shows, in words or by a direct equivalent.

    One noul per keyword (KEYWORD_MEANING_QUESTION) with the résumé's text as the
    state (cut to JD_CHARS), batched to fit Jev's limits. Returns the keywords at
    KEYWORD_MEANING_MIN or more, in the order given; the ATS report counts them in
    its meaning-level coverage line beside the literal one. None when Jev is off or
    fails, or there is no keyword."""
    def ask(j: Any) -> List[str]:
        items = [str(k) for k in keywords if str(k).strip()]
        if not items:
            raise _NothingToAsk
        resume = str(resume_text or "")[:JD_CHARS]

        def request(lo: int, hi: int) -> Tuple[Dict[str, Any], Dict[str, dict]]:
            state = {"resume": resume, "keywords": items[lo:hi]}
            questions = {f"keywords_{i}": {"type": "noul",
                                           "instructions": KEYWORD_MEANING_QUESTION.format(i=i)}
                         for i in range(hi - lo)}
            return state, questions

        shown: List[str] = []
        for lo, hi in _fit_spans(len(items), request):
            state, questions = request(lo, hi)
            answers = _send(STEP_ATS_MEANING, j, state, questions)
            shown += [k for i, k in enumerate(items[lo:hi])
                      if _noul_prob(answers.get(f"keywords_{i}")) >= KEYWORD_MEANING_MIN]
        return shown
    return _run_step(STEP_ATS_MEANING, judge, ask)
