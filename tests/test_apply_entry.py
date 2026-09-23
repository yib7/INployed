"""SP2: the entry. An Easy Apply job stops at once and nothing is ever filled
on LinkedIn (EZ-01, EZ-02); LinkedIn's pages are read without the judge
(READ-02, READ-03, TERM-04's LinkedIn part, NAV-06, NAV-10); every page
settles before it is read and an empty or unsure read is taken again
(NAV-01, NAV-02, NAV-03, study G5); a cookie banner is declined, never
accepted, and is chrome to the extractor (study G1); an entry click follows
whatever it did first (NAV-11, study G15). Then the SP1 review's carry-over
(N1 to N6).

Headless Chromium through the module-scoped test browser; the fixtures are
served by the flow server, LinkedIn and the application's fake host are
routed; the judge is `FakeJev`, `NoisyJev` or a scripted subclass. No
network."""
import dataclasses
import io
import json
import re
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_fill  # noqa: E402
import apply_form  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_linkedin  # noqa: E402
import apply_queue  # noqa: E402
import apply_run  # noqa: E402
import apply_trace  # noqa: E402
import ats_accounts  # noqa: E402
import jev  # noqa: E402
from apply_judge import FillPlan, PlannedField  # noqa: E402

pytest_plugins = ["conftest_browser"]

EZ = apply_run.EASY_APPLY_REASON
FORMS = REPO / "tests" / "fixtures" / "forms"
CAREERS = "https://careers.fabrikam.example"
LINKEDIN_JOB = "https://www.linkedin.com/jobs/view/4438751519/"


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


def _enqueue(folder, url, jid="42", **kw):
    apply_queue.enqueue(apply_queue.new_entry(jid, company="Fabrikam", title="Analytics Engineer",
                                              apply_url=url, **kw))
    apply_queue.set_artifacts(jid, {"folder": str(folder), "apply_md": str(folder / "apply.md"),
                                    "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")})


def _entry(jid="42"):
    return next(e for e in apply_queue.load()["jobs"] if e["job_posting_id"] == jid)


def _runner(context, tmp_path, judge=None, **settings):
    base = {"auto_apply_submit": True, "auto_apply_headless": True,
            "auto_apply_jev_mode": "fake", "auto_apply_batch_cap": 10,
            "auto_apply_generate": True}
    base.update(settings)
    return apply_run.Runner(jev=judge or jev.FakeJev(), profile_dir=tmp_path / "profile",
                            settings=base, context=context,
                            run_context={"signup_email": h.SIGNUP_EMAIL,
                                         "inbox_url": "https://mail.example.com/inbox"},
                            sleep=lambda s: None)


def _drain(context, tmp_path, url, judge=None, **settings):
    """One job at `url` through the runner, with the harness's action
    recorder on: (the outcome, the recorder)."""
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, url)
    rec = h.Recorder(None, park_mode=not settings.get("auto_apply_submit", True))
    with rec.recording():
        out = _runner(context, tmp_path, judge, **settings).drain(cap=1)[0]
    return out, rec


def _flow(name, _browser, flow_server, tmp_path, judge=None, judge_name="fake"):
    return h.run_flow(h.flow(name), judge or jev.FakeJev(), judge_name, browser=_browser,
                      server=flow_server, workdir=tmp_path)


def _pages(trace_dir) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(Path(trace_dir).glob("page-*.json"),
                            key=lambda p: int(p.stem.split("-")[1]))]


def _decisions(trace_dir, what):
    return [e for p in _pages(trace_dir) for e in p["events"]
            if e["kind"] == "decision" and e["what"] == what]


def _serve(context, pages: dict, host=CAREERS):
    """`pages` (path -> html) on `host`; any other path is a blank page."""
    def _handle(route):
        path = "/" + route.request.url.split(host + "/", 1)[1].split("?")[0]
        route.fulfill(body=pages.get(path, "<body>gone</body>"), content_type="text/html")
    context.route(f"{host}/**", _handle)


def _codes(breaks):
    return sorted({b.split(":")[0] for b in breaks})


# === Easy Apply (EZ-01, EZ-02) ==================================================================

def test_an_easy_apply_entry_ends_at_once_and_opens_no_page(context, flow_server, tmp_path):
    # LinkedIn is routed to the fixture all the same, so a run that did open
    # the page could never reach the real site
    for glob, body in h.linkedin_job_routes()(flow_server.base).items():
        context.route(glob, h._fulfiller(body))
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, LINKEDIN_JOB, is_easy_apply=True)
    opened = []
    context.on("page", lambda p: opened.append(p))
    out = _runner(context, tmp_path).drain(cap=1)[0]
    assert (out.status, out.reason, out.pages) == ("needs_human", EZ, 0), out
    assert opened == [] and context.pages == []
    entry = _entry()
    assert entry["status"] == "needs_human"
    assert entry["tab_note"] == apply_run.EASY_APPLY_NOTE
    assert "apply on LinkedIn yourself" in entry["tab_note"]


def test_the_easy_apply_entry_flow_holds_every_invariant(_browser, flow_server, tmp_path):
    r = _flow("linkedin_easy_apply_flag", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks and r.actions == [], r


@pytest.mark.parametrize("flag, easy", [(True, True), ("True", True), ("yes", True), ("1", True),
                                        (False, False), ("", False), (None, False),
                                        ("false", False)])
def test_the_easy_apply_flag_reads_a_legacy_text_value(flag, easy):
    assert apply_run._easy_apply({"is_easy_apply": flag}) is easy


def test_the_queue_still_takes_an_easy_apply_job():
    apply_queue.enqueue(apply_queue.new_entry("9", apply_url=LINKEDIN_JOB, is_easy_apply=True))
    e = _entry("9")
    assert (e["status"], e["is_easy_apply"]) == ("queued", True)


@pytest.mark.parametrize("name", ["linkedin_easy_apply", "linkedin_easy_apply_modal"])
def test_an_easy_apply_page_parks_before_any_click_and_fills_nothing(
        _browser, flow_server, tmp_path, name):
    r = _flow(name, _browser, flow_server, tmp_path)
    assert (r.status, r.reason) == ("needs_human", EZ), r
    assert r.ok and not r.breaks and r.sends == 0
    assert r.actions == [], r.actions            # no click, fill, tick, pick or upload at all
    decision = _decisions(r.trace, "linkedin_handler")[0]
    assert decision["found"] == ("easy_apply" if name == "linkedin_easy_apply" else "form_dialog")


def test_no_entry_choice_ever_picks_an_easy_apply_control():
    d = apply_form.FormDigest("jobs.example", "Job", "About the role", buttons=[
        apply_form.Button(0, (0, "#e"), "Easy Apply", ""),
        apply_form.Button(1, (0, "#a"), "Apply", "")])
    assert apply_run.fieldless_apply_choice(d) == 1
    assert apply_run.posting_entry_choice(d, FillPlan(buttons={"apply_entry": (0, 0.95)})) == \
        (1, "fieldless_text")
    only = apply_form.FormDigest("jobs.example", "Job", "", buttons=[d.buttons[0]])
    assert apply_run.fieldless_apply_choice(only) is None


_LINKEDIN_FORM = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>Apply to Fabrikam</h1>
<label>First name * <input id="first" name="first" required></label>
<label>Email * <input id="email" type="email" name="email" required></label>
<button type="button" id="next">Next</button>
<button type="button" id="send" onclick="document.body.dataset.submitted = 1">Submit application</button>
</body></html>"""


def _linkedin_unit_run(context, tmp_path, url="https://ca.linkedin.com/jobs/search/?keywords=x"):
    """A `_JobRun` whose page is a form on LinkedIn (a country subdomain)."""
    host = "https://" + url.split("/")[2]
    context.route(f"{host}/**", lambda route: route.fulfill(body=_LINKEDIN_FORM,
                                                            content_type="text/html"))
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, url)
    run = apply_run._JobRun(_runner(context, tmp_path), context, _entry())
    run._prepare()
    run.page = context.new_page()
    run.page.goto(url)
    run._new_page_record("application_form", 1.0)
    return run


@pytest.mark.parametrize("step", ["_application_form", "_review_page", "_code_gate",
                                  "_submit_gate"])
def test_no_form_step_runs_on_linkedin(context, tmp_path, step):
    run = _linkedin_unit_run(context, tmp_path)
    digest = apply_form.extract(run.page)
    plan = FillPlan(fields=[PlannedField(n=0, locator=(0, "#first"), label="First name",
                                         required=True, fact_key="first_name", value="Jane",
                                         option=None, confidence=1.0, action="fill")],
                    buttons={"submit": (1, 0.99), "advance": (0, 0.99)})
    args = {"_application_form": (digest, {}, plan, run.pages[-1]),
            "_review_page": (digest, {}, plan, run.pages[-1]),
            "_code_gate": (digest, plan, run.pages[-1]),
            "_submit_gate": (digest, plan, [], run.pages[-1])}[step]
    rec = h.Recorder(None)
    with rec.recording(), pytest.raises(apply_run._Parked) as parked:
        getattr(run, step)(*args)
    assert (parked.value.status, parked.value.reason) == ("needs_human", EZ)
    # the recorder notes the gate's own entry when the test calls it; nothing
    # is clicked, filled or sent after it
    assert [a for a in rec.actions if a.kind != "gate"] == []
    assert run.page.locator("#first").input_value() == ""
    assert run.page.locator("body[data-submitted]").count() == 0


def test_a_posting_the_judge_reads_elsewhere_on_linkedin_is_not_clicked(context, tmp_path):
    # only the handler clicks on LinkedIn, and only a job page's offsite Apply:
    # an "Apply now" on a search page the judge reads as a posting stays unclicked
    body = (FORMS / "job_posting.html").read_text(encoding="utf-8")
    context.route("https://www.linkedin.com/**",
                  lambda route: route.fulfill(body=body, content_type="text/html"))
    out, rec = _drain(context, tmp_path, "https://www.linkedin.com/jobs/search/?keywords=x")
    assert out.reason.startswith(apply_linkedin.NO_APPLY_REASON + " (read as a posting"), out
    assert [a for a in rec.actions if a.kind == "click"] == []


def test_a_form_the_judge_reads_on_linkedin_parks_with_the_easy_apply_reason(context, tmp_path):
    context.route("https://www.linkedin.com/**",
                  lambda route: route.fulfill(body=_LINKEDIN_FORM, content_type="text/html"))
    out, rec = _drain(context, tmp_path, "https://www.linkedin.com/jobs/search/?keywords=x")
    assert (out.status, out.reason) == ("needs_human", EZ), out
    assert [a for a in rec.actions if h.on_linkedin(a.url)] == []


# === the LinkedIn job page handler (READ-02, READ-03, TERM-04, NAV-06, NAV-10) ================

@pytest.mark.parametrize("url, kind", [
    ("https://www.linkedin.com/jobs/view/4438751519/", "job"),
    ("https://www.linkedin.com/jobs/view/analytics-engineer-at-fabrikam-4438751519", "job"),
    ("https://www.linkedin.com/comm/jobs/view/4438751519/?trk=eml", "job"),
    ("https://www.linkedin.com/jobs/search/?currentJobId=4438751519&keywords=x", "job"),
    ("https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4438751519", "job"),
    ("https://ca.linkedin.com/jobs/view/4438751519/", "job"),
    ("https://linkedin.com/jobs/view/4438751519/", "job"),
    ("https://www.linkedin.com/jobs/search/?keywords=analytics", "other"),
    ("https://www.linkedin.com/feed/", "other"),
    ("https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fcareers.example", "redirector"),
    ("https://www.linkedin.com/login?session_redirect=x", "signed_out"),
    ("https://www.linkedin.com/uas/login", "signed_out"),
    ("https://www.linkedin.com/checkpoint/challenge/abc", "signed_out"),
    ("https://www.linkedin.com/authwall?trk=x", "signed_out"),
    ("https://www.linkedin.com/signup/cold-join", "signed_out"),
    ("https://www.linkedin.com/loginout-news/", "other"),
    ("https://careers.fabrikam.example/jobs/view/1", ""),
    ("https://linkedin.com.evil.example/jobs/view/1", "")])
def test_linkedin_url_kinds(url, kind):
    assert apply_linkedin.url_kind(url) == kind


_C = apply_linkedin.Control
_V = apply_linkedin.View


@pytest.mark.parametrize("view, kind", [
    (_V(form_dialog=3, offsite=[_C("#a", "Apply")], applied="Applied"), "form_dialog"),
    (_V(applied="Applied 3 days ago", offsite=[_C("#a", "Apply")]), "applied"),
    (_V(closed="No longer accepting applications", offsite=[_C("#a", "Apply")]), "closed"),
    (_V(offsite=[_C("#a", "Apply")], easy=[_C("#e", "Easy Apply")], signin_dialog=True),
     "offsite"),
    (_V(easy=[_C("#e", "", aria="Easy Apply to Analytics Engineer at Fabrikam")]), "easy_apply"),
    (_V(signin_dialog=True), "signed_out"),
    (_V(password_box=True), "signed_out"),
    (_V(), "none")])
def test_the_handler_decides_in_its_order(view, kind):
    d = apply_linkedin.decide(view)
    assert d.kind == kind
    assert d.final is (kind not in ("signed_out", "none"))


def _read(context, url):
    page = context.new_page()
    page.goto(url)
    return page, apply_linkedin.read(page)


def test_the_handler_reads_the_top_cards_apply_whatever_else_the_page_holds(context,
                                                                             flow_server):
    page, view = _read(context, flow_server.url("linkedin_posting_noise.html"))
    assert [c.label for c in view.offsite] == ["Apply"] and view.easy == []
    assert view.offsite[0].aria == "Apply on company website"
    assert view.applied == ""               # the right rail's "Applied" is another job's card
    assert not view.password_box and not view.signin_dialog and view.form_dialog == 0
    assert apply_linkedin.decide(view).kind == "offsite"
    # the noise the old shortcut stopped at is on the page: boxes and a Submit
    digest = apply_form.extract(page)
    assert len(digest.fields) >= 2
    assert any("Submit" in b.text for b in digest.buttons)


@pytest.mark.parametrize("html, offsite, easy", [
    ('<main><h1>Engineer</h1><button aria-label="Easy Apply to Engineer at Fabrikam">'
     '<svg width="16" height="16"><rect width="16" height="16"></rect></svg></button></main>',
     0, 1),
    ("<main><h1>Engineer</h1><button>Easy Apply</button></main>", 0, 1),
    ('<main><h1>Engineer</h1><a href="/x" aria-label="Apply to Engineer on company website">'
     'Apply</a></main>', 1, 0),
    ('<main><h1>Engineer</h1><a href="/x">Apply now</a></main>', 1, 0),
    ('<main><h1>Engineer</h1><a href="/y">Data Engineer Contoso New York, United States '
     '(On-site) Easy Apply</a></main>', 0, 0),
    ("<header><button>Apply</button></header><main><h1>Engineer</h1></main>", 0, 0),
    ('<main><h1>Engineer</h1><div role="dialog"><button>Apply</button></div></main>', 0, 0)])
def test_the_handler_tells_an_offsite_apply_from_easy_apply(context, html, offsite, easy):
    page = context.new_page()
    page.set_content(html)
    view = apply_linkedin.read(page)
    assert (len(view.offsite), len(view.easy)) == (offsite, easy), view


@pytest.mark.parametrize("fixture, kind, mark", [
    ("linkedin_applied.html", "applied", "Applied 3 days ago"),
    ("linkedin_closed.html", "closed", "No longer accepting applications"),
    ("linkedin_signed_out.html", "signed_out", ""),
    ("linkedin_easy_apply_modal.html", "form_dialog", ""),
    ("linkedin_easy_apply.html", "easy_apply", ""),
    ("linkedin_posting.html", "offsite", "")])
def test_the_handler_reads_each_linkedin_fixture(context, flow_server, fixture, kind, mark):
    _, view = _read(context, flow_server.url(fixture))
    d = apply_linkedin.decide(view)
    assert d.kind == kind, (d, view)
    assert mark in (view.applied or view.closed or "")


def test_a_top_card_that_renders_late_is_waited_for(_browser, flow_server, tmp_path):
    r = _flow("linkedin_posting_late", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    decision = _decisions(r.trace, "linkedin_handler")[0]
    assert decision["found"] == "offsite" and decision["waited_ms"] >= 2000, decision


def test_stray_controls_beside_the_offsite_apply_do_not_stop_it(_browser, flow_server,
                                                                tmp_path):
    r = _flow("linkedin_posting_noise", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    li = [a for a in r.actions if h.on_linkedin(a.url)]
    assert [(a.kind, a.text) for a in li] == [("click", "Apply")], li


@pytest.mark.parametrize("seed", [None, 1, 2, 3, 4, 5])
def test_the_gts_shape_reaches_the_company_form_whatever_the_judge_reads(
        _browser, flow_server, tmp_path, seed):
    judge = jev.FakeJev() if seed is None else jev.NoisyJev(jev.FakeJev(), seed)
    r = _flow("linkedin_gts_other", _browser, flow_server, tmp_path, judge,
              "fake" if seed is None else f"noisy-{seed}")
    assert r.ok and not r.breaks, r
    first = _pages(r.trace)[0]
    assert first["url"] == LINKEDIN_JOB and first["page_state"] is None   # never judged


def test_a_linkedin_posting_the_judge_reads_signed_out_or_closed_still_reaches_the_form(
        _browser, flow_server, tmp_path):
    class _ReadsSignedOut(jev.FakeJev):
        def judge(self, state, questions):
            out = super().judge(state, questions)
            if "page_state" in out and state["page"]["url_host"] == "www.linkedin.com":
                out["page_state"] = jev.Answer(kind="choice", choice="login_wall",
                                               probabilities={"login_wall": 0.9},
                                               confidence=0.9)
            return out
    r = _flow("linkedin_posting_noise", _browser, flow_server, tmp_path, _ReadsSignedOut(),
              "signed-out")
    assert r.ok and not r.breaks, r


def test_a_job_linkedin_shows_as_applied_is_not_applied_to_again(_browser, flow_server,
                                                                 tmp_path):
    r = _flow("linkedin_applied", _browser, flow_server, tmp_path)
    assert r.reason == f"{apply_linkedin.APPLIED_REASON} (Applied 3 days ago)", r
    assert r.ok and not r.breaks and r.actions == []


def test_a_closed_posting_parks_with_its_own_reason(_browser, flow_server, tmp_path):
    r = _flow("linkedin_closed", _browser, flow_server, tmp_path)
    assert r.reason == f"{apply_linkedin.CLOSED_REASON} (No longer accepting applications)", r
    assert r.ok and not r.breaks and r.actions == []


def test_a_signed_out_page_without_an_offsite_apply_asks_for_a_sign_in(
        context, flow_server, tmp_path):
    for glob, body in h.linkedin_job_routes("linkedin_signed_out.html")(flow_server.base).items():
        context.route(glob, h._fulfiller(body))
    out, rec = _drain(context, tmp_path, LINKEDIN_JOB)
    assert (out.status, out.reason) == ("needs_human", "LinkedIn is signed out (a sign-in dialog)")
    assert _entry()["tab_note"] == apply_run.LINKEDIN_LOGIN_NOTE
    assert rec.actions == []


def test_a_signed_out_page_that_still_shows_an_offsite_apply_goes_through_it(
        context, flow_server, tmp_path):
    body = (FORMS / "linkedin_signed_out.html").read_text(encoding="utf-8").replace(
        '<button type="button">Sign in to apply</button>',
        f'<a aria-label="Apply on company website" href="{flow_server.url("lever_single.html")}">'
        'Apply</a>').replace("top: 60px; left: 50%;", "top: 600px; left: 70%;")
    context.route("https://www.linkedin.com/**",
                  lambda route: route.fulfill(body=body, content_type="text/html"))
    out, rec = _drain(context, tmp_path, LINKEDIN_JOB, auto_apply_submit=False)
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out
    assert not h.invariant_breaks(out, rec, h.Sends(rec))


@pytest.mark.parametrize("path, words", [
    ("/login?session_redirect=%2Fjobs%2Fview%2F1", "the login page"),
    ("/checkpoint/challenge/abc", "a checkpoint page"),
    ("/authwall?trk=bf", "the authwall")])
def test_a_signed_out_linkedin_url_parks_at_once(context, tmp_path, path, words):
    context.route("https://www.linkedin.com/**", lambda route: route.fulfill(
        body='<body><h1>Sign in</h1><label>Email <input name="session_key"></label>'
             '<label>Password <input type="password" name="session_password"></label>'
             '<button>Sign in</button></body>', content_type="text/html"))
    out, rec = _drain(context, tmp_path, "https://www.linkedin.com" + path)
    assert (out.status, out.reason) == ("needs_human", f"LinkedIn is signed out ({words})"), out
    assert _entry()["tab_note"] == apply_run.LINKEDIN_LOGIN_NOTE
    assert rec.actions == []


def test_the_safety_interstitial_is_followed_through_its_continue(_browser, flow_server,
                                                                  tmp_path):
    r = _flow("linkedin_safety_interstitial", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    entry = next(e for p in _pages(r.trace) for e in p["events"] if e["kind"] == "apply_entry")
    assert entry["popup"] is True and entry["destination"].endswith("/forms/lever_single.html")
    li = [(a.kind, a.text) for a in r.actions if h.on_linkedin(a.url)]
    assert li == [("click", "Apply"), ("click", "Continue")], li


def test_an_offsite_apply_that_opens_nothing_parks_after_its_second_click(context, tmp_path):
    body = (FORMS / "linkedin_posting.html").read_text(encoding="utf-8").replace(
        'href="linkedin_redirect.html" target="_blank" rel="opener"',
        'href="#" onclick="return false"')
    context.route("https://www.linkedin.com/**",
                  lambda route: route.fulfill(body=body, content_type="text/html"))
    out, rec = _drain(context, tmp_path, LINKEDIN_JOB)
    assert out.reason == ("the offsite Apply (Apply) did not open the company's site (clicked "
                          "twice)"), out
    assert [a.text for a in rec.actions if a.kind == "click"] == ["Apply"]


def test_a_linkedin_country_subdomain_is_linkedin(_browser, flow_server, tmp_path):
    f = h.Flow("linkedin_ca", "https://ca.linkedin.com/jobs/view/4438751519/", False,
               "ready_to_submit", h._PARKED, confirm="#thanks:visible", gate="#btn-submit:visible",
               routes=h.linkedin_job_routes("linkedin_posting.html", "lever_single.html",
                                            host="ca.linkedin.com"))
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, r


def test_a_linkedin_subdomain_the_job_did_not_start_on_is_linkedin_too(context, tmp_path):
    # the job starts on www.linkedin.com; a hop through a country subdomain is
    # LinkedIn's own page, never "left the allowed sites"
    run = _linkedin_unit_run(context, tmp_path, url="https://www.linkedin.com/jobs/search/?x=1")
    assert run._allowed_site("uk.linkedin.com") and run._allowed_site("www.linkedin.com")
    run._check_host("https://uk.linkedin.com/safety/go/?url=x")
    assert not run._allowed_site("linkedin.com.evil.example")
    assert not run._password_ok("uk.linkedin.com")
    assert not run._password_ok("www.linkedin.com")


# === settling (NAV-01, NAV-02, NAV-03, study G5) ================================================

def test_the_settle_holds_while_a_skeleton_shows(context, monkeypatch):
    monkeypatch.setattr(apply_fill, "SETTLE_QUIET_S", 0.3)
    monkeypatch.setattr(apply_fill, "SETTLE_MAX_S", 6.0)
    page = context.new_page()
    page.set_content('<div class="card-skeleton" aria-busy="true" style="height:120px">'
                     'Loading</div><script>setTimeout(() => document.querySelector('
                     '".card-skeleton").remove(), 1500)</script>')
    start = time.monotonic()
    info = apply_fill.settle(page, 5)
    took = time.monotonic() - start
    assert took >= 1.4, took
    assert info["busy"] and not info["capped"], info


def test_a_placeholder_below_the_fold_does_not_hold_the_settle(context, monkeypatch):
    monkeypatch.setattr(apply_fill, "SETTLE_QUIET_S", 0.3)
    monkeypatch.setattr(apply_fill, "SETTLE_MAX_S", 6.0)
    page = context.new_page()
    page.set_content('<p>Top</p><div style="margin-top:3000px" class="skeleton" '
                     'aria-busy="true">Loading more</div>')
    start = time.monotonic()
    info = apply_fill.settle(page, 5)
    assert time.monotonic() - start < 1.5 and not info["busy"], info


def test_a_placeholder_that_never_clears_is_released_at_the_cap(context, monkeypatch):
    monkeypatch.setattr(apply_fill, "SETTLE_QUIET_S", 0.3)
    monkeypatch.setattr(apply_fill, "SETTLE_MAX_S", 1.0)
    page = context.new_page()
    page.set_content('<div aria-busy="true" style="height:40px">Loading</div>')
    start = time.monotonic()
    info = apply_fill.settle(page, 5)
    assert 0.9 <= time.monotonic() - start < 3.0 and info["capped"], info


def test_a_page_that_renders_after_load_is_read_once_it_renders(_browser, flow_server,
                                                                tmp_path):
    r = _flow("spa_late_render", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    reread = _decisions(r.trace, "reread_after_settle")
    assert reread and reread[0]["still_empty"] is False, reread
    assert _pages(r.trace)[0]["state"] == "job_posting"


class _FirstReadUnsure(jev.FakeJev):
    """The fake, reading the first page it sees as `other` at 0.30 once (a
    read taken mid-render), then as the fake does."""

    def __init__(self):
        self.page_reads = 0

    def judge(self, state, questions):
        out = super().judge(state, questions)
        if "page_state" in out:
            self.page_reads += 1
            if self.page_reads == 1:
                out["page_state"] = jev.Answer(
                    kind="choice", choice="other", confidence=0.30,
                    probabilities={"other": 0.30, "job_posting": 0.25})
        return out


def test_an_unsure_read_is_taken_once_more_after_a_settle(context, flow_server, tmp_path):
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, flow_server.url("job_posting.html"))
    judge = _FirstReadUnsure()
    out = _runner(context, tmp_path, judge, auto_apply_submit=False).drain(cap=1)[0]
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out
    trace = folder / "apply_trace" / "attempt-1"
    reread = _decisions(trace, "reread_unsure")[0]
    assert reread["first_reads"] == "other 0.30, job_posting 0.25"
    assert reread["now"] == "job_posting 1.00"
    assert _pages(trace)[0]["state"] == "job_posting"


_HANGING = (FORMS / "lever_single.html").read_text(encoding="utf-8").replace(
    "<h1>Business Analyst</h1>", '<h1>Business Analyst</h1><img src="/slow.png" alt="">')


def test_the_first_load_does_not_wait_for_a_load_event_that_never_fires(context, tmp_path,
                                                                        monkeypatch):
    # an image that never answers keeps `load` from firing; the page is there
    # (the later route wins, so the image's comes second). A screenshot waits
    # for `load` too, so the trace's are given a short timeout here.
    monkeypatch.setattr(apply_trace, "SCREENSHOT_TIMEOUT_MS", 500)
    _serve(context, {"/apply/42": _HANGING})
    context.route(f"{CAREERS}/slow.png", lambda route: None)
    start = time.monotonic()
    out, _ = _drain(context, tmp_path, f"{CAREERS}/apply/42", auto_apply_submit=False)
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out
    assert time.monotonic() - start < 25


def test_a_first_load_the_network_dropped_is_retried_once(context, tmp_path):
    calls = []

    def _flaky(route):
        calls.append(1)
        if len(calls) == 1:
            route.abort("connectionreset")
        else:
            route.fulfill(body=(FORMS / "lever_single.html").read_text(encoding="utf-8"),
                          content_type="text/html")
    context.route(f"{CAREERS}/apply/42", _flaky)
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, f"{CAREERS}/apply/42")
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out
    assert len(calls) == 2
    assert _decisions(folder / "apply_trace" / "attempt-1", "goto_retry")


_LATE_SIGNUP = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Create an account - Fabrikam Careers</title></head><body>
<div id="app"></div>
<script>
  window.addEventListener('load', function () {
    setTimeout(function () {
      document.getElementById('app').innerHTML = __BODY__;
      document.getElementById('btn-create').addEventListener('click', function () {
        var p = document.getElementById('signup_password').value;
        if (!p || p !== document.getElementById('signup_confirm').value) { return; }
        window.location.href = 'ashby_steps.html';
      });
    }, 800);
  });
</script></body></html>"""


def test_the_sign_up_page_behind_a_create_account_link_is_read_once_it_renders(
        context, flow_server, tmp_path, monkeypatch):
    # NAV-03: the sign-up page renders its form after `load`; it is read once
    # it holds still (a quiet window that outlasts the 800 ms render)
    monkeypatch.setattr(apply_fill, "SETTLE_QUIET_S", 1.2)
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: h.PASSWORD)
    signup = (FORMS / "signup.html").read_text(encoding="utf-8")
    inner = signup.split('<body>', 1)[1].split("<script>", 1)[0].strip()
    body = _LATE_SIGNUP.replace("__BODY__", json.dumps(inner))
    context.route("**/forms/signup.html",
                  lambda route: route.fulfill(body=body, content_type="text/html"))
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, flow_server.url("login_two_signup_links.html"))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    pages = _pages(folder / "apply_trace" / "attempt-1")
    assert [p["state"] for p in pages[:2]] == ["login_wall", "signup_form"]
    assert any(e["kind"] == "decision" and e["what"] == "settled" for e in pages[1]["events"])


# === consent banners (study G1) =================================================================

def test_a_cookie_dialog_over_the_posting_is_declined_and_never_accepted(
        _browser, flow_server, tmp_path):
    r = _flow("consent_overlay", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    clicks = [a.text for a in r.actions if a.kind == "click"]
    assert clicks[0] == "Reject non-essential", clicks
    assert not any(re.search(r"accept|allow|agree", t, re.I) for t in clicks), clicks
    dismissed = _decisions(r.trace, "consent_dismissed")[0]
    assert dismissed["text"] == "Reject non-essential" and dismissed["error"] == ""


_BANNER = ('<div id="{id}" class="{cls}" style="position:fixed;bottom:0;left:0;right:0;'
           'background:#eee;padding:8px">{text}{controls}</div>')
_PAGE = "<main><h1>Analytics Engineer</h1><p>Own the analytics models.</p>{banner}</main>"


@pytest.mark.parametrize("banner, expected", [
    # a reject control wins, an input of type button is read by its value
    (_BANNER.format(id="onetrust-banner-sdk", cls="", text="We use cookies.",
                    controls="<button>Accept All Cookies</button><button>Reject All</button>"),
     ("reject", "Reject All")),
    (_BANNER.format(id="cc", cls="cookie-bar", text="This site uses cookies.",
                    controls='<button>Accept</button><input type="button" '
                             'value="Decline all non-necessary cookies">'),
     ("reject", "Decline all non-necessary cookies")),
    (_BANNER.format(id="cc", cls="cookie-bar", text="Cookies help us.",
                    controls="<button>Allow all</button><button>Use necessary cookies only"
                             "</button>"),
     ("reject", "Use necessary cookies only")),
    # no reject: the close control, by its text or its aria-label
    (_BANNER.format(id="cc", cls="cookie-notice", text="We use cookies.",
                    controls='<button>Accept</button><button aria-label="Close">x</button>'),
     ("close", "x")),
    # accept, allow or agree never, whatever else the text says
    (_BANNER.format(id="cc", cls="cookie-notice", text="We use cookies.",
                    controls="<button>Accept</button><button>Allow necessary only</button>"
                             "<button>I agree</button>"),
     None),
    # a dialog that mentions cookies counts, whatever its id
    ('<div role="dialog" style="position:fixed;top:0">Your privacy. We and our partners use '
     'cookies.<button>Accept</button><button>Refuse</button></div>', ("reject", "Refuse")),
    # a hidden banner is left alone
    (_BANNER.format(id="onetrust-banner-sdk", cls="", text="We use cookies.",
                    controls="<button>Reject All</button>").replace("position:fixed",
                                                                    "display:none"), None),
    # a consent block of the form itself is no banner
    ('<form><div class="gdpr-consent"><label><input type="checkbox" required> I consent to '
     'the processing of my personal data</label><button>Decline</button></div></form>', None)])
def test_the_consent_control_is_a_reject_or_a_close_and_never_an_accept(context, banner,
                                                                        expected):
    page = context.new_page()
    page.set_content(_PAGE.format(banner=banner))
    found = apply_form.consent_control(page)
    got = (found[1]["kind"], found[1]["text"]) if found else None
    assert got == expected, found


def test_the_extractor_treats_a_consent_banner_as_chrome(context):
    page = context.new_page()
    page.set_content(
        '<div id="onetrust-banner-sdk" style="position:fixed;top:0;left:0;right:0;'
        'background:#eee">We use cookies to improve your experience and for marketing.'
        '<label><input type="checkbox" id="perf"> Performance cookies</label>'
        '<button>Accept All Cookies</button><button>Reject All</button></div>'
        '<main style="margin-top:120px"><h1>Apply for Analytics Engineer</h1>'
        '<label>First name * <input name="first" required></label>'
        '<button type="button">Continue</button></main>')
    d = apply_form.extract(page)
    assert [f.label for f in d.fields] == ["First name"]
    assert [b.text for b in d.buttons] == ["Continue"]
    assert d.text.startswith("Apply for Analytics Engineer"), d.text
    assert d.text.rstrip().endswith("Reject All"), d.text


def test_a_consent_checkbox_inside_the_application_form_is_kept(context):
    page = context.new_page()
    page.set_content(
        '<form><h1>Apply</h1><label>Email * <input type="email" name="email" required></label>'
        '<div class="gdpr-consent" id="consent-block"><label><input type="checkbox" '
        'name="gdpr" required> I consent to the processing of my personal data</label></div>'
        '<button type="submit">Submit application</button></form>')
    d = apply_form.extract(page)
    assert [f.type for f in d.fields] == ["email", "checkbox"]
    assert [b.text for b in d.buttons] == ["Submit application"]


_BANNER_AGAIN = r"""<body><h1>Analytics Engineer</h1><p>About the role. Responsibilities.
Qualifications.</p><div id="host"></div>
<script>
  function bar() {
    document.getElementById('host').innerHTML = '<div id="cookie-bar" ' +
      'style="position:fixed;bottom:0">We use cookies.<button id="no">Reject all</button></div>';
    document.getElementById('no').addEventListener('click', function () {
      document.getElementById('host').innerHTML = '';
      setTimeout(bar, 50);
    });
  }
  bar();
</script></body>"""


def test_a_banner_that_comes_back_is_dismissed_at_most_the_limit(context, tmp_path, monkeypatch):
    monkeypatch.setattr(apply_run, "CONSENT_MAX", 2)
    _serve(context, {"/jobs/42": _BANNER_AGAIN})
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, f"{CAREERS}/jobs/42")
    run = apply_run._JobRun(_runner(context, tmp_path), context, _entry())
    run._prepare()
    run.page = context.new_page()
    run.page.goto(f"{CAREERS}/jobs/42")
    rec = h.Recorder(None)
    with rec.recording():
        for _ in range(4):
            run._dismiss_consent()
    clicks = [a.text for a in rec.actions if a.kind == "click"]
    assert clicks == ["Reject all", "Reject all"], clicks
    assert run.page.locator("#cookie-bar").count() == 1      # back again, and left alone


# === entry clicks (NAV-11, study G15) ===========================================================

_ENTRY = """<body><h1>Analytics Engineer</h1>
<a id="same" href="/apply">Apply</a>
<a id="tab" href="/apply" target="_blank">Apply in a tab</a>
<button id="inplace" onclick="document.body.insertAdjacentHTML('beforeend',
  '<form><label>First name <input name=first></label></form>')">Apply here</button>
<button id="dead">Apply later</button></body>"""


@pytest.mark.parametrize("which, signal, under_s", [
    ("#same", "navigation", 2.0), ("#inplace", "dom", 2.0), ("#tab", "popup", 2.0)])
def test_an_entry_click_follows_whatever_it_did_first(context, monkeypatch, which, signal,
                                                     under_s):
    monkeypatch.setattr(apply_run, "POPUP_TIMEOUT_MS", 5_000)
    monkeypatch.setattr(apply_run, "POPUP_GRACE_S", 0.5)
    _serve(context, {"/jobs/1": _ENTRY, "/apply": "<body><h1>Apply</h1></body>"})
    page = context.new_page()
    page.goto(f"{CAREERS}/jobs/1")
    start = time.monotonic()
    popup, got, waited = apply_run.click_entry(page, page.locator(which))
    took = time.monotonic() - start
    assert got == signal, (got, waited)
    assert took < under_s, took
    assert (popup is not None) is (signal == "popup")


def test_an_entry_click_that_does_nothing_waits_the_whole_window(context, monkeypatch):
    monkeypatch.setattr(apply_run, "POPUP_TIMEOUT_MS", 800)
    _serve(context, {"/jobs/1": _ENTRY})
    page = context.new_page()
    page.goto(f"{CAREERS}/jobs/1")
    popup, got, waited = apply_run.click_entry(page, page.locator("#dead"))
    assert (popup, got) == (None, "none") and waited >= 800


def test_an_entry_click_an_overlay_takes_is_reported_failed(context, monkeypatch):
    monkeypatch.setattr(apply_fill, "ACTION_TIMEOUT_MS", 500)
    page = context.new_page()
    page.set_content('<a id="apply" href="/x">Apply</a><div style="position:fixed;inset:0;'
                     'background:rgba(0,0,0,.3)"></div>')
    popup, got, _ = apply_run.click_entry(page, page.locator("#apply"))
    assert popup is None and got.startswith("failed: "), got


# === the invariant: nothing is filled, uploaded or gated on LinkedIn ===============================

_LINKEDIN_FLOWS = [f.name for f in h.FLOWS if f.name.startswith("linkedin_")]


@pytest.mark.parametrize("name", _LINKEDIN_FLOWS)
def test_no_linkedin_flow_fills_ticks_picks_uploads_or_gates_on_linkedin(
        _browser, flow_server, tmp_path, name):
    r = _flow(name, _browser, flow_server, tmp_path)
    on_li = [a for a in r.actions if h.on_linkedin(a.url)]
    assert not [a for a in on_li if a.kind != "click"], on_li
    assert {a.text for a in on_li} <= {"Apply", "Continue"}, on_li
    assert not [b for b in r.breaks if b.startswith(("LINKEDIN-", "EASY-APPLY"))], r.breaks
    assert r.ok, r


@pytest.mark.parametrize("plant, code", [
    ("easy_apply_text", "EASY-APPLY-CLICK"), ("easy_apply_aria", "EASY-APPLY-CLICK")])
def test_an_easy_apply_click_is_an_invariant_break(plant, code):
    rec = h.Recorder(h.flow("linkedin_easy_apply"))
    if plant == "easy_apply_text":
        rec.actions.append(h.Action("click", LINKEDIN_JOB, text="Easy Apply", tag="button"))
    else:
        rec.actions.append(h.Action("click", LINKEDIN_JOB, text="",
                                    aria="Easy Apply to Analytics Engineer at Fabrikam"))

    class _Out:
        status, reason = "needs_human", EZ
    assert code in _codes(h.invariant_breaks(_Out(), rec, h.Sends(rec)))


def test_a_flow_that_must_open_no_page_breaks_when_one_opens(_browser, flow_server, tmp_path):
    f = dataclasses.replace(h.flow("linkedin_easy_apply_flag"), easy_apply=False)
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert "PAGE-OPENED" in _codes(r.breaks), r


# === the SP1 review's carry-over ====================================================================

# --- N1: a login wall's evidence is the page it describes ------------------------------------------

_LOGIN = """<!doctype html><html><head><title>Sign in - Fabrikam Careers</title></head><body>
<h1>Sign in to continue your application</h1>
<label for="e">Email</label><input id="e" type="email" autocomplete="username">
<label for="p">Password</label><input id="p" type="password" autocomplete="current-password">
<button type="button">Sign in</button>
<p><a href="/register">Create an account</a></p></body></html>"""
_REGISTER = """<!doctype html><html><head><title>Talent network</title></head><body>
<h1>Join our talent network</h1><p>Hear about new roles first.</p>
<label for="k">Keywords</label><input id="k" name="keywords">
<button type="button">Search jobs</button></body></html>"""


class _ReadsByTitle(jev.FakeJev):
    """The fake, reading the sign-in page as a login wall and the page its
    create-account link leads to as a posting."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        title = state["page"]["title"]
        if "page_state" in out:
            choice = "login_wall" if title.startswith("Sign in") else "job_posting"
            out["page_state"] = jev.Answer(kind="choice", choice=choice, confidence=0.9,
                                           probabilities={choice: 0.9})
        return out


def test_a_login_wall_park_names_the_sign_up_page_with_its_own_evidence(
        context, tmp_path, monkeypatch):
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: h.PASSWORD)
    _serve(context, {"/login": _LOGIN, "/register": _REGISTER})
    out, _ = _drain(context, tmp_path, f"{CAREERS}/login", _ReadsByTitle())
    assert out.reason.startswith(f"login wall (the create-account link led to {CAREERS}/register:"
                                 " read as job_posting 0.90;"), out.reason
    assert "boxes: Keywords" in out.reason and "Search jobs" in out.reason
    assert "Password" not in out.reason          # the sign-in page's boxes are not its evidence


# --- N2: the harness exempts sign-in words as the loop does ------------------------------------------

@pytest.mark.parametrize("text, park, account, worded", [
    # the account step's own exemption (`_sends_application(account_only=True)`)
    ("Send code", False, True, False), ("Send me a link", True, True, False),
    ("Sign in to apply", False, True, False), ("Send verification code", False, True, False),
    ("Send application", False, True, True), ("Submit application", True, True, True),
    # anywhere else the loop routes a send word to the gate (M-6)
    ("Send code", False, False, True), ("Send me a link", True, False, True),
    ("Finish", False, False, True), ("Continue", True, False, False)])
def test_the_harness_exempts_sign_in_words_as_the_loop_does(text, park, account, worded):
    assert h.submit_worded(text, park_mode=park, account_step=account) is worded


# --- N3: CI reads the masked screenshots back ---------------------------------------------------------

def test_ci_installs_pillow_where_the_browser_tests_run():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = ci.split("- name: Install Playwright and Chromium", 1)[1].split("- name:", 1)[0]
    assert re.search(r"pip install [^\n]*\bPillow\b", step), step


# --- N4: an Enter inside a frame is tied to the frame's form -------------------------------------------

def test_an_enter_pressed_in_a_frame_is_read_in_that_frame(context):
    page = context.new_page()
    page.set_content('<body><iframe srcdoc="<form onsubmit=\'return false\'><input id=q '
                     'name=q></form>"></iframe></body>')
    frame = page.frames[1]
    frame.wait_for_selector("#q")
    frame.focus("#q")
    rec = h.Recorder(h.flow("ashby_wizard_park"), park_mode=True)
    with rec.recording():
        page.keyboard.press("Enter")
    press = rec.actions[-1]
    assert (press.kind, press.key, press.tag, press.form) == ("press", "Enter", "input", True)

    class _Out:
        status, reason = "ready_to_submit", "auto_apply_submit is off"
    assert "ENTER-OUTSIDE-GATE" in _codes(h.invariant_breaks(_Out(), rec, h.Sends(rec)))


# --- N5: `loop_step` says what `_loop` does ------------------------------------------------------------

class _Step(Exception):
    pass


def _category(text: str) -> tuple:
    """`loop_step`'s words as (step, detail)."""
    m = re.match(r"(?:.*; )?click the (?:Apply entry \[\d+\]|offsite Apply) '([^']*)'", text)
    if m:
        return ("click", m.group(1))
    tail = text.rsplit("; ", 1)[-1] if text.startswith(("read as", "go on")) else text
    for prefix, step in (("fill the page", "form"), ("the account step", "account"),
                         ("the code step", "code"), ("wait for the person", "captcha"),
                         ("finish: submitted", "submitted"),
                         ("follow the LinkedIn redirect", "redirect")):
        if prefix in tail:
            return (step, "")
    m = re.search(r"park: ([^(;]*)", text)
    return ("park", m.group(1).strip()) if m else ("?", text)


def _loop_category(run, monkeypatch) -> tuple:
    """What `_loop` does on its first page, stopped there."""
    def _stop(step):
        def _raise(*a, **kw):
            if step == "click":
                raise _Step(("click", a[2]))
            raise _Step((step, ""))
        return _raise
    for name, step in (("_click_entry", "click"), ("_application_form", "form"),
                       ("_review_page", "form"), ("_account_step", "account"),
                       ("_code_gate", "code"), ("_wait_for_human_check", "captcha"),
                       ("_await_destination", "redirect")):
        monkeypatch.setattr(run, name, _stop(step))
    try:
        run._loop()
    except _Step as s:
        return s.args[0]
    except apply_run._Parked as p:
        if p.status == "submitted":
            return ("submitted", "")
        return ("park", re.split(r" \(|;", p.reason, maxsplit=1)[0].strip())
    return ("?", "no step")


class _ReadAs(jev.FakeJev):
    STATE, CONF = "", 0.0

    def judge(self, state, questions):
        out = super().judge(state, questions)
        if "page_state" in out:
            out["page_state"] = jev.Answer(kind="choice", choice=self.STATE, confidence=self.CONF,
                                           probabilities={self.STATE: self.CONF,
                                                          "job_posting": 0.2})
        return out


def _read_as(state, conf):
    return type("J", (_ReadAs,), {"STATE": state, "CONF": conf})()


@pytest.mark.parametrize("where, judge, park_mode", [
    ("job_posting.html", None, False),
    ("ashby_steps.html", None, False),
    ("lever_single.html", None, True),
    ("login_wall.html", None, False),
    ("captcha.html", None, False),
    ("job_posting.html", ("other", 0.30), False),
    ("job_posting.html", ("other", 0.55), False),
    ("lever_single.html", ("login_wall", 0.90), False),
    ("confirmation.html", None, False),
    ("linkedin:linkedin_posting.html", None, False),
    ("linkedin:linkedin_posting_noise.html", ("other", 0.30), False),
    ("linkedin:linkedin_easy_apply.html", None, False),
    ("linkedin:linkedin_applied.html", None, False),
    ("linkedin:linkedin_closed.html", None, False),
    ("linkedin-other:form", ("application_form", 0.95), False),
    ("linkedin-other:posting", ("job_posting", 0.95), False)])
def test_loop_step_says_what_the_loop_does(context, flow_server, tmp_path, monkeypatch,
                                           where, judge, park_mode):
    judge_obj = _read_as(*judge) if judge else jev.FakeJev()
    if where.startswith("linkedin:"):
        routes = h.linkedin_job_routes(where.split(":", 1)[1], "lever_single.html")
        for glob, body in routes(flow_server.base).items():
            context.route(glob, h._fulfiller(body))
        url = LINKEDIN_JOB
    elif where.startswith("linkedin-other:"):
        body = _LINKEDIN_FORM if where.endswith("form") else (
            FORMS / "job_posting.html").read_text(encoding="utf-8")
        context.route("https://www.linkedin.com/**",
                      lambda route: route.fulfill(body=body, content_type="text/html"))
        url = "https://www.linkedin.com/jobs/search/?keywords=x"
    else:
        url = flow_server.url(where)
    # what `probe` prints
    page = context.new_page()
    page.goto(url)
    apply_fill.settle(page, 3)
    out = io.StringIO()
    apply_run._probe_page(page, 1, judge_obj, out, park_mode=park_mode)
    said = re.search(r"  the loop would: (.*)", out.getvalue()).group(1)
    page.close()
    # what the loop does
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, url)
    run = apply_run._JobRun(_runner(context, tmp_path, judge_obj,
                                    auto_apply_submit=not park_mode), context, _entry())
    run._prepare()
    run.page = context.new_page()
    run._open(url)
    did = _loop_category(run, monkeypatch)
    assert _category(said) == did, (said, did)


# --- N6: the trace never writes a field's value through its text fallback ---------------------------

def test_the_trace_writes_a_dataclass_or_a_valued_object_by_its_type_alone(tmp_path):
    trace = apply_trace.Trace(tmp_path, attempt=1, job_id="42")
    trace.start()
    pf = PlannedField(n=0, locator=(0, "#email"), label="Email", required=True,
                      fact_key="email", value="jane.secret@example.com", option=None,
                      confidence=1.0, action="fill")

    class _Box:
        value = "hunter2-secret"
    try:
        trace.event("odd", field=pf, box=_Box(), both={"a", "b"}, many=[pf])
    finally:
        trace.close()
    text = (tmp_path / "apply_trace" / "attempt-1" / "run.json").read_text(encoding="utf-8")
    row = json.loads(text)["setup_events"][-1]
    assert row["field"] == "<PlannedField>" and row["box"] == "<_Box>"
    assert row["many"] == ["<PlannedField>"] and row["both"] == "['a', 'b']"
    assert "jane.secret" not in text and "hunter2" not in text


# === SP2 review round 1 ==============================================================================

def _page_run(context, tmp_path, url):
    """A prepared `_JobRun` whose page is at `url` (the caller routes it)."""
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, url)
    run = apply_run._JobRun(_runner(context, tmp_path), context, _entry())
    run._prepare()
    run.page = context.new_page()
    run.page.goto(url)
    return run


class _HostsSeen(jev.FakeJev):
    """The fake, keeping every page host a page request carried."""

    def __init__(self):
        self.hosts = []

    def judge(self, state, questions):
        if "page_state" in questions:
            self.hosts.append(state["page"]["url_host"])
        return super().judge(state, questions)


_POSTING = (FORMS / "job_posting.html").read_text(encoding="utf-8")
_ELSEWHERE = "https://elsewhere.example"


# --- I-1: a consent wrapper with no size of its own ------------------------------------------------

def test_a_sizeless_consent_wrapper_is_found_and_its_banner_declined(context, flow_server):
    page = context.new_page()
    page.goto(flow_server.url("consent_wrapper.html"))
    assert page.locator("#onetrust-consent-sdk").bounding_box()["height"] == 0
    found = apply_form.consent_control(page)
    assert found and (found[1]["kind"], found[1]["text"]) == ("reject", "Reject All"), found


def test_the_wrapper_flow_declines_the_banner_and_reaches_the_form(_browser, flow_server,
                                                                   tmp_path):
    r = _flow("consent_wrapper", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    clicks = [a.text for a in r.actions if a.kind == "click"]
    assert clicks[0] == "Reject All" and "Accept All Cookies" not in clicks, clicks


# --- I-2: consent roots never swallow the application's own content ---------------------------------

def test_a_section_named_trusted_is_no_consent_banner(context):
    page = context.new_page()
    # the vendor name TRUSTe counts as a whole word only: "trusted" is no banner
    page.set_content('<main><h1>Analytics Engineer</h1><p>Own the analytics models.</p></main>'
                     '<div class="trusted-by-section">Trusted by teams worldwide.'
                     '<button>Apply now</button></div>')
    assert [b.text for b in apply_form.extract(page).buttons] == ["Apply now"]


def test_a_sign_in_dialog_that_mentions_cookies_keeps_its_boxes_and_is_not_closed(context):
    page = context.new_page()
    page.set_content('<main><h1>Careers</h1></main><div role="dialog" aria-modal="true" '
                     'style="position:fixed;top:40px;left:40px;background:#fff">'
                     '<p>Sign in to continue. We use cookies to keep you signed in.</p>'
                     '<label>Email <input type="email" name="email"></label>'
                     '<label>Password <input type="password" name="pw"></label>'
                     '<button type="button">Sign in</button>'
                     '<button type="button" aria-label="Close">x</button></div>')
    d = apply_form.extract(page)
    assert [f.type for f in d.fields] == ["email", "other"]
    assert [b.text for b in d.buttons] == ["Sign in", "x"]
    assert apply_form.consent_control(page) is None


def test_a_consent_block_inside_the_form_keeps_its_checkbox_and_its_decline(context):
    page = context.new_page()
    page.set_content('<form><h1>Apply for Analytics Engineer</h1>'
                     '<label>Email * <input type="email" name="email" required></label>'
                     '<div class="consent-block"><p>We store your data and use cookies to run '
                     'the application.</p><label><input type="checkbox" name="agree" required> '
                     'I agree</label><button type="button">Decline</button></div>'
                     '<button type="submit">Submit application</button></form>')
    d = apply_form.extract(page)
    assert [f.type for f in d.fields] == ["email", "checkbox"]
    assert [b.text for b in d.buttons] == ["Decline", "Submit application"]
    assert apply_form.consent_control(page) is None


# --- I-3: every browser context of the suite is offline ---------------------------------------------

# a documentation address (RFC 5737, never routed on the internet): without
# the guard the request fails on its own, never reaching a site
_NOT_LOCAL = "http://192.0.2.1/"


def _refuses(page) -> str:
    try:
        page.goto(_NOT_LOCAL, timeout=4_000)
    except Exception as e:      # noqa: BLE001  (Playwright's Error)
        return str(e)
    return "loaded"


@pytest.mark.parametrize("how", ["browser_page", "new_context", "new_page", "second_browser",
                                 "test_context"])
def test_every_browser_context_refuses_a_non_local_url(request, _browser, how):
    made = []
    if how == "browser_page":
        page = request.getfixturevalue("browser_page")
    elif how == "new_context":
        made.append(_browser.new_context())
        page = made[-1].new_page()
    elif how == "new_page":
        page = _browser.new_page()
        made.append(page.context)
    elif how == "second_browser":
        other = _browser.browser_type.launch(headless=True)
        made.append(other)
        page = other.new_context().new_page()
    else:
        page = request.getfixturevalue("context").new_page()
    try:
        assert "ERR_BLOCKED_BY_CLIENT" in _refuses(page)
    finally:
        for thing in made:
            thing.close()


def test_the_local_fixture_server_still_answers_through_the_guard(browser_page, flow_server):
    browser_page.goto(flow_server.url("job_posting.html"))
    assert browser_page.locator("h1").inner_text() == "Analytics Engineer"


# --- I-4: the LinkedIn click guard counts by job ---------------------------------------------------

@pytest.mark.parametrize("url, job", [
    ("https://www.linkedin.com/jobs/view/4438751519/", "4438751519"),
    ("https://www.linkedin.com/jobs/view/4438751519/?trk=abc&refId=9", "4438751519"),
    ("https://www.linkedin.com/jobs/view/analytics-engineer-at-fabrikam-4438751519", "4438751519"),
    ("https://www.linkedin.com/comm/jobs/view/4438751519/?trackingId=x", "4438751519"),
    ("https://www.linkedin.com/jobs/search/?currentJobId=4438751519&start=25", "4438751519"),
    ("https://www.linkedin.com/jobs/view/externalApply/4438751519?url=x", ""),
    ("https://www.linkedin.com/feed/", "")])
def test_the_job_a_linkedin_url_shows(url, job):
    assert apply_linkedin.job_id(url) == job


def test_linkedins_external_apply_hop_is_no_job_page():
    url = "https://www.linkedin.com/jobs/view/externalApply/4438751519?url=https%3A%2F%2Fx"
    assert apply_linkedin.url_kind(url) == "other"


_TRACKED = (FORMS / "linkedin_posting.html").read_text(encoding="utf-8").replace(
    'href="linkedin_redirect.html" target="_blank" rel="opener"',
    'href="#" onclick="location.search = \'?trk=\' + Date.now(); return false"')


def test_an_apply_whose_url_changes_per_click_parks_after_the_second_click(context, tmp_path):
    # the same job, a new tracking parameter per click: the old URL-keyed
    # count started again on every page and clicked to the page budget
    context.route("https://www.linkedin.com/**",
                  lambda route: route.fulfill(body=_TRACKED, content_type="text/html"))
    out, rec = _drain(context, tmp_path, LINKEDIN_JOB)
    assert out.reason == ("the offsite Apply (Apply) did not open the company's site (clicked "
                          "twice)"), out
    assert [a.text for a in rec.actions if a.kind == "click"] == ["Apply"]


_NEXT_JOB = (FORMS / "linkedin_posting.html").read_text(encoding="utf-8").replace(
    'href="linkedin_redirect.html" target="_blank" rel="opener"',
    'href="#" onclick="location.href = \'/jobs/view/\' + '
    '(parseInt(location.pathname.split(\'/\')[3]) + 1) + \'/\'; return false"')


def test_the_handler_clicks_at_most_its_cap_per_job(context, tmp_path, monkeypatch):
    # every click leads to another job's page: the per-job cap ends it
    monkeypatch.setattr(apply_run, "LINKEDIN_CLICKS_MAX", 3)
    context.route("https://www.linkedin.com/**",
                  lambda route: route.fulfill(body=_NEXT_JOB, content_type="text/html"))
    out, rec = _drain(context, tmp_path, "https://www.linkedin.com/jobs/view/100/")
    assert out.reason == ("the offsite Apply (Apply) did not open the company's site (3 LinkedIn "
                          "Apply clicks in this job)"), out
    assert [a.text for a in rec.actions if a.kind == "click"] == ["Apply"] * 3


# --- M-1: the two-pane view ---------------------------------------------------------------------------

@pytest.mark.parametrize("html, offsite, easy", [
    ('<main><h1>E</h1><ul><li><button aria-label="Easy Apply filter.">Easy Apply</button></li>'
     '</ul></main>', 0, 0),
    ('<main><h1>E</h1><aside><button>Easy Apply</button><a href="/x">Apply</a></aside></main>',
     0, 0),
    ('<main><h1>E</h1><ul><li><button>Easy Apply</button></li></ul>'
     '<a href="/x" aria-label="Apply on company website">Apply</a></main>', 1, 0)])
def test_list_and_rail_controls_are_other_jobs(context, html, offsite, easy):
    page = context.new_page()
    page.set_content(html)
    view = apply_linkedin.read(page)
    assert (len(view.offsite), len(view.easy)) == (offsite, easy), view


def test_the_two_pane_view_reads_the_jobs_own_pane(_browser, flow_server, tmp_path):
    r = _flow("linkedin_two_pane", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    decision = _decisions(r.trace, "linkedin_handler")[0]
    assert decision["found"] == "offsite", decision


# --- M-2: LinkedIn frames on a company's page; profile Applies ---------------------------------------

_WITH_LINKEDIN_FRAME = ("<body><h1>Analytics Engineer</h1><p>About the role.</p>"
                        "<a class='btn' href='/apply/form'>Apply now</a>"
                        "<iframe src='https://www.linkedin.com/apply-widget/42' "
                        "style='width:400px;height:200px'></iframe></body>")
_LINKEDIN_WIDGET = ("<body><button>Apply with LinkedIn</button>"
                    "<label>Email <input type='email' name='email'></label></body>")


def test_a_linkedin_frame_on_a_company_page_loses_its_controls(context, tmp_path):
    _serve(context, {"/jobs/42": _WITH_LINKEDIN_FRAME})
    context.route("https://www.linkedin.com/**",
                  lambda route: route.fulfill(body=_LINKEDIN_WIDGET, content_type="text/html"))
    run = _page_run(context, tmp_path, f"{CAREERS}/jobs/42")
    run.page.frames[1].wait_for_selector("button")
    digest = run._drop_foreign_controls(apply_form.extract(run.page))
    assert [b.text for b in digest.buttons] == ["Apply now"]
    assert digest.fields == []


@pytest.mark.parametrize("label", ["Apply with LinkedIn", "Apply using Indeed",
                                   "Quick apply", "1-click apply", "Easy Apply"])
def test_no_entry_choice_picks_an_apply_that_sends_a_stored_profile(label):
    d = apply_form.FormDigest("jobs.example", "Job", "About the role", buttons=[
        apply_form.Button(0, (0, "#p"), label, "")])
    assert apply_run.fieldless_apply_choice(d) is None
    assert apply_run.posting_entry_choice(d, FillPlan(buttons={"apply_entry": (0, 0.95)})) == \
        (None, "")


# --- M-3: the consent pre-step's frames and links ------------------------------------------------------

@pytest.mark.parametrize("control, found", [
    ('<a href="/privacy/cookies">Reject cookies</a>', None),
    ('<a href="#">Reject all</a>', ("reject", "Reject all")),
    ('<a href="javascript:void(0)">Reject all</a>', ("reject", "Reject all")),
    ('<a role="button" href="/settings">Decline</a>', None)])
def test_a_consent_link_is_clicked_only_when_it_goes_nowhere(context, control, found):
    page = context.new_page()
    page.set_content('<main><h1>Engineer</h1></main><div id="cookie-bar" style="position:fixed;'
                     f'bottom:0">We use cookies. {control}</div>')
    got = apply_form.consent_control(page)
    assert ((got[1]["kind"], got[1]["text"]) if got else None) == found, got


_BANNER_PAGE = ("<body><div id='cookie-banner' style='position:fixed;top:0'>We use cookies."
                "<button id='no'>Reject all</button></div></body>")


@pytest.mark.parametrize("frame_url", [
    "https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html",
    "https://ads.example.net/frame"])
def test_the_consent_pre_step_never_clicks_in_a_bot_check_or_foreign_frame(
        context, tmp_path, frame_url):
    host = frame_url.split("/")[2]
    _serve(context, {"/jobs/42": f"<body><h1>Engineer</h1><p>About the role.</p>"
                                 f"<iframe src='{frame_url}' style='width:600px;height:300px'>"
                                 f"</iframe></body>"})
    context.route(f"https://{host}/**",
                  lambda route: route.fulfill(body=_BANNER_PAGE, content_type="text/html"))
    run = _page_run(context, tmp_path, f"{CAREERS}/jobs/42")
    run.page.frames[1].wait_for_selector("#no")
    rec = h.Recorder(None)
    with rec.recording():
        run._dismiss_consent()
    assert rec.actions == [] and run._consent_clicks == 0


# --- M-4: a form whose footer renders late ------------------------------------------------------------

_LATE_FOOTER = """<body><form onsubmit="return false"><h1>Apply for Analytics Engineer</h1>
<label>Full name * <input name="name" required></label>
<label>Email * <input type="email" name="email" required></label></form>
<script>
  window.addEventListener('load', function () {
    setTimeout(function () {
      document.querySelector('form').insertAdjacentHTML('beforeend',
        '<button type="button" id="btn-submit" onclick="document.body.dataset.submitted = 1">' +
        'Submit application</button>');
    }, 600);
  });
</script></body>"""


def test_a_form_whose_footer_renders_late_is_read_once_it_renders(context, tmp_path):
    _serve(context, {"/apply/42": _LATE_FOOTER})
    out, _ = _drain(context, tmp_path, f"{CAREERS}/apply/42", auto_apply_submit=False)
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out


# --- M-5: the host is checked after every new settle ---------------------------------------------------

def _elsewhere(context):
    context.route(f"{_ELSEWHERE}/**",
                  lambda route: route.fulfill(body=_POSTING, content_type="text/html"))


def test_a_consent_click_that_leaves_the_site_parks_before_any_read(context, tmp_path,
                                                                   monkeypatch):
    # an off-site page loses its controls and reads as empty, whose re-read
    # checks the host too: that path stands down, so this check alone is tested
    monkeypatch.setattr(apply_run, "_empty_read", lambda digest: False)
    _elsewhere(context)
    _serve(context, {"/jobs/42": _POSTING.replace(
        "</body>", "<div id='cookie-bar' style='position:fixed;bottom:0'>We use cookies."
                   f"<button onclick=\"location.href='{_ELSEWHERE}/landing'\">Reject all</button>"
                   "</div></body>")})
    judge = _HostsSeen()
    out, _ = _drain(context, tmp_path, f"{CAREERS}/jobs/42", judge)
    assert out.reason == "left the allowed sites: elsewhere.example", out
    assert "elsewhere.example" not in judge.hosts


def test_an_empty_page_that_moves_off_the_site_is_never_read(context, tmp_path):
    _elsewhere(context)
    _serve(context, {"/jobs/42": "<body><div id='app'></div><script>setTimeout(() => "
                                 f"location.replace('{_ELSEWHERE}/landing'), 400)</script></body>"})
    judge = _HostsSeen()
    out, _ = _drain(context, tmp_path, f"{CAREERS}/jobs/42", judge)
    assert out.reason == "left the allowed sites: elsewhere.example", out
    assert judge.hosts == []


class _UnsureThenAway(_HostsSeen):
    """Reads the first page as `other` at 0.30 and sends that page off the
    site as it answers (a page that moves on while it is re-read)."""

    def __init__(self, context):
        super().__init__()
        self.context = context

    def judge(self, state, questions):
        out = super().judge(state, questions)
        if "page_state" in out and len(self.hosts) == 1:
            # the page has left by the time the unsure read is taken again
            self.context.pages[-1].goto(f"{_ELSEWHERE}/landing")
            out["page_state"] = jev.Answer(kind="choice", choice="other", confidence=0.30,
                                           probabilities={"other": 0.30, "job_posting": 0.25})
        return out


def test_an_unsure_page_that_moves_off_the_site_is_never_read_again(context, tmp_path,
                                                                    monkeypatch):
    # as above: the empty-read path's own host check stands down
    monkeypatch.setattr(apply_run, "_empty_read", lambda digest: False)
    _elsewhere(context)
    _serve(context, {"/jobs/42": _POSTING})
    judge = _UnsureThenAway(context)
    out, _ = _drain(context, tmp_path, f"{CAREERS}/jobs/42", judge)
    assert out.reason == "left the allowed sites: elsewhere.example", out
    assert judge.hosts == ["careers.fabrikam.example"]


# --- M-6: the harness's sign-in exemption is the account step's alone ----------------------------------

def test_a_send_code_click_breaks_the_invariants_only_outside_the_account_step():
    class _Out:
        status, reason = "needs_human", "login wall"
    for in_account, breaks in ((True, False), (False, True)):
        rec = h.Recorder(h.flow("login_wall_park"))
        rec.actions.append(h.Action("click", f"{CAREERS}/login", text="Send code", tag="button",
                                    in_account=in_account))
        codes = _codes(h.invariant_breaks(_Out(), rec, h.Sends(rec)))
        assert ("CLICK-OUTSIDE-GATE" in codes) is breaks, (in_account, codes)


def test_the_recorder_marks_the_account_steps_clicks(_browser, flow_server, tmp_path):
    r = _flow("login_wall_park", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    clicks = [a for a in r.actions if a.kind == "click"]
    account = [a.text for a in clicks if a.in_account]
    assert account == ["Create account"], [(a.text, a.in_account) for a in clicks]
    assert all(not a.in_account for a in clicks if a.text == "Continue")


# --- M-7: a tab the Apply opens late ----------------------------------------------------------------------

_LATE_TAB = _POSTING.replace(
    '<a class="btn" id="apply" href="ashby_steps.html" target="_blank" rel="opener">Apply now</a>',
    '<button type="button" class="btn" id="apply" onclick="this.textContent = \'Opening the '
    'application...\'; setTimeout(() => window.open(\'/apply/form\'), 700)">Apply now</button>')


def test_a_tab_the_apply_opens_late_is_adopted(context, tmp_path, monkeypatch):
    # the click changes the page at once and opens the tab 0.7 s later, past
    # the entry race's grace; the settle's quiet window outlasts it
    monkeypatch.setattr(apply_fill, "SETTLE_QUIET_S", 1.0)
    _serve(context, {"/jobs/42": _LATE_TAB,
                     "/apply/form": (FORMS / "lever_single.html").read_text(encoding="utf-8")})
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, f"{CAREERS}/jobs/42")
    rec = h.Recorder(None, park_mode=True)
    with rec.recording():
        out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out
    clicks = [a.text for a in rec.actions if a.kind == "click"]
    assert clicks.count("Apply now") == 1, clicks
    assert _decisions(folder / "apply_trace" / "attempt-1", "late_popup")


# --- M-8: a signed-in page whose offsite Apply is a window.open button -----------------------------------

def test_an_offsite_apply_button_that_opens_a_tab_by_script(_browser, flow_server, tmp_path):
    r = _flow("linkedin_button_popup", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    entry = next(e for p in _pages(r.trace) for e in p["events"] if e["kind"] == "apply_entry")
    assert entry["popup"] is True and entry["signal"] == "popup", entry


# --- M-9: the probe opens a page the run's way --------------------------------------------------------

def test_the_probe_reads_a_page_whose_load_never_fires(context, monkeypatch):
    monkeypatch.setattr(apply_run, "PROBE_GOTO_MS", 3_000)
    _serve(context, {"/apply/42": _HANGING})
    context.route(f"{CAREERS}/slow.png", lambda route: None)
    out = io.StringIO()
    code = apply_run.probe(f"{CAREERS}/apply/42", context=context, out=out, settle_s=1)
    assert code == 0, out.getvalue()
    assert f"page 1: {CAREERS}/apply/42" in out.getvalue()
    assert "  load: settled" in out.getvalue()


# --- M-10: a LinkedIn form after the company's form -------------------------------------------------------

_TO_LINKEDIN = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>Apply for Analytics Engineer</h1>
<label>First name * <input name="first" required></label>
<label>Last name * <input name="last" required></label>
<label>Email * <input type="email" name="email" required></label>
<button type="button" onclick="location.href='https://www.linkedin.com/jobs/search/?keywords=x'">Continue</button>
</body></html>"""


def test_a_linkedin_form_after_the_companys_form_says_the_application_went_back(
        context, tmp_path):
    _serve(context, {"/apply/42": _TO_LINKEDIN})
    context.route("https://www.linkedin.com/**",
                  lambda route: route.fulfill(body=_LINKEDIN_FORM, content_type="text/html"))
    out, rec = _drain(context, tmp_path, f"{CAREERS}/apply/42")
    assert out.reason.startswith(apply_run.LINKEDIN_RETURN_REASON + " ("), out
    assert [a for a in rec.actions if h.on_linkedin(a.url)] == []


# --- the review's notes: a capped settle shows in the trace; screenshots do not stall -----------------

def test_a_settle_released_at_its_cap_says_so_in_the_trace(context, tmp_path, monkeypatch):
    monkeypatch.setattr(apply_fill, "SETTLE_MAX_S", 0.5)
    _serve(context, {"/jobs/42": _POSTING.replace(
        "<h1>", "<div aria-busy='true' style='height:30px'>Loading more</div><h1>")})
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, f"{CAREERS}/jobs/42")
    _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)
    settled = _decisions(folder / "apply_trace" / "attempt-1", "settled")[0]
    assert settled["capped"] is True and ", capped" in settled["why"], settled


def test_a_screenshot_on_a_page_whose_load_never_fires_is_skipped_quickly(context, tmp_path):
    _serve(context, {"/apply/42": _HANGING})
    context.route(f"{CAREERS}/slow.png", lambda route: None)
    page = context.new_page()
    page.goto(f"{CAREERS}/apply/42", wait_until="domcontentloaded")
    trace = apply_trace.Trace(tmp_path, attempt=1, job_id="42")
    trace.start()
    try:
        start = time.monotonic()
        assert trace.screenshot(page, "page-1") == ""
        assert time.monotonic() - start < 5.0
    finally:
        trace.close()
