"""Page reading that holds up.

- A page read as a review with a confident advance and no submit
  clicks the advance (a wizard's middle step misread as the review), a review
  with its submit goes to the gate, a final-shaped advance on a review goes
  to the gate in either mode.
- The page read: its own request, the mapping only on a page the run acts
  on; a misread Choice gives way to the Nouls and the page's structure (the
  sign-up behind a login wall's link; a sign-up read as a sign-in;
  a code screen after the submit; a bot check read as `other`); an unsure
  read goes on as the kind the structure settles; a sure `other` whose
  structure settles a kind is that kind; a job the site says was applied to
  and a closed posting park with their own reasons; an Apply apart from a
  job-alert box is the entry.
- A ticker never makes a page new; tracker hops, job boards, an email Apply,
  which sites are application sites, a step in a new tab, a loading
  skeleton.
- What the judge reads first: a modal, a content frame first, header chrome
  and unnamed icons, a frame found by its URL, a privacy step's accept and
  never its decline.

Headless Chromium through the module-scoped test browser; the fixtures are
served by the flow server, fake hosts are routed; the judge is `FakeJev`,
`NoisyJev` or a scripted subclass. No network."""
import dataclasses
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
import apply_limits  # noqa: E402
import jev  # noqa: E402
from apply_judge import FillPlan  # noqa: E402
from apply_form import Button, Field, FormDigest  # noqa: E402

pytest_plugins = ["conftest_browser"]

CAREERS = "https://careers.fabrikam.example"
FORMS = REPO / "tests" / "fixtures" / "forms"


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


# --- a review read with only Next clicks the Next -----------------------------------------------

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
    # the page the login wall's link leads to is read as the loop reads
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
    # the ATS's own word that the job was applied to
    _serve(context, {"/jobs/7": _APPLIED, "/apply": "<body><h1>Apply</h1></body>"})
    out, rec, _ = _drain(context, tmp_path, f"{CAREERS}/jobs/7")
    assert out.status == "needs_human", out
    assert out.reason.startswith(apply_run.ALREADY_APPLIED_REASON), out
    assert "already applied" in out.reason
    assert not [a for a in rec.actions if a.kind == "click"], rec.actions
    assert h.policy_park(out.status, out.reason) is True


def test_a_closed_posting_has_its_own_reason(context, tmp_path):
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
    # the judged entry is the alert box's own button; the Apply-worded
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


# --- a structural "did not advance" signature -----------------------------------------------------

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
    # two steps that differ only by their step number are two pages
    one = apply_run.page_signature("https://x.example/apply", digest("Work Experience 1 of 3"))
    two = apply_run.page_signature("https://x.example/apply", digest("Work Experience 2 of 3"))
    assert one != two


# --- trackers, job boards, email, application sites -----------------------------------------------

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


def test_a_board_whose_company_link_lands_on_another_board_reads_that_board_the_same_way(
        _browser, flow_server, tmp_path):
    # LinkedIn, a board, a second board, the company's form
    r = _flow("aggregator_chain", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    boards = [a for a in r.actions if "dice.com" in a.url or "ziprecruiter.com" in a.url]
    assert [a.text for a in boards if a.kind == "click"] == ["Apply on company site"] * 2, boards
    assert not [a for a in boards if a.kind != "click"], boards
    gates = [a for a in r.actions if a.kind == "gate"]
    assert gates and all(a.url.endswith("/forms/lever_single.html") for a in gates), gates


def test_a_chain_of_job_boards_past_the_bound_parks_and_no_board_is_the_application(
        _browser, flow_server, tmp_path):
    flow = dataclasses.replace(
        h.flow("aggregator_chain"), name="aggregator_chain_3", status="needs_human",
        reason=r"^aggregator posting on www\.talent\.com: a chain of job boards",
        routes=lambda base: h.board_chain_routes(
            base, ("www.dice.com", "www.ziprecruiter.com", "www.talent.com")))
    r = h.run_flow(flow, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks and r.policy is True, r
    assert not [a for a in r.actions if "talent.com" in a.url], r.actions
    assert not [a for a in r.actions if a.kind in ("fill", "gate", "tick", "pick", "upload")]


def test_two_job_boards_that_link_to_each_other_park_at_the_first_board_seen_again(
        _browser, flow_server, tmp_path):
    # glassdoor -> ziprecruiter -> glassdoor; a board read once
    # is never read again, and the page budget is never what ends it
    flow = dataclasses.replace(
        h.flow("aggregator_chain"), name="aggregator_loop", status="needs_human",
        reason=r"^aggregator posting on www\.glassdoor\.com: the job boards link to each "
               r"other \(www\.glassdoor\.com -> www\.ziprecruiter\.com -> "
               r"www\.glassdoor\.com\)",
        routes=lambda base: h.board_chain_routes(
            base, ("www.glassdoor.com", "www.ziprecruiter.com"), loop=True))
    r = h.run_flow(flow, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks and r.policy is True, r
    boards = [a for a in r.actions if a.kind == "click"
              and ("glassdoor.com" in a.url or "ziprecruiter.com" in a.url)]
    assert [a.text for a in boards] == ["Apply on company site"] * 2, boards
    assert not [a for a in r.actions if a.kind in ("fill", "gate", "tick", "pick", "upload")]


def test_a_boards_lone_apply_link_off_the_board_is_its_company_link(
        _browser, flow_server, tmp_path):
    # the off-site control reads just "Apply now"
    flow = dataclasses.replace(
        h.flow("aggregator_company_site"), name="aggregator_plain_apply",
        routes=lambda base: h.board_chain_routes(base, ("www.dice.com",), plain_apply=True))
    r = h.run_flow(flow, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, r
    assert [a.text for a in r.actions if a.kind == "click" and "dice.com" in a.url] == [
        "Apply now"]


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
    # even once admitted as where LinkedIn's Apply led
    run = _job_run(context, tmp_path)
    run.allowed.add(host)
    run.ats_hosts.add(host)
    assert run._password_ok(host) is False


@pytest.mark.parametrize("host", ["acme.jobs2web.com", "acme.careers-page.com",
                                  "jobs.dover.com", "apply.jazz.co", "acme.wellfound.com"])
def test_the_missing_platforms_are_application_sites(context, tmp_path, host):
    run = _job_run(context, tmp_path)
    assert run._allowed_site(host) and run._password_ok(host)


# --- a click that opens its next page in a new tab ------------------------------------------------

@pytest.mark.parametrize("name", ["popup_step_park", "popup_step"])
def test_a_step_opened_in_a_new_tab_is_the_next_page_and_nothing_is_clicked_twice(
        _browser, flow_server, tmp_path, name):
    r = _flow(name, _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    nexts = [a for a in r.actions if a.kind == "click" and a.text == "Next"]
    assert len(nexts) == 1, nexts
    adopted = _decisions(Path(r.trace), "click_popup")
    assert adopted, adopted
    # the trace keeps an address without its query: the
    # run's own actions show the new tab it went on in is step 2's
    assert [a for a in r.actions if "step=2" in a.url], r.actions


# --- what the judge reads first: a modal, frames, chrome, a privacy step -------------------------

# The captured Workday popup's shape: a 442 x 451 px dialog, no
# aria-modal, the header and the posting (its Apply still showing) behind it
def _workday_page(attrs='data-automation-activepopup="true"'):
    return f"""<!doctype html><html><head><title>Analyst - Fabrikam</title></head><body>
<div data-automation-id="header"><button type="button">Sign In</button>
  <button type="button">Search for Jobs</button></div>
<h1>Analyst</h1><p>Fabrikam, Austin. About the role: own the dashboards.</p>
<button type="button" id="apply">Apply</button><button type="button">Share</button>
<div role="dialog" aria-label="Start Your Application" {attrs}
     style="position:fixed;left:50%;top:50%;width:394px;height:403px;margin:-225px 0 0 -221px;
            padding:24px;background:#fff;border:1px solid #555;z-index:10">
  <h2>Start Your Application</h2>
  <button type="button">Autofill with Resume</button>
  <button type="button">Apply Manually</button>
  <button type="button">Use My Last Application</button>
</div></body></html>"""


@pytest.mark.parametrize("attrs", ['data-automation-activepopup="true"', ""])
def test_workdays_start_popup_is_the_page_its_text_first_and_its_controls_alone(
        browser_page, attrs):
    # Workday's own marker, and a dialog on top of the page without it
    browser_page.set_viewport_size({"width": 1400, "height": 900})
    browser_page.set_content(_workday_page(attrs))
    d = apply_form.extract(browser_page)
    assert [b.text for b in d.buttons] == ["Autofill with Resume", "Apply Manually",
                                           "Use My Last Application"], d.buttons
    assert d.fields == []
    assert d.dialog == "Start Your Application"
    assert d.text.startswith("Start Your Application"), d.text[:80]
    state, _ = apply_judge.read_questions(d)
    assert state["page"]["dialog"] == "Start Your Application"


def test_a_dropped_bot_check_frame_leaves_the_popup_the_page(context, tmp_path):
    # dropping a reCAPTCHA frame's control rebuilds the digest; the
    # Workday popup it read stays the page
    captcha = "https://www.google.com/recaptcha/api2/anchor?k=1"
    _serve(context, {"/job/7": _workday_page().replace(
        "</div></body>", f"</div><iframe src='{captcha}' style='width:304px;height:78px'>"
                         "</iframe></body>")})
    context.route("https://www.google.com/recaptcha/**", lambda route: route.fulfill(
        body="<body><button type=button>Verify</button></body>", content_type="text/html"))
    run = _job_run(context, tmp_path, f"{CAREERS}/job/7")
    run.page = context.new_page()
    run.page.set_viewport_size({"width": 1400, "height": 900})
    run.page.goto(f"{CAREERS}/job/7")
    run.page.frames[1].wait_for_selector("button")
    read = apply_form.extract(run.page)
    assert "Verify" in [b.text for b in read.buttons]
    digest = run._drop_foreign_controls(read)
    assert run._last_dropped == {1: "www.google.com"}
    assert [b.text for b in digest.buttons] == ["Autofill with Resume", "Apply Manually",
                                                "Use My Last Application"], digest.buttons
    assert digest.dialog == "Start Your Application"
    state, _ = apply_judge.read_questions(digest)
    assert state["page"]["dialog"] == "Start Your Application"


def test_a_chat_window_or_a_dialog_behind_the_page_is_never_the_page(browser_page):
    browser_page.set_viewport_size({"width": 1400, "height": 900})
    chat = _workday_page('class="chat-window"').replace("Start Your Application", "Chat with us")
    browser_page.set_content(chat)
    assert apply_form.extract(browser_page).dialog == ""
    covered = _workday_page("").replace(
        "</body>", "<div style='position:fixed;inset:0;background:#fff;z-index:20'>"
                   "<h1>Other page</h1><button type='button'>Go</button></div></body>")
    browser_page.set_content(covered)
    assert apply_form.extract(browser_page).dialog == ""


def test_an_open_onetrust_preference_center_is_consent_and_never_the_pages_modal(browser_page):
    browser_page.set_viewport_size({"width": 1400, "height": 900})
    browser_page.set_content("""<body><h1>Analyst</h1><p>About the role.</p>
      <a class="btn" href="/apply">Apply</a>
      <div id="onetrust-pc-sdk" role="dialog" aria-modal="true"
           style="position:fixed;inset:10%;background:#fff;z-index:30">
        <h2>Privacy Preference Center</h2><input type="text" placeholder="Search vendors">
        <button type="button">Allow All</button><button type="button">Reject All</button>
      </div></body>""")
    d = apply_form.extract(browser_page)
    assert d.dialog == ""
    assert [b.text for b in d.buttons] == ["Apply"], d.buttons
    assert d.fields == []


def test_a_fieldless_posting_frame_is_read_before_the_host_pages_text(browser_page):
    # iCIMS: the posting's frame holds no field; its words must come
    # first, inside the read's head
    chrome = "Careers home. Our locations. Benefits. Sign in. " * 40
    frame = ("<h1>Software Developer</h1><h2>Overview</h2><p>Build the tools.</p>"
             "<a href=/apply>Apply for this job online</a>")
    browser_page.set_content(f"<body><p>{chrome}</p><iframe style='width:900px;height:600px' "
                             f"srcdoc=\"{frame}\"></iframe></body>")
    browser_page.frames[1].wait_for_selector("h2")
    d = apply_form.extract(browser_page)
    assert d.text.startswith("Software Developer"), d.text[:80]
    state, _ = apply_judge.read_questions(d)
    assert "Overview" in state["page"]["headline_text"]


def test_a_content_frame_is_read_before_the_host_pages_chrome(browser_page):
    # an iCIMS content frame, a Greenhouse embed
    chrome = "Our company. " * 60
    frame = ("<h1>Apply for Data Engineer</h1><label for=f>First name</label><input id=f>"
             "<button type=button>Submit application</button>")
    browser_page.set_content(f"<body><p>{chrome}</p><iframe style='width:900px;height:600px' "
                             f"srcdoc=\"{frame}\"></iframe></body>")
    browser_page.frames[1].wait_for_selector("#f")
    d = apply_form.extract(browser_page)
    assert d.text.startswith("Apply for Data Engineer"), d.text[:80]
    assert [f.label for f in d.fields] == ["First name"]


def _posting_with_frames(context, frames: dict) -> object:
    """A posting on `CAREERS` with a child frame per (src, (width, height)),
    each frame's host routed to its own small page."""
    def _page(html):
        return lambda route: route.fulfill(body=html, content_type="text/html")
    for src, (html, _) in frames.items():
        context.route(src, _page(html))
    tags = "".join(f"<iframe src='{src}' style='width:{w}px;height:{h}px'></iframe>"
                   for src, (_, (w, h)) in frames.items())
    _serve(context, {"/job": "<body><h1>Payroll Analyst</h1><p>Reconcile the ledgers.</p>"
                             f"<a class=btn href=/apply>Apply</a>{tags}</body>"})
    page = context.new_page()
    page.goto(f"{CAREERS}/job")
    for frame in page.frames[1:]:
        frame.wait_for_selector("body *")
    return page


def test_an_embedded_video_and_an_ad_stay_in_place_behind_the_posting(context):
    # a 640 x 360 video and a 300 x 250 ad reading "Apply now" are
    # big enough to be content frames; neither is the page's site or an ATS
    page = _posting_with_frames(context, {
        "https://video.example/embed/1": ("<body><p>Meet the team. Play video.</p></body>",
                                          (640, 360)),
        "https://ads.example/slot/1": ("<body><p>Earn a degree online.</p>"
                                       "<a href=/go>Apply now</a></body>", (300, 250))})
    d = apply_form.extract(page)
    assert d.text.startswith("Payroll Analyst"), d.text[:80]
    run_site = apply_form.extract(page, content_site=lambda url: apply_run.content_frame_site(
        url, str(page.url)))
    assert run_site.text.startswith("Payroll Analyst"), run_site.text[:80]
    state, _ = apply_judge.read_questions(run_site)
    assert "Reconcile" in state["page"]["headline_text"]


def test_an_ats_frame_is_read_before_the_careers_page_that_embeds_it(context):
    # the run's own predicate still takes a Greenhouse embed first
    page = _posting_with_frames(context, {
        "https://boards.greenhouse.io/embed/job_app": (
            "<body><h1>Apply for Payroll Analyst</h1><label for=f>First name</label>"
            "<input id=f></body>", (900, 600))})
    assert apply_run.content_frame_site("https://boards.greenhouse.io/x", str(page.url))
    assert not apply_run.content_frame_site("https://video.example/x", str(page.url))
    d = apply_form.extract(page, content_site=lambda url: apply_run.content_frame_site(
        url, str(page.url)))
    assert d.text.startswith("Apply for Payroll Analyst"), d.text[:80]


def test_a_workday_header_and_a_top_bar_are_chrome_and_an_icon_with_no_name_is_dropped(
        browser_page):
    # a header's Sign In invites a login misread; an unnamed icon is noise
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
    # an ad frame detaching between the read and the act shifts the indexes
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
    # Taleo's "Privacy Agreement", I Accept and I Decline
    r = _flow("privacy_gate", _browser, flow_server, tmp_path)
    assert r.ok and not r.breaks, r
    clicked = [a.text for a in r.actions if a.kind == "click"]
    assert clicked[:1] == ["I Accept"] and "I Decline" not in clicked, clicked


# --- a loading skeleton is no read of the page ------------------------------------------------------

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


def test_a_skeleton_that_clears_right_after_it_was_read_is_read_again(_browser, flow_server,
                                                                      tmp_path):
    """A skeleton under load: the form can come between the read and the
    loading check, so the check sees no placeholder and the read of the
    skeleton would go to the judge. Here the form comes during the first read
    itself (`?clear=now`), every time: the loading state is the one the read
    was taken under, and the page is read again."""
    import dataclasses
    f = dataclasses.replace(h.flow("skeleton_then_form"),
                            start="skeleton_then_form.html?clear=now")
    r = h.run_flow(f, _SkeletonAsSignUp(), "skeleton-cleared-on-read", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, r
    waited = _decisions(Path(r.trace), "reread_after_settle")
    assert waited and waited[0]["why"].startswith("a loading placeholder"), waited
    assert waited[0]["still_loading"] is False


class _ReadPage:
    url = "https://jobs.example.com/apply/42"

    def wait_for_timeout(self, ms):
        pass


class _ScriptedRead:
    """`_JobRun._read_digest` over scripted looks (`_busy`) and reads
    (`_extract`), the page never touched."""

    _read_busy = apply_run._JobRun._read_busy
    _loading = apply_run._JobRun._loading
    last_sig = None

    def __init__(self, looks, reads):
        self.page = _ReadPage()
        self._loading_waited: set = set()
        self.looks, self.reads, self.decisions = list(looks), list(reads), []

    def _busy(self):
        return self.looks.pop(0) if self.looks else False

    def _extract(self):
        return self.reads.pop(0)

    def _drop_foreign_controls(self, digest):
        return digest

    def _check_host(self, url):
        pass

    def _decide_next(self, what, why, **kw):
        self.decisions.append((what, why, kw))


_SKELETON = FormDigest(url_host="jobs.example.com", title="Apply", text="Loading your "
                       "application. This can take a few seconds while we fetch the questions "
                       "for this role and the documents you uploaded before. Please keep this "
                       "window open; closing it now would lose the answers you have not saved.",
                       buttons=[Button(n=0, locator=(0, "#cancel"), text="Cancel")])
_FORM = FormDigest(url_host="jobs.example.com", title="Apply", text="Apply for Business Analyst",
                   fields=[Field(n=0, locator=(0, "#first"), label="First name", type="text",
                                 required=True)],
                   buttons=[Button(n=0, locator=(0, "#submit"), text="Submit application")])


@pytest.mark.parametrize("looks", [
    [True],             # the placeholder showed before the read and cleared after it
    [False, True],      # painted between the look and the read
])
def test_a_skeleton_seen_before_or_after_the_read_is_read_again(looks):
    run = _ScriptedRead(looks, [_SKELETON, _FORM])
    digest = apply_run._JobRun._read_digest(run)
    assert digest.fields, "the skeleton's read went on as the page"
    what, why, kw = run.decisions[0]
    assert what == "reread_after_settle" and why.startswith("a loading placeholder"), why
    assert kw["still_loading"] is False


_STEP_TWO = FormDigest(url_host="jobs.example.com", title="Apply", text="Step 2 of 2",
                       fields=[Field(n=0, locator=(0, "#email"), label="Email", type="email",
                                     required=True)],
                       buttons=[Button(n=0, locator=(0, "#submit"), text="Submit application")])


def test_each_step_of_a_wizard_at_one_url_gets_its_own_placeholder_wait():
    """The placeholder's one wait is per step (the URL and the step
    before it). A single-page wizard's second step loads behind a skeleton
    at the first step's URL, and it is waited out like the first."""
    # each step: the placeholder is up at the first look and gone at the next
    run = _ScriptedRead([True, False, True, False], [_SKELETON, _FORM, _SKELETON, _STEP_TWO])
    first = apply_run._JobRun._read_digest(run)
    assert first.fields, "the first step's skeleton went on as the page"
    run.last_sig = apply_run.page_signature(run.page.url, first)    # the loop's next step
    second = apply_run._JobRun._read_digest(run)
    assert [f.label for f in second.fields] == ["Email"], "the second step's skeleton went on"
    assert [(w, why.split(" (")[0]) for w, why, _ in run.decisions] == [
        ("reread_after_settle", "a loading placeholder")] * 2, run.decisions


def test_a_placeholder_that_stays_up_on_one_step_is_waited_on_once(monkeypatch):
    """One wait per page holds within a step: a re-read of the same step (the loop's
    `_reread`, `last_sig` unchanged) does not wait on the placeholder again."""
    monkeypatch.setattr(apply_limits, "LOADING_WAIT_S", 0.0)     # the wait runs out at once
    run = _ScriptedRead([True, True, True], [_SKELETON] * 3)
    apply_run._JobRun._read_digest(run)
    apply_run._JobRun._read_digest(run)
    assert len(run.decisions) == 1 and run.decisions[0][2]["still_loading"] is True, \
        run.decisions
    assert not run.reads and run.looks == [True], "the re-read looked for a placeholder again"


def test_a_wizard_whose_second_step_loads_at_the_same_url_waits_out_both_skeletons(
        _browser, flow_server, tmp_path):
    """The per-step wait end to end: `skeleton_wizard.html` keeps one URL and
    shows each step behind a skeleton that stays until it is read. The click's
    own busy wait is cut short here, so the second skeleton reaches the read."""
    f = dataclasses.replace(
        h.flow("skeleton_then_form"), name="skeleton_wizard", start="skeleton_wizard.html",
        timing=(*h.flow("skeleton_then_form").timing, (("apply_limits", "BUSY_WAIT_S"), 0.3)))
    r = h.run_flow(f, _SkeletonAsSignUp(), "skeleton-wizard", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, r
    waited = [w for w in _decisions(Path(r.trace), "reread_after_settle")
              if w["why"].startswith("a loading placeholder")]
    assert len(waited) == 2, [w["why"] for w in _decisions(Path(r.trace), "reread_after_settle")]
    assert not any(w["still_loading"] for w in waited), waited


# --- a privacy step's accept is its way on, its decline never --------------------------------------

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


_LATE_TAB = """<!doctype html><html><head><title>Apply - Tailwind Traders</title></head><body>
<h1>Tailwind Traders: Data Engineer</h1>
<label for="first_name">First name *</label><input id="first_name" name="first_name" required>
<p id="note"></p>
<button type="button" id="next">Next</button>
<script>
  document.getElementById('next').addEventListener('click', function () {
    if (!document.getElementById('first_name').value) { return; }
    // "Opening..." then the tab a moment later, after the click has returned
    setTimeout(function () { window.open('/apply/step2', '_blank'); }, 150);
  });
</script></body></html>"""
_STEP2 = """<!doctype html><html><head><title>Apply - step 2</title></head><body>
<h1>Contact</h1><label for="email">Email *</label><input id="email" type="email" required>
<button type="button" id="btn-submit">Submit application</button></body></html>"""


def test_a_tab_that_opens_a_moment_after_the_click_is_followed_before_any_second_click(
        context, tmp_path, monkeypatch):
    # the click's own wait ends before the tab opens
    real = apply_run.apply_fill.click
    monkeypatch.setattr(apply_run.apply_fill, "click",
                        lambda page, digest, n, **kw: real(page, digest, n,
                                                           **{**kw, "timeout_s": 0.05}))
    _serve(context, {"/apply/1": _LATE_TAB, "/apply/step2": _STEP2})
    out, rec, folder = _drain(context, tmp_path, f"{CAREERS}/apply/1", auto_apply_submit=False)
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out
    assert [a.text for a in rec.actions if a.kind == "click"].count("Next") == 1, rec.actions


_LINGERING = """<!doctype html><html><head><title>Analyst - Fabrikam</title></head><body>
<h1>Analyst</h1><p>About the role: own the dashboards. Qualifications: SQL.</p>
<a class="btn" href="/apply">Apply</a>
<aside><h2>Similar jobs</h2><div aria-busy="true" class="skeleton" style="height:40px"></div>
</aside></body></html>"""


def test_a_placeholder_that_never_clears_costs_one_short_wait_per_page(context, tmp_path,
                                                                     monkeypatch):
    # a widget's region stays busy; the read goes on after
    # LOADING_WAIT_S, once for the page, and the trace says so
    monkeypatch.setattr(apply_limits, "EMPTY_READ_MAX_S", 8.0)
    monkeypatch.setattr(apply_limits, "LOADING_WAIT_S", 1.0, raising=False)
    _serve(context, {"/jobs/7": _LINGERING, "/apply": (FORMS / "lever_single.html").read_text(
        encoding="utf-8")})
    judge = _reads({"Analyst - Fabrikam": ("other", 0.30)}, nouls="neutral")
    out, _, folder = _drain(context, tmp_path, f"{CAREERS}/jobs/7", judge,
                            auto_apply_submit=False)
    assert out.status == "ready_to_submit", out
    waits = _decisions(folder / "apply_trace" / "attempt-1", "reread_after_settle")
    lingered = [d for d in waits if d.get("still_loading")]
    assert len(lingered) == 1 and "stayed up" in lingered[0]["why"], waits
    # the page is short, so its one wait is an empty read's: the settle (3 s
    # in the tests) and the placeholder's own LOADING_WAIT_S, never 8 s
    assert all(d["waited_ms"] < 6500 for d in waits), waits


# --- the site's header never holds the entry or the way on ------------------------------------------

_HEADER_ENTRY = """<!doctype html><html><head><title>Analyst - Fabrikam</title></head><body>
<div data-automation-id="header"><button type="button"
  onclick="document.body.dataset.general = 1">Submit A General Application</button></div>
<main><h1>Analyst</h1><p>About the role: own the dashboards. Qualifications: SQL.</p>
<a class="btn" href="/apply">Apply</a></main></body></html>"""


class _HeaderAsEntry(jev.FakeJev):
    """The fake, judging the header's "Submit A General Application" the
    posting's entry at 0.95, above the page's own Apply at 0.90."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        for b in state.get("buttons") or []:
            qid = f"button_{b['n']}_role"
            conf = {"Submit A General Application": 0.95, "Apply": 0.90}.get(b.get("text"))
            if conf and qid in out:
                out[qid] = jev.Answer(kind="choice", choice="apply_entry", confidence=conf,
                                      probabilities={"apply_entry": conf})
        return out


def test_a_header_control_judged_the_entry_never_beats_the_postings_own_apply(context, tmp_path):
    _serve(context, {"/jobs/7": _HEADER_ENTRY,
                     "/apply": (FORMS / "lever_single.html").read_text(encoding="utf-8")})
    out, rec, _ = _drain(context, tmp_path, f"{CAREERS}/jobs/7", _HeaderAsEntry(),
                         auto_apply_submit=False)
    assert (out.status, out.reason) == ("ready_to_submit", "auto_apply_submit is off"), out
    clicked = [a.text for a in rec.actions if a.kind == "click"]
    assert "Submit A General Application" not in clicked and "Apply" in clicked, clicked


def test_a_header_advance_is_no_way_on_and_a_footers_next_is_the_pages(browser_page):
    browser_page.set_content("""<body><header><button type="button">Sign In</button></header>
      <form><label>First name <input name="f"></label></form>
      <footer><button type="button">Next</button></footer></body>""")
    d = apply_form.extract(browser_page)
    assert {b.text: b.chrome for b in d.buttons} == {"Sign In": True, "Next": False}
    header = next(b.n for b in d.buttons if b.text == "Sign In")
    got, button, _ = apply_run.form_route(d, FillPlan(buttons={"advance": (header, 0.9)}),
                                          park_mode=False)
    assert got == "stuck", (got, button)
