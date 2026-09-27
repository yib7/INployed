"""The screening set: real-world screening questions run through the auto-apply
runner's own mapping and option pick for one answer list (cycle 18, TS-1,
TS-2, ED-9).

`load_questions()` reads the shipped questions (`screening_questions.json`
beside this module). Each is one form field as the extractor reads it: a label,
help text, a widget (`radio`, `select`, `toggle` for Yes / No buttons,
`checkbox` for one tick box or a question's tick boxes), its options and
whether it is required, plus the fact it asks for and, for the test's two
synthetic profiles, the option the run should pick (null where the right end
is no answer).

`screen(question, catalog, judge)` builds the one-field page the question
stands for and asks `judge` what the runner asks, in the runner's order
(`apply_run._JobRun._map` and `_complete_option_plan`): the page's mapping
(`apply_judge.page_requests`), the plan, the second look at a required field's
dropped or weak mapping, the option picks for a mapping the judge made
(`apply_judge.option_questions`), the plan again, the second look at a
dropped or weak pick, and the settle question for a saved answer the
own-question gate held back (`apply_judge.settle_questions`). It returns what
the plan puts in the field, or None. `screen_page(digest, catalog, judge,
job)` asks the same for a whole page and returns the plan.
`run_screening(answers, judge)` does that for every question over the fact
catalog one answer list gives. The page read (what kind of page this is) is
left out: every question sits on an application form.

Pure: no Qt, no browser, no file written but a temporary empty job folder.
Each question costs one to four judge requests; the judge is the only thing
that leaves the process.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import apply_facts
import apply_judge
from apply_form import Field, FormDigest
from apply_judge import FillPlan, PlannedField

QUESTIONS_PATH = Path(__file__).resolve().with_name("screening_questions.json")

# widget name in the questions file -> (the extractor's field type, its widget)
WIDGETS: dict[str, tuple[str, str]] = {
    "radio": ("radio", ""),
    "select": ("select", ""),
    "toggle": ("radio", "choice"),       # Yes / No buttons with aria-pressed (Ashby)
    "checkbox": ("checkbox", ""),        # one tick box (options ["checked"]) or a group
}
# a tick box's one option, as the extractor lists it
TICKED = "checked"

# the page every question sits on: fixed, so a recorded answer replays
PAGE_HOST = "jobs.example.com"
PAGE_TITLE = "Application"
JOB = {"company_name": "Example Co", "job_title": "Software Engineer"}


@dataclass(frozen=True)
class ScreeningRow:
    qid: str
    question: str          # the field label shown on the form
    answer: str | None     # the option or value the run would give; None means the run stops here


@dataclass(frozen=True)
class Outcome:
    """What the run does with one question: `answer` as in `ScreeningRow`,
    the fact the plan used (`fact_key`, None when it found none), and
    `by_code` when code settled the pick with no judge (`apply_judge.code_pick`)."""
    answer: str | None
    fact_key: str | None
    by_code: bool = False


def load_questions(path: Path | None = None) -> list[dict]:
    """The shipped screening questions (the fixture schema from decision 1)."""
    raw = json.loads(Path(path or QUESTIONS_PATH).read_text(encoding="utf-8"))
    questions = raw.get("questions") if isinstance(raw, dict) else None
    if not isinstance(questions, list):
        raise ValueError(f"{path or QUESTIONS_PATH}: no \"questions\" list")
    return [dict(q) for q in questions]


def field_for(question: dict[str, Any], n: int = 0) -> Field:
    """The form field `question` stands for, as the extractor would read it."""
    widget = str(question.get("widget", ""))
    if widget not in WIDGETS:
        raise ValueError(f"question {question.get('id')!r}: unknown widget {widget!r}")
    type_, how = WIDGETS[widget]
    options = [str(o) for o in question.get("options") or []]
    if widget == "checkbox" and options != [TICKED]:
        how = "checkbox_group"
    return Field(n=n, locator=(0, f"#q{n}"), label=str(question.get("label", "")),
                 type=type_, required=bool(question.get("required", True)),
                 help=str(question.get("help", "") or ""), options=options, widget=how)


def page_for(question: dict[str, Any]) -> FormDigest:
    """The one-field page: the question's words are the page's text."""
    f = field_for(question)
    text = " ".join(t for t in (f.label, f.help) if t)
    return FormDigest(url_host=PAGE_HOST, title=PAGE_TITLE, text=text, fields=[f])


def catalog_for(answers: list[dict]) -> apply_facts.FactCatalog:
    """The fact catalog a v2 answer list gives on its own: an empty job
    folder (no sheet, no PDFs), so only the confirmed answers are facts."""
    with tempfile.TemporaryDirectory(prefix="inployed-screening-") as folder:
        # through the module: a record or replay run pins the catalog's date there
        return apply_facts.build(Path(folder), answers=list(answers or []))


def _asked(judge, state: dict, questions: dict) -> dict:
    """`judge`'s answers to its own questions only (as `_JobRun._map` keeps them)."""
    return {k: v for k, v in judge.judge(state, questions).items() if k in questions}


def _plan(digest: FormDigest, catalog, answers: dict, job: dict = JOB) -> FillPlan:
    # drafting never answers a choice: a question mapped to a draft reads as no answer
    return apply_judge.plan(digest, catalog, answers, generation_enabled=False,
                            company=job["company_name"])


def _second_look(digest: FormDigest, catalog, answers: dict, plan: FillPlan, judge,
                 what: str, job: dict = JOB) -> FillPlan:
    """`_JobRun._reask`: the required fields the plan skipped for a dropped or
    weak mapping (`what` "source") or pick ("pick") asked once more, and the
    plan made again with those answers."""
    targets = apply_judge.reask_targets(digest, catalog, answers, plan, what=what,
                                        company=job["company_name"])
    if not targets:
        return plan
    state, questions = apply_judge.reask_questions(digest, catalog, plan, targets, what=what,
                                                   job=job)
    answers.update(_asked(judge, state, questions))
    return _plan(digest, catalog, answers, job)


def _given(pf: PlannedField) -> str | None:
    if pf.action == "select" and pf.option:
        return pf.option
    if pf.action == "fill" and pf.value:
        return pf.value
    return None


def screen_page(digest: FormDigest, catalog, judge, job: dict = JOB) -> FillPlan:
    """The plan the run makes for the page `digest` with `catalog`'s facts
    and `judge`'s answers, asked in the runner's order (`screen`); `job` is
    the posting, its company name read as the company."""
    company = job["company_name"]
    answers: dict = {}
    requests = [(s, q) for s, q in apply_judge.page_requests(digest, catalog, job) if q]
    answers.update(apply_judge.merge_answers([_asked(judge, s, q) for s, q in requests]))
    plan = _plan(digest, catalog, answers, job)
    plan = _second_look(digest, catalog, answers, plan, judge, "source", job)
    state, questions = apply_judge.option_questions(digest, plan, catalog=catalog,
                                                    company=company)
    if questions:
        answers.update(_asked(judge, state, questions))
        plan = _plan(digest, catalog, answers, job)
    plan = _second_look(digest, catalog, answers, plan, judge, "pick", job)
    state, questions = apply_judge.settle_questions(digest, plan, answers, catalog,
                                                    company=company)
    if questions:
        answers.update(_asked(judge, state, questions))
        plan = _plan(digest, catalog, answers, job)
    return plan


def screen(question: dict[str, Any], catalog, judge) -> Outcome:
    """What the run gives `question` with `catalog`'s facts and `judge`'s answers."""
    digest = page_for(question)
    plan = screen_page(digest, catalog, judge)
    pf = plan.fields[0]
    answer = _given(pf)
    f = digest.fields[0]
    by_code = (answer is not None and pf.action == "select" and bool(pf.value)
               and apply_judge.code_pick(pf.value, f.options) == answer)
    return Outcome(answer=answer, fact_key=pf.fact_key if answer is not None else None,
                   by_code=by_code)


def run_screening(answers: list[dict], judge) -> list[ScreeningRow]:
    """Run every shipped question for a v2 answer list (the store's "answers") with `judge`.
    Builds the fact catalog from the answers alone. Pure; no Qt; safe off the UI thread."""
    catalog = catalog_for(answers)
    return [ScreeningRow(qid=str(q.get("id", "")), question=str(q.get("label", "")),
                         answer=screen(q, catalog, judge).answer)
            for q in load_questions()]
