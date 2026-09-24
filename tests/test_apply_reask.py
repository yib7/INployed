"""SP5: the second look at a required field's dropped or weak mapping.

SP4's matrix left every noisy miss on one class: a required field whose
mapping the judge dropped, or whose consent tick it read under the consent
floor, parked "required field without an answer" although the data answers
it. The run now asks that field alone once more (a small request: its label,
type, options and the sources its type can take) before it parks, and only
then; a field the data cannot answer still parks.

- `reask_targets`: which fields get the second look (a dropped or weak
  source, a consent tick under its floor, a dropped or weak pick), and which
  never do (a confident "nothing fits", an optional field, a sensitive box,
  a `quick_map` field's source).
- `reask_questions`: the request carries the one field and the descriptions
  of its type's sources, never a value; the pick carries the value in its
  instruction like the first look's.
- `NoisyJev` drops the second look's answer at the same rate as the first,
  on its own draw, so a gain comes from the second look alone.
- End to end: the Ashby wizard with the sponsorship mapping dropped reaches
  its confirmation; with the second look dropped too it parks; the embedded
  Greenhouse form's certify box read under the consent floor is ticked after
  a sure second look; a question nothing answers parks after it.

No network; the browser tests use the module-scoped headless Chromium and
the flow server; the judges are `FakeJev` and wraps of it."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_facts  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_judge  # noqa: E402
import jev  # noqa: E402
from apply_form import Field, FormDigest  # noqa: E402

pytest_plugins = ["conftest_browser"]


@pytest.fixture
def catalog(tmp_path):
    folder = h.write_job_folder(tmp_path / "job")
    return apply_facts.build(folder, answers=h.bank())


def _f(n, label, type_="text", required=True, options=()):
    return Field(n=n, locator=(0, f"#f{n}"), label=label, type=type_, required=required,
                 options=list(options))


def _choice(choice, conf):
    return jev.Answer(kind="choice", choice=choice, probabilities={choice: conf},
                      confidence=conf)


def _digest():
    return FormDigest(url_host="jobs.example.com", title="Apply", text="", fields=[
        _f(0, "Are you authorized to work in the US?", "select", options=("Yes", "No")),
        _f(1, "Will you now or in the future require sponsorship?", "radio",
           options=("Yes", "No")),
        _f(2, "I consent to a background check", "checkbox", options=("checked",)),
        _f(3, "Anything else?", "textarea", required=False),
        _f(4, "What is your favourite colour?", "text"),
        _f(5, "Social Security Number", "text"),
    ])


def test_the_second_look_takes_a_dropped_or_weak_source_and_a_consent_under_its_floor(catalog):
    digest = _digest()
    answers = {
        # field 0: the first look dropped its mapping
        "field_1_source": _choice("requires_sponsorship", 0.9),     # its pick dropped
        "field_2_source": _choice("consent_attest", 0.80),          # under the consent floor
        # field 3: optional, dropped: never asked again
        "field_4_source": _choice("leave_blank", 0.95),             # nothing fits: parks
        # field 5: a sensitive box, dropped: never asked
    }
    plan = apply_judge.plan(digest, catalog, answers)
    assert apply_judge.reask_targets(digest, catalog, answers, plan, what="source") == [0, 2]
    assert apply_judge.reask_targets(digest, catalog, answers, plan, what="pick") == [1]
    # an unsure "nothing fits" is no answer; a weak mapping of any key neither
    answers["field_4_source"] = _choice("leave_blank", 0.55)
    answers["field_0_source"] = _choice("work_authorized", 0.6)
    plan = apply_judge.plan(digest, catalog, answers)
    assert apply_judge.reask_targets(digest, catalog, answers, plan, what="source") == [0, 2, 4]
    # a confident no_match is the data's answer; a weak pick is not
    answers["field_1_pick"] = _choice("no_match", 0.9)
    plan = apply_judge.plan(digest, catalog, answers)
    assert apply_judge.reask_targets(digest, catalog, answers, plan, what="pick") == []
    answers["field_1_pick"] = _choice("No", 0.5)
    plan = apply_judge.plan(digest, catalog, answers)
    assert apply_judge.reask_targets(digest, catalog, answers, plan, what="pick") == [1]
    # a routine consent (SP5 round 2) at 0.80 ticks: it is no target
    routine = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "I certify that the information provided is accurate", "checkbox",
           options=("checked",))])
    answers = {"field_0_source": _choice("consent_attest", 0.80)}
    plan = apply_judge.plan(routine, catalog, answers)
    assert apply_judge.reask_targets(routine, catalog, answers, plan, what="source") == []


def test_a_quick_map_field_gets_its_pick_asked_again_never_its_source(catalog):
    digest = FormDigest(url_host="x", title="t", text="",
                        fields=[_f(0, "Country", "select", options=("Canada", "United States"))])
    plan = apply_judge.plan(digest, catalog, {})
    assert plan.fields[0].quick and plan.fields[0].action == "skip"
    assert apply_judge.reask_targets(digest, catalog, {}, plan, what="source") == []
    assert apply_judge.reask_targets(digest, catalog, {}, plan, what="pick") == [0]
    state, q = apply_judge.reask_questions(digest, catalog, plan, [0], what="pick")
    assert list(q) == ["field_0_option"]
    assert q["field_0_option"]["instructions"]["candidate_answer"] == "United States"
    assert state == {"fields": [{"n": 0, "label": "Country", "type": "select", "required": True,
                                 "options": ["Canada", "United States"]}]}


def test_the_second_look_is_the_field_alone_with_its_types_sources_and_no_value(catalog):
    digest = _digest()
    plan = apply_judge.plan(digest, catalog, {})
    state, q = apply_judge.reask_questions(digest, catalog, plan, [0], what="source",
                                           job={"company_name": "Fabrikam",
                                                "job_title": "Analytics Engineer"})
    assert list(q) == ["field_0_source"]
    assert [f["label"] for f in state["fields"]] == ["Are you authorized to work in the US?"]
    assert state["fields"][0]["options"] == ["Yes", "No"]
    assert "`fields[0]`" in q["field_0_source"]["instructions"]
    assert state["job"] == {"company": "Fabrikam", "title": "Analytics Engineer"}
    crit = q["field_0_source"]["criteria"]
    # a select never takes a name, an email or a file
    assert "work_authorized" in crit and "leave_blank" in crit
    assert not {"first_name", "email", "resume_file"} & set(crit)
    assert set(state["facts"]) == set(crit)
    blob = repr(state)
    for fact in ("Jane", "jane.doe@example.com", "555-555-0100"):
        assert fact not in blob
    # the fake reads it as the first look would
    got = jev.FakeJev().judge(state, q)
    assert got["field_0_source"].choice == "work_authorized"
    state2, q2 = apply_judge.reask_questions(digest, catalog, plan, [2], what="source")
    assert "consent_attest" in q2["field_2_source"]["criteria"]
    assert jev.FakeJev().judge(state2, q2)["field_2_source"].choice == "consent_attest"


def test_the_second_look_asks_every_target_in_one_request(catalog):
    # review M11: one request per read and kind, whatever the number of targets
    digest = _digest()
    plan = apply_judge.plan(digest, catalog, {})
    targets = apply_judge.reask_targets(digest, catalog, {}, plan, what="source")
    assert targets == [0, 1, 2, 4]
    state, q = apply_judge.reask_questions(digest, catalog, plan, targets, what="source")
    assert sorted(q) == [f"field_{n}_source" for n in targets]
    assert [f["n"] for f in state["fields"]] == targets
    got = jev.FakeJev().judge(state, q)
    assert got["field_0_source"].choice == "work_authorized"
    assert got["field_1_source"].choice == "requires_sponsorship"
    assert got["field_2_source"].choice == "consent_attest"


def test_the_noisy_judge_drops_the_second_look_at_its_own_rate_and_draw(catalog):
    """The second look faces the first look's drop rate on a draw of its own:
    over 400 seeds each look is dropped near `drop_p`, and both together
    near its square."""
    digest = _digest()
    plan = apply_judge.plan(digest, catalog, {})
    first = apply_judge.page_questions(digest, catalog, {})
    second = apply_judge.reask_questions(digest, catalog, plan, [0], what="source")
    drops = [0, 0, 0]
    seeds = range(1, 401)
    for seed in seeds:
        noisy = jev.NoisyJev(jev.FakeJev(), seed, drop_p=0.2)
        a = "field_0_source" not in noisy.judge(*first)
        b = "field_0_source" not in noisy.judge(*second)
        drops[0] += a
        drops[1] += b
        drops[2] += a and b
    assert 0.14 * 400 <= drops[0] <= 0.26 * 400, drops
    assert 0.14 * 400 <= drops[1] <= 0.26 * 400, drops
    assert drops[2] <= 0.08 * 400, drops
    # its confidence is scaled as the first look's is
    noisy = jev.NoisyJev(jev.FakeJev(), 3, drop_p=0.0)
    conf = noisy.judge(*second)["field_0_source"].confidence
    assert 0.75 <= conf <= 1.0


# --- end to end ------------------------------------------------------------------------------

class DropFirst:
    """The first request that asks `qid` gets no answer for it (a first look
    that dropped the mapping); `every` drops it from every request that asks
    it (the second look dropped too). Everything else is the inner judge's."""

    def __init__(self, inner, qid_word: str, *, every: bool = False):
        self.inner = inner
        self.word = qid_word
        self.every = every
        self.dropped = 0

    def _hit(self, state, questions):
        blob = repr(state)
        return [q for q in questions if q.startswith("field_") and q.endswith("_source")
                and self.word in blob and self._field_of(state, q)]

    def _field_of(self, state, qid):
        n = int(qid.split("_")[1])
        rows = state.get("fields") or []
        return any(r.get("n") == n and self.word in r.get("label", "") for r in rows)

    def judge(self, state, questions):
        out = dict(self.inner.judge(state, questions))
        if self.every or not self.dropped:
            for qid in self._hit(state, questions):
                out.pop(qid, None)
                self.dropped += 1
        return out


class ScaleFirst:
    """The first request that maps a field labelled with `word` to
    `consent_attest` answers it at `conf` (under the consent floor); later
    looks are the inner judge's."""

    def __init__(self, inner, word: str, conf: float = 0.80):
        self.inner = inner
        self.word = word
        self.conf = conf
        self.done = False

    def judge(self, state, questions):
        out = dict(self.inner.judge(state, questions))
        if self.done:
            return out
        rows = state.get("fields") or []
        for row in rows:
            qid = f"field_{row.get('n')}_source"
            a = out.get(qid)
            if self.word in row.get("label", "") and a is not None \
                    and a.choice == "consent_attest":
                out[qid] = jev.Answer(kind="choice", choice=a.choice,
                                      probabilities={a.choice: self.conf},
                                      confidence=self.conf)
                self.done = True
        return out


def _run(name, judge, _browser, flow_server, tmp_path):
    return h.run_flow(h.flow(name), judge, "wrapped", browser=_browser, server=flow_server,
                      workdir=tmp_path)


def _events(trace_dir, kind):
    import json
    return [e for p in sorted(Path(trace_dir).glob("page-*.json"))
            for e in json.loads(p.read_text(encoding="utf-8"))["events"] if e["kind"] == kind]


def test_a_dropped_required_mapping_is_asked_again_and_the_wizard_reaches_its_end(
        _browser, flow_server, tmp_path):
    judge = DropFirst(jev.FakeJev(), "sponsorship")
    r = _run("ashby_wizard", judge, _browser, flow_server, tmp_path)
    assert judge.dropped == 1
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    asked = _events(r.trace, "reask")
    assert [(e["what"], e["fields"]) for e in asked] == [("source", [1])]
    assert "field_1_source" in asked[0]["answers"]


def test_a_second_look_that_is_dropped_too_parks_on_the_field(_browser, flow_server, tmp_path):
    judge = DropFirst(jev.FakeJev(), "sponsorship", every=True)
    r = _run("ashby_wizard", judge, _browser, flow_server, tmp_path)
    assert judge.dropped == 2          # the first look and the second, nothing more
    assert (r.status, r.reason) == ("needs_human", "required field without an answer: Will "
                                                   "you now or in the future require "
                                                   "sponsorship?")
    assert not r.breaks


_SHOE = """<!doctype html><html><head><title>Apply - Fabrikam</title></head><body>
<h1>Analytics Engineer</h1>
<form id="app" onsubmit="event.preventDefault(); document.body.dataset.submitted = 1">
  <label for="fn">First name *</label><input id="fn" name="first_name" required>
  <label for="em">Email *</label><input id="em" name="email" type="email" required>
  <label for="shoe">Which shoe size do you wear? *</label><input id="shoe" name="shoe" required>
  <button type="submit" id="btn-submit">Submit application</button>
</form></body></html>"""


class NothingFits:
    """Every look at a field labelled with `word` answers `leave_blank`, as a
    judge does for a question no fact answers."""

    def __init__(self, inner, word: str):
        self.inner = inner
        self.word = word

    def judge(self, state, questions):
        out = dict(self.inner.judge(state, questions))
        rows = state.get("fields") or []
        for row in rows:
            qid = f"field_{row.get('n')}_source"
            if self.word in row.get("label", "") and qid in out:
                out[qid] = jev.Answer(kind="choice", choice="leave_blank",
                                      probabilities={"leave_blank": 1.0}, confidence=1.0)
        return out


def test_a_question_the_data_cannot_answer_still_parks_after_the_second_look(
        _browser, flow_server, tmp_path):
    import dataclasses
    f = dataclasses.replace(h.flow("lever_single_park"), name="shoe_size",
                            start="https://careers.fabrikam.example/apply/42",
                            routes=lambda base: {"https://careers.fabrikam.example/**": _SHOE})
    judge = DropFirst(NothingFits(jev.FakeJev(), "shoe"), "shoe")
    r = h.run_flow(f, judge, "wrapped", browser=_browser, server=flow_server, workdir=tmp_path)
    assert judge.dropped == 1
    assert (r.status, r.reason) == ("needs_human", "required field without an answer: Which "
                                                   "shoe size do you wear?")
    asked = _events(r.trace, "reask")
    assert len(asked) == 1 and asked[0]["answers"]["field_2_source"]["choice"] == "leave_blank"
    assert not r.breaks


def test_a_consent_tick_read_under_its_floor_is_ticked_after_a_sure_second_look(
        _browser, flow_server, tmp_path):
    # a routine consent's floor is the mapping floor (SP5 round 2): read under it
    judge = ScaleFirst(jev.FakeJev(), "certify", conf=0.60)
    r = _run("greenhouse_embed", judge, _browser, flow_server, tmp_path)
    assert judge.done
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    asked = _events(r.trace, "reask")
    assert len(asked) == 1 and asked[0]["what"] == "source"


# --- a consent tick's second look stands alone (review I1) -------------------------------------

_CERTIFY = "I certify that the information provided is accurate"      # routine
_BACKGROUND_CHECK = "I consent to a background check"                    # a commitment


@pytest.mark.parametrize("label, second, ticked", [
    (_BACKGROUND_CHECK, ("consent_attest", 0.90), True),
    # under the floor: parks, however the first look read
    (_BACKGROUND_CHECK, ("consent_attest", 0.84), False),
    (_CERTIFY, ("consent_attest", 0.72), True),         # a routine consent's floor is 0.70
    (_CERTIFY, ("consent_attest", 0.68), False),
    (_CERTIFY, ("leave_blank", 0.95), False),
])
def test_a_consent_ticks_second_look_stands_alone_against_its_floor(catalog, label, second,
                                                                    ticked):
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, label, "checkbox", options=("checked",))])
    pf = apply_judge.plan(digest, catalog, {"field_0_source": _choice(*second)}).fields[0]
    assert (pf.action == "select" and pf.option == "checked") is ticked


class ConsentEvery:
    """Every look maps a box labelled with `word` to `consent_attest` at `conf`:
    a judge that finds the box borderline reads it borderline twice."""

    def __init__(self, inner, word: str, conf: float):
        self.inner = inner
        self.word = word
        self.conf = conf

    def judge(self, state, questions):
        out = dict(self.inner.judge(state, questions))
        for row in state.get("fields") or []:
            qid = f"field_{row.get('n')}_source"
            if self.word in row.get("label", "") and qid in questions:
                out[qid] = jev.Answer(kind="choice", choice="consent_attest",
                                      probabilities={"consent_attest": self.conf},
                                      confidence=self.conf)
        return out


_BACKGROUND = """<!doctype html><html><head><title>Apply - Fabrikam</title></head><body>
<h1>Analytics Engineer</h1>
<form id="app" onsubmit="event.preventDefault(); document.body.dataset.submitted = 1">
  <label for="fn">First name *</label><input id="fn" name="first_name" required>
  <label for="em">Email *</label><input id="em" name="email" type="email" required>
  <label><input type="checkbox" id="bg" name="bg" required> I consent to a background check *</label>
  <button type="submit" id="btn-submit">Submit application</button>
</form></body>"""


def test_a_borderline_commitment_read_under_the_floor_twice_still_parks(
        _browser, flow_server, tmp_path):
    import dataclasses
    f = dataclasses.replace(h.flow("lever_single_park"), name="background_check",
                            start="https://careers.fabrikam.example/apply/42",
                            routes=lambda base: {"https://careers.fabrikam.example/**":
                                                 _BACKGROUND})
    r = h.run_flow(f, ConsentEvery(jev.FakeJev(), "background", 0.78), "borderline",
                   browser=_browser, server=flow_server, workdir=tmp_path)
    assert (r.status, r.reason) == ("needs_human", "required field without an answer: I "
                                                   "consent to a background check"), r.reason
    asked = _events(r.trace, "reask")
    assert len(asked) == 1 and asked[0]["answers"]["field_2_source"]["choice"] == "consent_attest"
    assert not [a for a in r.actions if a.kind in ("tick", "click") and "background" in a.text]
    assert not r.breaks


def test_a_certify_box_read_under_its_floor_on_both_looks_parks(_browser, flow_server, tmp_path):
    # read under a routine consent's floor (the mapping floor) on both looks
    r = _run("greenhouse_embed", ConsentEvery(jev.FakeJev(), "certify", 0.66), _browser,
             flow_server, tmp_path)
    assert (r.status, r.reason) == ("needs_human", "required field without an answer: I "
                                                   "certify that the information provided is "
                                                   "accurate"), r.reason


class DropInMapping:
    """The page's mapping (a request that carries the page's buttons) never
    answers a field labelled with `word`; the second look does."""

    def __init__(self, inner, word: str):
        self.inner = inner
        self.word = word
        self.second_looks = 0

    def judge(self, state, questions):
        out = dict(self.inner.judge(state, questions))
        rows = state.get("fields") or []
        if "buttons" not in state and any(q.endswith("_source") for q in questions):
            self.second_looks += 1
            return out
        for row in rows:
            if self.word in row.get("label", ""):
                out.pop(f"field_{row.get('n')}_source", None)
        return out


def test_a_re_read_of_the_same_page_reuses_the_second_look(_browser, flow_server, tmp_path):
    """Review M11: the same step comes back with an error line (a new page to
    the loop, the same fields); its second look is asked once and reused."""
    import dataclasses
    page = """<!doctype html><html><head><title>Apply - Fabrikam</title></head><body>
    <h1>Analytics Engineer</h1><h2>Eligibility</h2>
    <p id="err" role="alert"></p>
    <label for="wa">Are you authorized to work in the US? *</label>
    <select id="wa" required><option value="">Select</option><option>Yes</option>
      <option>No</option></select>
    <button type="button" id="next"
      onclick="document.getElementById('err').textContent = 'Please check the answers above.'"
      >Continue</button></body></html>"""
    f = dataclasses.replace(h.flow("lever_single_park"), name="same_step",
                            start="https://careers.fabrikam.example/apply/42",
                            routes=lambda base: {"https://careers.fabrikam.example/**": page})
    judge = DropInMapping(jev.FakeJev(), "authorized")
    r = h.run_flow(f, judge, "same", browser=_browser, server=flow_server, workdir=tmp_path)
    asked = [e for e in _events(r.trace, "reask") if e["what"] == "source"]
    assert [e["reused"] for e in asked] == [False, True], (r.reason, asked)
    assert judge.second_looks == 1


# --- review round 5, R5-I1: discovery never clicks a popup that sends -----------------------------

_SPLIT_SUBMIT = """<!doctype html><html><head><title>Apply - Fabrikam</title></head><body>
<h1>Analytics Engineer</h1>
<form id="app" onsubmit="event.preventDefault(); document.body.dataset.submitted = 1">
  <label for="fn">First name *</label><input id="fn" name="first_name" required>
  <label for="em">Email *</label><input id="em" name="email" type="email" required>
  <div class="split"><button type="submit" id="btn-submit">Submit application</button>
    <button type="button" id="more" aria-haspopup="menu" aria-label="__ARROW__"
      onclick="document.getElementById('menu').hidden = false">
      <svg width="10" height="10"><path d="M0 0 L10 0 L5 8 z"/></svg></button></div>
  <div role="menu" id="menu" hidden><div role="menuitem">Submit and save a copy</div></div>
</form></body></html>"""


@pytest.mark.parametrize("arrow", ["More submit options",
                                   # review round 7: an icon-only arrow named at any length
                                   "Submit your application right now"])
def test_discovery_never_clicks_a_popup_whose_own_words_send(_browser, flow_server, tmp_path,
                                                             arrow):
    """Review R5-I1: a submit's menu arrow ("More submit options") read as a
    dropdown was opened by the options discovery, a click outside the
    submit gate on a control that reads submit (CLICK-OUTSIDE-GATE)."""
    import dataclasses
    page = _SPLIT_SUBMIT.replace("__ARROW__", arrow)
    f = dataclasses.replace(h.flow("lever_single_park"), name="split_submit",
                            start="https://careers.fabrikam.example/apply/42",
                            routes=lambda base: {"https://careers.fabrikam.example/**": page})
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert not r.breaks, r.breaks
    assert r.ok, (r.status, r.reason)
    assert not [a for a in r.actions if a.kind == "click" and "submit" in (a.text or "").lower()
                and not a.in_gate]


# --- review round 8 (1): a misread send popup is never opened in a run -----------------------------

_SEND_ARROW_PAGE = """<!doctype html><html><head><title>Apply - Fabrikam</title></head><body>
<h1>Analytics Engineer</h1>
<form id="app" onsubmit="event.preventDefault(); document.body.dataset.submitted = 1">
  <label for="fn">First name *</label><input id="fn" name="first_name" required>
  <label for="em">Email *</label><input id="em" name="email" type="email" required>
  <button type="submit" id="btn-submit">Submit application</button>
  __ARROW__
</form></body></html>"""


@pytest.mark.parametrize("shape", ["shown", "long", "legend"])
def test_a_misread_send_popup_is_never_opened_in_a_run(_browser, flow_server, tmp_path,
                                                       monkeypatch, shape):
    """Review round 8: whatever the extractor decides, a popup whose own
    words send is never opened; a misread one (added here as a dropdown
    field) is left unopened and, optional, skipped."""
    import dataclasses

    import apply_form
    import test_apply_widgets as tw
    arrow = tw._SEND_POPUPS[shape].replace('id="btn-submit"', 'id="btn-submit-2"')
    page = _SEND_ARROW_PAGE.replace("__ARROW__", arrow)
    real = apply_form.extract

    def _misread(p, *a, **kw):
        d = real(p, *a, **kw)
        if p.locator("#arrow").count() and not any(f.locator[1] == "#arrow" for f in d.fields):
            d.fields.append(apply_form.Field(n=len(d.fields), locator=(0, "#arrow"),
                                             label="Delivery options", type="listbox",
                                             required=False, widget="popup"))
        return d
    monkeypatch.setattr(apply_form, "extract", _misread)
    f = dataclasses.replace(h.flow("lever_single_park"), name=f"send_arrow_{shape}",
                            start="https://careers.fabrikam.example/apply/42",
                            routes=lambda base: {"https://careers.fabrikam.example/**": page})
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert not r.breaks, r.breaks
    assert r.ok, (r.status, r.reason)


def test_a_required_popup_the_guard_refused_parks_on_its_question(catalog):
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Delivery options", "listbox", required=True)])
    digest.fields[0].refused = "its text reads 'Submit', a send"
    answers = {"field_0_source": _choice("full_name", 0.95)}
    p = apply_judge.plan(digest, catalog, answers)
    assert p.fields[0].action == "skip"
    assert p.park_reason == "required field without an answer: Delivery options"


# --- review round 3, M4: the commitment floor in the matrix ---------------------------------------

@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_the_commitment_flow_parks_on_its_box_and_never_ticks_it(_browser, flow_server, tmp_path,
                                                                 seed):
    f = h.flow("consent_commitment")
    judge = jev.FakeJev() if seed == 0 else jev.NoisyJev(jev.FakeJev(), seed)
    r = h.run_flow(f, judge, f"noisy-{seed}", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.policy is True
    # the run parks before it acts: no box is ticked, nothing is sent
    assert not [a for a in r.actions if a.kind in ("tick", "click", "pick")], r.actions
    assert r.sends == 0
