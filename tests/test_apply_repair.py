"""SP6: the page read again after its fill, and a step the form refuses repaired.

- The page read again after the fill: uploads first, a parser's or a
  lookup's writes put right (FILL-03), revealed fields filled (FILL-10),
  buttons the fill enabled or revealed judged (ADV-01, study G10), a still
  disabled way on parked with its evidence.
- The form's refusals: validation messages mapped to fields (code first,
  then one judge request), the fields repaired and the step clicked once
  more, a submit refused with nothing sent repaired once through the gate
  (ADV-02, ADV-06); a loading indicator waited out (ADV-07); the noise the
  new question takes.

Headless Chromium through the module-scoped test browser; no network, no
judge but `FakeJev` or a scripted one."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_fill  # noqa: E402
import apply_form  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_run  # noqa: E402
import jev  # noqa: E402
from apply_judge import FillPlan, PlannedField  # noqa: E402

pytest_plugins = ["conftest_browser"]


def _planned(f, action, value="", option=None, fact_key="x"):
    return PlannedField(n=f.n, locator=f.locator, label=f.label, required=f.required,
                        fact_key=fact_key, value=value, option=option, confidence=1.0,
                        action=action, widget=f.widget, click_locator=f.click_locator,
                        option_locators=list(f.option_locators), options=list(f.options),
                        ident=f.ident)


def _by_label(digest, label):
    found = [f for f in digest.fields if f.label == label]
    assert len(found) == 1, [(f.label, f.type) for f in digest.fields]
    return found[0]


def _fill(page, *planned):
    errors: list = []
    out = apply_fill.apply(page, FillPlan(fields=list(planned)), errors=errors)
    assert not errors, errors
    return {f.n: f.value for f in out}


def _decisions(r) -> list[dict]:
    import json
    out = []
    for p in sorted(Path(r.trace).glob("*.json")):
        out += [e for e in json.loads(p.read_text(encoding="utf-8")).get("events", [])
                if e.get("kind") == "decision"]
    return out


# === the page read again after the fill (FILL-03, FILL-10, ADV-01, study G10) ============================

def test_uploads_go_first_and_the_page_settles_before_the_rest(browser_page, tmp_path):
    pdf = tmp_path / "Jane_Doe_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    browser_page.set_content("""<body><form>
      <label>First name <input id="first" type="text"></label>
      <label>Resume <input id="resume" type="file"></label></form>
      <div id="busy"></div><script>
      window.events = [];
      document.getElementById('first').addEventListener('input', () => events.push('first'));
      document.getElementById('resume').addEventListener('change', () => {
        events.push('upload');
        const b = document.getElementById('busy');
        b.setAttribute('aria-busy', 'true'); b.textContent = 'Reading your resume...';
        setTimeout(() => { events.push('parsed'); b.removeAttribute('aria-busy');
                           b.textContent = ''; }, 400);
      });</script></body>""")
    d = apply_form.extract(browser_page)
    first, resume = _by_label(d, "First name"), _by_label(d, "Resume")
    got = apply_fill.apply(browser_page, FillPlan(fields=[
        _planned(first, "fill", "Jane"), _planned(resume, "upload", str(pdf))]))
    # the result keeps the plan's order; the page saw the upload, its parse, then the name
    assert [f.label for f in got] == ["First name", "Resume"]
    assert browser_page.evaluate("window.events")[:3] == ["upload", "parsed", "first"]


def test_what_the_parser_and_the_lookup_wrote_is_put_right_before_the_gate(
        _browser, flow_server, tmp_path):
    # the resume parser's guesses land before the fill (uploads first); the
    # lookup's capitals after it are put back; the headline the parser wrote
    # into a box the plan left alone is read as wrong and cleared
    r = h.run_flow(h.flow("resume_parse_autofill"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    what = {d["what"]: d for d in _decisions(r)}
    assert what["page_changed"]["fields"] == ["First name"]
    assert what["page_wrote"]["cleared"] == ["Headline"]


def test_a_revealed_field_is_filled_and_a_revealed_submit_is_judged(_browser, flow_server,
                                                                   tmp_path):
    r = h.run_flow(h.flow("conditional_fields"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    what = {d["what"]: d for d in _decisions(r)}
    assert what["revealed"]["fields"] == ["LinkedIn profile URL"]
    assert "Submit application" in what["buttons_after_fill"]["why"]


_STAYS_DISABLED = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>Analytics Engineer</h1><form id="f">
<label>Full name * <input name="name" required></label>
<label>Email * <input type="email" name="email" required></label>
<label>Referral code <input name="referral" id="referral"></label>
<button type="submit" id="btn-submit" disabled>Submit application</button></form>
<script>
  document.getElementById('referral').addEventListener('input', (e) => {
    document.getElementById('btn-submit').disabled = !e.target.value; });
  document.getElementById('f').addEventListener('submit', (e) => { e.preventDefault();
    document.body.dataset.submitted = 1; });
</script></body></html>"""


class _LeavesBlank:
    """The fake judge, with the fields named in `LABELS` mapped to
    `leave_blank` (no fact on the sheet answers them)."""
    LABELS = ("Referral code",)

    def __init__(self, inner):
        self.inner = inner

    def judge(self, state, questions):
        out = dict(self.inner.judge(state, questions))
        for row in (state or {}).get("fields") or []:
            qid = f"field_{row.get('n')}_source"
            if row.get("label") in self.LABELS and qid in out:
                out[qid] = jev.Answer(kind="choice", choice="leave_blank", confidence=0.95,
                                      probabilities={"leave_blank": 0.95})
        return out


def test_a_still_disabled_submit_parks_naming_what_was_left_blank(_browser, flow_server,
                                                                  tmp_path):
    import dataclasses
    f = dataclasses.replace(h.flow("lever_single_park"), name="stays_disabled",
                            start="https://careers.fabrikam.example/apply/42", wrap=_LeavesBlank,
                            routes=lambda base: {"https://careers.fabrikam.example/**":
                                                 _STAYS_DISABLED})
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert not r.breaks, r.breaks
    assert r.status == "needs_human", (r.status, r.reason)
    assert r.reason == ("required field without an answer: Referral code (the Submit "
                        "application button stays disabled after the fill)"), r.reason
    assert r.policy is True


# === the form's refusals, read and repaired (ADV-02, ADV-06, ADV-07) =====================================

def test_a_refused_next_is_repaired_and_clicked_again_only_after_the_repair(
        _browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("validation_errors"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    what = {d["what"]: d for d in _decisions(r)}
    assert what["repair"]["typed"] == ["Phone"]
    assert what["repair"]["blank"] == ["Years of experience"]
    # the summary no control names went to the judge, which named its field
    assert what["errors_mapped"]["messages"] == [
        "Please correct 2 error(s). Years of experience is required."]
    # ADV-06: the refused Next was never clicked again before the repair
    nexts = [a for a in r.actions if a.kind == "click" and a.text == "Next"]
    assert len(nexts) == 2


def test_a_submit_refused_with_nothing_sent_is_repaired_once_and_sent_through_the_gate(
        _browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("validation_errors_submit"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.sends == 1
    gates = [a for a in r.actions if a.kind == "gate"]
    submits = [a for a in r.actions if a.kind == "click" and a.text == "Submit application"]
    assert len(gates) == 2 and len(submits) == 2 and all(a.in_gate for a in submits)


_SPINNER_STEP = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>Analytics Engineer</h1><form id="f" novalidate>
<div id="s1"><label>Full name * <input name="name" required></label>
<label>Email * <input type="email" name="email" required></label>
<button type="button" id="next">Next</button></div>
<div id="s2" hidden><label>Resume * <input type="file" name="resume" required></label>
<button type="submit" id="btn-submit">Submit application</button></div></form>
<div id="spin"></div>
<script>
  document.getElementById('next').addEventListener('click', () => {
    document.getElementById('s1').hidden = true;
    const s = document.getElementById('spin');
    s.setAttribute('aria-busy', 'true'); s.textContent = 'Saving your answers...';
    setTimeout(() => { s.removeAttribute('aria-busy'); s.textContent = '';
                       document.getElementById('s2').hidden = false; }, __MS__);
  });
  document.getElementById('f').addEventListener('submit', (e) => { e.preventDefault();
    document.body.dataset.submitted = 1; });
</script></body></html>"""


def test_a_step_that_shows_a_loading_indicator_is_waited_out(_browser, flow_server, tmp_path):
    # ADV-07, the chaos case's long spinner: the step's indicator stays up
    # past every short wait the harness sets (a click's 3 s, a settle's 3 s)
    import dataclasses
    page = _SPINNER_STEP.replace("__MS__", "7000")
    f = dataclasses.replace(h.flow("lever_single_park"), name="spinner_step",
                            start="https://careers.fabrikam.example/apply/42",
                            routes=lambda base: {"https://careers.fabrikam.example/**": page})
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert any(d["what"] == "busy_after_click" for d in _decisions(r))


# --- the one request that maps messages to fields, and its noise ----------------------------------

def _fields(*labels):
    return [apply_form.Field(n=i, locator=(0, f"#f{i}"), label=lab, type="text", required=False)
            for i, lab in enumerate(labels)]


def test_the_fake_judge_maps_a_message_to_the_field_it_names_and_none_else():
    import apply_judge
    fields = _fields("Phone", "Years of experience", "Start date")
    msgs = ["Years of experience is required.", "Start date must be MM/DD/YYYY",
            "There is a problem with this page"]
    state, questions = apply_judge.error_questions(msgs, fields)
    got = apply_judge.read_error_fields(jev.FakeJev().judge(state, questions), len(msgs))
    assert got == {0: (1, 1.0), 1: (2, 1.0), 2: (None, 1.0)}
    # the state is the messages alone; the fields ride in the options
    assert set(state) == {"messages"}
    assert set(questions["error_0_field"]["criteria"]) == {"none", "q0", "q1", "q2"}


def test_the_noisy_judge_drops_flips_and_scales_a_messages_field():
    import apply_judge
    fields = _fields("Phone", "Years of experience", "Start date", "Email")
    msgs = ["Years of experience is required."]
    dropped = flipped = kept = 0
    for seed in range(1, 401):
        state, questions = apply_judge.error_questions(msgs, fields)
        out = jev.NoisyJev(jev.FakeJev(), seed).judge(state, questions)
        a = out.get("error_0_field")
        if a is None:
            dropped += 1
            continue
        assert a.probabilities[a.choice] == max(a.probabilities.values())
        if a.choice != "q1":
            flipped += 1
            assert 0.30 <= a.confidence <= 0.60
            assert a.probabilities["q1"] > 0      # the truth second
        else:
            kept += 1
            assert 0.75 <= a.confidence <= 1.0
    # drop_p 0.05 and swap_p 0.15 over 400 draws: at least as hard as a
    # field's mapping (drops) and a page state (flips, under the floor)
    assert 8 <= dropped <= 35 and 35 <= flipped <= 90 and kept > 250, (dropped, flipped, kept)


def test_a_message_the_judge_maps_under_its_floor_names_no_field(tmp_path):
    from unittest.mock import Mock

    class Unsure:
        def judge(self, state, questions):
            return {q: jev.Answer(kind="choice", choice="q0", confidence=0.55,
                                  probabilities={"q0": 0.55, "none": 0.45}) for q in questions}
    run = apply_run._JobRun(apply_run.Runner(jev=Unsure(), context=Mock(), run_context={},
                                             sleep=lambda s: None), Mock(),
                            {"job_posting_id": "s", "apply_url": "https://x.example/1"})
    digest = apply_form.FormDigest("x.example", "Apply", "", fields=_fields("Phone"))
    problem = {"label": "", "text": "Something is wrong with your number", "kind": "error"}
    assert run._problem_fields(digest, [problem]) == {}
    # a message tied to its control needs no judge at all
    tied = {"label": "Phone", "text": "Digits only", "kind": "invalid"}
    assert list(run._problem_fields(digest, [tied])) == [0]




# === SP6 review round 1 ====================================================================================

# --- I2: after a repair the same control is clicked, never another form's -------------------------

def test_a_repaired_submit_is_the_same_control_never_another_forms_with_its_words(
        _browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("talent_beside_application"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    submits = [a for a in r.actions if a.kind == "click" and a.text == "Submit"]
    assert len(submits) == 2 and all(a.in_gate for a in submits)
    assert any(d["what"] == "repair" for d in _decisions(r))


def test_a_control_gone_after_a_repair_is_never_replaced_by_a_look_alike(browser_page):
    from unittest.mock import Mock
    browser_page.set_content("""<body>
      <form id="talent"><label>Your email <input type="email" name="t"></label>
        <button type="submit">Submit</button></form>
      <form id="application"><label>First name <input name="f"></label>
        <label>Last name <input name="l"></label>
        <button type="submit" id="app">Submit</button></form></body>""")
    run = apply_run._JobRun(apply_run.Runner(jev=jev.FakeJev(), context=Mock(), run_context={},
                                             sleep=lambda s: None), Mock(),
                            {"job_posting_id": "s", "apply_url": "https://x.example/1"})
    run.page = browser_page
    d = apply_form.extract(browser_page)
    app = next(b.n for b in d.buttons if b.locator[1] == "#app")
    who = run._button_identity(d, app)
    assert run._same_button(d, who) == app
    # the application's Submit re-rendered: found again by its identity
    browser_page.evaluate("""() => { const b = document.getElementById('app');
      b.replaceWith(b.cloneNode(true)); }""")
    assert run._same_button(apply_form.extract(browser_page), who) == app
    # gone: the talent box's Submit never takes its place
    browser_page.evaluate("document.getElementById('app').remove()")
    assert run._same_button(apply_form.extract(browser_page), who) is None
    with pytest.raises(apply_run._Parked, match="could not be found again"):
        raise run._button_lost(who)


# --- I3 (ADV-06): a quiet click that set a request going is waited for, never made again -----------

_SLOW_STEP = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>Analytics Engineer</h1><form id="f" novalidate>
<div id="s1"><label>Full name * <input name="name" required></label>
<button type="button" id="next">Save and continue</button></div>
<div id="s2" hidden><label>Resume * <input type="file" name="resume" required></label></div>
</form><script>
  let saves = 0;
  document.getElementById('next').addEventListener('click', () => {
    fetch('/submit/__NAME__', {method: 'POST', body: 'x'}).then(() => {
      saves++; document.body.dataset.saves = String(saves);
      document.getElementById('s1').hidden = true; document.getElementById('s2').hidden = false;
    });
  });
</script></body></html>"""


@pytest.mark.parametrize("delay, name", [(5.0, "sp6_slow_step"), (0.0, "sp6_quick_step")])
def test_a_slow_step_posts_once_and_is_waited_for(_browser, flow_server, delay, name):
    flow_server.answers[name] = (delay, "ok")
    context = _browser.new_context()
    try:
        h.offline(context)
        page = context.new_page()
        base = flow_server.base
        context.route(f"{base}/forms/{name}.html", lambda route: route.fulfill(
            body=_SLOW_STEP.replace("__NAME__", name), content_type="text/html"))
        page.goto(f"{base}/forms/{name}.html")
        run = apply_run._JobRun(apply_run.Runner(jev=jev.FakeJev(), context=context,
                                                 run_context={}, sleep=lambda s: None),
                                context, {"job_posting_id": "s", "apply_url": page.url})
        run._build_allowlist()
        run.page = page
        digest = apply_form.extract(page)
        n = next(b.n for b in digest.buttons if b.text == "Save and continue")
        with h.fast_timing():
            result = run._click(digest, n, "advance", {"clicked": []}, conf=0.9)
        assert result.changed
        assert flow_server.posts.get(name) == 1
        assert page.evaluate("document.body.dataset.saves") == "1"
    finally:
        flow_server.answers.pop(name, None)
        context.close()


def test_a_step_that_posted_and_never_moved_parks_without_a_second_click(_browser, flow_server,
                                                                        monkeypatch):
    name = "sp6_dead_step"
    flow_server.answers[name] = (0.0, "ok")
    context = _browser.new_context()
    try:
        h.offline(context)
        page = context.new_page()
        base = flow_server.base
        body = _SLOW_STEP.replace("__NAME__", name).replace(
            "saves++; document.body.dataset.saves = String(saves);\n      "
            "document.getElementById('s1').hidden = true; "
            "document.getElementById('s2').hidden = false;", "saves++;")
        context.route(f"{base}/forms/{name}.html",
                      lambda route: route.fulfill(body=body, content_type="text/html"))
        page.goto(f"{base}/forms/{name}.html")
        run = apply_run._JobRun(apply_run.Runner(jev=jev.FakeJev(), context=context,
                                                 run_context={}, sleep=lambda s: None),
                                context, {"job_posting_id": "s", "apply_url": page.url})
        run._build_allowlist()
        run.page = page
        digest = apply_form.extract(page)
        n = next(b.n for b in digest.buttons if b.text == "Save and continue")
        monkeypatch.setattr(apply_run, "STEP_SETTLE_S", 1)
        with h.fast_timing(), pytest.raises(apply_run._Parked, match="it was not clicked again"):
            run._click(digest, n, "advance", {"clicked": []}, conf=0.9)
        assert flow_server.posts.get(name) == 1
    finally:
        flow_server.answers.pop(name, None)
        context.close()


# --- I4: a submit disabled until the CAPTCHA tick goes the gate's CAPTCHA path -----------------------

@pytest.mark.parametrize("name", ["recaptcha_disabled_submit", "recaptcha_disabled_submit_park"])
def test_a_submit_disabled_until_the_captcha_tick_goes_the_gates_captcha_path(
        _browser, flow_server, tmp_path, name):
    r = h.run_flow(h.flow(name), jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.policy is True
    assert any(d["what"] == "disabled_captcha" for d in _decisions(r))
