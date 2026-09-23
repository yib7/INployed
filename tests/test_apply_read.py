"""SP4: page reading that holds up.

- READ-06: a page read as a review with a confident advance and no submit
  clicks the advance (a wizard's middle step misread as the review), a review
  with its submit goes to the gate, a final-shaped advance on a review goes
  to the gate in either mode.

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
            "<a class=\"btn\" href=\"/apply\">Apply now</a></body></html>")
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
