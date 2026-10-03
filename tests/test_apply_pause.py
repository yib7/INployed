"""`apply_pause` (SP7, park and resume): the request and answer files, the
wait, the card's questions, the person's values into plan fields, and the
save to the answer store. Every store is a `tmp_path` file and LOCALAPPDATA
points at `tmp_path`; no page, browser or judge here (the runner's pauses
are in `test_apply_run.py`)."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_pause  # noqa: E402
from apply_form import Field  # noqa: E402
from apply_judge import FillPlan, PlannedField  # noqa: E402
from resume_tailor import apply_answers  # noqa: E402

_JOB = {"job_posting_id": "42", "company": "Fabrikam", "title": "Analytics Engineer"}


@pytest.fixture(autouse=True)
def _appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")


def _field(n=1, label="Favourite colour", type_="text", options=(), required=True, **kw):
    return Field(n=n, locator=(0, f"#f{n}"), label=label, type=type_, required=required,
                 options=list(options), **kw)


def _pf(f, action="skip"):
    return PlannedField(n=f.n, locator=f.locator, label=f.label, required=f.required,
                        fact_key=None, value="", option=None, confidence=0.0, action=action,
                        widget=f.widget, options=list(f.options))


# -- the files -------------------------------------------------------------------------------

def test_the_pause_folder_is_under_localappdata_read_at_call_time(tmp_path, monkeypatch):
    assert apply_pause.pause_dir() == tmp_path / "appdata" / "linkedin_watcher" / "apply_pause"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "other"))
    assert apply_pause.pause_dir() == tmp_path / "other" / "linkedin_watcher" / "apply_pause"


def test_a_request_carries_the_job_the_page_and_each_question():
    q = apply_pause.question_for(_field(help="Pick one", placeholder="e.g. blue",
                                        id_or_name="colour"))
    path = apply_pause.write_request(_JOB, "https://jobs.example.com/apply", "required field "
                                     "without an answer: Favourite colour", [q], pause_id="p1")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "42.json"
    assert data["job"] == "42" and data["company"] == "Fabrikam"
    assert data["title"] == "Analytics Engineer"
    assert data["page_url"] == "https://jobs.example.com/apply"
    assert data["reason"].startswith("required field without an answer")
    assert data["pause_id"] == "p1"
    (got,) = data["questions"]
    assert {k: got[k] for k in ("key", "field_id", "label", "help", "placeholder", "type",
                                "widget", "options", "required", "sensitive")} == {
        "key": "1", "field_id": "colour", "label": "Favourite colour", "help": "Pick one",
        "placeholder": "e.g. blue", "type": "text", "widget": "text", "options": [],
        "required": True, "sensitive": False}


def test_a_new_request_removes_a_stale_answer():
    apply_pause.write_answer("42", "fill", {"1": "blue"})
    assert apply_pause.answer_path("42").exists()
    apply_pause.write_request(_JOB, "https://x.example/", "r", [])
    assert not apply_pause.answer_path("42").exists()


def test_pending_requests_lists_only_the_unanswered():
    apply_pause.write_request(_JOB, "https://x.example/", "r", [])
    apply_pause.write_request({**_JOB, "job_posting_id": "7"}, "https://y.example/", "r", [])
    assert [r["job"] for r in apply_pause.pending_requests()] == ["42", "7"]
    apply_pause.write_answer("7", "park")
    assert [r["job"] for r in apply_pause.pending_requests()] == ["42"]


def test_an_answer_names_a_known_mode_only():
    with pytest.raises(ValueError):
        apply_pause.write_answer("42", "submit")
    apply_pause.write_answer("42", "fill", {"1": "blue", "2": "  "}, {"1": True, "2": False})
    assert apply_pause.read_answer("42") == {"mode": "fill", "values": {"1": "blue"},
                                             "save": {"1": True}}
    apply_pause.answer_path("42").write_text('{"mode": "submit"}', encoding="utf-8")
    assert apply_pause.read_answer("42") is None


def test_an_answer_to_another_pause_is_not_read():
    apply_pause.write_answer("42", "park", pause_id="old")
    assert apply_pause.read_answer("42", "new") is None
    assert apply_pause.read_answer("42", "old")["mode"] == "park"
    # a hand-written answer names no pause and answers the current one
    apply_pause.write_answer("42", "park")
    assert apply_pause.read_answer("42", "new")["mode"] == "park"


# -- the wait --------------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _Page:
    """A page double: each wait moves the clock on; `closed_after` waits
    later the page is closed."""

    def __init__(self, clock, closed_after=None, on_wait=None):
        self.clock, self.waits, self.closed_after, self.on_wait = clock, 0, closed_after, on_wait

    def is_closed(self):
        return self.closed_after is not None and self.waits >= self.closed_after

    def wait_for_timeout(self, ms):
        self.waits += 1
        self.clock.t += ms / 1000
        if self.on_wait:
            self.on_wait(self.waits)


def test_the_wait_returns_the_answer_once_it_lands():
    clock = _Clock()
    page = _Page(clock, on_wait=lambda n: n == 3 and apply_pause.write_answer("42", "fill",
                                                                                 {"1": "blue"}))
    got = apply_pause.wait_for_answer(page, "42", 10, clock=clock, poll_s=1)
    assert got["values"] == {"1": "blue"} and page.waits == 3


def test_the_wait_gives_up_after_its_minutes():
    clock = _Clock()
    page = _Page(clock)
    assert apply_pause.wait_for_answer(page, "42", 1, clock=clock, poll_s=5) is None
    assert clock.t >= 60


def test_a_closed_window_ends_the_wait():
    clock = _Clock()
    with pytest.raises(apply_pause.PageClosed):
        apply_pause.wait_for_answer(_Page(clock, closed_after=2), "42", 10, clock=clock, poll_s=1)


def test_a_wait_that_raises_is_a_closed_window():
    class Gone(_Page):
        def wait_for_timeout(self, ms):
            raise RuntimeError("Target page, context or browser has been closed")
    with pytest.raises(apply_pause.PageClosed):
        apply_pause.wait_for_answer(Gone(_Clock()), "42", 10, poll_s=1)


# -- questions -------------------------------------------------------------------------------

def test_each_field_gets_its_own_widget():
    assert apply_pause.widget_for(_field(type_="select", options=["Red", "Blue"])) == "choice"
    assert apply_pause.widget_for(_field(type_="radio", options=["Yes", "No"])) == "choice"
    assert apply_pause.widget_for(_field(type_="checkbox", label="I agree")) == "yes_no"
    assert apply_pause.widget_for(_field(type_="number", label="Years of Rust")) == "number"
    assert apply_pause.widget_for(_field(type_="textarea")) == "text"
    assert apply_pause.widget_for(_field(label="Date of birth", type_="date")) == "browser"
    assert apply_pause.widget_for(_field(label="Resume", type_="file")) == "browser"
    # a list whose options were never read has nothing to pick from
    assert apply_pause.widget_for(_field(type_="select")) == "browser"


def test_a_sensitive_question_is_marked_and_carries_no_options():
    q = apply_pause.question_for(_field(label="Social Security Number", type_="text"))
    assert q["sensitive"] is True and q["widget"] == "browser"


def test_a_yes_no_list_is_asked_as_yes_no():
    q = apply_pause.question_for(_field(type_="radio", options=["Yes", "No"]))
    assert q["type"] == "yes_no" and q["options"] == ["Yes", "No"]


def test_the_answerable_reasons():
    assert apply_pause.answerable("required field without an answer: Colour")
    assert apply_pause.answerable("asks for Date of birth, which auto-apply never fills; "
                                  "finish it by hand")
    assert not apply_pause.answerable("page did not advance")
    assert not apply_pause.answerable("")


# -- the person's value into the plan ----------------------------------------------------------

def test_text_goes_in_as_the_persons_exact_value():
    f = _field()
    pf = _pf(f)
    assert apply_pause.apply_value(pf, f, "  deep teal ")
    assert (pf.action, pf.value, pf.fact_key) == ("fill", "deep teal", apply_pause.USER_SOURCE)


def test_a_choice_must_be_one_of_the_live_options():
    f = _field(type_="select", options=["Red", "Blue green"])
    pf = _pf(f)
    assert not apply_pause.apply_value(pf, f, "Purple")
    assert pf.action == "skip"
    assert apply_pause.apply_value(pf, f, "Blue  green")
    assert (pf.action, pf.option) == ("select", "Blue green")


def test_a_tick_box_is_ticked_for_yes_only():
    f = _field(type_="checkbox", label="I agree")
    pf = _pf(f)
    assert not apply_pause.apply_value(pf, f, "No")
    assert apply_pause.apply_value(pf, f, "Yes")
    assert (pf.action, pf.option) == ("select", "checked")


def test_a_number_box_takes_a_number_only():
    f = _field(type_="number", label="Years of Rust")
    pf = _pf(f)
    assert not apply_pause.apply_value(pf, f, "three")
    assert apply_pause.apply_value(pf, f, "3")


def test_code_never_types_a_sensitive_field():
    f = _field(label="Date of birth", type_="text")
    pf = _pf(f)
    assert not apply_pause.apply_value(pf, f, "01/01/1990")
    assert pf.action == "skip" and pf.value == ""


def test_a_value_the_person_typed_in_the_browser_counts():
    assert apply_pause.holds_value(_field(placeholder="e.g. blue"), "teal")
    assert not apply_pause.holds_value(_field(placeholder="e.g. blue"), "e.g. blue")
    assert not apply_pause.holds_value(_field(type_="select", options=["Red"]), "Select...")
    assert apply_pause.holds_value(_field(type_="select", options=["Red"]), "Red")
    assert apply_pause.holds_value(_field(type_="checkbox"), "checked")
    assert not apply_pause.holds_value(_field(type_="checkbox"), "")


def test_the_park_reason_is_read_again_after_the_answers():
    a, b = _field(1, "Colour"), _field(2, "Date of birth")
    plan = FillPlan(fields=[_pf(a), _pf(b)], park_reason="asks for Date of birth, ...")
    fields = {1: a, 2: b}
    apply_pause.recompute_park(plan, fields)
    assert plan.park_reason.startswith("asks for Date of birth")
    plan.fields[1].action = apply_pause.KEPT
    apply_pause.recompute_park(plan, fields)
    assert plan.park_reason == "required field without an answer: Colour"
    plan.fields[0].action = "fill"
    apply_pause.recompute_park(plan, fields)
    assert plan.park_reason == ""


def test_user_text_is_checked_in_code_unless_its_shape_is():
    f = _field()
    pf = _pf(f)
    apply_pause.apply_value(pf, f, "teal")
    plan = FillPlan(fields=[pf])
    assert apply_pause.user_drafts(plan, {}) == {1: "teal"}
    assert apply_pause.user_drafts(plan, {1: ("phone", "teal")}) == {}


# -- the save (PR-6) -----------------------------------------------------------------------------

def _question(**kw):
    q = apply_pause.question_for(_field(**kw))
    return q


def test_a_saved_answer_is_a_confirmed_custom_answer_with_its_note(tmp_path):
    apply_answers.save(apply_answers.seed_defaults())
    q = _question(label="What is your favourite colour?", help="Any shade")
    assert apply_pause.save_answer(q, "teal", "Fabrikam", today=date(2026, 9, 27)) == ""
    saved = [e for e in apply_answers.load() if e["answer"] == "teal"]
    assert saved == [{"id": saved[0]["id"], "question": "What is your favourite colour? Any "
                      "shade", "type": "text", "answer": "teal",
                      "note": "Saved from Fabrikam on 2026-09-27", "confirmed": True,
                      "status": "active"}]
    assert (tmp_path / "apply_answers.json.bak").exists()


def test_a_choice_is_saved_as_text_and_a_yes_no_as_yes_or_no():
    apply_answers.save(apply_answers.seed_defaults())
    q = _question(label="Preferred team", type_="select", options=["Data", "Platform"])
    assert apply_pause.save_answer(q, "Platform", "Fabrikam") == ""
    y = _question(label="Have you used dbt in production?", type_="radio",
                  options=["Yes", "No"])
    assert apply_pause.save_answer(y, "yes", "Fabrikam") == ""
    got = {e["question"]: (e["type"], e["answer"]) for e in apply_answers.load()}
    assert got["Preferred team"] == ("text", "Platform")
    assert got["Have you used dbt in production?"] == ("yes_no", "Yes")


def test_a_question_a_builtin_answers_is_refused():
    apply_answers.save(apply_answers.seed_defaults())
    builtin = apply_answers.BUILTINS["work_authorized"].question
    why = apply_pause.save_answer(_question(label=builtin, type_="radio", options=["Yes", "No"]),
                                  "Yes", "Fabrikam")
    assert why.startswith("the built-in answer")
    assert len(apply_answers.load()) == len(apply_answers.seed_defaults())


def test_a_duplicate_question_is_refused():
    apply_answers.save(apply_answers.seed_defaults())
    q = _question(label="Favourite colour")
    assert apply_pause.save_answer(q, "teal", "Fabrikam") == ""
    assert apply_pause.save_answer(q, "blue", "Fabrikam").startswith("your custom answer")
    assert [e["answer"] for e in apply_answers.load() if e["question"] == "Favourite colour"] \
        == ["teal"]


def test_a_value_the_store_refuses_is_not_saved():
    apply_answers.save(apply_answers.seed_defaults())
    q = _question(label="Years of Rust", type_="number")
    assert apply_pause.save_answer(q, "300", "Fabrikam")
    assert not [e for e in apply_answers.load() if e["question"] == "Years of Rust"]


def test_two_saves_at_once_both_keep_their_answer(monkeypatch):
    """Two runs saving at the same moment each read the store, add one
    answer and write it back; the second write never drops the first's."""
    import threading
    apply_answers.save(apply_answers.seed_defaults())
    real = apply_answers.load_store
    both_read = threading.Barrier(2)

    def _read_then_wait(*a, **kw):
        store = real(*a, **kw)
        try:
            both_read.wait(timeout=1)       # a lock lets one read at a time
        except threading.BrokenBarrierError:
            pass
        return store
    monkeypatch.setattr(apply_answers, "load_store", _read_then_wait)
    got: list = []
    saves = [threading.Thread(target=lambda label=label, value=value: got.append(
        apply_pause.save_answer(_question(label=label), value, "Fabrikam")))
        for label, value in (("Favourite colour", "teal"), ("Favourite tree", "oak"))]
    for t in saves:
        t.start()
    for t in saves:
        t.join(10)
    assert got == ["", ""]
    answers = {e["question"]: e["answer"] for e in real()["answers"]}
    assert answers.get("Favourite colour") == "teal" and answers.get("Favourite tree") == "oak"


def test_a_save_while_the_store_stays_locked_says_so(monkeypatch):
    import locks
    apply_answers.save(apply_answers.seed_defaults())
    monkeypatch.setattr(apply_pause, "SAVE_LOCK_TIMEOUT", 0.2)
    with locks.file_lock(apply_answers.STORE_PATH):
        why = apply_pause.save_answer(_question(label="Favourite colour"), "teal", "Fabrikam")
    assert why == apply_pause.STORE_LOCKED
    assert not [e for e in apply_answers.load() if e["question"] == "Favourite colour"]


def test_the_builtin_check_is_shared_with_the_answers_tab():
    pytest.importorskip("PySide6")
    from qt import answers_tab
    assert answers_tab.builtin_answering is apply_pause.builtin_answering


# -- one job's pauses (SP7 fix round 1) ----------------------------------------------------------

import os  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import apply_fill  # noqa: E402
from apply_form import FormDigest  # noqa: E402

_URL = "https://ats.example/apply/1"


class _Log:
    def warning(self, *a, **k):
        pass

    info = warning


class _Jr:
    """A job run double for `Pauser`: a page double at `_URL`, the pause's
    settings, and every decision kept. `moved` is what `_pause_moved`
    returns (the run's own check that the page moved on during a wait)."""

    def __init__(self, page, *, headless=False, digest=None, moved=None):
        self.page = page
        page.url = _URL
        page.bring_to_front = lambda: None
        self.entry = dict(_JOB)
        self.job_id = "42"
        self.log = _Log()
        self.r = SimpleNamespace(clock=page.clock, sleep=None, drain_report=False,
                                 settings={"auto_apply_pause_minutes": 10,
                                           "auto_apply_headless": headless})
        self.deadline = 100.0
        self.decided: list[str] = []
        self.reloads = 0
        self.digest = digest
        self.moved = moved
        self.moved_calls: list[tuple] = []

    def _decide(self, what, why, **kw):
        self.decided.append(what)

    def _trace(self, *a, **kw):
        pass

    def _extract(self):
        if self.digest is None:
            raise RuntimeError("no page")
        return self.digest

    def _page_text(self):
        return "Apply"

    def _pause_reload(self):
        self.reloads += 1

    def _maybe_sent(self):
        return False

    def _pause_moved(self, before, text, reason, buttons=()):
        self.moved_calls.append((before, text, reason))
        return self.moved


def _answers_with(mode, values=None, save=None, then=None):
    """A page double's `on_wait` that answers the pause on its first poll
    (the dashboard's card), after `then()` (what the person did in the
    browser meanwhile)."""
    def on_wait(n):
        if n != 1:
            return
        if then is not None:
            then()
        req = apply_pause.read_request(apply_pause.request_path("42"))
        apply_pause.write_answer("42", mode, values or {}, save or {}, pause_id=req["pause_id"])
    return on_wait


@pytest.fixture
def pauses_on(monkeypatch):
    monkeypatch.setattr(apply_pause, "NEVER_WAIT", False)


@pytest.fixture
def boxes(monkeypatch):
    """The page's boxes as `apply_fill.read_back` reads them: locator -> value."""
    held: dict = {}
    monkeypatch.setattr(apply_fill, "read_back", lambda page, pf: held.get(pf.locator, ""))
    return held


def _explain_fields():
    return [_field(1, "Please explain", id_or_name="explain_a"),
            _field(2, "Please explain", id_or_name="explain_b")]


def _digest(*fields):
    return FormDigest(url_host="ats.example", title="Apply", text="Apply", fields=list(fields))


def test_an_answer_to_one_of_two_same_labelled_fields_goes_in_its_own_field(pauses_on, boxes):
    # review I2: the answer for the second "Please explain" lands in the second
    a, b = _explain_fields()
    digest = _digest(a, b)
    jr = _Jr(_Page(_Clock(), on_wait=_answers_with("fill", {"2": "answer for B"})),
             digest=digest)
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(apply_pause.Replan):
        p.at_disabled(digest, FillPlan(fields=[_pf(a), _pf(b)]), "the Submit button stays "
                      "disabled after the fill")
    plan = FillPlan(fields=[_pf(a), _pf(b)])
    p.apply_pending(digest, plan)
    got = {pf.n: (pf.action, pf.value) for pf in plan.fields}
    assert got == {1: ("skip", ""), 2: ("fill", "answer for B")}, got


def test_same_labelled_fields_with_no_id_are_told_apart_by_their_place(pauses_on, boxes):
    a, b = _field(1, "Please explain"), _field(2, "Please explain")
    digest = _digest(a, b)
    jr = _Jr(_Page(_Clock(), on_wait=_answers_with("fill", {"1": "A", "2": "B"})),
             digest=digest)
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(apply_pause.Replan):
        p.at_disabled(digest, FillPlan(fields=[_pf(a), _pf(b)]), "disabled")
    plan = FillPlan(fields=[_pf(a), _pf(b)])
    p.apply_pending(digest, plan)
    assert [pf.value for pf in plan.fields] == ["A", "B"]


def test_the_answers_kept_for_a_replan_apply_to_the_next_plan_only(pauses_on, boxes):
    # review M7: a single-page app keeps its path; the fields asked on one
    # step are never read back as the person's on a later one
    f = _field(1, "Please explain", id_or_name="explain")
    digest = _digest(f)
    jr = _Jr(_Page(_Clock(), on_wait=_answers_with("browser")), digest=digest)
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(apply_pause.Replan):
        p.at_disabled(digest, FillPlan(fields=[_pf(f)]), "disabled")
    p.apply_pending(digest, FillPlan(fields=[_pf(f)]))
    boxes[f.locator] = "typed by the site"
    later = FillPlan(fields=[_pf(f)])
    p.apply_pending(digest, later)
    assert later.fields[0].action == "skip"
    assert (p.pending, p.asked, p.typed) == ({}, set(), {})


def _typed(f, value):
    pf = _pf(f, "fill")
    pf.value = value
    return pf


def test_a_value_the_person_changed_in_the_browser_is_kept_on_the_replan(pauses_on, boxes):
    # review I3: the run typed the phone, the person rewrote it in the site's
    # format during the pause; the page read again keeps theirs
    phone, email, referral = (_field(1, "Phone", "tel", id_or_name="phone"),
                              _field(2, "Email", "email", id_or_name="email"),
                              _field(3, "Referral code", required=False, id_or_name="referral"))
    digest = _digest(phone, email, referral)
    boxes[phone.locator] = "555-555-0100"
    boxes[email.locator] = "jane@example.com"

    def person():
        boxes[phone.locator] = "+1 (555) 555-0100"
        boxes[referral.locator] = "FRIEND-7"
    jr = _Jr(_Page(_Clock(), on_wait=_answers_with("browser", then=person)), digest=digest)
    p = apply_pause.Pauser(jr, RuntimeError)
    first = FillPlan(fields=[_typed(phone, "555-555-0100"), _typed(email, "jane@example.com"),
                             _pf(referral)])
    with pytest.raises(apply_pause.Replan):
        p.at_disabled(digest, first, "disabled")
    again = FillPlan(fields=[_typed(phone, "555-555-0100"), _typed(email, "jane@example.com"),
                             _pf(referral)])
    p.apply_pending(digest, again)
    got = {pf.label: pf.action for pf in again.fields}
    assert got == {"Phone": apply_pause.KEPT, "Email": "fill",
                   "Referral code": apply_pause.KEPT}, got


def test_a_page_that_moved_on_during_the_wait_ends_the_pause_with_the_runs_park(pauses_on,
                                                                               boxes):
    # review I1: the run's own check (`_pause_moved`) reads the page after
    # every wait; a page that moved on raises its park, whatever the answer
    f = _field(1, "Referral code", required=False, id_or_name="referral")
    digest = _digest(f)
    for mode in ("browser", "fill", "park"):
        moved = RuntimeError("check whether the application went through")
        jr = _Jr(_Page(_Clock(), on_wait=_answers_with(mode, {"1": "X"})), digest=digest,
                 moved=moved)
        p = apply_pause.Pauser(jr, RuntimeError)
        with pytest.raises(RuntimeError) as got:
            p.at_disabled(digest, FillPlan(fields=[_pf(f)]), "disabled")
        assert got.value is moved, mode
        before, text, reason = jr.moved_calls[0]
        assert before[0] == _URL and text == "Apply" and reason.startswith("disabled")
        assert p.pending == {} and p.asked == set()


def test_a_headless_pause_with_only_browser_questions_parks_at_once(pauses_on, boxes):
    # review M4: nobody can type into a headless browser
    dob = _field(1, "Date of birth")
    digest = _digest(dob)
    page = _Page(_Clock())
    jr = _Jr(page, headless=True, digest=digest)
    p = apply_pause.Pauser(jr, RuntimeError)
    reason = apply_pause.apply_judge.sensitive_reason("Date of birth")
    plan = FillPlan(fields=[_pf(dob)], park_reason=reason)
    p.at_plan(digest, plan)
    assert page.waits == 0 and p.count == 0
    assert not apply_pause.request_path("42").exists()
    assert "pause_skipped" in jr.decided and "pause" not in jr.decided
    # a headed browser still pauses for it
    headed = _Jr(_Page(_Clock(), on_wait=_answers_with("park")), digest=digest)
    apply_pause.Pauser(headed, RuntimeError).at_plan(digest, FillPlan(fields=[_pf(dob)],
                                                                      park_reason=reason))
    assert "pause" in headed.decided


def test_a_choice_that_names_no_live_option_is_never_saved(pauses_on, boxes):
    # review M5: the value is checked against the live options before the save
    apply_answers.save(apply_answers.seed_defaults())
    team = _field(1, "Preferred team", "select", options=["Data", "Platform"],
                  id_or_name="team")
    talks = _field(2, "Number of conference talks given", "number", id_or_name="talks")
    digest = _digest(team, talks)
    jr = _Jr(_Page(_Clock(), on_wait=_answers_with("fill", {"1": "Plattform", "2": "four"},
                                                   {"1": True, "2": True})), digest=digest)
    p = apply_pause.Pauser(jr, RuntimeError)
    plan = FillPlan(fields=[_pf(team), _pf(talks)],
                    park_reason="required field without an answer: Preferred team")
    p.at_plan(digest, plan)
    questions = {e["question"] for e in apply_answers.load()}
    assert "Preferred team" not in questions
    assert "Number of conference talks given" not in questions
    assert jr.decided.count("pause_value_refused") == 2, jr.decided
    assert [pf.action for pf in plan.fields] == ["skip", "skip"]


def test_every_resume_reads_the_answer_store_again(pauses_on, boxes):
    # review M2: an answer added in the Apply Answers tab during the pause
    # counts from the next page, a save or none
    f = _field(1, "Referral code", required=False, id_or_name="referral")
    digest = _digest(f)
    jr = _Jr(_Page(_Clock(), on_wait=_answers_with("browser")), digest=digest)
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(apply_pause.Replan):
        p.at_disabled(digest, FillPlan(fields=[_pf(f)]), "disabled")
    assert jr.reloads == 1


# -- a page that moved on during the wait (final review A I-1) ---------------------------------

def _moves_on(jr, step_two):
    """What the person does in the browser during the wait: goes on to the
    next step, whose own "Please explain" box sits at the same path."""
    def then():
        jr.page.url = _URL + "?step=2"
        jr.digest = step_two
    return then


def test_a_card_answer_is_dropped_when_the_page_moved_on_at_a_disabled_way_on(pauses_on,
                                                                             boxes):
    # the person answered in the card, then clicked Next in the browser: the
    # answer belongs to step 1's box and never goes in step 2's box
    one = _field(1, "Please explain", required=False, id_or_name="explain")
    two = _field(5, "Please explain", required=False, id_or_name="explain")
    step_one, step_two = _digest(one), _digest(two)
    page = _Page(_Clock())
    jr = _Jr(page, digest=step_one)
    page.on_wait = _answers_with("fill", {"1": "step one answer"},
                                 then=_moves_on(jr, step_two))
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(apply_pause.Replan):
        p.at_disabled(step_one, FillPlan(fields=[_pf(one)]), "disabled")
    assert p.pending == {}
    plan = FillPlan(fields=[_pf(two)])
    p.apply_pending(step_two, plan)
    assert [(pf.action, pf.value) for pf in plan.fields] == [("skip", "")]


def test_a_card_answer_is_dropped_when_the_page_moved_on_at_a_required_field(pauses_on,
                                                                           boxes):
    one = _field(1, "Please explain", id_or_name="explain")
    two = _field(5, "Please explain", id_or_name="explain")
    step_one, step_two = _digest(one), _digest(two)
    page = _Page(_Clock())
    jr = _Jr(page, digest=step_one)
    page.on_wait = _answers_with("fill", {"1": "step one answer"},
                                 then=_moves_on(jr, step_two))
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(apply_pause.Replan):
        p.at_plan(step_one, FillPlan(fields=[_pf(one)], park_reason="required field without "
                                                                     "an answer: Please explain"))
    plan = FillPlan(fields=[_pf(two)])
    p.apply_pending(step_two, plan)
    assert [(pf.action, pf.value) for pf in plan.fields] == [("skip", "")]


def test_a_card_answer_is_dropped_on_a_same_address_app_whose_next_step_repeats_the_field(
        pauses_on, boxes):
    # final fix review Important 1: a single-page app never changes its
    # address. Step 1 has "Full name" and "Please explain" (id explain); the
    # person answers in the card and clicks Next; step 2 shows its own
    # "Please explain" (id explain). Step 1's "Full name" is gone, so the page
    # moved on and step 2's box never takes step 1's answer
    name = _field(1, "Full name", id_or_name="name")
    one = _field(2, "Please explain", required=False, id_or_name="explain")
    two = _field(5, "Please explain", required=False, id_or_name="explain")
    step_one, step_two = _digest(name, one), _digest(two)
    page = _Page(_Clock())
    jr = _Jr(page, digest=step_one)

    def next_step():
        jr.digest = step_two            # the address stays _URL
    page.on_wait = _answers_with("fill", {"2": "step one answer"}, then=next_step)
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(apply_pause.Replan):
        p.at_disabled(step_one, FillPlan(fields=[_pf(name, "fill"), _pf(one)]), "disabled")
    assert jr.page.url == _URL
    assert p.pending == {}
    assert "pause_answer_dropped" in jr.decided, jr.decided
    plan = FillPlan(fields=[_pf(two)])
    p.apply_pending(step_two, plan)
    assert [(pf.action, pf.value) for pf in plan.fields] == [("skip", "")]
    assert "pause_answer" not in jr.decided, jr.decided


def test_a_card_answer_stays_when_every_field_of_the_paused_page_is_still_there(pauses_on,
                                                                               boxes):
    # the same address and the same fields: the answer goes in its own box
    name = _field(1, "Full name", id_or_name="name")
    one = _field(2, "Please explain", required=False, id_or_name="explain")
    digest = _digest(name, one)
    jr = _Jr(_Page(_Clock(), on_wait=_answers_with("fill", {"2": "my answer"})), digest=digest)
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(apply_pause.Replan):
        p.at_disabled(digest, FillPlan(fields=[_pf(name, "fill"), _pf(one)]), "disabled")
    plan = FillPlan(fields=[_pf(name, "fill"), _pf(one)])
    p.apply_pending(digest, plan)
    assert [(pf.action, pf.value) for pf in plan.fields][1] == ("fill", "my answer")
    assert "pause_answer_dropped" not in jr.decided, jr.decided


def test_a_card_answer_is_keyed_by_the_page_the_run_paused_on(pauses_on, boxes):
    # a page that grew during the wait (a new box, the asked one still there)
    # keeps the answer, keyed by the paused page's own path
    one = _field(1, "Please explain", id_or_name="explain")
    extra = _field(2, "Portfolio URL", required=False, id_or_name="portfolio")
    step_one, grown = _digest(one), _digest(one, extra)
    page = _Page(_Clock())
    jr = _Jr(page, digest=step_one)

    def grow():
        jr.digest = grown
    page.on_wait = _answers_with("fill", {"1": "my answer"}, then=grow)
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(apply_pause.Replan):
        p.at_plan(step_one, FillPlan(fields=[_pf(one)], park_reason="required field without "
                                                                    "an answer: Please explain"))
    assert list(p.pending.values()) == ["my answer"]
    assert {key[0] for key in p.pending} == {"ats.example/apply/1"}
    plan = FillPlan(fields=[_pf(one), _pf(extra)])
    p.apply_pending(grown, plan)
    assert [(pf.action, pf.value) for pf in plan.fields] == [("fill", "my answer"),
                                                             ("skip", "")]


class _ClosedPark(Exception):
    """What the run's `_pause_closed` returns: its check-whether park."""


def _closing_hook(jr) -> list:
    """`jr._pause_closed` as the run gives it: each call kept, its own park
    returned."""
    seen: list = []

    def hook(buttons, reason):
        seen.append((buttons, reason))
        return _ClosedPark(reason)
    jr._pause_closed = hook
    return seen


def test_a_close_during_the_wait_raises_the_runs_own_park(pauses_on, boxes):
    # final review A I-2, final fix review Minor 2: the run reads the buttons
    # the pause saw (`_JobRun._pause_closed`) and its park, already the
    # check-whether end, is the one raised
    f = _field(1, "Please explain", id_or_name="explain")
    digest = _digest(f)
    digest.buttons.append(SimpleNamespace(text="Submit application", locator=(0, "#go")))
    jr = _Jr(_Page(_Clock(), closed_after=1), digest=digest)
    seen = _closing_hook(jr)
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(_ClosedPark):
        p.at_plan(digest, FillPlan(fields=[_pf(f)], park_reason="required field without an "
                                                                "answer: Please explain"))
    assert seen == [((("Submit application", (0, "#go")),),
                     "required field without an answer: Please explain")]
    assert apply_pause.pending_requests() == []


def test_a_close_after_the_waits_last_poll_raises_the_runs_own_park(pauses_on, boxes):
    # final fix review Minor 2: the answer lands, then the tab closes before
    # the run reads the page again; the close is still the pause's
    class _ClosesOnThirdLook(_Page):
        looks = 0

        def is_closed(self):
            self.looks += 1
            return self.looks >= 3      # the wait's two polls, then the check after it
    f = _field(1, "Referral code", required=False, id_or_name="referral")
    digest = _digest(f)
    page = _ClosesOnThirdLook(_Clock(), on_wait=_answers_with("fill", {"1": "X"}))
    jr = _Jr(page, digest=digest)
    seen = _closing_hook(jr)
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(_ClosedPark):
        p.at_disabled(digest, FillPlan(fields=[_pf(f)]), "disabled")
    assert len(seen) == 1 and jr.moved_calls == []
    assert p.pending == {} and "pause_resume" not in jr.decided


def test_a_close_with_no_run_hook_parks_with_the_pauses_reason(pauses_on, boxes):
    f = _field(1, "Please explain", id_or_name="explain")
    digest = _digest(f)
    jr = _Jr(_Page(_Clock(), closed_after=1), digest=digest)
    p = apply_pause.Pauser(jr, RuntimeError)
    with pytest.raises(RuntimeError, match="required field without an answer"):
        p.at_plan(digest, FillPlan(fields=[_pf(f)], park_reason="required field without an "
                                                                "answer: Please explain"))


def test_a_save_keeps_the_review_list_of_a_version_1_store(tmp_path):
    # final review A I-3: a store still version 1 on disk migrates in memory
    # with its review list; the pause's save keeps that list
    path = tmp_path / "apply_answers.json"
    path.write_text(json.dumps({"answers": [
        {"id": "work_authorized", "question": apply_answers.BUILTINS["work_authorized"].question,
         "answer": "Yes, I am a US citizen", "kind": "open-ended", "status": "active"}]}),
        encoding="utf-8")
    review = apply_answers.load_store(path)["review"]
    assert len(review) == 1
    assert apply_pause.save_answer(_question(label="Favourite colour"), "teal", "Fabrikam",
                                   path=path) == ""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 2 and data["review"] == review
    assert "Favourite colour" in {e["question"] for e in data["answers"]}


def test_a_request_names_the_runs_process_and_its_minutes():
    path = apply_pause.write_request(_JOB, _URL, "r", [], minutes=7)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid() and data["minutes"] == 7


def test_a_request_whose_run_is_gone_is_cleaned_up(monkeypatch):
    # review M3: a killed drain leaves its request behind
    apply_pause.write_request(_JOB, _URL, "r", [], minutes=10)
    apply_pause.write_request({**_JOB, "job_posting_id": "7"}, _URL, "r", [], minutes=10)
    dead = json.loads(apply_pause.request_path("7").read_text(encoding="utf-8"))
    dead["pid"] = 999_999_999
    apply_pause.request_path("7").write_text(json.dumps(dead), encoding="utf-8")
    monkeypatch.setattr(apply_pause, "pid_alive", lambda pid: pid == os.getpid())
    assert [r["job"] for r in apply_pause.pending_requests()] == ["42"]
    assert not apply_pause.request_path("7").exists()


def test_a_request_older_than_its_wait_is_cleaned_up():
    apply_pause.write_request(_JOB, _URL, "r", [], minutes=10)
    path = apply_pause.request_path("42")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["asked_epoch"] -= 10 * 60 + apply_pause.STALE_MARGIN_S + 5
    path.write_text(json.dumps(data), encoding="utf-8")
    assert apply_pause.pending_requests() == []
    assert not path.exists()


def test_a_request_is_aged_by_epoch_seconds_so_a_clock_change_keeps_it(monkeypatch):
    # review N3: the local asked_at is for display; a DST change or a clock
    # jump that makes it read hours old never clears a live request
    before = time.time()
    apply_pause.write_request(_JOB, _URL, "r", [], minutes=10)
    path = apply_pause.request_path("42")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert before <= data["asked_epoch"] <= time.time()
    data["asked_at"] = (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert [r["job"] for r in apply_pause.pending_requests()] == ["42"]
    # the wait's own minutes, counted in epoch seconds, still age it out
    monkeypatch.setattr(apply_pause.time, "time",
                        lambda: data["asked_epoch"] + 10 * 60 + apply_pause.STALE_MARGIN_S + 1)
    assert apply_pause.pending_requests() == []


def test_pid_alive_tells_a_live_process_from_a_finished_one():
    assert apply_pause.pid_alive(os.getpid())
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, encoding="utf-8", check=True)
    assert not apply_pause.pid_alive(int(done.stdout.strip()))
    assert not apply_pause.pid_alive(0)
