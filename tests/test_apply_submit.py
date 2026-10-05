"""SP3: submit truth and the gate's invariants.

- TERM-01: after the submit click the run reads what happened before it
  decides: a confirmation (received words new since the click, or the
  judge's read on a page with no form field), validation errors (nothing
  sent: `needs_human`, `submit_clicked` reset), a challenge the submit raised
  (the person solves it, then the page is read again), an emailed-code
  screen (a refused code parks), an error banner; "submitted (unconfirmed)"
  only when a request was seen leaving (`SendWatch`); a slow confirmation is
  waited for.
- TERM-02: a click that dispatched and then timed out waiting for its
  navigation stays clicked (no "did not register", no second send).
- TERM-03: only a confirmation closes the tab.
- INV-01..06: an Apply entry or "Apply Manually" never goes through the
  gate; an Apply-worded button is the submit only with the DOM evidence and
  the judge's `button_{n}_sends`; the gate reads the form's validity, the
  controls the extractor leaves out and an unticked CAPTCHA checkbox; every
  click reads the live control first; the code step refuses a final button
  before any submit and a submit-role click marks the job clicked.
- Study G11 and G4: DataDome and PerimeterX are bot checks; a reCAPTCHA or
  hCaptcha checkbox counts whatever its height; the extractor never hints
  `submit` for "Apply with LinkedIn", "Cancel" and the like.
- NoisyJev reads a form or a review as a confirmation now and then; a
  confirmation read before any submit never ends a job `submitted`.

Headless Chromium through the module-scoped test browser; the fixtures are
served by the flow server, fake hosts and the bot-check providers' URLs are
routed to local pages; the judge is `FakeJev`, `NoisyJev` or a scripted
subclass. No network."""
import dataclasses
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_click  # noqa: E402
import apply_fill  # noqa: E402
import apply_form  # noqa: E402
import apply_form_js  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_judge  # noqa: E402
import apply_queue  # noqa: E402
import apply_run  # noqa: E402
import apply_limits  # noqa: E402
import apply_send_words  # noqa: E402
import jev  # noqa: E402
import jev_doubles  # noqa: E402
from apply_judge import FillPlan  # noqa: E402

pytest_plugins = ["conftest_browser"]

CAREERS = "https://careers.fabrikam.example"
APPLY_URL = f"{CAREERS}/apply/42"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path):
    with h.hermetic(tmp_path), h.fast_timing():
        yield


@pytest.fixture
def context(_browser):
    ctx = _browser.new_context()
    h.offline(ctx)          # the tests' own routes, added later, answer first
    try:
        yield ctx
    finally:
        ctx.close()


def _enqueue(folder, url, jid="42"):
    apply_queue.enqueue(apply_queue.new_entry(jid, company="Fabrikam", title="Analytics Engineer",
                                              apply_url=url))
    apply_queue.set_artifacts(jid, {"folder": str(folder), "apply_md": str(folder / "apply.md"),
                                    "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")})


def _runner(context, tmp_path, judge=None, sleep=None, **settings):
    base = {"auto_apply_submit": True, "auto_apply_headless": True,
            "auto_apply_jev_mode": "fake", "auto_apply_batch_cap": 10,
            "auto_apply_generate": True}
    base.update(settings)
    return apply_run.Runner(jev=judge or jev.FakeJev(), profile_dir=tmp_path / "profile",
                            settings=base, context=context,
                            run_context={"signup_email": h.SIGNUP_EMAIL,
                                         "inbox_url": "https://mail.example.com/inbox"},
                            sleep=sleep or (lambda s: None))


def _drain(context, tmp_path, url, judge=None, sleep=None, runner_hook=None, **settings):
    """One job at `url` through the runner with the harness's recorder on:
    (the outcome, the recorder, the runner)."""
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, url)
    rec = h.Recorder(None, park_mode=not settings.get("auto_apply_submit", True))
    runner = _runner(context, tmp_path, judge, sleep, **settings)
    if runner_hook is not None:
        runner_hook(runner)
    with rec.recording():
        out = runner.drain(cap=1)[0]
    return out, rec, runner


def _flow(name, _browser, flow_server, tmp_path, judge=None, judge_name="fake"):
    return h.run_flow(h.flow(name), judge or jev.FakeJev(), judge_name, browser=_browser,
                      server=flow_server, workdir=tmp_path)


class _Posts:
    """A routed fake host: `pages` (path -> html) for GETs, and every POST to
    it counted and answered with `answer` (after nothing: the route answers
    at once)."""

    def __init__(self, context, pages: dict, answer: str = "", host: str = CAREERS):
        self.count = 0
        self.pages = pages
        self.answer = answer
        context.route(f"{host}/**", self._handle)
        self.host = host

    def _handle(self, route):
        request = route.request
        path = "/" + request.url.split(self.host + "/", 1)[1].split("?")[0]
        if request.method == "POST":
            self.count += 1
            route.fulfill(body=self.answer or "{}", content_type="application/json"
                          if not self.answer.startswith("<") else "text/html")
            return
        route.fulfill(body=self.pages.get(path, "<body>gone</body>"), content_type="text/html")


def _quiet_click(monkeypatch) -> None:
    """Short waits for a submit click that changes nothing on the page."""
    monkeypatch.setattr(apply_limits, "CLICK_TIMEOUT_S", 1)
    monkeypatch.setattr(apply_limits, "SUBMIT_SETTLE_S", 1)


def _form(button: str, script: str, extra: str = "") -> str:
    """A one-page application on the fake host: three facts the sheet
    answers, the button, and the button's script."""
    return f"""<!doctype html><html><head><title>Apply</title></head><body>
<h1>Analytics Engineer</h1>
<div id="app">
<label for="first_name">First name *</label><input id="first_name" name="first_name" required>
<label for="last_name">Last name *</label><input id="last_name" name="last_name" required>
<label for="email">Email *</label><input id="email" name="email" type="email" required>
{extra}
<button type="button" id="go">{button}</button>
</div>
<script>{script}</script></body></html>"""


# === TERM-01: what the submit did ================================================================

def test_the_server_answering_with_the_same_form_marked_invalid_sent_nothing(
        _browser, flow_server, tmp_path):
    before = flow_server.posts.get("server_validation", 0)
    r = _flow("server_validation", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    assert r.status == "needs_human"
    assert "Email (Enter an email address we can reach.)" in r.reason, r.reason
    assert flow_server.posts["server_validation"] - before == 1      # one post, never a second


def test_validation_after_the_click_is_read_when_the_gate_could_not_see_it(
        context, flow_server, tmp_path, monkeypatch):
    # the gate's own read of the form off: the post-submit read still finds
    # the hidden required box the browser refused to send without
    _quiet_click(monkeypatch)
    monkeypatch.setattr(apply_run._JobRun, "_gate_read", lambda self, digest, plan: {})
    out, rec, _ = _drain(context, tmp_path, flow_server.url("native_required_submit.html"))
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.NOT_SENT_REASON + ": validation errors"), out.reason
    assert "Cover letter" in out.reason
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("body[data-submitted]").count() == 0


def test_a_challenge_the_submit_raised_parks_headless(_browser, flow_server, tmp_path):
    r = _flow("submit_then_challenge", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    assert not any(a.kind == "click" and apply_run._is_captcha_url(a.url) for a in r.actions)


def test_a_challenge_the_submit_raised_waits_for_the_person_then_reads_the_page(
        context, flow_server, tmp_path):
    context.route("https://hcaptcha.com/**", lambda route: route.fulfill(
        body="<body><p>Select every bus</p></body>", content_type="text/html"))
    solved = []

    def _sleep(seconds):
        # the person solves the challenge while the run waits
        for p in context.pages:
            if not p.is_closed():
                p.evaluate("window.__solve && window.__solve()")
        solved.append(seconds)
    out, rec, _ = _drain(context, tmp_path, flow_server.url("submit_then_challenge.html"),
                         sleep=_sleep, auto_apply_headless=False)
    assert (out.status, out.reason) == ("submitted", "confirmation page"), out
    assert solved, "the run did not wait for the person"
    assert [a.text for a in rec.actions if a.kind == "click"].count("Submit application") == 1


def test_a_slow_confirmation_after_a_sending_page_is_waited_for(context, flow_server, tmp_path):
    # the page shows "Sending..." at once (the click's own wait ends there)
    # and the confirmation 3 s later, when the server answers the fetch
    flow_server.answers["slow_fetch"] = (3.0, '{"ok": true}')
    url = flow_server.url("slow_fetch_page.html")
    context.route(url, lambda route: route.fulfill(content_type="text/html", body=_form(
        "Submit application", """
      document.getElementById('go').onclick = function () {
        document.getElementById('app').outerHTML = '<p id="wait">Sending your details...</p>';
        fetch('/submit/slow_fetch', {method: 'POST', body: '{}'}).then(function () {
          document.getElementById('wait').outerHTML = '<h2>Thanks for applying!</h2>';
        });
      };""")))
    try:
        out, _, _ = _drain(context, tmp_path, url)
    finally:
        flow_server.answers.pop("slow_fetch", None)
    assert (out.status, out.reason) == ("submitted", "confirmation page"), out


def test_a_confirmation_that_came_with_the_answer_during_a_look_is_read(
        context, flow_server, tmp_path, monkeypatch):
    # SP6 review R2-M2: the POST's answer lands while one of the wait's later
    # looks is under way (after its read of the page, before its exit test,
    # the "Sending..." page long quiet), as a loaded machine makes it; the
    # page is read once more before the ruling
    import time
    real = apply_run._JobRun._post_submit_validity
    held, looks = [], []

    def slow_look(self, as_before):
        looks.append(time.monotonic())
        watch = self._send_watch
        if not held and watch is not None and watch.pending and looks[-1] - looks[0] >= 0.6:
            held.append(True)
            t0 = time.monotonic()
            while watch is not None and watch.pending and time.monotonic() - t0 < 8:
                self.page.wait_for_timeout(50)
            self.page.wait_for_timeout(300)     # the page shows the answer
        return real(self, as_before)
    monkeypatch.setattr(apply_run._JobRun, "_post_submit_validity", slow_look)
    flow_server.answers["slow_fetch_look"] = (2.0, '{"ok": true}')
    url = flow_server.url("slow_fetch_look_page.html")
    context.route(url, lambda route: route.fulfill(content_type="text/html", body=_form(
        "Submit application", """
      document.getElementById('go').onclick = function () {
        document.getElementById('app').outerHTML = '<p id="wait">Sending your details...</p>';
        fetch('/submit/slow_fetch_look', {method: 'POST', body: '{}'}).then(function () {
          document.getElementById('wait').outerHTML = '<h2>Thanks for applying!</h2>';
        });
      };""")))
    try:
        out, _, _ = _drain(context, tmp_path, url)
    finally:
        flow_server.answers.pop("slow_fetch_look", None)
    assert held, "the wait never looked at the page"
    assert (out.status, out.reason) == ("submitted", "confirmation page"), out


def test_a_crash_after_the_submit_click_with_nothing_seen_leaving_claims_no_send(
        context, tmp_path, monkeypatch):
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () { document.body.dataset.x = 1; };""")})

    def _boom(self, **kw):
        raise RuntimeError("the page went away")
    monkeypatch.setattr(apply_run._JobRun, "_after_submit", _boom)
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.CHECK_SENT_REASON + ": the run stopped after the "
                                                               "submit click"), out.reason


@pytest.mark.parametrize("what", ["window", "tab"])
def test_a_park_after_the_submit_click_whose_window_or_tab_closed_asks_the_person_to_check(
        _browser, tmp_path, monkeypatch, what):
    # final review A-I1 and A Known Minor 11: the park after the submit click
    # is reached as the window or the job's tab goes away. The job keeps the
    # check-sent end and its note, never the bare closed end whose note is a
    # Re-queue; a closed window still stops the drain
    ctx = _browser.new_context()
    h.offline(ctx)
    _Posts(ctx, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'});
      };""")})

    def _gone(self, watch, before, account, handoff):
        (self.ctx if what == "window" else self.page).close()
        raise apply_run._Parked("needs_human", "the site showed an error after the submit click "
                                               "(Something went wrong)")
    monkeypatch.setattr(apply_run._JobRun, "_read_after_submit", _gone)
    try:
        out, _, _ = _drain(ctx, tmp_path, APPLY_URL)
    finally:
        try:
            ctx.close()
        except Exception:       # noqa: BLE001  (closed by the test)
            pass
    closed = apply_run.CLOSED_REASON if what == "window" else apply_run.TAB_CLOSED_REASON
    assert out.status == "needs_human", out
    assert out.reason.startswith(f"{apply_run.CHECK_SENT_REASON}: the run stopped after the "
                                 f"submit click ({closed}); the run had reached: the site showed "
                                 f"an error after the submit click"), out.reason
    assert out.browser_closed is (what == "window")
    entry = apply_queue.load()["jobs"][-1]
    assert entry["tab_note"] == apply_run.CHECK_SENT_NOTE, entry


def test_submitted_unconfirmed_only_with_a_request_seen_leaving(context, tmp_path):
    # the submit posts by fetch and the page then shows a neutral panel
    posts = _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'}).then(function () {
          document.getElementById('app').outerHTML = '<p id="done">Your profile</p>';
        });
      };""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert posts.count == 1
    assert out.status == "submitted", out
    assert out.reason.startswith("submitted (unconfirmed): a request left after the submit "
                                 "click (POST https://careers.fabrikam.example/api/"
                                 "applications)"), out.reason
    # TERM-03: an unconfirmed send keeps its tab open for the person
    assert any(not p.is_closed() for p in context.pages)


def test_nothing_sent_and_the_same_form_is_a_submit_that_did_not_go_through(
        context, tmp_path, monkeypatch):
    _quiet_click(monkeypatch)
    _Posts(context, {"/apply/42": _form("Submit application", "")})
    out, _, runner = _drain(context, tmp_path, APPLY_URL)
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.NOT_SENT_REASON + " (the form did not change after "
                                                             "the click and no request left"), out


def test_nothing_sent_and_a_changed_page_asks_the_person_to_check(context, tmp_path):
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        document.getElementById('app').outerHTML = '<p>Your profile is up to date.</p>';
      };""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.CHECK_SENT_REASON + ": the page changed after the "
                                                               "submit click"), out.reason
    assert "no request was seen leaving" in out.reason


def test_a_request_and_the_form_again_asks_the_person_to_check(context, tmp_path, monkeypatch):
    _quiet_click(monkeypatch)
    posts = _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'});
      };""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert posts.count == 1
    assert out.status == "needs_human", out
    assert "and the page reads as the form again" in out.reason, out.reason
    assert apply_queue.load()["jobs"][-1]["status"] == "needs_human"     # never re-queued


def test_an_error_banner_after_the_submit_parks_with_its_text(context, tmp_path):
    posts = _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'}).then(function () {
          document.body.insertAdjacentHTML('afterbegin',
            '<div role="alert">Something went wrong on our side. Try again later.</div>');
        });
      };""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert posts.count == 1
    assert out.status == "needs_human", out
    assert out.reason.startswith("the site showed an error after the submit click (Something "
                                 "went wrong on our side"), out.reason


_TYPED = ("jane.doe", "example.com", "555", "0199", "Jane Q")


def test_an_error_banner_that_quotes_the_persons_data_parks_without_it(context, tmp_path):
    # final review A-M2: a reason reaches the queue row and the drain's report
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'}).then(function () {
          document.body.insertAdjacentHTML('afterbegin', '<div role="alert">' +
            'jane.doe@example.com is already registered to &quot;Jane Q&quot;; ' +
            'call +1 (555) 010-0199 to fix it.</div>');
        });
      };""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert out.reason.startswith("the site showed an error after the submit click ([email] is "
                                 "already registered to [quoted]; call [number] to fix it"), \
        out.reason
    assert not [t for t in _TYPED if t in out.reason], out.reason


def test_a_reasons_form_and_page_words_leave_out_what_they_quote():
    # final review A-M2: Chrome's message for a type=email box quotes the value
    native = {"label": "Email", "reason": "typeMismatch", "message":
              "Please include an '@' in the email address. 'jane.doe' is missing an '@'."}
    assert apply_run._invalid_words(native) == (
        "the form reports an invalid field: Email (not the kind of value the box takes)")
    assert apply_run._invalid_words({**native, "reason": "valueMissing"}) == (
        "required field without an answer: Email (the form reports it empty)")
    site = {"label": "Phone", "reason": "aria-invalid",
            "message": 'The number "+1 (555) 010-0199" is not valid for Jane Q'}
    assert apply_run._invalid_words(site) == (
        "the form reports an invalid field: Phone (The number [quoted] is not valid for Jane Q)")
    assert apply_run._invalid_words({**site, "message": ""}) == (
        "the form reports an invalid field: Phone (marked invalid by the site)")
    refused = apply_run._JobRun._refused_words(None, [
        {"kind": "error", "text": "jane.doe@example.com is already in use"},
        {"kind": "invalid", **native}])
    assert refused == ("the form says: [email] is already in use; the form reports an invalid "
                       "field: Email (not the kind of value the box takes)")
    digest = apply_form.FormDigest(url_host="x", title="t", fields=[], buttons=[], text=(
        "Create your account\n'jane.doe@example.com' is already registered. Call 555-010-0199."))
    assert apply_run.page_problem(digest) == "[quoted] is already registered. Call [number]."
    # a step, a year or a count stays: fewer than seven digits
    assert apply_run._page_words("Step 2 of 4: 2024 hires, 100000 per year", 80) == (
        "Step 2 of 4: 2024 hires, 100000 per year")
    ns = SimpleNamespace(submit_clicked=True, _submit_repairs=1, _spared_park=lambda t: None,
                         _decide=lambda *a, **k: None)
    watch = SimpleNamespace(any=lambda: True, first=lambda: "POST https://x/apply")
    with pytest.raises(apply_run._Parked) as e:
        apply_run._JobRun._not_sent(ns, [native], [
            {"text": "We could not reach jane.doe@example.com", "field": True}], watch)
    assert "jane.doe" not in e.value.reason and "[email]" in e.value.reason, e.value.reason
    assert not ns.submit_clicked


def test_a_quoted_field_name_in_the_forms_words_stays():
    # final review A R2-M7: a message that quotes the field's own label keeps
    # it (a label is the form's words, never what the person typed); any
    # other quote is still left out
    labels = ["Start date *", "Email:"]
    assert apply_run._page_words("Please complete the 'Start date' field", 100, labels) == (
        "Please complete the 'Start date' field")
    assert apply_run._page_words('"email" must be filled in; "2026-10-01" is too early', 100,
                                 labels) == '"email" must be filled in; [quoted] is too early'
    assert apply_run._page_words("Please complete the 'Start date' field", 100) == (
        "Please complete the [quoted] field")
    refused = apply_run._JobRun._refused_words(
        None, [{"kind": "error", "text": "'Start date' is required"}], labels)
    assert refused == "the form says: 'Start date' is required"
    ns = SimpleNamespace(submit_clicked=True, _submit_repairs=1, _spared_park=lambda t: None,
                         _decide=lambda *a, **k: None)
    watch = SimpleNamespace(any=lambda: True, first=lambda: "POST https://x/apply")
    with pytest.raises(apply_run._Parked) as e:
        apply_run._JobRun._not_sent(ns, [], [{"text": "Please complete the 'Start date' field",
                                              "field": True}], watch, labels)
    assert "'Start date'" in e.value.reason, e.value.reason


def test_received_words_new_since_the_click_are_the_confirmation(context, tmp_path):
    class _Other(jev.FakeJev):
        """The fake, reading every page after the first as `other`."""

        def judge(self, state, questions):
            out = super().judge(state, questions)
            if "page_state" in out and not state.get("fields"):
                out["page_state"] = jev.Answer(kind="choice", choice="other", confidence=0.9,
                                               probabilities={"other": 0.9})
            return out
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        document.getElementById('app').outerHTML =
          '<h2>Thanks for applying!</h2><p>We will be in touch.</p>';
      };""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL, judge=_Other())
    assert (out.status, out.reason) == ("submitted", "confirmation page"), out
    assert not any(not p.is_closed() for p in context.pages)       # a confirmation closes it


def test_received_words_already_on_the_form_are_no_confirmation():
    before = "Thanks for applying to Fabrikam! Fill in the form below."
    assert apply_run.new_confirmation(before, before + " First name") == ""
    assert apply_run.new_confirmation(before, "Your application has been received.") == \
        "application has been received"
    assert apply_run.new_confirmation("", "Thanks for your interest in the role") == ""


def test_a_refused_emailed_code_parks_and_never_reads_as_submitted(context, tmp_path):
    code_page = """<!doctype html><html><body><h1>Verify your email</h1>
      <p id="msg">Enter the security code we emailed you.</p>
      <label for="code">Security code</label><input id="code" name="code">
      <button type="button" id="verify" onclick="document.getElementById('msg').textContent =
        'That code is not valid. Enter the security code we emailed you.'">Verify and
        continue</button>
      </body></html>"""
    _Posts(context, {"/apply/42": _form("Submit application",
                                        "document.getElementById('go').onclick = function () {"
                                        " location.href = '/apply/42/verify'; };"),
                     "/apply/42/verify": code_page})

    class _Inbox:
        def fetch_code(self, page, site, inbox_url):
            return "SYNTH123"

    def _hook(runner):
        runner.inbox = _Inbox()
    out, _, _ = _drain(context, tmp_path, APPLY_URL, runner_hook=_hook)
    assert out.status == "needs_human", out
    assert out.reason.startswith("the emailed code was not accepted"), out.reason


# === TERM-02: a dispatched click that timed out stays clicked ====================================

def test_a_dispatched_click_whose_navigation_timed_out_has_landed(browser_page, flow_server,
                                                                  monkeypatch):
    # a 1 s action timeout and a 2.5 s answer: the click still times out
    # after its dispatch, with the post in flight
    monkeypatch.setattr(apply_click, "ACTION_TIMEOUT_MS", 1_000)
    monkeypatch.setitem(flow_server.answers, "slow_post", (2.5, None))
    browser_page.goto(flow_server.url("slow_post.html"))
    for sel, value in (("#first_name", "Jane"), ("#last_name", "Doe"),
                       ("#email", "jane@example.com")):
        browser_page.fill(sel, value)
    browser_page.set_input_files("#resume", str(REPO / "tests" / "fixtures" / "forms" /
                                                "slow_post.html"))
    d = apply_form.extract(browser_page)
    n = next(b.n for b in d.buttons if b.text == "Submit application")
    r = apply_fill.click(browser_page, d, n, timeout_s=1)
    assert r.clicked and r.late == "TimeoutError", r
    # the slow_post flow (tests/test_apply_matrix.py, every seed) runs the
    # whole job: one post, the confirmation read SLOW_POST_S after the click


def test_a_send_then_ready_to_submit_breaks_the_invariants():
    # the matrix's invariant: a send, then "ready_to_submit", breaks it
    rec = h.Recorder(h.flow("slow_post"))
    sends = h.Sends(rec)
    sends.events.append(h.Send("post", "/submit/slow_post", True))
    out = SimpleNamespace(status="ready_to_submit", reason="submit did not register")
    assert "READY-AFTER-SEND" in {b.split(":")[0] for b in h.invariant_breaks(out, rec, sends)}


# === INV-01: an Apply entry is never the submit ==================================================

def test_a_job_alert_box_beside_the_posting_leaves_its_apply_the_entry(
        _browser, flow_server, tmp_path):
    r = _flow("posting_with_alert_box", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    clicks = [(a.text, a.in_gate) for a in r.actions if a.kind == "click"]
    assert ("Apply now", False) in clicks and ("Submit application", True) in clicks, clicks


def test_workdays_apply_manually_opens_the_form_and_never_reaches_the_gate(
        _browser, flow_server, tmp_path):
    r = _flow("workday_start_modal", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    clicks = [(a.text, a.in_gate) for a in r.actions if a.kind == "click"]
    assert ("Apply Manually", False) in clicks, clicks
    assert not any(t == "Use My Last Application" for t, _ in clicks), clicks


class _SendsNo(jev.FakeJev):
    """The fake, saying no Apply-worded button sends the finished
    application."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        for qid in out:
            if qid.endswith("_sends"):
                out[qid] = jev.Answer(kind="noul", noul=0.10)
        return out


def test_an_apply_button_is_the_submit_only_with_the_judges_word(
        context, flow_server, tmp_path):
    out, rec, _ = _drain(context, tmp_path, flow_server.url("apply_now_form.html"),
                         judge=_SendsNo())
    assert out.status == "needs_human", out
    assert "the Apply button (Apply now) may open or start an application" in out.reason, out
    assert "0.10" in out.reason
    page = next(p for p in context.pages if not p.is_closed())
    assert page.evaluate("window.__clicks") == 0


def test_an_apply_button_in_the_filled_form_with_the_judges_word_sends(
        context, flow_server, tmp_path):
    out, rec, _ = _drain(context, tmp_path, flow_server.url("apply_now_form.html"))
    assert (out.status, out.reason) == ("submitted", "confirmation page"), out
    assert [a.in_gate for a in rec.actions if a.kind == "click" and a.text == "Apply now"] == [True]


def test_the_page_request_asks_whether_an_apply_button_sends(tmp_path):
    import apply_facts
    folder = h.write_job_folder(tmp_path / "job")
    catalog = apply_facts.build(folder)
    digest = apply_form.FormDigest("jobs.example", "Apply", "", buttons=[
        apply_form.Button(0, (0, "#a"), "Apply now"), apply_form.Button(1, (0, "#b"), "Submit"),
        apply_form.Button(2, (0, "#c"), "Submit application"),
        apply_form.Button(3, (0, "#d"), "Apply Manually")])
    _, q = apply_judge.page_questions(digest, catalog, {})
    assert {k for k in q if k.endswith("_sends")} == {"button_0_sends", "button_3_sends"}
    assert q["button_0_sends"]["type"] == "noul"
    assert "`buttons[0]`" in q["button_0_sends"]["instructions"]
    assert apply_judge.BUTTON_SENDS_MIN == 0.40


@pytest.mark.parametrize("html, verdict", [
    ('<form><input id="f"><button id="b">Apply</button></form>', "same"),
    ('<form><input id="f"></form><form><button id="b">Apply</button></form>', "apart"),
    ('<h1>Job</h1><div id="app"><input id="f"><button id="b">Apply</button></div>', "same"),
    ('<h1>Job</h1><a id="b" href="#">Apply</a>'
     '<aside><form><input id="f"><button>Notify me</button></form></aside>', "apart"),
    # a form's own Apply laid out apart from it is never read as apart
    ('<h1>Job</h1><form><input id="f"></form><div><a id="b" href="#">Apply</a></div>',
     "unclear"),
    ('<h1>Job</h1><a id="b" href="#">Apply</a><aside><input id="f"></aside>', "unclear"),
    ('<main><div><h1>Job</h1><input id="f"><button id="b">Apply</button></div></main>',
     "unclear")])
def test_same_scope_reads_the_form_or_a_box_smaller_than_the_page(browser_page, html, verdict):
    browser_page.set_content(f"<body>{html}</body>")
    got, why = apply_form.same_scope(browser_page, (0, "#b"), [(0, "#f")])
    assert got == verdict, why


def test_a_form_step_with_nothing_filled_never_sends_through_the_gate(context, tmp_path):
    # a page with no field at all and a "Submit application" button: the
    # gate refuses (nothing of the application is on the page)
    page = """<!doctype html><html><body><h1>Analytics Engineer</h1>
      <p>Start your application below.</p>
      <button type="button" id="go" onclick="document.body.dataset.submitted = 1">Submit
      application</button></body></html>"""
    _Posts(context, {"/apply/42": page})

    class _Form(jev.FakeJev):
        def judge(self, state, questions):
            out = super().judge(state, questions)
            if "page_state" in out:
                out["page_state"] = jev.Answer(kind="choice", choice="application_form",
                                               confidence=0.9,
                                               probabilities={"application_form": 0.9})
            return out
    out, rec, _ = _drain(context, tmp_path, APPLY_URL, judge=_Form())
    assert out.status == "needs_human", out
    assert out.reason.startswith("no application on the page"), out.reason
    assert not [a for a in rec.actions if a.kind == "click"]


def test_an_apply_inside_a_form_of_controls_is_never_a_fieldless_entry(browser_page):
    # INV-03: the form's control is a lone spin box the extractor leaves out
    # (an editable box is a field since SP5); its Apply is the form's own button
    browser_page.set_content("""<body><h1>Role</h1><form>
      <div role="spinbutton" tabindex="0" aria-label="Years in the role">0</div>
      <button type="button">Apply</button></form></body>""")
    d = apply_form.extract(browser_page)
    assert d.fields == [] and [b.in_form for b in d.buttons] == [True]
    assert apply_run.fieldless_apply_choice(d) is None
    plan = FillPlan()
    apart, unclassified, scan = apply_run.posting_context(browser_page, d, plan)
    assert unclassified == 1 and scan[0]["kind"] == "spinbutton"
    assert apply_run.posting_entry_choice(d, plan, apart=apart, unclassified=unclassified) \
        == (None, "")


# === INV-02: the gate reads the page =============================================================

def test_the_gate_reads_the_forms_validity_before_the_click(_browser, flow_server, tmp_path):
    r = _flow("native_required_submit", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    assert "the form reports it empty" in r.reason, r.reason
    assert not [a for a in r.actions if a.kind == "click" and a.in_gate]


def test_the_gate_refuses_a_control_the_site_marked_invalid(context, tmp_path):
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('email').addEventListener('input', function (e) {
        e.target.setAttribute('aria-invalid', 'true'); });
      document.getElementById('go').onclick = function () {
        document.body.dataset.submitted = 1; };""")})
    out, rec, _ = _drain(context, tmp_path, APPLY_URL)
    assert out.status == "needs_human", out
    assert out.reason.startswith("the form reports an invalid field: Email"), out.reason
    assert not [a for a in rec.actions if a.kind == "click"]


# (an editable box and a box in an open shadow root are fields since SP5: the
# run fills them; a lone spin box is still left out)
@pytest.mark.parametrize("extra, label", [
    ('<div role="spinbutton" tabindex="0" aria-required="true" '
     'aria-label="Years in the role"></div>', "Years in the role"),
    ('<x-question></x-question><script>customElements.define("x-question", class extends '
     'HTMLElement { constructor() { super(); this.attachShadow({mode: "open"}).innerHTML = '
     '"<div role=\\"spinbutton\\" tabindex=\\"0\\" aria-required=\\"true\\" '
     'aria-label=\\"Start year\\"></div>"; } });</script>', "Start year")])
def test_the_gate_refuses_an_empty_required_control_the_extractor_leaves_out(
        context, tmp_path, extra, label):
    _Posts(context, {"/apply/42": _form("Submit application",
                                        "document.getElementById('go').onclick = function () {"
                                        " document.body.dataset.submitted = 1; };", extra)})
    out, rec, _ = _drain(context, tmp_path, APPLY_URL)
    assert out.status == "needs_human", out
    assert out.reason.startswith(f"required field without an answer: {label} (a "), out.reason
    assert not [a for a in rec.actions if a.kind == "click"]


@pytest.mark.parametrize("text, only", [
    ("Next", True), ("Save and continue", True), ("Sign in", True), ("Log in", True),
    ("Create account", True), ("Back", True), ("Continue →", True),
    # final review B R2 KM4: a review step is a step
    ("Continue to review", True), ("Review application", True), ("Preview", True),
    ("Review your application", True),
    ("Submit application", False), ("Save and submit", False), ("Review and submit", False),
    ("Complete", False), ("Create account and apply", False), ("Send", False), ("", False)])
def test_a_step_only_button_is_one_of_a_steps_words_alone(text, only):
    # final review B Known Minor 4
    assert apply_send_words.step_only(text) is only


class _StepAsSubmit(jev.FakeJev):
    """Reads `TEXT` ("Save and continue") as the submit at 0.9."""

    TEXT = "Save and continue"

    def __init__(self, text: str = TEXT):
        super().__init__()
        self.text = text

    def judge(self, state, questions):
        out = dict(super().judge(state, questions))
        texts = {b["n"]: b["text"] for b in (state or {}).get("buttons") or []}
        for qid in questions:
            m = re.fullmatch(r"button_(\d+)_role", qid)
            if m and texts.get(int(m[1])) == self.text:
                out[qid] = jev.Answer(kind="choice", choice="submit", confidence=0.9,
                                      probabilities={"submit": 0.9, "advance": 0.1})
        return out


@pytest.mark.parametrize("submit", [True, False], ids=["submit_mode", "park_mode"])
def test_a_step_button_read_as_the_submit_is_never_clicked_as_the_send(context, tmp_path, submit):
    # final review B Known Minor 4: in submit mode the gate refuses a submit
    # whose words are only a step's, and nothing is clicked; park mode parks
    # at the gate as before
    _Posts(context, {"/apply/42": _form("Save and continue",
                                        "document.getElementById('go').onclick = function () {"
                                        " document.body.dataset.submitted = 1; };")})
    out, rec, _ = _drain(context, tmp_path, APPLY_URL, judge=_StepAsSubmit(),
                         auto_apply_submit=submit)
    assert not [a for a in rec.actions if a.kind == "click"], rec.actions
    if submit:
        assert (out.status, out.reason) == (
            "needs_human", "the submit button (Save and continue) holds only a step's words; "
                           "it is never clicked as the send"), out
    else:
        assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out


@pytest.mark.parametrize("text, handed_off, password, step", [
    ("Continue", False, True, True), ("Next", True, False, True),
    ("Save and continue", False, True, True), ("Continue to review", True, False, True),
    ("Create account", False, True, False), ("Sign in", True, False, False),
    ("Register", False, True, False), ("Log in", True, False, False),
    ("Create account", False, False, True)],
    ids=["password_continue", "handed_off_next", "password_save", "handed_off_review",
         "password_create", "handed_off_sign_in", "password_register", "handed_off_log_in",
         "no_account_create"])
def test_an_account_screen_keeps_only_its_account_buttons_words_as_the_send(
        text, handed_off, password, step):
    # final review B R2 KM4: a page that types the master password (or one the
    # account step handed back) was exempt for every step word, "Next" and
    # "Continue" too; only the account's own button may be its send
    from apply_judge import PlannedField
    job, digest, _ = _code_job(text, "submit")
    job.handed_off = handed_off
    job.form_filled = True
    fields = [PlannedField(0, (0, "#pw"), "Password", True, None, "", None, 1.0,
                           apply_judge.PASSWORD_ACTION)] if password else []
    out = job._gate_read(digest, FillPlan(fields=fields, buttons={"submit": (0, 0.9)}))
    assert ("step_button" in out) is step, out


def test_can_submit_names_each_live_refusal():
    from apply_judge import PlannedField, VerifyResult
    plan = FillPlan(fields=[PlannedField(0, (0, "#f"), "First name", True, "first_name", "Jane",
                                         None, 1.0, "fill")], buttons={"submit": (1, 0.9)})
    ver = [VerifyResult(0, "First name", True, 0.95, 0.0)]
    on = {"auto_apply_submit": True}
    assert apply_run.can_submit(plan, ver, on, {}) == (True, "")
    assert apply_run.can_submit(plan, ver, on, {"no_application": "x"}) == (
        False, "no application on the page (x)")
    assert apply_run.can_submit(plan, ver, on, {"invalid": [
        {"label": "Phone", "message": "Match the format", "reason": "patternMismatch"}]}) == (
        False, "the form reports an invalid field: Phone (does not match the requested format)")
    assert apply_run.can_submit(plan, ver, on, {"required_empty": [
        {"label": "Start date", "kind": "input"}]})[1].startswith(
        "required field without an answer: Start date")


# --- the CAPTCHA checkbox (study G11) --------------------------------------------------------------

_ANCHOR = ('<iframe style="width:304px;height:78px" src="https://www.google.com/recaptcha/api2/'
           'anchor?k=x&size={size}"></iframe>')


@pytest.mark.parametrize("size, token, showing", [
    ("normal", "", True), ("normal", "synthetic-token", False), ("invisible", "", False)])
def test_a_checkbox_counts_whatever_its_height_until_its_token_is_set(
        browser_page, size, token, showing):
    browser_page.set_content(f"<body>{_ANCHOR.format(size=size)}<textarea "
                             f"name='g-recaptcha-response'>{token}</textarea></body>")
    assert bool(apply_form.unsolved_checkbox(browser_page)) is showing
    run = apply_run._JobRun.__new__(apply_run._JobRun)
    run.page = browser_page
    assert run._human_check_showing(checkbox=True) is showing
    # the checkbox counts only where it blocks a send (the gate, the
    # account step's click); the loop's own check reads a challenge alone
    assert run._human_check_showing() is False


def test_the_recaptcha_checkbox_flow_waits_for_the_person(_browser, flow_server, tmp_path):
    r = _flow("recaptcha_checkbox", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    assert not [a for a in r.actions if a.kind == "click" and a.in_gate]
    # I3: the form is filled first; the checkbox stops only the send
    assert len([a for a in r.actions if a.kind == "fill"]) >= 3, r.actions


def test_park_mode_fills_the_form_and_leaves_the_checkbox_to_the_person(
        _browser, flow_server, tmp_path):
    r = _flow("recaptcha_checkbox_park", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    assert r.reason == "auto_apply_submit is off; " + apply_run.CHECKBOX_NOTE
    assert len([a for a in r.actions if a.kind == "fill"]) >= 3, r.actions


def _late_widget_page() -> str:
    """recaptcha_checkbox.html with its widget rendered on the first input,
    after the run read the page: only the check before the gate sees it."""
    html = (REPO / "tests" / "fixtures" / "forms" / "recaptcha_checkbox.html").read_text(
        encoding="utf-8")
    widget = html.split('<div class="g-recaptcha" id="widget">', 1)[1].split("</div>", 1)[0]
    html = html.replace(widget, "")
    return html.replace("</script>", """
  document.getElementById('first_name').addEventListener('input', function () {
    var w = document.getElementById('widget');
    if (!w.children.length) w.innerHTML = %s;
  }, {once: true});
</script>""" % repr(widget.strip()).replace("\\n", " "), 1)


def test_a_checkbox_that_shows_after_the_read_is_checked_again_before_the_gate(
        context, tmp_path):
    context.route("https://www.google.com/recaptcha/**", lambda route: route.fulfill(
        body="<body>I'm not a robot</body>", content_type="text/html"))
    _Posts(context, {"/apply/42": _late_widget_page()})
    out, rec, _ = _drain(context, tmp_path, APPLY_URL)
    assert (out.status, out.reason) == ("needs_human", "a CAPTCHA check is on the form before "
                                                       "the submit"), out
    assert not [a for a in rec.actions if a.kind == "click"]


def test_in_the_window_the_run_waits_for_the_tick_then_submits(context, tmp_path):
    context.route("https://www.google.com/recaptcha/**", lambda route: route.fulfill(
        body="<body>I'm not a robot</body>", content_type="text/html"))
    _Posts(context, {"/apply/42": _late_widget_page()})
    ticks = []

    def _sleep(seconds):
        for p in context.pages:
            if not p.is_closed():
                p.evaluate("window.__tick && window.__tick()")
        ticks.append(seconds)
    out, rec, _ = _drain(context, tmp_path, APPLY_URL, sleep=_sleep, auto_apply_headless=False)
    assert (out.status, out.reason) == ("submitted", "confirmation page"), out
    assert ticks
    assert not any(apply_run._is_captcha_url(a.url) for a in rec.actions)


@pytest.mark.parametrize("url", [
    "https://geo.captcha-delivery.com/captcha/?initialCid=x", "https://client.perimeterx.net/x",
    "https://captcha.px-cloud.net/x/captcha.js", "https://collector-x.px-cdn.net/api"])
def test_datadome_and_perimeterx_are_bot_checks(url):
    assert apply_run._is_captcha_url(url)


# === INV-04: every click reads the live control ==================================================

def test_a_footer_button_that_turns_into_submit_is_never_clicked_as_an_advance(
        context, flow_server, tmp_path, monkeypatch):
    # the chaos case: 300 ms after the page is read, a script swaps the
    # wizard's Continue into "Submit"
    real_extract = apply_form.extract
    scheduled = []

    def _extract(page, **kw):
        digest = real_extract(page, **kw)
        if not scheduled and "Continue" in [b.text for b in digest.buttons]:
            scheduled.append(__import__("time").monotonic())
            page.evaluate("setTimeout(function () { document.querySelector("
                          "'button[data-next=\"2\"]').textContent = 'Submit'; }, 300)")
        return digest
    monkeypatch.setattr(apply_form, "extract", _extract)
    real_buttons = apply_run._JobRun._form_buttons

    def _buttons(self, *a, **kw):
        self.page.wait_for_timeout(400)
        return real_buttons(self, *a, **kw)
    monkeypatch.setattr(apply_run._JobRun, "_form_buttons", _buttons)
    out, rec, _ = _drain(context, tmp_path, flow_server.url("ashby_steps.html"))
    assert out.status == "needs_human", out
    assert out.reason.startswith("the advance button (Continue) changed before the click: it "
                                 "now reads 'Submit'"), out.reason
    h.assert_invariants(out, rec, h.Sends(rec))
    assert not [a for a in rec.actions if a.kind == "click"]


def test_a_control_whose_locator_now_names_another_is_found_again_by_its_text(browser_page):
    browser_page.set_content("""<body><div id="bar"><button onclick="document.body.dataset.back
      = 1">Back</button><button onclick="document.body.dataset.next = 1">Continue</button></div>
      </body>""")
    d = apply_form.extract(browser_page)
    n = next(b.n for b in d.buttons if b.text == "Continue")
    assert "nth-of-type(2)" in next(b for b in d.buttons if b.n == n).locator[1]
    browser_page.evaluate("document.getElementById('bar').insertAdjacentHTML('afterbegin', "
                          "'<button>Save draft</button>')")
    check = apply_run._JobRun._live_check(Mock(), "advance")
    r = apply_fill.click(browser_page, d, n, timeout_s=1, check=check)
    assert r.clicked and not r.refused, r
    assert browser_page.evaluate("[document.body.dataset.next, document.body.dataset.back]") == \
        ["1", None]


def test_an_entry_click_reads_the_live_control_first(context, tmp_path):
    # the posting's Apply turned into the form's send between the read and
    # the click: the entry click is refused, nothing is clicked
    _Posts(context, {"/apply/42": "<body><h1>Role</h1><a id='go' href='#' onclick=\""
                                  "document.body.dataset.submitted = 1\">Submit application</a>"
                                  "</body>"})
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, APPLY_URL)
    runner = _runner(context, tmp_path)
    run = apply_run._JobRun(runner, context, apply_queue.load()["jobs"][-1])
    run._prepare()
    run.page = context.new_page()
    run.page.goto(APPLY_URL)
    rec = {"clicked": []}
    with pytest.raises(apply_run._Parked, match="changed before the click"):
        run._click_entry(rec, run.page.locator("#go"), "Apply now", how="judged_apply_entry")
    assert run.page.locator("body[data-submitted]").count() == 0 and rec["clicked"] == []


def test_an_entry_whose_live_text_reads_as_a_send_is_refused():
    live = {"text": "Submit application"}
    assert apply_run.live_refusal("apply_entry", "Apply now", live)
    assert apply_run.live_refusal("apply_entry", "Apply now", {"text": "Apply now"}) == ""
    assert apply_run.live_refusal("advance", "Continue", {"text": "Finish"})
    # a text that was already final when the loop chose the advance stays
    # its choice (submit mode's "Complete profile")
    assert apply_run.live_refusal("advance", "Complete profile", {"text": "Complete profile"}) \
        == ""
    assert apply_run.live_refusal("advance", "Sign in", {"text": "Sign in to apply"},
                                  account=True) == ""
    assert apply_run.live_refusal("submit", "Next", {"text": "Submit"}) == ""


# === INV-06: the code step =======================================================================

def _code_job(button_text, role, conf=0.9, submit=True):
    context = Mock()
    runner = apply_run.Runner(jev=jev.FakeJev(), context=context, run_context={},
                              settings={"auto_apply_submit": submit}, sleep=lambda s: None)
    job = apply_run._JobRun(runner, context, {"job_posting_id": "s",
                                              "apply_url": "https://careers.example/verify"})
    job._build_allowlist()
    job.page = Mock(url="https://careers.example/verify")
    job.inbox = SimpleNamespace(fetch_code=lambda *a: "SYNTH123")
    code = apply_form.Field(0, (0, "#code"), "Security code", "text", True, id_or_name="code")
    digest = apply_form.FormDigest("careers.example", "Verify", "", fields=[code],
                                   buttons=[apply_form.Button(0, (0, "#go"), button_text)])
    return job, digest, FillPlan(buttons={role: (0, conf)})


@pytest.mark.parametrize("text", ["Confirm", "Finish application", "Done"])
def test_the_code_step_refuses_a_final_button_before_any_submit(monkeypatch, text):
    job, digest, plan = _code_job(text, "advance")
    locator = SimpleNamespace(first=SimpleNamespace(fill=lambda *a, **kw: None))
    monkeypatch.setattr(apply_run.apply_form, "resolve", lambda *a: locator)
    monkeypatch.setattr(job, "_click", Mock(side_effect=AssertionError("clicked")))
    with pytest.raises(apply_run._Parked, match="would send the application"):
        job._code_gate(digest, plan, {"filled": []})


def _landed_clicks(monkeypatch, job):
    """The code step's clicks (the role each was made in); each one lands."""
    locator = SimpleNamespace(first=SimpleNamespace(fill=lambda *a, **kw: None))
    monkeypatch.setattr(apply_run.apply_form, "resolve", lambda *a: locator)
    roles: list[str] = []

    def click(digest, n, role, rec, **kw):
        roles.append(role)
        return apply_fill.ClickResult(clicked=True, changed=True)

    monkeypatch.setattr(job, "_click", click)
    return roles


@pytest.mark.parametrize("submit", [True, False], ids=["submit_mode", "park_mode"])
@pytest.mark.parametrize("conf", [0.50, 0.55, 0.74, 0.9])
def test_an_account_code_steps_verify_read_as_the_submit_is_a_step_control(
        monkeypatch, conf, submit):
    # SP8b review I1: before the submit gate, the code step's "Verify" read
    # as the submit is the account's check; the job is never marked clicked,
    # so a "verified" page after it never ends it submitted
    job, digest, plan = _code_job("Verify", "submit", conf=conf, submit=submit)
    roles = _landed_clicks(monkeypatch, job)
    job._code_gate(digest, plan, {"filled": []})
    assert roles == ["advance"]
    assert job._code_sent and not job.submit_clicked and not job._maybe_sent()


# the code step's button after the application's answers: read as the
# submit, read as the way on, or no button judged (the step's own "Verify",
# `code_advance`)
_CODE_ROLES = pytest.mark.parametrize("role, conf, clicked_as", [
    ("submit", 0.6, "submit"), ("advance", 0.8, "advance"), (None, 0.0, "advance")],
    ids=["submit_role", "advance_role", "code_advance"])


@_CODE_ROLES
def test_a_code_steps_button_after_the_form_may_send_in_submit_mode(monkeypatch, role, conf,
                                                                    clicked_as):
    job, digest, plan = _code_job("Verify", role or "advance", conf=conf)
    if role is None:
        plan = FillPlan(buttons={})
    job.form_filled = True
    roles = _landed_clicks(monkeypatch, job)
    job._code_gate(digest, plan, {"filled": []})
    # the site may have held the application for the code: clicked once (no
    # second click), never re-queued, and only received words confirm it (M9)
    assert roles == [clicked_as]
    assert not job.submit_clicked and job._code_may_send and job._maybe_sent()


@_CODE_ROLES
def test_park_mode_never_clicks_a_code_steps_button_after_the_form(monkeypatch, role, conf,
                                                                   clicked_as):
    # final review A-I2 and A Known Minor 10: whatever the judge read the
    # button as, it may send what the site held for the code. Park mode ends
    # there as its submit end, the code typed and the button never clicked
    job, digest, plan = _code_job("Verify", role or "advance", conf=conf, submit=False)
    if role is None:
        plan = FillPlan(buttons={})
    job.form_filled = True
    roles = _landed_clicks(monkeypatch, job)
    with pytest.raises(apply_run._Parked) as p:
        job._code_gate(digest, plan, {"filled": []})
    assert p.value.status == "ready_to_submit", p.value.reason
    assert p.value.reason.startswith("auto_apply_submit is off; the emailed code is entered and "
                                     "its button (Verify) is the step that may send the "
                                     "application"), p.value.reason
    assert roles == []
    assert not job.submit_clicked and not job._code_sent and not job._maybe_sent()


def test_an_email_first_page_is_no_filled_application():
    # final review A-I2: an address box alone (a remember-me beside it)
    # starts a sign-in or an application; its fill puts none of the
    # application on the site, so the code step after it goes on in park mode
    email = apply_form.Field(0, (0, "#email"), "Email address", "email", True,
                             id_or_name="email")
    remember = apply_form.Field(1, (0, "#r"), "Remember me", "checkbox", False, id_or_name="r")
    first = apply_form.Field(2, (0, "#first"), "First name", "text", True, id_or_name="first")
    filled = [SimpleNamespace(n=0, value="x")]
    start = apply_form.FormDigest("careers.example", "Apply", "", fields=[email, remember])
    assert not apply_run._fills_the_application(start, FillPlan(), filled)
    form = apply_form.FormDigest("careers.example", "Apply", "", fields=[email, first])
    assert apply_run._fills_the_application(form, FillPlan(), filled)


def _link_job(monkeypatch, submit: bool, shown: str = ""):
    """A job on a page that says a link was emailed, its inbox holding the
    link and its link tab showing `shown`; the page settles at once."""
    job, _, _ = _code_job("Verify", "advance", submit=submit)
    job.allowed.add("careers.example")
    job.inbox = SimpleNamespace(fetch_code=Mock(side_effect=AssertionError("a code")),
                                fetch_link=Mock(return_value="https://careers.example/c?t=1"),
                                refused=[])
    job.page = Mock(url="https://careers.example/apply/done")
    job._open_link = Mock(return_value=shown)
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a, **kw: {})
    digest = apply_form.FormDigest("careers.example", "Check your inbox",
                                   "We sent a verification email. Click the link in the email "
                                   "to confirm your application.")
    return job, digest


def test_park_mode_never_opens_an_emailed_link_after_the_form(monkeypatch):
    # final review A-I2: the link may be the step that sends the application
    job, digest = _link_job(monkeypatch, submit=False)
    job.form_filled = True
    with pytest.raises(apply_run._Parked) as p:
        job._code_gate(digest, FillPlan(), {"filled": [], "clicked": []})
    assert p.value.status == "ready_to_submit", p.value.reason
    assert p.value.reason.startswith("auto_apply_submit is off; the emailed link from "
                                     "careers.example is the step that may send the "
                                     "application"), p.value.reason
    job._open_link.assert_not_called()
    assert not job._maybe_sent()


@pytest.mark.parametrize("url, loads", [
    ("https://careers.example/apply/done", ["https://careers.example/apply/done"]),
    # final review A R2-M1: a load of the same URL with its fragment moves
    # inside the document and loads nothing; the URL without it is a new
    # document, and the fragment then goes back on
    ("https://careers.example/apply/done#/verify",
     ["https://careers.example/apply/done", "https://careers.example/apply/done#/verify"])],
    ids=["path", "hash_route"])
def test_an_accounts_emailed_link_before_the_form_is_opened_in_park_mode_and_the_tab_read_again(
        monkeypatch, url, loads):
    # ACC-05's account check: none of the application is on the site yet.
    # The job's tab is read again from its URL by a GET, never a reload
    # (final review A-C1: a reload re-sends the POST that led to the page)
    job, digest = _link_job(monkeypatch, submit=False)
    job.page.url = url
    job._code_gate(digest, FillPlan(), {"filled": [], "clicked": []})
    job._open_link.assert_called_once()
    assert [c.args[0] for c in job.page.goto.call_args_list] == loads
    job.page.reload.assert_not_called()
    assert not job._maybe_sent()


def test_a_hash_routed_accounts_page_is_loaded_again_by_a_get_after_its_link(
        context, monkeypatch):
    # final review A R2-M1 in a browser: the job's tab is a new document after
    # the link (its script runs again) and keeps its route; nothing is posted
    page_html = """<body><h1>Check your inbox</h1><p id="route"></p><script>
      sessionStorage.n = String(Number(sessionStorage.n || 0) + 1);
      const show = () => { document.getElementById('route').textContent = location.hash; };
      show(); window.addEventListener('hashchange', show);</script></body>"""
    posts = _Posts(context, {"/apply/done": page_html, "/c": "<body>Email confirmed</body>"})
    job, digest = _link_job(monkeypatch, submit=False)
    job.page = context.new_page()
    job.page.goto(f"{CAREERS}/apply/done#/verify")
    job.allowed.add("careers.fabrikam.example")
    job.inbox.fetch_link = Mock(return_value=f"{CAREERS}/c?t=1")
    digest = dataclasses.replace(digest, url_host="careers.fabrikam.example")
    job._code_gate(digest, FillPlan(), {"filled": [], "clicked": []})
    assert job.page.evaluate("sessionStorage.n") == "2", "the page was loaded again"
    assert job.page.url == f"{CAREERS}/apply/done#/verify"
    assert job.page.locator("#route").inner_text() == "#/verify"
    assert posts.count == 0 and not job._maybe_sent()


@pytest.mark.parametrize("shown, status, start", [
    ("Thank you. Your application has been received.", "submitted",
     "submitted (unconfirmed): the emailed link's page on careers.example says "
     "'application has been received'"),
    ("Your email address is confirmed.", "needs_human",
     apply_run.CHECK_SENT_REASON + ": the emailed link on careers.example was opened")],
    ids=["received_words", "no_received_words"])
@pytest.mark.parametrize("after", ["form", "submit"])
def test_an_emailed_link_after_a_possible_send_never_loads_the_jobs_tab_again(
        monkeypatch, shown, status, start, after):
    # final review A-C1: the job's tab came from the post that sent the
    # application; loading it again would send it again. The link's own page
    # is read: received words end the job submitted, else the person checks
    job, digest = _link_job(monkeypatch, submit=True, shown=shown)
    job.form_filled = True
    if after == "submit":
        job.submit_clicked = True
    seen: list[bool] = []
    job._open_link.side_effect = lambda link: seen.append(job._maybe_sent()) or shown
    with pytest.raises(apply_run._Parked) as p:
        job._code_gate(digest, FillPlan(), {"filled": [], "clicked": []})
    assert seen == [True], "a possible send once the link is opened"
    assert (p.value.status, p.value.reason[:len(start)]) == (status, start), p.value.reason
    if status == "needs_human":
        assert p.value.tab_note == apply_run.CHECK_SENT_NOTE
    job.page.reload.assert_not_called()
    job.page.goto.assert_not_called()
    assert job._maybe_sent()
    # final review A R2-M5: the step a check-sent end names
    assert job._sent_step() == ("the submit click" if after == "submit" else "the link step")


def _no_link(job):
    job.inbox.fetch_link = Mock(return_value=None)


def _refused_link(job):
    job.inbox.fetch_link = Mock(return_value=None)
    job.inbox.refused = ["mail-tracker.example"]


def _off_sites_link(job):
    job.inbox.fetch_link = Mock(return_value="https://elsewhere.example/c?t=1")


def _followed_link(job):
    job._links_followed.add(apply_run._site("careers.example"))


_UNOPENED = pytest.mark.parametrize("setup, why", [
    (_no_link, "no verification link from careers.example in the inbox"),
    (_refused_link, "the email's link goes to mail-tracker.example, outside the application's "
                    "sites; it was never opened"),
    (_off_sites_link, "the email's link goes to elsewhere.example, outside the application's "
                      "sites; it was never opened"),
    (_followed_link, "the emailed link was opened and careers.example still asks for it")],
    ids=["no_link", "refused", "off_sites", "followed"])


@_UNOPENED
@pytest.mark.parametrize("after", ["form", "submit"])
def test_an_unopened_emailed_link_after_the_answers_never_asks_for_a_requeue(
        monkeypatch, setup, why, after):
    # final review A R2-M3: in submit mode after the answers, the link may be
    # the step that sends the application; "open it, then Re-queue" would
    # apply a second time. The park carries the check-sent reason, and its
    # note asks the person to open the link and Mark applied
    job, digest = _link_job(monkeypatch, submit=True)
    job.form_filled = True
    if after == "submit":
        job.submit_clicked = True
    setup(job)
    with pytest.raises(apply_run._Parked) as p:
        job._code_gate(digest, FillPlan(), {"filled": [], "clicked": []})
    assert p.value.status == "needs_human"
    assert p.value.reason == (f"{apply_run.CHECK_SENT_REASON}: {apply_run.LINK_REASON}, and the "
                              f"link may send the application: {why}")
    assert p.value.tab_note == apply_run.LINK_HELD_NOTE
    assert "Re-queue" not in p.value.tab_note
    job._open_link.assert_not_called()
    assert job._maybe_sent()


@_UNOPENED
def test_an_unopened_emailed_link_before_the_answers_asks_for_the_link(monkeypatch, setup, why):
    job, digest = _link_job(monkeypatch, submit=True)
    setup(job)
    with pytest.raises(apply_run._Parked) as p:
        job._code_gate(digest, FillPlan(), {"filled": [], "clicked": []})
    assert (p.value.status, p.value.reason, p.value.tab_note) == (
        "needs_human", f"{apply_run.LINK_REASON}: {why}", apply_run.LINK_NOTE)
    assert not job._maybe_sent()


def _final_checks_clean(monkeypatch, job):
    """The page double answers the gate's live checks a final-worded advance
    runs first (`_JobRun._final_step_checks`): a valid form, no CAPTCHA."""
    monkeypatch.setattr(job, "_form_live", lambda b: {"invalid": [], "required_empty": []})
    monkeypatch.setattr(job, "_human_check_showing", lambda **kw: False)


@pytest.mark.parametrize("text, submit, filled, marked", [
    ("Confirm", True, True, True), ("Complete", True, True, True),
    ("Continue", True, True, False), ("Confirm", False, True, False),
    ("Confirm", True, False, False)],
    ids=["confirm", "complete", "continue", "park_mode", "before_the_answers"])
def test_a_final_worded_advance_in_submit_mode_is_a_possible_send(monkeypatch, text, submit,
                                                                  filled, marked):
    # final review A-M3: submit mode clicks a final-worded advance as a step
    # (park mode sends it to the gate). It may send: marked before the
    # click, so an outage after it never hands the job back to the queue.
    # Before the application's answers are on the site it has nothing to
    # send (an address screen's "Confirm", final review A R2-M2)
    job, digest, plan = _code_job(text, "advance", submit=submit)
    job.form_filled = filled
    _final_checks_clean(monkeypatch, job)
    seen: list[bool] = []

    def click(digest, n, role, rec, **kw):
        seen.append(job._maybe_sent())
        return apply_fill.ClickResult(clicked=True, changed=True)
    monkeypatch.setattr(job, "_click", click)
    monkeypatch.setattr(job, "_form_state", lambda *a: {})
    monkeypatch.setattr(job, "_button_identity", lambda *a: None)
    job._advance(digest, plan, [], {"clicked": []}, 0, 0.9)
    assert seen == [marked] and job._maybe_sent() is marked


def test_a_final_worded_advance_that_never_landed_is_no_possible_send(monkeypatch):
    job, digest, plan = _code_job("Confirm", "advance")
    job.form_filled = True
    _final_checks_clean(monkeypatch, job)
    monkeypatch.setattr(job, "_click", lambda *a, **kw: apply_fill.ClickResult(False, False))
    monkeypatch.setattr(job, "_form_state", lambda *a: {})
    monkeypatch.setattr(job, "_button_identity", lambda *a: None)
    job._advance(digest, plan, [], {"clicked": []}, 0, 0.9)
    assert not job._maybe_sent()


def _live_refused(job):
    """A `_click` whose live check stops the click (`_refused_click`)."""
    def click(digest, n, role, rec, **kw):
        job._refused_click(role, _button_text(digest, n), "it reads 'Submit' now")
    return click


def _button_text(digest, n):
    return next(b.text for b in digest.buttons if b.n == n)


def test_a_final_worded_advance_the_live_check_stopped_is_no_possible_send(monkeypatch):
    # final review A R2-M5: the live check raises inside `_click`, and the
    # mark set before the click stayed while the park said nothing was clicked
    job, digest, plan = _code_job("Confirm", "advance")
    job.form_filled = True
    _final_checks_clean(monkeypatch, job)
    monkeypatch.setattr(job, "_click", _live_refused(job))
    monkeypatch.setattr(job, "_form_state", lambda *a: {})
    monkeypatch.setattr(job, "_button_identity", lambda *a: None)
    with pytest.raises(apply_run._Parked) as p:
        job._advance(digest, plan, [], {"clicked": []}, 0, 0.9)
    assert p.value.reason.endswith("nothing was clicked"), p.value.reason
    assert not job._final_advance and not job._maybe_sent()


@pytest.mark.parametrize("role, conf", [("advance", 0.8), ("submit", 0.6)])
def test_a_code_steps_button_the_live_check_stopped_is_no_possible_send(monkeypatch, role, conf):
    # final review A R2-M5: an advance's refusal raised inside `_click`, past
    # the code step's restore (the submit role's refusal came back as a result)
    job, digest, plan = _code_job("Verify", role, conf=conf)
    job.form_filled = True
    locator = SimpleNamespace(first=SimpleNamespace(fill=lambda *a, **kw: None))
    monkeypatch.setattr(apply_run.apply_form, "resolve", lambda *a: locator)
    monkeypatch.setattr(apply_run.apply_fill, "click",
                        lambda *a, **kw: apply_fill.ClickResult(False, False,
                                                                refused="it reads 'Submit' now"))
    with pytest.raises(apply_run._Parked) as p:
        job._code_gate(digest, plan, {"filled": [], "clicked": []})
    assert "changed before the click" in p.value.reason, p.value.reason
    assert not job._code_sent and not job._code_may_send and not job._maybe_sent()


def test_a_retry_the_live_check_stopped_keeps_the_possible_send_and_says_the_first_landed(
        monkeypatch):
    # final review A R2-M5: on the retry the first click had landed and
    # changed nothing; the mark stays (fail safe) and the words say so
    job, digest, plan = _code_job("Confirm", "advance")
    job.form_filled = True
    _final_checks_clean(monkeypatch, job)
    clicks = iter([apply_fill.ClickResult(True, False),
                   apply_fill.ClickResult(False, False, refused="it reads 'Submit' now")])
    monkeypatch.setattr(apply_run.apply_fill, "click", lambda *a, **kw: next(clicks))
    monkeypatch.setattr(job, "_form_state", lambda *a: {})
    monkeypatch.setattr(job, "_form_problems", lambda *a: [])
    monkeypatch.setattr(job, "_button_identity", lambda *a: None)
    monkeypatch.setattr(job, "_busy", lambda: False)
    job.page.wait_for_timeout = lambda ms: None
    with pytest.raises(apply_run._Parked) as p:
        job._advance(digest, plan, [], {"clicked": []}, 0, 0.9)
    assert not isinstance(p.value, apply_run._NotClicked)
    assert p.value.reason == ("the advance button (Confirm) changed before its second click: it "
                              "reads 'Submit' now; the first click landed and changed nothing")
    assert job._final_advance and job._maybe_sent()


def test_a_request_the_submits_watch_saw_leave_keeps_the_job_a_possible_send():
    # final review A-M4: validation errors after the submit reset its click
    # (`_not_sent`), and a request that left still makes the job a possible
    # send; only a send that never reached the site (`_Unsent`) rules it out
    job, _, _ = _code_job("Verify", "advance")
    job._send_watch = SimpleNamespace(any=lambda: True)
    assert not job.submit_clicked and job._maybe_sent()
    job._unsent = True
    assert not job._maybe_sent()
    job._unsent = False
    job._send_watch = SimpleNamespace(any=lambda: False)
    assert not job._maybe_sent()


_BOARD_POST = "POST https://boards.example/apply"


def _board_watch(*rows):
    rows = list(rows)
    return SimpleNamespace(sent=rows, possible=[], unplaced_sends=lambda: [],
                           any=lambda: bool(rows), first=lambda: rows[0])


def _board_guard():
    return SimpleNamespace(posts=[_BOARD_POST], blocked=["boards.example"], posted=True,
                           post_host=lambda: "boards.example", before="the form")


def test_a_stopped_post_the_watch_counted_as_sent_is_no_send():
    # final review A R2-M6: the watch counts a post to an admitted job board
    # as sent. When that post is the one the guard stopped, a "submitted
    # (unconfirmed)" resting on the watch's rows alone claims nothing
    job, digest, _ = _code_job("Submit application", "submit")
    job.submit_clicked = True
    job._send_watch = _board_watch(_BOARD_POST)
    with pytest.raises(apply_run._SentSeen) as seen:
        job._inconclusive("confirmation", 0.9, digest, job._send_watch, {"url": "x"}, False)
    out = job._stopped_post(_board_guard(), seen.value)
    assert isinstance(out, apply_run._Unsent), out.reason
    assert out.reason == ("the form posts to boards.example, outside the allowed sites; the run "
                          "stopped it and nothing was sent")
    assert not job._maybe_sent()


@pytest.mark.parametrize("seen, rows", [
    (False, [_BOARD_POST]),
    (True, [_BOARD_POST, "POST https://careers.example/api/applications"])],
    ids=["received_words", "another_request"])
def test_a_stopped_post_keeps_a_send_the_step_or_another_request_shows(seen, rows):
    step = (apply_run._SentSeen("submitted", "submitted (unconfirmed): a request left") if seen
            else apply_run._Parked("submitted", "confirmation page: 'application received'"))
    job, _, _ = _code_job("Submit application", "submit")
    job.submit_clicked = True
    job._send_watch = _board_watch(*rows)
    out = job._stopped_post(_board_guard(), step)
    assert not isinstance(out, apply_run._Unsent)
    assert (out.status, out.reason) == ("submitted",
                                        f"{step.reason}; the run stopped a post to boards.example")
    assert job._maybe_sent()


class _Blocked:
    """A navigation guard that stopped a GET to another host."""

    def __init__(self, run, page):
        self.blocked, self.posted, self.posts = ["elsewhere.example"], False, []
        self.before = "the form"

    def stop(self):
        pass

    def post_host(self):
        return ""


def test_a_stopped_page_after_a_request_left_keeps_the_check_sent_end(monkeypatch):
    # final review A R2-M6: validation errors after the submit reset its
    # click while a request had left; the guard's "before the submit" test
    # read the click and replaced the check-sent end with "left the allowed
    # sites", whose note invites a Re-queue
    job, _, _ = _code_job("Submit application", "submit")
    job._send_watch = SimpleNamespace(any=lambda: True)
    monkeypatch.setattr(apply_run, "_NavGuard", _Blocked)
    step = apply_run._Parked("needs_human", f"{apply_run.CHECK_SENT_REASON}: a request left after "
                                            "the submit click and the page reads as the form "
                                            "again", apply_run.CHECK_SENT_NOTE)
    with pytest.raises(apply_run._Parked) as p:
        with job._password_guard():
            raise step
    assert p.value is step


def test_a_stopped_page_after_a_final_worded_step_asks_the_person_to_check(monkeypatch):
    job, _, _ = _code_job("Confirm", "advance")
    job._final_advance = True
    monkeypatch.setattr(apply_run, "_NavGuard", _Blocked)
    with pytest.raises(apply_run._Parked) as p:
        with job._password_guard():
            pass
    assert (p.value.status, p.value.reason, p.value.tab_note) == (
        "needs_human", f"{apply_run.CHECK_SENT_REASON}: after the final-worded step the run "
                       f"stopped the page going to elsewhere.example", apply_run.CHECK_SENT_NOTE)
    job._final_advance = False
    with pytest.raises(apply_run._Parked) as p:
        with job._password_guard():
            pass
    assert p.value.reason == "left the allowed sites: elsewhere.example"


@pytest.mark.parametrize("where", ["_after_submit", "_open"])
def test_an_interrupted_run_leaves_no_entry_in_progress(context, tmp_path, monkeypatch, where):
    # final review A-M5: Ctrl+C in a drain ends the job for the person
    # (after the submit click: the check-sent end) and stops the drain
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'});
      };""")})

    def _stop(self, *a, **kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(apply_run._JobRun, where, _stop)
    with pytest.raises(KeyboardInterrupt):
        _drain(context, tmp_path, APPLY_URL)
    entry = apply_queue.load()["jobs"][-1]
    assert entry["status"] == "needs_human", entry
    if where == "_after_submit":
        assert entry["notes"].startswith(f"{apply_run.CHECK_SENT_REASON}: the run stopped after "
                                         f"the submit click (KeyboardInterrupt at "), entry
        assert entry["tab_note"] == apply_run.CHECK_SENT_NOTE
    else:
        assert entry["notes"].startswith("the run was interrupted (KeyboardInterrupt at "), entry


def test_a_post_answered_by_a_link_page_goes_once_and_the_links_page_confirms_it(
        _browser, flow_server, tmp_path):
    # final review A-C1: the submit's post is answered, with no redirect, by
    # a page that says a link to confirm the application was emailed. The
    # link opens in a tab of its own, and the job's tab (the post's answer)
    # is never loaded again: the server sees one post
    before = flow_server.posts.get("link_after_submit", 0)
    r = _flow("link_after_submit", _browser, flow_server, tmp_path)
    assert flow_server.posts.get("link_after_submit", 0) - before == 1
    assert r.status == "submitted" and r.ok and not r.breaks, r


def test_a_code_step_after_the_submit_gates_click_keeps_its_submit_role(monkeypatch):
    job, digest, plan = _code_job("Verify", "submit", conf=0.6)
    job.submit_clicked = True           # the gate sent it; the site asks for a code
    roles = _landed_clicks(monkeypatch, job)
    job._code_gate(digest, plan, {"filled": []})
    assert roles == ["submit"] and job.submit_clicked and job._code_may_send


@pytest.mark.parametrize("result", [apply_fill.ClickResult(clicked=False, changed=False),
                                    apply_fill.ClickResult(False, False, refused="it changed")])
def test_a_code_step_click_that_never_landed_leaves_the_job_unsent(monkeypatch, result):
    job, digest, plan = _code_job("Verify", "submit")
    locator = SimpleNamespace(first=SimpleNamespace(fill=lambda *a, **kw: None))
    monkeypatch.setattr(apply_run.apply_form, "resolve", lambda *a: locator)
    monkeypatch.setattr(job, "_click", lambda *a, **kw: result)
    if result.refused:
        with pytest.raises(apply_run._Parked, match="changed before the click"):
            job._code_gate(digest, plan, {"filled": []})
    else:
        job._code_gate(digest, plan, {"filled": []})
    assert not job.submit_clicked and not job._code_sent


# === the rest: the extractor's hints, the embed, the misreads ====================================

def test_the_extractor_never_hints_submit_for_third_party_or_cancel_buttons(browser_page):
    browser_page.set_content("""<body><form><input name="q">
      <button type="submit">Apply with LinkedIn</button>
      <button type="submit" aria-label="Apply using Indeed">Indeed</button>
      <button type="submit">Submit a general application</button>
      <button type="submit">Cancel</button>
      <button type="button">Save for later</button><button type="button">Apply later</button>
      <button type="submit">Submit application</button></form></body>""")
    hints = {b.text: b.kind_hint for b in apply_form.extract(browser_page).buttons}
    assert hints.pop("Submit application") == "submit"
    assert all(h_ != "submit" for h_ in hints.values()), hints


def _spelled(alts: list[str]) -> set[str]:
    """Each alternative as the words it matches: a `[sz]` class spelled both
    ways, a last letter marked `?` with and without it."""
    out: set[str] = set()
    for alt in alts:
        m = re.fullmatch(r"(\w*)\[(\w)(\w)\](\w*)", alt)
        if m:
            out |= {m[1] + m[2] + m[4], m[1] + m[3] + m[4]}
            continue
        m = re.fullmatch(r"(\w*)(\w)\?", alt)
        out |= {m[1], m[1] + m[2]} if m else {alt}
    return out


def _words(pattern: str) -> set[str]:
    """The words of a regex source that is one group of words between
    anchors (`\\b(a|b|c)\\b`, `^(a|b)$`). A pattern with an alternative or a
    group outside that group fails here: its other words would go unread
    (final review B R2 M4)."""
    m = re.fullmatch(r"([^()|]*)\((?:\?:)?([^()]*)\)([^()|]*)", pattern)
    assert m, f"a pattern with more than its one group of words: {pattern!r}"
    return _spelled(m[2].split("|"))


def _top(pattern: str) -> list[str]:
    """The top-level alternatives of a regex source."""
    out, cur, depth, i = [], "", 0, 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            cur, i = cur + pattern[i:i + 2], i + 2
            continue
        depth += {"(": 1, ")": -1}.get(c, 0)
        if c == "|" and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    return out + [cur]


def _js_regex(js: str, name: str) -> str:
    return re.search(r"const " + name + r" = /(.*?)/i;", js).group(1)


def test_every_copy_of_the_send_and_step_words_holds_the_loops_own():
    # final review B-M4: the loop's send and last-step words
    # (`apply_send_words.SUBMIT_WORDS`, `FINAL_WORDS`) are the source; a word added
    # there and missing from a copy fails here
    submit = _words(apply_send_words.SUBMIT_WORDS.pattern)
    final = _words(apply_send_words.FINAL_WORDS.pattern)
    assert "submit" in submit and "done" in final
    # the judge's send words: the loop's but "apply" (an Apply is an entry
    # until the judge says it sends)
    assert _words(apply_send_words.SEND_WORDS.pattern) == submit - {"apply"}
    # the extractor's `submit` hint
    hint = re.search(r"submits \|\| /(.*?)/i\.test\(text\)", apply_form_js._EXTRACT_JS).group(1)
    assert _words(hint) == submit
    # a popup's send name, the extractor's and `send_phrase`'s
    verbs = (submit - {"apply"}) | final
    assert _words(_js_regex(apply_form_js._EXTRACT_JS, "SEND_VERB")) == verbs
    assert _words(apply_send_words._POPUP_VERB.pattern) == verbs
    assert _words(_js_regex(apply_form_js._EXTRACT_JS, "SEND_LEAD")) == _words(
        apply_send_words._POPUP_LEAD.pattern)
    assert _words(_js_regex(apply_form_js._EXTRACT_JS, "SEND_OBJECT")) == _words(
        apply_send_words._POPUP_SEND_OBJECT.pattern)
    # the click's arm and the overlay picker splice every send and last-step word
    assert _words(apply_click._SEND_JS) == submit | final
    for js in (apply_click._ARM_JS, apply_click._OVERLAY_JS):
        assert f"new RegExp('{apply_click._SEND_JS}', 'i')" in js


def test_the_words_reader_fails_on_an_alternative_outside_its_group():
    # final review B R2 M4: a word added outside the group is never missed
    for pattern in (r"\b(submit|send)\b|\bsend\s+it\b", r"^(a|b)$|c", r"\b(a|b)\b(c|d)"):
        with pytest.raises(AssertionError):
            _words(pattern)
    assert _words(r"^(forms?|finali[sz]e)$") == {"form", "forms", "finalise", "finalize"}


def test_the_popup_names_the_overlays_never_and_the_step_words_pin_each_difference():
    # final review B R2 M4: the copies the send-word parity test does not
    # hold equal, each known difference pinned, so a change on either side
    # shows up here
    submit = _words(apply_send_words.SUBMIT_WORDS.pattern)
    final = _words(apply_send_words.FINAL_WORDS.pattern)
    js = apply_form_js._EXTRACT_JS
    # the extractor's `sendName` reads no leading Apply: the live check
    # (`send_phrase`) refuses "Apply now" and "Apply with" as a popup's name,
    # the extractor only reads them as a way on (POPUP_GO_ON, "apply with"),
    # so the Python side is the stricter and the difference fails safe
    body = re.search(r"const sendName = \(t\) => \{(.*?)\n  \};", js, re.S).group(1)
    assert "apply" not in body.lower()
    assert apply_send_words._POPUP_APPLY.pattern == r"^\s*apply\b"
    assert _words(apply_send_words._POPUP_APPLY_OBJECT.pattern) == {
        "with", "using", "via", "through", "now", "here", "for", "to", "online", "today"}
    # the extractor's fillers are `_POPUP_FILLER` but "&": its words are
    # letters only (/[a-z]+/gi), so "&" never reaches FILLER
    assert _words(_js_regex(js, "FILLER")) == set(apply_send_words._POPUP_FILLER) - {"&"}
    assert "&" in apply_send_words._POPUP_FILLER
    # POPUP_GO_ON has no Python twin: its words, and its "continue" is the
    # loop's own next word
    go_on = _words(_js_regex(js, "POPUP_GO_ON"))
    assert go_on == {"continue", "apply with", "next step"}
    assert "continue" in _words(apply_run._NEXT_WORDS.pattern)
    # the overlay's NEVER: the send words it holds are the loop's submit
    # words; "finish" and the last-step words are refused through its SEND
    # (`_SEND_JS`); the rest accept, sign up or chat
    never = _top(_js_regex(apply_click._OVERLAY_JS, "NEVER"))
    assert set(never) == {"accept", "allow", "agree", "submit", "apply", "send", "sign ?up",
                          "subscribe", "start chat", "chat now"}
    assert set(never) & (submit | final) == submit - {"finish"}
    # the step words: the loop's next words are step verbs, every step verb
    # is a step-only word, and no step-only word sends or ends
    step_verb = _words(apply_send_words._STEP_VERB.pattern)
    only = re.fullmatch(r"\(\?:\\b\(\?:([^()]*)\)\\b\|\[\\W_\]\)\+", apply_send_words._STEP_ONLY.pattern)
    assert only, apply_send_words._STEP_ONLY.pattern
    step_only = _spelled(only[1].split("|"))
    assert _words(apply_run._NEXT_WORDS.pattern) == {"next", "continue"}
    assert _words(apply_run._NEXT_WORDS.pattern) <= step_verb <= step_only
    assert {"review", "preview", "application"} <= step_only
    assert not step_only & (submit | final)
    # the judge's advance words: the next words, and a consent step's
    # leading accept or agree, which is never a step-only word
    advance = _top(apply_judge.ADVANCE_WORDS.pattern)
    assert advance == [r"\b(next|continue)\b", r"^\s*(i\s+)?(accept|agree)\b"]
    assert _words(advance[0]) == _words(apply_run._NEXT_WORDS.pattern)
    assert not {"accept", "agree"} & step_only


def test_the_embeds_form_leaves_no_field_once_its_thanks_show(browser_page, flow_server):
    browser_page.goto(flow_server.url("greenhouse_embed.html"))
    frame = browser_page.frames[1]
    frame.wait_for_selector("#submit_app")
    assert [f.type for f in apply_form.extract(browser_page).fields].count("file") == 2
    frame.evaluate("document.getElementById('application_form').dispatchEvent("
                   "new Event('submit', {cancelable: true}))")
    assert apply_form.extract(browser_page).fields == []


def test_the_embed_flow_reaches_its_confirmation(_browser, flow_server, tmp_path):
    r = _flow("greenhouse_embed", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks and (r.status, r.reason) == ("submitted", "confirmation page"), r


class _FormAsConfirmation(jev.FakeJev):
    """The fake, reading every form page as a confirmation at 0.9 with the
    form second."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        a = out.get("page_state")
        if a is not None and a.choice == "application_form" and state.get("fields"):
            out["page_state"] = jev.Answer(kind="choice", choice="confirmation",
                                           probabilities={"confirmation": 0.9,
                                                          "application_form": 0.1},
                                           confidence=0.9)
        return out


def test_a_form_read_as_a_confident_confirmation_before_any_submit_goes_on_as_the_form(
        _browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("ashby_wizard"), _FormAsConfirmation(), "misread", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert not r.breaks, r
    assert (r.status, r.reason) == ("submitted", "confirmation page") and r.ok, r


class _LinkPageAsConfirmation(jev.FakeJev):
    """The fake, reading a page that says a link was emailed ("Check your
    inbox") as a confirmation, as the live judge did in cycle 18's
    recording."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        a = out.get("page_state")
        if a is not None and "check your inbox" in str((state.get("page") or {}).get("title", "")).lower():
            out["page_state"] = jev.Answer(kind="choice", choice="confirmation",
                                           probabilities={"confirmation": 0.875,
                                                          a.choice: 0.125},
                                           confidence=0.875)
        return out


@pytest.mark.parametrize("name", ["link_after_submit", "link_after_answers",
                                  "link_after_answers_park"])
def test_a_link_page_read_as_a_confirmation_is_still_the_emailed_link_step(
        name, _browser, flow_server, tmp_path):
    # cycle 18 recording: the live judge read link_sent.html as a confirmation
    # (0.875 after the submit, 0.74 after the answers). The page says a link was
    # emailed and holds no received words, so it is the link step: after the
    # submit a "submitted" there went out unconfirmed (FALSE-SUBMITTED)
    r = h.run_flow(h.flow(name), _LinkPageAsConfirmation(), "misread", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, r


def test_an_all_set_page_that_says_check_your_inbox_after_the_submit_waits_for_the_link(
        context, tmp_path):
    # final review M7: a thank-you worded outside the received words that
    # also says "Check your inbox" is the emailed link step after the submit;
    # with no link in the inbox the person checks the send, and the job never
    # reads as submitted on that page alone
    posts = _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('/apply/42/send', {method: 'POST', body: '{}'}).then(function () {
          document.getElementById('app').outerHTML = "<h2>You're all set!</h2>"
            + '<p>Check your inbox for a confirmation email.</p>';
        });
      };""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL, judge=_reads("all set", "confirmation"))
    assert posts.count == 1
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.CHECK_SENT_REASON), out
    remaps = _trace_decisions(_trace_dir(tmp_path), "remap")
    assert remaps and remaps[0]["to"] == "code_gate", remaps


def test_a_confirmation_before_any_submit_never_reads_as_submitted(tmp_path):
    digest = apply_form.FormDigest("jobs.example", "Thanks", "Thank you for applying.")
    answers = {"page_state": jev.Answer(kind="choice", choice="confirmation", confidence=1.0,
                                        probabilities={"confirmation": 1.0})}
    step, why, _ = apply_run.confirmation_step(digest, answers, 1.0, submit_clicked=False)
    assert step == "park" and why.startswith("a confirmation page before any submit")
    assert apply_run.confirmation_step(digest, answers, 1.0, submit_clicked=True)[0] == \
        "submitted"


@pytest.mark.parametrize("truth", sorted(jev_doubles.CONFIRM_MISREADS))
def test_noisy_reads_a_form_or_a_review_as_a_confirmation_now_and_then(truth):
    from test_apply_matrix import _request, _Scripted
    reads = [jev_doubles.NoisyJev(_Scripted(truth), seed).judge(*_request(title=f"P {i}"))["page_state"]
             for seed in range(1, 6) for i in range(40)]
    misreads = [a for a in reads if a.choice == "confirmation"]
    assert 0 < len(misreads) < len(reads) * 0.15, len(misreads)
    for a in misreads:
        assert 0.40 <= a.confidence <= 0.80
        assert sorted(a.probabilities, key=a.probabilities.get)[-2] == truth
    quiet = jev_doubles.NoisyJev(_Scripted(truth), 3, swap_p=0.0)
    assert all(quiet.judge(*_request(title=f"Q {i}"))["page_state"].choice == truth
               for i in range(60))


def test_other_states_are_never_read_as_a_confirmation_by_the_new_misread():
    from test_apply_matrix import _request, _Scripted
    for truth in ("job_posting", "login_wall", "signup_form", "code_gate"):
        reads = {jev_doubles.NoisyJev(_Scripted(truth), s).judge(*_request(title=f"R {i}"))[
            "page_state"].choice for s in range(1, 4) for i in range(30)}
        assert "confirmation" not in reads, truth


# =================================================================================================
# SP3 review round 1
# =================================================================================================

def _trace_decisions(trace_dir, what):
    import json
    rows = []
    for p in sorted(Path(trace_dir).glob("page-*.json")):
        rows += [e for e in json.loads(p.read_text(encoding="utf-8"))["events"]
                 if e["kind"] == "decision" and e["what"] == what]
    return rows


def _trace_dir(tmp_path):
    return sorted((tmp_path / "job" / "apply_trace").glob("attempt-*"))[-1]


class _ReadAfterClickAs(jev.FakeJev):
    """The fake, reading every page with no field whose headline carries
    `WORDS` as `STATE` at 0.9."""
    WORDS, STATE = "", ""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        text = str((state.get("page") or {}).get("headline_text") or "")
        if "page_state" in out and self.WORDS in text:
            out["page_state"] = jev.Answer(kind="choice", choice=self.STATE, confidence=0.9,
                                           probabilities={self.STATE: 0.9, "other": 0.1})
        return out


def _reads(words, state):
    return type("J", (_ReadAfterClickAs,), {"WORDS": words, "STATE": state})()


# --- I1: after a request left, an emptied or reset form is never "not sent" -----------------------

@pytest.mark.parametrize("name", ["postback_emptied", "ajax_reset"])
def test_a_form_emptied_after_its_send_is_never_read_as_not_sent(
        _browser, flow_server, tmp_path, name):
    r = _flow(name, _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    assert r.sends == 1 and not r.reason.startswith(apply_run.NOT_SENT_REASON), r


# --- I2: any post may be the send; LinkedIn and trackers never are -------------------------------

def test_a_post_to_a_backend_on_another_domain_is_never_read_as_not_sent(context, tmp_path,
                                                                         monkeypatch):
    _quiet_click(monkeypatch)
    backend = []
    context.route("https://forms.backend.example/**", lambda route: backend.append(1)
                  or route.fulfill(body="{}", content_type="application/json"))
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('https://forms.backend.example/submit', {method: 'POST', body: '{}'});
      };""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert backend == [1]
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.CHECK_SENT_REASON + ": a request left for a host "
                                                               "outside the application"), out


def test_a_post_the_submit_opens_in_a_new_tab_counts_as_a_send(context, tmp_path, monkeypatch):
    _quiet_click(monkeypatch)
    posts = _Posts(context, {"/apply/42": """<!doctype html><html><body><h1>Analytics Engineer</h1>
      <form id="f" method="post" action="/api/applications" target="_blank">
      <label for="first_name">First name *</label><input id="first_name" name="first_name" required>
      <label for="last_name">Last name *</label><input id="last_name" name="last_name" required>
      <label for="email">Email *</label><input id="email" name="email" type="email" required>
      <button type="submit" id="go">Submit application</button></form></body></html>"""},
                   answer="<body><p>Your details are with us.</p></body>")
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert posts.count == 1
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.CHECK_SENT_REASON), out
    assert "POST https://careers.fabrikam.example/api/applications" in out.reason, out


@pytest.mark.parametrize("host", ["https://px.ads.linkedin.com/wa/",
                                  "https://www.google-analytics.com/g/collect"])
def test_a_tag_or_analytics_post_is_never_a_send(context, tmp_path, host):
    tags = []
    context.route(host + "**", lambda route: tags.append(1) or route.fulfill(body=""))
    _Posts(context, {"/apply/42": _form("Submit application", f"""
      document.getElementById('go').onclick = function () {{
        fetch('{host}', {{method: 'POST', body: 'x'}});
        document.getElementById('app').outerHTML = '<p>Your profile</p>';
      }};""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert tags == [1]
    assert out.status == "needs_human", out
    assert "no request was seen leaving" in out.reason, out


# --- I3: the account step's click waits for the checkbox -------------------------------------------

def test_the_account_steps_click_waits_for_an_unticked_checkbox(context, tmp_path, monkeypatch):
    import ats_accounts
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: h.PASSWORD)
    context.route("https://www.google.com/recaptcha/**", lambda route: route.fulfill(
        body="<body>I'm not a robot</body>", content_type="text/html"))
    _Posts(context, {"/apply/42": """<!doctype html><html><body><h1>Sign in to apply</h1>
      <label for="email">Email</label><input id="email" name="email" type="email">
      <label for="pw">Password</label><input id="pw" name="pw" type="password">
      <iframe style="width:304px;height:78px"
        src="https://www.google.com/recaptcha/api2/anchor?k=x&size=normal"></iframe>
      <textarea name="g-recaptcha-response" style="display:none"></textarea>
      <button type="button" id="go" onclick="document.body.dataset.signedIn = 1">Sign in</button>
      </body></html>"""})
    ats_accounts.record("careers.fabrikam.example", h.SIGNUP_EMAIL)
    out, rec, _ = _drain(context, tmp_path, APPLY_URL)
    assert out.status == "needs_human", out
    assert out.reason == "a CAPTCHA check is on the account form", out
    assert not [a for a in rec.actions if a.kind == "click"]


# --- I4: after the click the confirmation is read first; a checkbox is no challenge then ----------

def test_after_the_click_a_confirmation_wins_over_a_reset_checkbox(context, tmp_path):
    # the person ticked the box before the send; the page resets the token
    # and shows the received words
    context.route("https://www.google.com/recaptcha/**", lambda route: route.fulfill(
        body="<body>I'm not a robot</body>", content_type="text/html"))
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        document.getElementById('tok').value = '';
        document.getElementById('app').insertAdjacentHTML('afterend',
          '<h2>Thank you for applying</h2>');
        document.body.dataset.submitted = 1;
      };""", extra="""<iframe style="width:304px;height:78px"
        src="https://www.google.com/recaptcha/api2/anchor?k=x&size=normal"></iframe>
      <textarea id="tok" name="g-recaptcha-response" style="display:none">synthetic</textarea>""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert (out.status, out.reason) == ("submitted", "confirmation page"), out


def test_every_park_after_the_click_asks_the_person_to_check_and_names_the_request(
        context, flow_server, tmp_path):
    out, _, _ = _drain(context, tmp_path, flow_server.url("server_validation.html"))
    assert out.reason.startswith(apply_run.NOT_SENT_REASON + ": validation errors"), out
    assert "a request left after the click: POST " in out.reason, out
    assert apply_queue.load()["jobs"][-1]["tab_note"] == apply_run.CHECK_SENT_NOTE


# --- I5: a code step's click alone needs received words ---------------------------------------------

def test_a_sign_ups_email_verified_thanks_is_never_read_as_submitted(
        _browser, flow_server, tmp_path):
    r = _flow("email_verify_thanks", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    assert r.status == "needs_human"


# the account's email-code page with one plain "Verify" (the review's I1 page)
_PLAIN_VERIFY = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Verify your email</title></head><body>
<h1>Check your inbox</h1>
<p>We emailed you a security code to confirm your new candidate account.</p>
<label for="code">Security code</label>
<input id="code" name="code" type="text" autocomplete="one-time-code">
<p><button type="button" id="btn-verify">Verify</button></p>
<script>
  document.getElementById('btn-verify').addEventListener('click', function () {
    window.location.href = 'email_verified.html';
  });
</script></body></html>"""


class _CodeVerifyReadAsSubmit:
    """A judge that reads every button on the code page (the one with the
    "Security code" box) as the submit at `conf`, the advance second."""

    def __init__(self, inner, conf):
        self.inner, self.conf = inner, conf

    def judge(self, state, questions):
        out = dict(self.inner.judge(state, questions))
        labels = {str(f.get("label", "")).strip() for f in (state or {}).get("fields") or []}
        if "Security code" in labels:
            for qid in questions:
                if qid.startswith("button_") and qid.endswith("_role"):
                    out[qid] = jev.Answer(kind="choice", choice="submit", confidence=self.conf,
                                          probabilities={"submit": self.conf,
                                                         "advance": 1 - self.conf})
        return out


@pytest.mark.parametrize("submit", [True, False], ids=["submit_mode", "park_mode"])
@pytest.mark.parametrize("conf", [0.55, 0.74])
def test_an_accounts_verify_read_as_the_submit_never_ends_submitted_on_the_verified_page(
        _browser, flow_server, tmp_path, conf, submit):
    # SP8b review I1: the code step clicked this "Verify" as the submit, and
    # the "Your email is verified, thank you" page (read as a confirmation
    # at 0.90, no received words) ended the job submitted in either mode
    f = dataclasses.replace(
        h.flow("email_verify_thanks"), name="email_verify_as_submit", submit=submit,
        routes=lambda base: {f"{base}/forms/verify_email_code.html": _PLAIN_VERIFY},
        wrap=lambda judge: _CodeVerifyReadAsSubmit(h.VerifiedReadAsConfirmation(judge), conf))
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert not r.breaks, r
    assert r.status == "needs_human" and re.match(
        r"check whether the application went through: after the emailed code the page reads "
        r"as confirmation \(\d\.\d\d\) with no received words", r.reason), r
    trace = "".join(x.read_text(encoding="utf-8") for x in Path(r.trace).glob("*.json"))
    assert '"code_step_control"' in trace and '"role": "submit"' not in trace


# --- I6: a review page after the click is no confirmation; a form after a send is checked ---------

def test_a_review_page_after_the_click_read_as_a_confirmation_is_not_submitted(context, tmp_path):
    _Posts(context, {"/apply/42": _form("Continue", """
      document.getElementById('go').onclick = function () {
        document.getElementById('app').outerHTML = '<h2>Review your application</h2>'
          + '<p>Jane Doering, jane.doe@example.com</p>'
          + '<button type="button" id="send">Submit application</button>';
      };""").replace('id="go">Continue', 'id="go">Submit my details')})
    out, _, _ = _drain(context, tmp_path, APPLY_URL, judge=_reads("Review your", "confirmation"))
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.CHECK_SENT_REASON), out


def test_a_request_then_a_review_page_with_its_send_button_is_checked(context, tmp_path):
    _Posts(context, {"/apply/42": _form("Submit my details", """
      document.getElementById('go').onclick = function () {
        fetch('/api/step', {method: 'POST', body: '{}'}).then(function () {
          document.getElementById('app').outerHTML = '<h2>Review your application</h2>'
            + '<button type="button" id="send">Submit application</button>';
        });
      };""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL, judge=_reads("Review your", "review_page"))
    assert out.status == "needs_human", out
    assert "form with its own send button" in out.reason, out


def test_a_sign_in_after_the_send_is_checked_never_submitted(context, tmp_path):
    # a session that expired: the post answers with the sign-in page
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'}).then(function () {
          location.href = '/login';
        });
      };"""), "/login": """<!doctype html><html><body><h1>Your session expired</h1>
      <label for="u">Email</label><input id="u" name="u" type="email">
      <label for="p">Password</label><input id="p" name="p" type="password">
      <button type="button">Sign in</button></body></html>"""})
    out, _, _ = _drain(context, tmp_path, APPLY_URL, judge=_reads("session expired",
                                                                  "login_wall"))
    assert out.status == "needs_human", out
    assert "the session may have expired" in out.reason, out


# --- the follow-ups: Turnstile -------------------------------------------------------------------

_TURNSTILE = ('<iframe style="width:300px;height:65px" src="https://challenges.cloudflare.com/'
              'cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv0/0/x/0xAAAA/light/fbE/'
              '{size}/auto/"></iframe><input type="hidden" name="cf-turnstile-response" '
              'value="{token}">')


@pytest.mark.parametrize("size, token, found", [("normal", "", "turnstile"),
                                                ("normal", "synthetic", ""),
                                                ("invisible", "", "")])
def test_a_turnstile_checkbox_counts_until_its_token_is_set(browser_page, size, token, found):
    browser_page.set_content("<body>" + _TURNSTILE.format(size=size, token=token) + "</body>")
    assert apply_form.unsolved_checkbox(browser_page) == found
    assert apply_run._is_captcha_url("https://challenges.cloudflare.com/cdn-cgi/x")


def test_headless_a_turnstile_gets_a_moment_to_tick_itself(context, tmp_path):
    context.route("https://challenges.cloudflare.com/**", lambda route: route.fulfill(
        body="<body>Verifying...</body>", content_type="text/html"))
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        document.getElementById('app').outerHTML = '<h2>Thank you for applying</h2>';
      };""", extra=_TURNSTILE.format(size="normal", token=""))})
    ticks = []

    def _sleep(seconds):
        # the managed widget ticks itself a moment later
        for p in context.pages:
            if not p.is_closed():
                p.evaluate("document.querySelector('[name=cf-turnstile-response]').value = 'ok'")
        ticks.append(seconds)
    out, _, _ = _drain(context, tmp_path, APPLY_URL, sleep=_sleep)
    assert (out.status, out.reason) == ("submitted", "confirmation page"), out
    assert ticks


# --- M1: the live check and the click are one step --------------------------------------------------

def test_a_button_that_turns_into_submit_at_the_click_is_stopped_there(browser_page):
    browser_page.set_content("""<body><div id="bar"><button id="go">Continue</button></div>
      <script>
        var go = document.getElementById('go');
        go.addEventListener('mousedown', function () { go.textContent = 'Submit'; });
        go.addEventListener('click', function () {
          if (go.textContent === 'Submit') document.body.dataset.submitted = 1; });
      </script></body>""")
    d = apply_form.extract(browser_page)
    n = next(b.n for b in d.buttons if b.text == "Continue")
    check = apply_run._JobRun._live_check(Mock(), "advance")
    r = apply_fill.click(browser_page, d, n, timeout_s=1, check=check)
    assert not r.clicked and "turned into 'Submit'" in r.refused, r
    assert browser_page.evaluate("document.body.dataset.submitted") is None


# --- M2: only a navigation or a send proves a dispatch --------------------------------------------

def test_an_image_request_during_a_failed_click_is_no_dispatch(browser_page, monkeypatch):
    from playwright.sync_api import ElementHandle, Locator
    from playwright.sync_api import TimeoutError as PWTimeout
    browser_page.set_content('<body><button id="go">Continue</button></body>')
    d = apply_form.extract(browser_page)

    def _fails(self, *a, **kw):
        browser_page.evaluate("new Image().src = 'http://127.0.0.1:9/nothing.png?' + Date.now()")
        browser_page.wait_for_timeout(200)
        raise PWTimeout("Timeout 5000ms exceeded")
    monkeypatch.setattr(ElementHandle, "click", _fails)
    monkeypatch.setattr(Locator, "click", _fails)
    r = apply_fill.click(browser_page, d, 0, timeout_s=1)
    assert not r.clicked, r


# --- M3 to M7: the scan, the form's controls, the validity scope -----------------------------------

def test_a_shadow_header_search_is_chrome_to_the_scan(browser_page):
    browser_page.set_content("""<body><header><x-search></x-search></header>
      <main><x-field></x-field></main>
      <script>
        for (const [tag, label] of [['x-search', 'Search jobs'], ['x-field', 'Start date']]) {
          customElements.define(tag, class extends HTMLElement { constructor() { super();
            this.attachShadow({mode: 'open'}).innerHTML = '<input aria-label="' + label + '">'; } });
        }
      </script></body>""")
    assert [r["label"] for r in apply_form.control_scan(browser_page)] == ["Start date"]


def test_a_page_wide_forms_header_search_leaves_its_buttons_no_forms_own(browser_page):
    browser_page.set_content("""<body><form action="/page">
      <header><input name="q" aria-label="Search"></header>
      <input type="hidden" name="state" value="x">
      <h1>Role</h1><button type="button">Apply</button></form></body>""")
    d = apply_form.extract(browser_page)
    assert [b.in_form for b in d.buttons if b.text == "Apply"] == [False]


def test_the_scan_filters_required_boxes_before_its_cap(browser_page):
    boxes = "".join(f'<div role="textbox" contenteditable="true" aria-label="Note {i}"></div>'
                    for i in range(45))
    browser_page.set_content(f"<body>{boxes}<div role='textbox' contenteditable='true' "
                             f"aria-required='true' aria-label='Required note'></div></body>")
    rows = apply_form.control_scan(browser_page, required_only=True)
    assert [r["label"] for r in rows] == ["Required note"]


def test_a_wizard_footers_submit_reads_the_form_of_its_filled_fields(browser_page):
    browser_page.set_content("""<body><form id="step"><input id="a" name="a" required>
      <input id="b" name="b" aria-label="Last name" required></form>
      <div class="footer"><button id="send" type="button">Submit</button></div></body>""")
    browser_page.fill("#a", "Jane")
    report = apply_form.validity_report(browser_page, (0, "#send"), [(0, "#a")])
    assert [r["label"] for r in report["invalid"]] == ["Last name"]


@pytest.mark.parametrize("novalidate, flagged", [(True, []), (False, ["hidden_pick"])])
def test_a_novalidate_forms_hidden_required_box_is_the_sites_own(browser_page, novalidate,
                                                                   flagged):
    attr = " novalidate" if novalidate else ""
    browser_page.set_content(f"""<body><form{attr}><input id="a" name="a" required value="x">
      <input name="hidden_pick" required style="display:none">
      <button id="send">Submit</button></form></body>""")
    report = apply_form.validity_report(browser_page, (0, "#send"))
    assert [r["label"] for r in report["invalid"]] == flagged


# --- M8: a page that moved on during the gate's wait -------------------------------------------------

def test_a_page_the_person_sent_during_the_gates_wait_is_read_after_the_submit(
        context, flow_server, tmp_path):
    context.route("https://www.google.com/recaptcha/**", lambda route: route.fulfill(
        body="<body>I'm not a robot</body>", content_type="text/html"))

    def _sleep(seconds):
        # the person ticks the box and sends the form themselves
        for p in context.pages:
            if not p.is_closed():
                p.evaluate("window.__tick(); document.getElementById('btn-submit').click()")
    out, rec, _ = _drain(context, tmp_path, flow_server.url("recaptcha_checkbox.html"),
                         sleep=_sleep, auto_apply_headless=False)
    assert (out.status, out.reason) == ("submitted", "confirmation page"), out
    assert not [a for a in rec.actions if a.kind == "click" and a.text == "Submit application"]


# --- M10, M11 ------------------------------------------------------------------------------------------

def test_received_words_read_through_typographic_apostrophes():
    assert apply_run.new_confirmation("", "We’ve received your application.") == \
        "we've received your application"


def test_the_apply_noul_carries_its_true_and_false_criteria(tmp_path):
    import apply_facts
    folder = h.write_job_folder(tmp_path / "job")
    catalog = apply_facts.build(folder)
    digest = apply_form.FormDigest("jobs.example", "Apply", "", buttons=[
        apply_form.Button(0, (0, "#a"), "Apply now")])
    _, q = apply_judge.page_questions(digest, catalog, {})
    assert set(q["button_0_sends"]["criteria"]) == {"true", "false"}


# --- M12, M13: one rule for the form step's entry; never a form's own Apply ------------------------

_ALERT_FORM_PAGE = """<!doctype html><html><body><h1>Analytics Engineer</h1>
  <a class="btn" id="apply" href="/apply/42/form">Apply now</a>
  <aside><form><label for="nick">Nickname</label><input id="nick" name="nick">
  <button type="submit">Notify me</button></form></aside></body></html>"""


class _AsFormNothingMapped(jev.FakeJev):
    """The fake, reading the page as a form whose box maps to nothing."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        if "page_state" in out and "Analytics Engineer" in str(state.get("page")):
            out["page_state"] = jev.Answer(kind="choice", choice="application_form",
                                           confidence=0.9,
                                           probabilities={"application_form": 0.9})
        for qid in out:
            if qid.endswith("_source"):
                out[qid] = jev.Answer(kind="choice", choice="leave_blank", confidence=1.0,
                                      probabilities={"leave_blank": 1.0})
        return out


def test_the_probe_and_the_run_take_the_same_form_step_entry(context, tmp_path, monkeypatch):
    import io
    _Posts(context, {"/apply/42": _ALERT_FORM_PAGE})
    page = context.new_page()
    page.goto(APPLY_URL)
    out = io.StringIO()
    apply_run._probe_page(page, 1, _AsFormNothingMapped(), out)
    said = [line for line in out.getvalue().splitlines() if "the loop would" in line][0]
    assert "click the Apply entry" in said and "'Apply now'" in said, said
    page.close()
    clicked = []

    def _entry(self, rec, loc, text, **kw):
        clicked.append(text)
        raise apply_run._Parked("needs_human", "stop here")
    monkeypatch.setattr(apply_run._JobRun, "_click_entry", _entry)
    _drain(context, tmp_path, APPLY_URL, judge=_AsFormNothingMapped())
    assert clicked == ["Apply now"]


def test_a_form_step_never_clicks_its_own_apply_as_an_entry(browser_page):
    browser_page.set_content("""<body><h1>Role</h1><div id="app">
      <label for="f">First name</label><input id="f" name="f">
      <button id="b" type="button">Apply</button></div></body>""")
    d = apply_form.extract(browser_page)
    plan = FillPlan(buttons={"apply_entry": (d.buttons[0].n, 0.9)})
    assert apply_run.form_entry_choice(browser_page, d, plan, park_mode=False,
                                       filled=False) is None


# --- the container ruling: which rule let the Apply through ----------------------------------------

def test_the_trace_names_the_rule_that_made_an_apply_the_submit(context, flow_server, tmp_path):
    out, _, _ = _drain(context, tmp_path, flow_server.url("apply_now_form.html"))
    assert (out.status, out.reason) == ("submitted", "confirmation page"), out
    rows = _trace_decisions(_trace_dir(tmp_path), "apply_button_submit")
    assert rows and "it sits with the fields this page filled" in rows[0]["why"], rows


# =================================================================================================
# SP3 review round 2
# =================================================================================================

# --- R1: after a send, "not sent" only on the form as the run typed it ------------------------------

@pytest.mark.parametrize("name", ["success_flash_emptied", "reset_aria_invalid"])
def test_a_flash_or_a_reset_form_after_its_send_is_never_read_as_not_sent(
        _browser, flow_server, tmp_path, name):
    r = _flow(name, _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    assert r.sends == 1 and not r.reason.startswith(apply_run.NOT_SENT_REASON), r


def test_a_bare_alert_beside_the_kept_form_after_a_send_is_never_not_sent(context, tmp_path,
                                                                           monkeypatch):
    # the form keeps what the run typed, and a status line no control names
    # shows beside a box: only a message a control names says "refused"
    _quiet_click(monkeypatch)
    posts = _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'}).then(function () {
          var n = document.createElement('div');
          n.setAttribute('role', 'alert');
          n.textContent = 'We are processing your application';
          document.getElementById('email').insertAdjacentElement('afterend', n);
        });
      };""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert posts.count == 1, out
    assert out.status == "needs_human", out
    assert not out.reason.startswith(apply_run.NOT_SENT_REASON), out
    assert out.reason.startswith(apply_run.CHECK_SENT_REASON), out


def test_a_success_flash_is_never_an_error_text(browser_page):
    browser_page.set_content("""<body><form>
      <div class="alert alert-success" role="alert">Your details were saved</div>
      <label for="e">Email</label><input id="e" name="e" type="email">
      <button type="submit">Submit</button></form></body>""")
    assert apply_form.validity_report(browser_page)["errors"] == []
    # a class that names an error stays one, whatever else it says
    browser_page.set_content("""<body><form>
      <label for="e">Email</label><input id="e" name="e" type="email">
      <div class="field-error-info">Enter an email address</div>
      <button type="submit">Submit</button></form></body>""")
    assert [e["text"] for e in apply_form.validity_report(browser_page)["errors"]] == [
        "Enter an email address"]


# --- R2: the click guard is one-shot ----------------------------------------------------------------

def test_the_persons_own_click_after_the_runs_goes_through(browser_page):
    # a wizard footer: the run's Next lands and the node reads "Submit" on the
    # review step; later the person clicks it themselves
    browser_page.set_content("""<body><div id="bar"><button id="go">Next</button></div><script>
      var go = document.getElementById('go');
      go.addEventListener('click', function () {
        if (go.textContent === 'Submit') { document.body.dataset.submitted = 1; return; }
        go.textContent = 'Submit';
      });</script></body>""")
    d = apply_form.extract(browser_page)
    check = apply_run._JobRun._live_check(Mock(), "advance")
    r = apply_fill.click(browser_page, d, d.buttons[0].n, timeout_s=1, check=check)
    assert r.clicked and not r.refused, r
    browser_page.evaluate("document.getElementById('go').click()")
    assert browser_page.evaluate("document.body.dataset.submitted") == "1"


# --- M-a: the handle is disposed; a re-rendered node is found again -------------------------------

def test_a_node_re_rendered_before_the_click_is_found_again(browser_page, monkeypatch):
    from playwright.sync_api import ElementHandle
    browser_page.set_content('<body><div id="bar"><button id="go" onclick="document.body.dataset'
                             '.next = 1">Continue</button></div></body>')
    d = apply_form.extract(browser_page)
    real = ElementHandle.evaluate
    disposed, rendered = [], []

    def _evaluate(self, expression, *a, **kw):
        out = real(self, expression, *a, **kw)
        if expression == apply_click._ARM_JS and not rendered:
            # the page re-renders the node once, between the check and the click
            rendered.append(1)
            browser_page.evaluate("var b = document.getElementById('go');"
                                  " b.replaceWith(b.cloneNode(true));")
        return out
    real_dispose = ElementHandle.dispose

    def _dispose(self):
        disposed.append(1)
        return real_dispose(self)
    monkeypatch.setattr(ElementHandle, "evaluate", _evaluate)
    monkeypatch.setattr(ElementHandle, "dispose", _dispose)
    check = apply_run._JobRun._live_check(Mock(), "advance")
    r = apply_fill.click(browser_page, d, d.buttons[0].n, timeout_s=1, check=check)
    assert r.clicked, r
    assert browser_page.evaluate("document.body.dataset.next") == "1"
    assert len(disposed) == 2        # the first node's handle and the one found again


# --- R3: the person's own send during the gate's wait ----------------------------------------------

_AJAX_CHECKBOX = """<!doctype html><html><body><h1>Analytics Engineer</h1>
<div id="app">
<label for="first_name">First name *</label><input id="first_name" name="first_name" required>
<label for="last_name">Last name *</label><input id="last_name" name="last_name" required>
<label for="email">Email *</label><input id="email" name="email" type="email" required>
<iframe style="width:304px;height:78px"
  src="https://www.google.com/recaptcha/api2/anchor?k=x&size=normal"></iframe>
<textarea id="tok" name="g-recaptcha-response" style="display:none"></textarea>
<button type="button" id="go">Submit application</button>
<p id="toast"></p>
</div><script>
  window.__tick = function () { document.getElementById('tok').value = 'synthetic'; };
  document.getElementById('go').onclick = function () {
    if (!document.getElementById('tok').value) return;
    fetch('/api/applications', {method: 'POST', body: '{}'}).then(function () {
      document.getElementById('toast').textContent = 'Sent!';
    });
  };
</script></body></html>"""


@pytest.mark.parametrize("reset", [False, True])
def test_a_send_the_person_made_during_the_wait_is_never_sent_again(context, tmp_path, reset):
    context.route("https://www.google.com/recaptcha/**", lambda route: route.fulfill(
        body="<body>I'm not a robot</body>", content_type="text/html"))
    posts = _Posts(context, {"/apply/42": _AJAX_CHECKBOX})
    calls = []

    def _sleep(seconds):
        for p in context.pages:
            if p.is_closed():
                continue
            if not calls:
                # the person ticks the box and sends the form themselves
                p.evaluate("window.__tick(); document.getElementById('go').click()")
                p.wait_for_timeout(200)
            if reset:
                # the site resets the CAPTCHA after its send
                p.evaluate("document.getElementById('tok').value = ''")
        calls.append(seconds)
    out, rec, _ = _drain(context, tmp_path, APPLY_URL, sleep=_sleep, auto_apply_headless=False)
    assert posts.count == 1, out
    assert out.status == "needs_human", out
    assert "POST https://careers.fabrikam.example/api/applications" in out.reason, out
    assert apply_queue.load()["jobs"][-1]["tab_note"] == apply_run.CHECK_SENT_NOTE
    assert not [a for a in rec.actions if a.kind == "click" and a.text == "Submit application"]


# --- R4: only the job's page and its tabs count ---------------------------------------------------

def test_another_tabs_post_during_the_window_changes_nothing(context, tmp_path, monkeypatch):
    _quiet_click(monkeypatch)
    ats = []
    context.route("https://acme.greenhouse.io/**", lambda route: ats.append(1)
                  or route.fulfill(body="{}", content_type="application/json"))
    context.route("https://other.example/**", lambda route: route.fulfill(
        body="<body>another tab</body>", content_type="text/html"))
    other = context.new_page()
    other.goto("https://other.example/")
    _Posts(context, {"/apply/42": _form("Submit application", "")})
    real = apply_run._JobRun._after_submit

    def _after(self, **kw):
        # the person, in another tab, posts to an ATS host while the run reads
        other.evaluate("fetch('https://acme.greenhouse.io/api', {method: 'POST', body: 'x'})")
        other.wait_for_timeout(300)
        return real(self, **kw)
    monkeypatch.setattr(apply_run._JobRun, "_after_submit", _after)
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert ats == [1]
    assert out.reason.startswith(apply_run.NOT_SENT_REASON + " (the form did not change"), out


def test_a_post_to_the_inbox_host_is_never_a_send(context, tmp_path, monkeypatch):
    _quiet_click(monkeypatch)
    inbox = []
    context.route("https://mail.example.com/**", lambda route: inbox.append(1)
                  or route.fulfill(body="{}", content_type="application/json"))
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('https://mail.example.com/api/ping', {method: 'POST', body: 'x'});
      };""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert inbox == [1]
    assert out.reason.startswith(apply_run.NOT_SENT_REASON + " (the form did not change"), out


def test_an_application_served_from_the_inbox_host_still_counts_its_send(context, tmp_path):
    # the job's page lives on the inbox's host (a test server, a form on the
    # mail provider's own site): its own post is the send
    _Posts(context, {"/apply/42": """<!doctype html><html><body><h1>Analytics Engineer</h1>
      <form method="post" action="/api/applications">
      <label for="first_name">First name *</label><input id="first_name" name="first_name" required>
      <label for="last_name">Last name *</label><input id="last_name" name="last_name" required>
      <label for="email">Email *</label><input id="email" name="email" type="email" required>
      <button type="submit">Submit application</button></form></body></html>"""},
           answer="<!doctype html><html><body><h1>Next steps</h1><p>Your profile</p></body></html>",
           host="https://mail.example.com")
    out, _, _ = _drain(context, tmp_path, "https://mail.example.com/apply/42")
    assert out.status == "submitted", out
    assert "a request left after the submit click (POST https://mail.example.com/api/"            "applications)" in out.reason, out


# --- R5: the gate reads the filled fields' form and the required scan ------------------------------

def test_the_gate_reads_the_form_behind_a_footer_submit(context, tmp_path):
    _Posts(context, {"/apply/42": """<!doctype html><html><body><h1>Analytics Engineer</h1>
      <form id="step">
      <label for="first_name">First name *</label><input id="first_name" name="first_name" required>
      <label for="last_name">Last name *</label><input id="last_name" name="last_name" required>
      <label for="email">Email *</label><input id="email" name="email" type="email" required>
      <textarea name="cover" aria-label="Cover letter" required style="display:none"></textarea>
      </form>
      <div class="footer"><button type="button" id="go"
        onclick="document.getElementById('step').requestSubmit()">Submit application</button></div>
      </body></html>"""})
    out, rec, _ = _drain(context, tmp_path, APPLY_URL)
    assert out.status == "needs_human", out
    assert out.reason.startswith("required field without an answer: Cover letter (the form "
                                 "reports it empty"), out
    assert not [a for a in rec.actions if a.kind == "click"]


def test_the_gate_finds_a_required_box_past_the_scans_cap(context, tmp_path):
    boxes = "".join(f'<div role="spinbutton" tabindex="0" aria-label="Count {i}">1</div>'
                    for i in range(45))
    _Posts(context, {"/apply/42": _form("Submit application", "", extra=boxes + (
        '<div role="spinbutton" tabindex="0" aria-required="true" '
        'aria-label="Required count"></div>'))})
    out, rec, _ = _drain(context, tmp_path, APPLY_URL)
    assert out.status == "needs_human", out
    assert out.reason.startswith("required field without an answer: Required count (a "
                                 "spinbutton"), out
    assert not [a for a in rec.actions if a.kind == "click"]


# --- M-b: the refused form is read when the submit's path moved -----------------------------------

def test_a_server_answer_that_moved_the_submit_is_still_read_as_refused(context, tmp_path):
    form = """<label for="first_name">First name *</label><input id="first_name" name="first_name"
      required value="{first}">
      <label for="last_name">Last name *</label><input id="last_name" name="last_name" required
      value="{last}">
      <label for="email">Email *</label><input id="email" name="email" type="email" required
      value="{email}"{invalid}><span id="email-error">{message}</span>
      <button type="submit">Submit application</button>"""
    page = ("<!doctype html><html><body><h1>Analytics Engineer</h1><form method='post' action=''>"
            + form.format(first="", last="", email="", invalid="", message="") + "</form></body></html>")
    answer = ("<!doctype html><html><body><h1>Analytics Engineer</h1><div class='wrap'>"
              "<div role='alert'>Please correct the highlighted field.</div>"
              "<form method='post' action=''>"
              + form.format(first="Jane", last="Doering", email="jane.doe@example.com",
                            invalid=' aria-invalid="true" aria-describedby="email-error"',
                            message="Use your work email.")
              + "</form></div></body></html>")
    posts = _Posts(context, {"/apply/42": page}, answer=answer)
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert posts.count == 1
    assert out.reason.startswith(apply_run.NOT_SENT_REASON + ": validation errors (the form "
                                                             "reports an invalid field: Email"), out


# --- M-c: the advance path names an unticked checkbox -------------------------------------------

def test_a_next_that_waits_for_the_checkbox_names_it(context, tmp_path, monkeypatch):
    _quiet_click(monkeypatch)
    context.route("https://www.google.com/recaptcha/**", lambda route: route.fulfill(
        body="<body>I'm not a robot</body>", content_type="text/html"))
    _Posts(context, {"/apply/42": _form("Continue", "", extra="""<iframe
        style="width:304px;height:78px"
        src="https://www.google.com/recaptcha/api2/anchor?k=x&size=normal"></iframe>
      <textarea name="g-recaptcha-response" style="display:none"></textarea>""")})
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert out.status == "needs_human", out
    assert "did nothing" in out.reason and "a CAPTCHA checkbox on the page is unticked" in \
        out.reason, out


# --- M-d: a stale read is taken once more before the requests decide ------------------------------

class _ReadsByHeadline(jev.FakeJev):
    """The fake, with the page state read from the headline (`READS`:
    headline words -> state, at 0.9)."""
    READS: dict = {}

    def judge(self, state, questions):
        out = super().judge(state, questions)
        text = str((state.get("page") or {}).get("headline_text") or "")
        for words, read in self.READS.items():
            if "page_state" in out and words in text:
                h.read_as(out, read, 0.9, {read: 0.9, "other": 0.1})
        return out


def test_a_page_the_judge_never_read_is_read_once_more(context, tmp_path, monkeypatch):
    # the judge's budget is spent on the first look (a progress note); the
    # page then turns into a review step with its own send button: read
    # once more, it is the person's to check, never "submitted"
    monkeypatch.setattr(apply_limits, "POST_SUBMIT_READS", 1)
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'}).then(function () {
          // a progress note that grows past the click's settle cap (the
          // judge's one read lands on it), then the review step
          var n = 0;
          var tick = setInterval(function () {
            document.body.innerHTML = '<h1>Processing</h1><p>' + '.'.repeat(++n) + '</p>';
          }, 50);
          setTimeout(function () {
            clearInterval(tick);
            document.body.innerHTML = '<h1>Review your application</h1>'
              + '<p>First name: Ada</p><button type="button">Submit application</button>';
          }, 4500);
        });
      };""")})
    judge = type("J", (_ReadsByHeadline,), {"READS": {"Processing": "other",
                                                       "Review your": "review_page"}})()
    out, _, _ = _drain(context, tmp_path, APPLY_URL, judge=judge)
    assert out.status == "needs_human", out
    assert "a form with its own send button (review_page " in out.reason, out
    rows = _trace_decisions(_trace_dir(tmp_path), "after_submit")
    assert any("the last read was stale" in r["why"] for r in rows), rows


# --- M-e: the ad and chat hosts -----------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://insight.adsrvr.org/track/cei", "https://ib.adnxs.com/x",
                                 "https://api-iam.intercom.io/messenger/web/ping",
                                 "https://www.facebook.com/tr/", "https://bat.bing.com/action/0"])
def test_ad_and_chat_posts_are_trackers(url):
    assert apply_run._tracking(url)



# =================================================================================================
# SP3 review round 3
# =================================================================================================

# --- U1: a post to a new tab whose answer redirects still counts ------------------------------------

def test_a_post_to_a_new_tab_that_redirects_still_counts_as_a_send(context, tmp_path, monkeypatch):
    _quiet_click(monkeypatch)
    posts = []
    page = """<!doctype html><html><body><h1>Analytics Engineer</h1>
      <form id="f" method="post" action="/api/applications" target="_blank">
      <label for="first_name">First name *</label><input id="first_name" name="first_name" required>
      <label for="last_name">Last name *</label><input id="last_name" name="last_name" required>
      <label for="email">Email *</label><input id="email" name="email" type="email" required>
      <button type="submit" id="go">Submit application</button></form></body></html>"""

    def _handle(route):
        request = route.request
        if request.method == "POST":
            posts.append(1)
            route.fulfill(status=303, headers={"Location": f"{CAREERS}/thanks"}, body="")
        elif request.url.endswith("/thanks"):
            route.fulfill(body="<body><p>Your details are with us.</p></body>",
                          content_type="text/html")
        else:
            route.fulfill(body=page, content_type="text/html")
    context.route(f"{CAREERS}/**", _handle)
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert posts == [1]
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.CHECK_SENT_REASON + ": a request left from a new tab "
                                 "or a worker"), out
    assert "POST https://careers.fabrikam.example/api/applications" in out.reason, out
    assert apply_queue.load()["jobs"][-1]["tab_note"] == apply_run.CHECK_SENT_NOTE


def test_a_held_send_no_tab_claimed_is_a_possible_send_when_the_watch_stops():
    watch = apply_run.SendWatch(Mock(), Mock())
    watch._unplaced = [("https://a.example/x", "possible", "POST https://a.example/x"),
                       ("https://a.example/y", "sent", "GET https://a.example/y")]
    assert watch.any() and watch.first() == "POST https://a.example/x"
    watch._on = True
    watch.stop()
    assert watch.possible == ["POST https://a.example/x"] and watch.sent == []


# --- m1: a warning notice stays an error text --------------------------------------------------------

def test_a_warning_notice_is_an_error_text(browser_page):
    browser_page.set_content("""<body><form>
      <label for="e">Email</label><input id="e" name="e" type="email">
      <div role="alert" class="notice notice-warning">Please enter a valid email.</div>
      <button type="submit">Submit</button></form></body>""")
    assert [e["text"] for e in apply_form.validity_report(browser_page)["errors"]] == [
        "Please enter a valid email."]


# --- m2: the fresh read gets the confirmation test ---------------------------------------------------

def test_a_stale_last_read_that_is_a_confirmation_reads_as_submitted(context, tmp_path,
                                                                     monkeypatch):
    monkeypatch.setattr(apply_limits, "POST_SUBMIT_READS", 1)
    _Posts(context, {"/apply/42": _form("Submit application", """
      document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'}).then(function () {
          var n = 0;
          var tick = setInterval(function () {
            document.body.innerHTML = '<h1>Processing</h1><p>' + '.'.repeat(++n) + '</p>';
          }, 50);
          setTimeout(function () {
            clearInterval(tick);
            document.body.innerHTML = '<h1>All set</h1><p>We will be in touch.</p>';
          }, 4500);
        });
      };""")})
    judge = type("J", (_ReadsByHeadline,), {"READS": {"Processing": "other",
                                                       "All set": "confirmation"}})()
    out, _, _ = _drain(context, tmp_path, APPLY_URL, judge=judge)
    assert (out.status, out.reason) == ("submitted", "confirmation page"), out


# --- m3: Cloudflare's beacons ------------------------------------------------------------------------

@pytest.mark.parametrize("url, tracking", [
    ("https://careers.fabrikam.example/cdn-cgi/rum?x=1", True),
    ("https://careers.fabrikam.example/cdn-cgi/challenge-platform/h/b/jsd/r/abc", True),
    ("https://static.cloudflareinsights.com/beacon.min.js", True),
    ("https://careers.fabrikam.example/api/applications", False)])
def test_cloudflare_beacons_are_trackers(url, tracking):
    assert apply_run._tracking(url) is tracking


# --- m4: the password box's value is never read back -------------------------------------------------

def test_the_password_box_is_never_read_back_even_as_text(context, tmp_path, monkeypatch):
    import ats_accounts
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: h.PASSWORD)
    _Posts(context, {"/apply/42": _form("Submit application", """
      var pw = document.getElementById('pw');
      pw.addEventListener('input', function () { pw.type = 'text'; });   // a shown password
      document.getElementById('go').onclick = function () {
        fetch('/api/applications', {method: 'POST', body: '{}'});
      };""", extra='<label for="pw">Create a password *</label>'
                   '<input id="pw" name="pw" type="password" autocomplete="new-password" '
                   'required>')})
    real = apply_form.box_values
    seen = []

    def _values(page, locators):
        out = real(page, locators)
        seen.extend(out)
        return out
    monkeypatch.setattr(apply_form, "box_values", _values)
    out, _, _ = _drain(context, tmp_path, APPLY_URL)
    assert seen, out                          # the baseline read the boxes
    assert h.PASSWORD not in seen, out
    assert "jane.doe@example.com" in seen, out


# --- m5: the person's send during the wait is named as such ------------------------------------------

@pytest.mark.parametrize("reset", [False, True])
def test_a_send_during_the_wait_is_named_as_one(context, tmp_path, reset):
    context.route("https://www.google.com/recaptcha/**", lambda route: route.fulfill(
        body="<body>I'm not a robot</body>", content_type="text/html"))
    _Posts(context, {"/apply/42": _AJAX_CHECKBOX})
    calls = []

    def _sleep(seconds):
        for p in context.pages:
            if p.is_closed():
                continue
            if not calls:
                p.evaluate("window.__tick(); document.getElementById('go').click()")
                p.wait_for_timeout(200)
            if reset:
                p.evaluate("document.getElementById('tok').value = ''")
        calls.append(seconds)
    out, _, _ = _drain(context, tmp_path, APPLY_URL, sleep=_sleep, auto_apply_headless=False)
    assert out.status == "needs_human", out
    assert "a request left during the" in out.reason, out
    assert "after the submit click" not in out.reason and "after the click" not in out.reason, out


# --- a final-worded advance in submit mode runs the gate's live checks first ---------

@pytest.mark.parametrize("live, captcha, words", [
    ({"invalid": [{"label": "Email", "reason": "typeMismatch"}], "required_empty": []}, False,
     "the form reports an invalid field: Email"),
    ({"invalid": [], "required_empty": [{"label": "Signature", "kind": "canvas"}]}, False,
     "required field without an answer: Signature"),
    ({"unreadable": "the form's validity could not be read before Confirm (TypeError)"}, False,
     "the form's validity could not be read"),
    ({"invalid": [], "required_empty": []}, True, "a CAPTCHA checkbox on the page is unticked")],
    ids=["invalid", "required_empty", "unreadable", "captcha"])
def test_a_final_worded_advance_parks_on_the_gates_live_checks_before_any_click(
        monkeypatch, live, captcha, words):
    job, digest, plan = _code_job("Confirm", "advance")
    job.form_filled = True
    job._gate_repairs = apply_limits.REPAIR_ROUNDS      # no repair left: the check decides
    clicks: list = []
    monkeypatch.setattr(job, "_form_live", lambda b: live)
    monkeypatch.setattr(job, "_human_check_showing", lambda **kw: captcha)
    monkeypatch.setattr(job, "_click", lambda *a, **kw: clicks.append(a))
    monkeypatch.setattr(job, "_form_state", lambda *a: {})
    monkeypatch.setattr(job, "_button_identity", lambda *a: None)
    with pytest.raises(apply_run._Parked) as p:
        job._advance(digest, plan, [], {"clicked": []}, 0, 0.9)
    assert p.value.status == "needs_human"
    assert p.value.reason.startswith(words), p.value.reason
    assert "before the Confirm step" in p.value.reason
    assert clicks == [] and not job._maybe_sent()


def test_a_gate_read_that_raises_fails_the_gate(monkeypatch):
    job, digest, plan = _code_job("Submit application", "submit")
    job.form_filled = True

    def _boom(*a, **kw):
        raise TypeError("page double")
    monkeypatch.setattr(apply_run.apply_form, "validity_report", _boom)
    live = job._gate_read(digest, plan)
    assert live["unreadable"].startswith("the form's validity could not be read")
    ok, why = apply_run.can_submit(plan, [], {"auto_apply_submit": True}, live)
    assert not ok and why == live["unreadable"]


def test_a_refused_form_read_after_the_captcha_wait_parks_as_not_sent(monkeypatch):
    """The page moved on while the run waited for the person's CAPTCHA tick,
    and its read finds the form's own refusal with nothing sent (`_Refused`):
    the job parks with that refusal's own reason, never a crash."""
    job, digest, plan = _code_job("Submit application", "submit")
    job._filled_any = True
    _final_checks_clean(monkeypatch, job)

    class _Watch:
        def __init__(self, *a):
            pass
        start = stop = lambda self: None
        any = lambda self: False    # noqa: E731
        first = lambda self: ""     # noqa: E731
    monkeypatch.setattr(apply_run, "SendWatch", _Watch)
    monkeypatch.setattr(job, "_human_check_showing", lambda **kw: True)
    monkeypatch.setattr(job, "_wait_for_human_check", lambda *a, **kw: None)
    monkeypatch.setattr(job, "_moved_during_wait", lambda *a: True)
    park = apply_run._Parked("needs_human", "the form refused the submit as typed")

    def _refused(**kw):
        assert kw.get("during_wait")
        raise apply_run._Refused([{"text": "Enter a valid date"}], park)
    monkeypatch.setattr(job, "_after_submit", _refused)
    monkeypatch.setattr(job, "_click", Mock(side_effect=AssertionError("clicked")))
    with pytest.raises(apply_run._Parked) as p:
        job._submit_gate(digest, plan, [], {"clicked": [], "filled": []})
    assert p.value is park
