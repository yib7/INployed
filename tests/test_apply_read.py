"""SP4: page reading that holds up.

- READ-06: a page read as a review with a confident advance and no submit
  clicks the advance (a wizard's middle step misread as the review), a review
  with its submit goes to the gate, a final-shaped advance on a review goes
  to the gate in either mode.
- The page read: its own request, the mapping only on a page the run acts
  on; a misread Choice gives way to the Nouls and the page's structure (the
  sign-up behind a login wall's link, NAV-03; a sign-up read as a sign-in;
  a code screen after the submit; a bot check read as `other`); an unsure
  read goes on as the kind the structure settles; a sure `other` whose
  structure settles a kind is that kind; a job the site says was applied to
  and a closed posting park with their own reasons (TERM-04, READ-08); an
  Apply apart from a job-alert box is the entry (READ-09).
- READ-04 (a ticker never makes a page new), NAV-07 (tracker hops), NAV-08
  (job boards), NAV-09 (an email Apply), ALLOW-01, ALLOW-02, NAV-05 (a step
  in a new tab), NAV-04 (a loading skeleton).
- Study G9 (a modal, a content frame first), G4 (header chrome, unnamed
  icons), G14 (a frame found by its URL), G13 (a privacy step's accept, never
  its decline).

Headless Chromium through the module-scoped test browser; the fixtures are
served by the flow server, fake hosts are routed; the judge is `FakeJev`,
`NoisyJev` or a scripted subclass. No network."""
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_form  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_judge  # noqa: E402
import apply_queue  # noqa: E402
import apply_run  # noqa: E402
import jev  # noqa: E402
from apply_judge import FillPlan  # noqa: E402

pytest_plugins = ["conftest_browser"]

CAREERS = "https://careers.fabrikam.example"


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
    recorder on: (the outcome, the recorder, the job folder)."""
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, url)
    rec = h.Recorder(None, park_mode=not settings.get("auto_apply_submit", True))
    with rec.recording():
        out = _runner(context, tmp_path, judge, **settings).drain(cap=1)[0]
    return out, rec, folder


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


def _digest(buttons, fields=()):
    return apply_form.FormDigest(
        url_host="careers.fabrikam.example", title="Review", text="Review your details",
        fields=[apply_form.Field(n=i, locator=(0, f"#f{i}"), label=label, type="text",
                                 required=True) for i, label in enumerate(fields)],
        buttons=[apply_form.Button(i, (0, f"#b{i}"), text) for i, text in enumerate(buttons)])


# --- READ-06: a review read with only Next clicks the Next --------------------------------------

def test_a_wizard_step_read_as_a_review_clicks_its_next_and_reaches_the_gate(
        _browser, flow_server, tmp_path):
    r = _flow("review_with_next", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    pages = _pages(Path(r.trace))
    assert pages[0]["state"] == "review_page", [p["state"] for p in pages]
    clicked = [e for e in pages[0]["events"] if e["kind"] == "click"]
    assert [(c["text"], c["role"]) for c in clicked] == [("Next", "advance")], clicked


@pytest.mark.parametrize("buttons, roles, park, step", [
    (("Back", "Next"), {"back": (0, 1.0), "advance": (1, 0.9)}, False,
     "fill the page, then click the advance [1] 'Next'"),
    (("Edit", "Submit application"), {"other": (0, 1.0), "submit": (1, 0.9)}, False,
     "fill the page, then the submit gate with [1] 'Submit application'"),
    # a final word on a review is its last step in either mode
    (("Back", "Confirm"), {"back": (0, 1.0), "advance": (1, 0.9)}, False,
     "fill the page, then the submit gate with [1] 'Confirm'"),
    (("Back",), {"back": (0, 1.0)}, False, "fill the page, then park: no submit button")])
def test_the_probe_says_what_a_review_read_does(buttons, roles, park, step):
    plan = FillPlan(buttons=dict(roles))
    got = apply_run.loop_step(CAREERS + "/review", _digest(buttons), plan, "review_page", 0.9,
                              park_mode=park)
    assert got == step, got


def test_a_confirm_advance_on_a_review_goes_through_the_gate_in_submit_mode(context, tmp_path):
    page = ("<body><h1>Review your application</h1><p>Please review before you confirm.</p>"
            "<label>Email * <input type='email' name='email' required></label>"
            "<button type='button'>Back</button>"
            "<button type='button' onclick=\"document.body.dataset.submitted = 1\">Confirm"
            "</button></body>")

    class _Reads(jev.FakeJev):
        def judge(self, state, questions):
            out = super().judge(state, questions)
            if "page_state" in out:
                out["page_state"] = jev.Answer(kind="choice", choice="review_page",
                                               probabilities={"review_page": 0.9},
                                               confidence=0.9)
            for b in state.get("buttons") or []:
                qid = f"button_{b['n']}_role"
                if b.get("text") == "Confirm" and qid in out:
                    out[qid] = jev.Answer(kind="choice", choice="advance", confidence=0.9,
                                          probabilities={"advance": 0.9, "submit": 0.1})
            return out
    _serve(context, {"/review": page})
    out, rec, _ = _drain(context, tmp_path, f"{CAREERS}/review", _Reads())
    confirm = [a for a in rec.actions if a.kind == "click" and a.text == "Confirm"]
    assert confirm and all(a.in_gate for a in confirm), rec.actions
    assert out.status != "submitted" or "confirmation" in out.reason or "unconfirmed" in out.reason


# --- the page read: its own request, combined with the page's structure ------------------------

class _ReadsByTitle(jev.FakeJev):
    """The fake, with the page state of a page whose title holds a key of
    `READS` read as that key's (state, confidence): the judge's Choice alone
    misreads, its Nouls stay the fake's (`nouls="keep"`) unless `NOULS`
    says otherwise."""
    READS: dict = {}
    NOULS = "keep"

    def judge(self, state, questions):
        out = super().judge(state, questions)
        title = str((state.get("page") or {}).get("title") or "")
        for words, (read, conf) in self.READS.items():
            if "page_state" in out and words in title:
                second = {"signup_form": "login_wall", "login_wall": "signup_form",
                          "other": "job_posting"}.get(read, "other")
                h.read_as(out, read, conf, {read: conf, second: round(min(1 - conf, conf / 3), 2)},
                          nouls=self.NOULS)
        return out


def _reads(reads, nouls="keep"):
    return type("J", (_ReadsByTitle,), {"READS": reads, "NOULS": nouls})()


def _requests(judge):
    """Record the question ids of every request the judge gets."""
    seen = []
    inner = judge.judge

    def _judge(state, questions):
        seen.append(set(questions))
        return inner(state, questions)
    judge.judge = _judge
    return seen


def test_the_read_and_the_mapping_are_separate_requests_and_a_park_page_is_never_mapped(
        context, flow_server, tmp_path):
    judge = jev.FakeJev()
    seen = _requests(judge)
    out, _, _ = _drain(context, tmp_path, flow_server.url("captcha.html"), judge)
    assert out.reason.startswith("captcha or bot check"), out
    # the read (and its second look), no mapping on a page the run parks on
    assert seen and all("page_state" in q for q in seen), seen
    assert not any(k.startswith(("field_", "button_")) for q in seen for k in q), seen


def test_a_posting_with_no_field_is_mapped_for_its_buttons_alone(context, flow_server, tmp_path):
    judge = jev.FakeJev()
    seen = _requests(judge)
    out, _, _ = _drain(context, tmp_path, flow_server.url("job_posting.html"), judge,
                       auto_apply_submit=False)
    assert out.status == "ready_to_submit", out
    assert not any("page_state" in q and any(k.startswith(("field_", "button_")) for k in q)
                   for q in seen), "the read carries no mapping question"
    first_mapping = next(q for q in seen if any(k.startswith("button_") for k in q))
    assert not any(k.startswith("field_") for k in first_mapping), first_mapping


def test_a_sign_up_page_behind_the_create_account_link_misread_as_a_form_is_the_sign_up(
        context, flow_server, tmp_path, monkeypatch):
    # NAV-03: the page the login wall's link leads to is read as the loop reads
    # any page; the judge's Choice misreads it as a form, its Nouls and the box
    # that makes the password read it as the sign-up
    monkeypatch.setattr(apply_run.ats_accounts, "_get_master_password", lambda: h.PASSWORD)
    judge = _reads({"Create an account": ("application_form", 0.55)})
    out, rec, folder = _drain(context, tmp_path, flow_server.url("login_wall.html"), judge,
                              auto_apply_submit=False)
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out
    pages = _pages(folder / "apply_trace" / "attempt-1")
    signup = next(p for p in pages if p["url"].endswith("/signup.html"))
    assert signup["state"] == "signup_form"
    assert signup["answers"]["page_state_judged"]["choice"] == "application_form"


def test_a_sign_up_misread_as_a_sign_in_is_the_sign_up(context, flow_server, tmp_path,
                                                       monkeypatch):
    monkeypatch.setattr(apply_run.ats_accounts, "_get_master_password", lambda: h.PASSWORD)
    judge = _reads({"Create an account": ("login_wall", 0.55)})
    out, _, folder = _drain(context, tmp_path, flow_server.url("signup.html"), judge,
                            auto_apply_submit=False)
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out
    assert _pages(folder / "apply_trace" / "attempt-1")[0]["state"] == "signup_form"
    assert _decisions(folder / "apply_trace" / "attempt-1", "read_combined")


def test_a_code_screen_after_the_submit_misread_as_a_sign_in_is_the_code_step(
        _browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("submit_code"), _reads({"Verify your email": ("login_wall", 0.55)}),
                   "code-as-login", browser=_browser, server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, r


def test_a_bot_check_misread_as_other_parks_as_the_bot_check(context, flow_server, tmp_path):
    judge = _reads({"Security check": ("other", 0.55)})
    out, _, _ = _drain(context, tmp_path, flow_server.url("captcha.html"), judge)
    assert out.status == "needs_human" and out.reason.startswith("captcha or bot check"), out


def test_an_unsure_read_goes_on_as_the_kind_the_structure_settles(context, flow_server, tmp_path):
    # the judge's other at 0.35, its Nouls with it, outweighs the posting's
    # structure but stays under the floor twice: the Apply entry with no box
    # settles it
    judge = _reads({"Analytics Engineer at": ("other", 0.35)}, nouls="coherent")
    out, _, folder = _drain(context, tmp_path, flow_server.url("job_posting.html"), judge,
                            auto_apply_submit=False)
    assert out.status == "ready_to_submit", out
    fallback = _decisions(folder / "apply_trace" / "attempt-1", "structural_fallback")
    assert fallback and fallback[0]["to"] == "job_posting", fallback


_APPLIED = ("<!doctype html><html><head><title>Data Engineer - Fabrikam</title></head><body>"
            "<h1>Data Engineer</h1><p>You have already applied to this job. We will be in "
            "touch.</p><h2>About the role</h2><p>Build the pipelines.</p>"
            "<a href=\"/jobs\">See other jobs</a></body></html>")
_CLOSED = ("<!doctype html><html><head><title>Data Engineer - Fabrikam</title></head><body>"
           "<h1>Data Engineer</h1><p>This job is no longer available.</p>"
           "<a href=\"/jobs\">See other jobs</a></body></html>")


def test_a_job_the_site_says_was_applied_to_is_never_applied_to_again(context, tmp_path):
    # TERM-04's ATS part
    _serve(context, {"/jobs/7": _APPLIED, "/apply": "<body><h1>Apply</h1></body>"})
    out, rec, _ = _drain(context, tmp_path, f"{CAREERS}/jobs/7")
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.ALREADY_APPLIED_REASON), out
    assert "already applied" in out.reason
    assert not [a for a in rec.actions if a.kind == "click"], rec.actions
    assert h.policy_park(out.status, out.reason) is True


def test_a_closed_posting_has_its_own_reason(context, tmp_path):
    # READ-08
    _serve(context, {"/jobs/7": _CLOSED})
    out, _, _ = _drain(context, tmp_path, f"{CAREERS}/jobs/7")
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.CLOSED_POSTING_REASON), out
    assert "no longer available" in out.reason
    assert h.policy_park(out.status, out.reason) is True


class _AlertRolesExchanged(jev.FakeJev):
    """The fake, with the posting's two buttons' roles exchanged (the
    NoisyJev role noise on the alert-box posting): "Apply now" other, the
    alert box's "Notify me" the Apply entry."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        for b in state.get("buttons") or []:
            qid = f"button_{b['n']}_role"
            role = {"Apply now": "other", "Notify me": "apply_entry"}.get(b.get("text"))
            if role and qid in out:
                out[qid] = jev.Answer(kind="choice", choice=role, confidence=0.95,
                                      probabilities={role: 0.95})
        return out


def test_an_apply_apart_from_a_job_alert_box_is_the_entry_when_the_roles_are_exchanged(
        _browser, flow_server, tmp_path):
    # READ-09: the judged entry is the alert box's own button; the Apply-worded
    # control apart from the box is the entry
    r = h.run_flow(h.flow("posting_with_alert_box"), _AlertRolesExchanged(), "exchanged",
                   browser=_browser, server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, r
    clicked = [a.text for a in r.actions if a.kind == "click"]
    assert "Notify me" not in clicked and "Apply now" in clicked, clicked


def test_a_posting_the_judge_reads_as_other_is_the_posting_its_structure_settles(
        context, flow_server, tmp_path):
    # `other` is none of the listed kinds: a sure `other` on a page whose
    # structure settles a kind (an Apply entry, no box, a job description) is
    # that kind, never "unrecognised page"
    judge = _reads({"Analytics Engineer at": ("other", 0.9)}, nouls="coherent")
    out, _, folder = _drain(context, tmp_path, flow_server.url("job_posting.html"), judge,
                            auto_apply_submit=False)
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out
    moved = _decisions(folder / "apply_trace" / "attempt-1", "structure_over_other")
    assert moved and moved[0]["to"] == "job_posting", moved


# --- READ-04: a structural "did not advance" signature --------------------------------------------

def test_a_step_that_comes_back_the_same_does_not_advance_whatever_its_ticker_says(
        _browser, flow_server, tmp_path):
    r = _flow("ticker_page", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    assert r.pages == 2, r
    assert r.reason.endswith("again after Continue (advance))"), r


def test_the_signature_ignores_times_counts_and_the_judges_read():
    def digest(text):
        return apply_form.FormDigest(url_host="x", title="Apply", text=text,
                                     fields=[apply_form.Field(0, (0, "#e"), "Email", "email",
                                                              True)],
                                     buttons=[apply_form.Button(0, (0, "#c"), "Continue")])
    a = apply_run.page_signature("https://x.example/apply?step=1",
                                 digest("Last saved 09:00:01; posted 3 minutes ago. 12 openings"))
    b = apply_run.page_signature("https://x.example/apply?step=1",
                                 digest("Last saved 09:00:07 PM; posted 4 minutes ago. 13 openings"))
    assert a == b
    c = apply_run.page_signature("https://x.example/apply?step=2",
                                 digest("Step two: tell us about your experience"))
    assert c != a


# --- NAV-07, NAV-08, NAV-09, ALLOW-01, ALLOW-02: trackers, job boards, email ----------------------

def _entry_ats():
    return next(e for e in apply_queue.load()["jobs"] if e["job_posting_id"] == "42").get("ats")


def test_an_ad_trackers_hop_is_waited_out_and_never_the_destination(
        _browser, flow_server, tmp_path):
    r = _flow("tracker_redirect", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    hops = _decisions(Path(r.trace), "tracker_hops")
    assert hops and hops[0]["trackers"] == ["click.appcast.io"], hops
    assert not any("appcast" in a.url for a in r.actions if a.kind in ("fill", "click")), r.actions


def test_a_job_boards_link_to_the_company_site_is_followed_once(_browser, flow_server, tmp_path):
    r = _flow("aggregator_company_site", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    on_board = [a for a in r.actions if "dice.com" in a.url]
    assert [a.text for a in on_board if a.kind == "click"] == ["Apply on company site"], on_board
    assert not [a for a in on_board if a.kind != "click"], on_board


def test_a_job_board_with_no_link_to_the_company_site_parks_at_once(
        _browser, flow_server, tmp_path):
    r = _flow("aggregator_board_only", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    assert r.policy is True
    assert not [a for a in r.actions if "dice.com" in a.url], r.actions


def test_an_apply_that_is_an_email_address_parks_with_the_address(
        _browser, flow_server, tmp_path):
    r = _flow("mailto_apply", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    assert r.policy is True and r.pages == 1
    assert not [a for a in r.actions if a.kind == "click"], r.actions


def _job_run(context, tmp_path, url="https://www.linkedin.com/jobs/view/1/"):
    folder = h.write_job_folder(tmp_path / "job")
    _enqueue(folder, url)
    entry = next(e for e in apply_queue.load()["jobs"] if e["job_posting_id"] == "42")
    run = apply_run._JobRun(_runner(context, tmp_path), context, entry)
    run._prepare()
    return run


@pytest.mark.parametrize("host", ["www.dice.com", "click.appcast.io", "www.indeed.com"])
def test_the_master_password_never_goes_to_a_job_board_or_a_tracker(context, tmp_path, host):
    # ALLOW-02: even once admitted as where LinkedIn's Apply led
    run = _job_run(context, tmp_path)
    run.allowed.add(host)
    run.ats_hosts.add(host)
    assert run._password_ok(host) is False


@pytest.mark.parametrize("host", ["acme.jobs2web.com", "acme.careers-page.com",
                                  "jobs.dover.com", "apply.jazz.co", "acme.wellfound.com"])
def test_the_missing_platforms_are_application_sites(context, tmp_path, host):
    # ALLOW-01
    run = _job_run(context, tmp_path)
    assert run._allowed_site(host) and run._password_ok(host)


# --- NAV-05: a click that opens its next page in a new tab ----------------------------------------

@pytest.mark.parametrize("name", ["popup_step_park", "popup_step"])
def test_a_step_opened_in_a_new_tab_is_the_next_page_and_nothing_is_clicked_twice(
        _browser, flow_server, tmp_path, name):
    r = _flow(name, _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    nexts = [a for a in r.actions if a.kind == "click" and a.text == "Next"]
    assert len(nexts) == 1, nexts
    adopted = _decisions(Path(r.trace), "click_popup")
    assert adopted and "step=2" in adopted[0]["url"], adopted


# --- what the judge reads first: study G9, G13, G14, G4 --------------------------------------------

_MODAL_PAGE = """<!doctype html><html><head><title>Analyst - Fabrikam</title></head><body>
<h1>Analyst</h1><p>Fabrikam, Austin. About the role: own the dashboards.</p>
<button type="button" id="apply">Apply</button><button type="button">Share</button>
<label for="q">Search jobs</label><input id="q">
<div role="dialog" aria-modal="true" aria-labelledby="t"
     style="position:fixed;inset:10% 20%;background:#fff;border:1px solid #555;padding:24px">
  <h2 id="t">Start Your Application</h2>
  <button type="button">Autofill with Resume</button>
  <button type="button">Apply Manually</button>
</div></body></html>"""


def test_an_open_modal_is_the_page_its_text_first_and_its_controls_alone(browser_page):
    # G9: Workday's "Start Your Application" dialog over the posting
    browser_page.set_content(_MODAL_PAGE)
    d = apply_form.extract(browser_page)
    assert [b.text for b in d.buttons] == ["Autofill with Resume", "Apply Manually"], d.buttons
    assert d.fields == []
    assert d.dialog == "Start Your Application"
    assert d.text.startswith("Start Your Application"), d.text[:80]
    state, _ = apply_judge.read_questions(d)
    assert state["page"]["dialog"] == "Start Your Application"


def test_a_content_frame_is_read_before_the_host_pages_chrome(browser_page):
    # G9: an iCIMS content frame, a Greenhouse embed
    chrome = "Our company. " * 60
    frame = ("<h1>Apply for Data Engineer</h1><label for=f>First name</label><input id=f>"
             "<button type=button>Submit application</button>")
    browser_page.set_content(f"<body><p>{chrome}</p><iframe style='width:900px;height:600px' "
                             f"srcdoc=\"{frame}\"></iframe></body>")
    browser_page.frames[1].wait_for_selector("#f")
    d = apply_form.extract(browser_page)
    assert d.text.startswith("Apply for Data Engineer"), d.text[:80]
    assert [f.label for f in d.fields] == ["First name"]


def test_a_workday_header_and_a_top_bar_are_chrome_and_an_icon_with_no_name_is_dropped(
        browser_page):
    # G4: a header's Sign In invites a login misread; an unnamed icon is noise
    browser_page.set_content("""<body>
      <div data-automation-id="headerContainer"><button type="button">Sign In</button>
        <button type="button">Search for Jobs</button></div>
      <div style="position:fixed;top:0;left:0;right:0;height:50px;background:#eee">
        <label for="s">Keyword</label><input id="s"><button type="button">Menu</button></div>
      <main style="margin-top:80px"><h1>Analyst</h1><p>About the role.</p>
        <button type="button"><svg width="10" height="10"></svg></button>
        <a class="btn" href="/apply">Apply</a></main></body>""")
    d = apply_form.extract(browser_page)
    chrome = {b.text: b.chrome for b in d.buttons}
    assert chrome == {"Sign In": True, "Search for Jobs": True, "Menu": True, "Apply": False}, chrome
    assert d.fields == []
    state, _ = apply_judge.read_questions(d)
    assert [b["text"] for b in state["buttons"]] == ["Apply"]
    assert apply_judge.page_facts(d).apply_entries == 1


def test_a_locator_finds_its_frame_by_url_when_an_earlier_frame_went_away(browser_page, fixture_url):
    # G14: an ad frame detaching between the read and the act shifts the indexes
    browser_page.goto(fixture_url("job_posting.html"))
    browser_page.set_content(
        "<body><iframe id=ad src='about:blank'></iframe>"
        f"<iframe id=form src='{fixture_url('lever_single.html')}'></iframe></body>")
    browser_page.frame_locator("#form").locator("input").first.wait_for()
    d = apply_form.extract(browser_page)
    field = next(f for f in d.fields if f.locator[0] == 2)
    browser_page.evaluate("document.getElementById('ad').remove()")
    loc = apply_form.resolve(browser_page, field.locator)
    assert loc.count() == 1
    assert loc.first.evaluate("el => el.ownerDocument.location.href").endswith("lever_single.html")


def test_a_privacy_agreement_first_is_a_step_accepted_to_go_on(_browser, flow_server, tmp_path):
    # G13: Taleo's "Privacy Agreement", I Accept and I Decline
    r = _flow("privacy_gate", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    clicked = [a.text for a in r.actions if a.kind == "click"]
    assert clicked[:1] == ["I Accept"] and "I Decline" not in clicked, clicked


# --- NAV-04 / READ-02: a loading skeleton is no read of the page ------------------------------------

class _SkeletonAsSignUp(jev.FakeJev):
    """The fake, reading any page that says it is loading as a sign-up at
    0.78, its Nouls with it (the noisy-6 misread of the skeleton)."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        text = str((state.get("page") or {}).get("headline_text") or "")
        if "page_state" in out and "Loading your application" in text:
            h.read_as(out, "signup_form", 0.78)
        return out


def test_a_loading_skeleton_is_waited_out_before_the_page_is_read(_browser, flow_server,
                                                                   tmp_path):
    r = _flow("skeleton_then_form", _browser, flow_server, tmp_path, _SkeletonAsSignUp(),
              "skeleton-as-signup")
    assert r.ok and not r.breaks, r
    waited = _decisions(Path(r.trace), "reread_after_settle")
    assert waited and waited[0]["why"].startswith("a loading placeholder"), waited
    assert waited[0]["still_loading"] is False


# --- study G13: a privacy step's accept is its way on, its decline never ---------------------------

@pytest.mark.parametrize("buttons, roles, step", [
    (("I Accept", "I Decline"), {"advance": (1, 0.9), "other": (0, 0.9)}, ("advance", 0)),
    (("I Accept", "I Decline"), {}, ("advance", 0)),
    (("Accept all cookies", "Menu"), {}, ("stuck", None)),
    (("Cancel", "Continue"), {"advance": (0, 0.9)}, ("stuck", None))])
def test_a_decline_is_never_the_way_on_and_an_accept_is_when_nothing_else_is(buttons, roles, step):
    plan = FillPlan(buttons=dict(roles))
    got, button, _ = apply_run.form_route(_digest(buttons), plan, park_mode=False)
    assert (got, button[0] if button else None) == step, (got, button)


class _DeclineAsAdvance(jev.FakeJev):
    """The fake, with the privacy step's two buttons' roles exchanged: I
    Decline the advance, I Accept other (NoisyJev's advance / other trade)."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        for b in state.get("buttons") or []:
            qid = f"button_{b['n']}_role"
            role = {"I Decline": "advance", "I Accept": "other"}.get(b.get("text"))
            if role and qid in out:
                out[qid] = jev.Answer(kind="choice", choice=role, confidence=0.9,
                                      probabilities={role: 0.9})
        return out


def test_a_privacy_step_whose_roles_are_exchanged_is_still_accepted(_browser, flow_server,
                                                                    tmp_path):
    r = _flow("privacy_gate", _browser, flow_server, tmp_path, _DeclineAsAdvance(), "exchanged")
    assert r.ok and not r.breaks, r
    clicked = [a.text for a in r.actions if a.kind == "click"]
    assert "I Decline" not in clicked and "I Accept" in clicked, clicked
