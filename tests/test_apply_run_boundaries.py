"""Regression checks for runner boundaries using synthetic pages and judges."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import apply_form
import apply_run
import jev_harness

pytest_plugins = ["conftest_jev"]


@pytest.fixture(autouse=True)
def _judge_selector(jev_judge):
    """Every runner here takes the harness judge (fake unless AUTO_APPLY_TEST_JEV says otherwise)."""


@pytest.fixture(autouse=True)
def _fast_timing():
    """The harness's short settle and click windows (`apply_harness.FAST_TIMING`):
    nothing here waits on a real page's timer."""
    import apply_harness
    with apply_harness.fast_timing():
        yield


def _job():
    context = Mock()
    runner = apply_run.Runner(jev=jev_harness.judge(), context=context, run_context={},
                              sleep=lambda seconds: None)
    entry = {"job_posting_id": "synthetic", "apply_url": "https://www.linkedin.com/jobs/1",
             "ats": {"domain": "www.linkedin.com", "system": "linkedin"}}
    job = apply_run._JobRun(runner, context, entry)
    job._build_allowlist()
    job.page = Mock(url=entry["apply_url"])
    return job


@pytest.mark.parametrize("mode", ["fake", "replay"])
def test_cli_fixture_judges_refuse_before_queue_or_browser(monkeypatch, mode, capsys):
    monkeypatch.setattr(apply_run, "_load_env", lambda: None)
    monkeypatch.setattr(apply_run, "_settings_from_args", lambda args: {
        **apply_run.DEFAULT_SETTINGS, "auto_apply_jev_mode": mode})
    constructor = Mock(side_effect=AssertionError("must not construct a production runner"))
    monkeypatch.setattr(apply_run, "Runner", constructor)
    assert apply_run.main(["drain", "--jev", mode]) == 2
    constructor.assert_not_called()
    assert "fixture" in capsys.readouterr().err.lower()


def test_native_continue_is_an_advance_while_final_submit_stays_guarded():
    digest = apply_form.FormDigest("jobs.example", "Apply", "", buttons=[
        apply_form.Button(0, (0, "#next"), "Continue", "submit"),
        apply_form.Button(1, (0, "#submit"), "Submit application", "submit"),
    ])
    assert not apply_run._submit_shaped(digest, 0)
    assert apply_run._submit_shaped(digest, 1)


def test_generic_ats_cannot_extend_allowlist_twice(monkeypatch):
    job = _job()
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    monkeypatch.setattr(apply_run.apply_queue, "update", lambda *a, **kw: None)
    job._follow_popup(Mock(url="https://careers.example/apply"))
    assert "careers.example" in job.allowed
    with pytest.raises(apply_run._Parked, match="allowed"):
        job._follow_popup(Mock(url="https://unrelated.example/collect"))
    assert "unrelated.example" not in job.allowed


class _RedirectingTab:
    """A popup on LinkedIn's `/safety/go/` hop whose script moves on to
    `dest` only once waited on, as the real one does a few seconds in."""

    def __init__(self, dest):
        self.url = "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fcareers.example"
        self.dest = dest
        self.waited = []

    def wait_for_url(self, predicate, timeout):
        self.waited.append(timeout)
        if self.dest is None:
            raise TimeoutError("still on the hop")
        self.url = self.dest
        assert predicate(self.url)


def test_popup_on_the_linkedin_redirector_admits_the_destination(monkeypatch):
    job = _job()
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    monkeypatch.setattr(apply_run.apply_queue, "update", lambda *a, **kw: None)
    tab = _RedirectingTab("https://careers-gtsx.icims.com/jobs/1605/job")
    job._follow_popup(tab, source_url=job.page.url)
    assert tab.waited, "the hop was not waited on"
    assert job.page is tab and "careers-gtsx.icims.com" in job.allowed
    job._check_host(job.page.url)       # the loop's next check passes


def test_a_tab_that_stays_on_the_redirector_admits_nothing(monkeypatch):
    job = _job()
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    monkeypatch.setattr(apply_run.apply_queue, "update", lambda *a, **kw: None)
    before = set(job.allowed)
    job._follow_popup(_RedirectingTab(None), source_url=job.page.url)
    assert job.allowed == before and not job.ats_transition_used


def test_same_tab_linkedin_transition_admits_one_ats_host(monkeypatch):
    job = _job()
    source = job.page.url
    rec = {"clicked": []}
    job.pages.append(rec)

    def _same_tab(page, loc, **kw):
        page.url = "https://careers.example/apply"
        return None, "navigation", 5

    monkeypatch.setattr(apply_run, "click_entry", _same_tab)
    monkeypatch.setattr(apply_run.apply_form, "live_text", lambda loc: {"text": "Apply"})
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    monkeypatch.setattr(apply_run.apply_queue, "update", lambda *a, **kw: None)

    job._click_entry(rec, object(), "Apply", how="linkedin_handler")

    assert rec["clicked"] == ["Apply (apply_entry)"]
    assert source.startswith("https://www.linkedin.com/")
    assert "careers.example" in job.allowed
    with pytest.raises(apply_run._Parked, match="allowed"):
        job._follow_popup(Mock(url="https://unrelated.example/collect"))


def test_post_submit_redirect_is_checked_before_reading_or_filling(monkeypatch):
    job = _job()
    job.page = Mock(url="https://unrelated.example/code")
    job.submit_clicked = True
    extract = Mock(side_effect=AssertionError("untrusted destination must not be extracted"))
    monkeypatch.setattr(apply_run.apply_form, "extract", extract)
    with pytest.raises(apply_run._Parked, match="allowed"):
        job._after_submit()
    extract.assert_not_called()


def test_post_submit_foreign_frame_controls_are_dropped_before_the_judge(monkeypatch):
    job = _job()
    main = SimpleNamespace(url=job.page.url, parent_frame=None)
    job.page.frames = [main, SimpleNamespace(url="https://unrelated.example/code",
                                             parent_frame=main)]
    job.submit_clicked = True
    digest = apply_form.FormDigest("www.linkedin.com", "Verify", "", fields=[
        apply_form.Field(0, (1, "#code"), "Security code", "text", True)])
    monkeypatch.setattr(apply_run.apply_form, "extract", lambda page, **_: digest)
    assert job._post_submit_digest().fields == []


def test_a_foreign_frame_loses_its_controls_and_the_page_goes_on():
    # the 2026-09-22 iCIMS sign-in: hCaptcha's frame carried "Verify" and
    # "Refresh Challenge" buttons and the old check parked the whole job
    job = _job()
    main = SimpleNamespace(url=job.page.url, parent_frame=None)
    job.page.frames = [main,
                       SimpleNamespace(url="https://unrelated.example/form", parent_frame=main),
                       SimpleNamespace(url="https://newassets.hcaptcha.com/captcha/v1/x/static/"
                                           "hcaptcha.html", parent_frame=main)]
    digest = apply_form.FormDigest("www.linkedin.com", "Apply", "", fields=[
        apply_form.Field(0, (0, "#name"), "Name", "text", True),
        apply_form.Field(1, (1, "#email"), "Email", "email", True)], buttons=[
        apply_form.Button(0, (0, "#next"), "Next"),
        apply_form.Button(1, (2, "#verify"), "Verify")])
    kept = job._drop_foreign_controls(digest)
    assert [f.label for f in kept.fields] == ["Name"]
    assert [b.text for b in kept.buttons] == ["Next"]


@pytest.mark.parametrize("frame_url", [
    "https://js.stripe.com/v3/elements-inner-card-abc.html",
    "https://assets.braintreegateway.com/web/3.97.2/html/hosted-fields-frame.min.html",
    "https://www.linkedin.com/embed/feed/apply"])
def test_a_payment_or_a_linkedin_frame_on_the_form_loses_its_fields(frame_url):
    # final review B-M7: a card frame's "ZIP" box and a LinkedIn widget's
    # boxes on the company's form are never planned; the form's own box is
    job = _job()
    job.page = Mock(url="https://boards.greenhouse.io/acme/jobs/1")
    main = SimpleNamespace(url=job.page.url, parent_frame=None)
    job.page.frames = [main, SimpleNamespace(url=frame_url, parent_frame=main)]
    digest = apply_form.FormDigest("boards.greenhouse.io", "Apply", "", fields=[
        apply_form.Field(0, (0, "#first_name"), "First name", "text", True),
        apply_form.Field(1, (1, "#postal"), "ZIP", "text", False),
        apply_form.Field(2, (1, "#card"), "Card number", "text", True)])
    kept = job._drop_foreign_controls(digest)
    assert [f.label for f in kept.fields] == ["First name"]
    assert list(job._last_dropped) == [1]


@pytest.mark.parametrize("url", ["about:blank", "about:srcdoc"])
def test_blank_frame_inherits_allowed_parent(url):
    job = _job()
    main = SimpleNamespace(url=job.page.url, parent_frame=None)
    job.page.frames = [main, SimpleNamespace(url=url, parent_frame=main)]
    digest = apply_form.FormDigest("www.linkedin.com", "Apply", "", fields=[
        apply_form.Field(0, (1, "#email"), "Email", "email", True)])
    assert job._drop_foreign_controls(digest).fields == digest.fields


@pytest.mark.parametrize("role,confidence", [
    ("advance", apply_run.apply_judge.BUTTON_ADVANCE_MIN_CONF - 0.01),
    ("submit", apply_run.apply_judge.BUTTON_SUBMIT_MIN_CONF - 0.01),
])
def test_code_gate_refuses_low_confidence_continue(monkeypatch, role, confidence):
    job = _job()
    job.page.url = "https://careers.example/verify"     # a code step never runs on LinkedIn
    job.inbox = SimpleNamespace(fetch_code=lambda *a: "SYNTHETIC-CODE")
    code = apply_form.Field(0, (0, "#code"), "Security code", "text", True,
                            id_or_name="code")
    digest = apply_form.FormDigest("careers.example", "Verify", "", fields=[code])
    plan = apply_run.FillPlan(buttons={role: (0, confidence)})
    locator = SimpleNamespace(first=SimpleNamespace(fill=lambda *a, **kw: None))
    monkeypatch.setattr(apply_run.apply_form, "resolve", lambda *a: locator)
    monkeypatch.setattr(job, "_click",
                        Mock(side_effect=AssertionError("low-confidence button was clicked")))

    with pytest.raises(apply_run._Parked, match="confidence"):
        job._code_gate(digest, plan, {"filled": []})


def test_code_gate_submit_uses_submit_no_retry_path(monkeypatch):
    job = _job()
    job.page.url = "https://careers.example/verify"     # a code step never runs on LinkedIn
    job.inbox = SimpleNamespace(fetch_code=lambda *a: "SYNTHETIC-CODE")
    code = apply_form.Field(0, (0, "#code"), "Security code", "text", True,
                            id_or_name="code")
    digest = apply_form.FormDigest("careers.example", "Verify", "", fields=[code])
    plan = apply_run.FillPlan(buttons={
        "submit": (0, apply_run.apply_judge.BUTTON_SUBMIT_MIN_CONF)})
    locator = SimpleNamespace(first=SimpleNamespace(fill=lambda *a, **kw: None))
    monkeypatch.setattr(apply_run.apply_form, "resolve", lambda *a: locator)
    clicked = Mock(return_value=apply_run.apply_fill.ClickResult(clicked=True, changed=True))
    monkeypatch.setattr(job, "_click", clicked)
    # the application's answers are on the site, so the button may send what
    # the site held for the code (before them it is the account's own check,
    # a step control: SP8b review I1, tests/test_apply_submit.py)
    job.form_filled = True

    job._code_gate(digest, plan, {"filled": []})

    clicked.assert_called_once_with(digest, 0, "submit", {"filled": [{
        "n": 0, "label": "Security code", "value": apply_run.HIDDEN,
        "type": "text", "id_or_name": "code", "upload": False, "hidden": True}]},
        conf=apply_run.apply_judge.BUTTON_SUBMIT_MIN_CONF)
    # the gate is the only send: the job reads as may-have-sent, never clicked
    assert not job.submit_clicked and job._code_may_send


# --- the relaxed rules (2026-09-22): sites, the password's sites, the human check ---------

@pytest.mark.parametrize("host,site", [
    ("careers-gtsx.icims.com", "icims.com"), ("login.icims.com", "icims.com"),
    ("https://jobs.example.co.uk/apply", "example.co.uk"), ("127.0.0.1", "127.0.0.1"),
    ("localhost", "localhost"), ("www.linkedin.com", "linkedin.com")])
def test_site_is_the_registrable_domain(host, site):
    assert apply_run._site(host) == site


@pytest.mark.parametrize("url,captcha", [
    ("https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html", True),
    ("https://www.google.com/recaptcha/api2/anchor?k=x", True),
    ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x", True),
    ("https://www.google.com/search?q=recaptcha", False),
    ("https://careers-gtsx.icims.com/jobs/1605/login", False)])
def test_captcha_urls(url, captcha):
    assert apply_run._is_captcha_url(url) is captcha


def test_the_ats_site_and_known_platforms_are_allowed_and_others_are_not(monkeypatch):
    job = _job()
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    monkeypatch.setattr(apply_run.apply_queue, "update", lambda *a, **kw: None)
    job._follow_popup(Mock(url="https://careers.gts.example/jobs/1"), source_url=job.page.url)
    job._check_host("https://login.gts.example/sso")          # the same site
    job._check_host("https://acme.wd5.myworkdayjobs.com/x")    # a known platform, any time
    with pytest.raises(apply_run._Parked, match="allowed"):
        job._check_host("https://unrelated.example/collect")


def test_the_master_password_goes_only_to_the_application_site():
    job = _job()
    job.r._run_context = {"inbox_url": "https://outlook.office.com/mail/"}
    job.ats_hosts.add("careers.gts.example")
    assert job._password_ok("login.gts.example")
    assert job._password_ok("careers-gtsx.icims.com")          # a known platform
    assert not job._password_ok("www.linkedin.com")
    assert not job._password_ok("outlook.office.com")
    assert not job._password_ok("unrelated.example")


def test_linkedin_signed_out_parks_with_the_login_command():
    job = _job()
    digest = apply_form.FormDigest("www.linkedin.com", "Sign in", "")
    job.accounts = Mock()
    job.accounts.login.side_effect = AssertionError("the password must not go to LinkedIn")
    with pytest.raises(apply_run._Parked, match="LinkedIn is signed out") as parked:
        job._account_step("login_wall", digest)
    assert "apply_run.py login" in parked.value.tab_note


class _Frame:
    def __init__(self, url, heights):
        self.url = url
        self.heights = list(heights)       # the challenge's height at each look

    def frame_element(self):
        height = self.heights.pop(0) if len(self.heights) > 1 else self.heights[0]
        return SimpleNamespace(is_visible=lambda: height > 0,
                               bounding_box=lambda: {"x": 0, "y": 10, "width": 400,
                                                     "height": height})


def _human_check_job(monkeypatch, heights, headless=False):
    job = _job()
    job.r.settings["auto_apply_headless"] = headless
    frame = _Frame("https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html", heights)
    job.page.frames = [SimpleNamespace(url=job.page.url), frame]
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    return job


def test_a_visible_challenge_waits_for_the_user_and_the_run_goes_on(monkeypatch):
    job = _human_check_job(monkeypatch, [480, 480, 480, 480, 0])
    naps = []
    job.r.sleep = naps.append
    deadline = job.deadline
    assert job._human_check_showing()
    job._wait_for_human_check("a CAPTCHA challenge is showing")
    assert len(naps) == 2 and job.deadline >= deadline
    assert job.last_sig is None


def test_a_hidden_or_small_captcha_frame_is_no_challenge(monkeypatch):
    assert not _human_check_job(monkeypatch, [0])._human_check_showing()
    assert not _human_check_job(monkeypatch, [78])._human_check_showing()   # the badge


def test_a_challenge_headless_or_unsolved_parks(monkeypatch):
    with pytest.raises(apply_run._Parked, match="showing"):
        _human_check_job(monkeypatch, [480], headless=True)._wait_for_human_check(
            "a CAPTCHA challenge is showing")
    job = _human_check_job(monkeypatch, [480])
    monkeypatch.setattr(apply_run, "HUMAN_CHECK_WAIT_S", 3 * apply_run.HUMAN_CHECK_POLL_S)
    with pytest.raises(apply_run._Parked, match="not solved in time"):
        job._wait_for_human_check("a CAPTCHA challenge is showing")


def test_linkedin_and_the_inbox_are_matched_by_exact_host():
    # their domains carry other people's content: a Google Form, a Google
    # sign-in frame, a Microsoft form (the SP8-live review)
    job = _job()
    job.r._run_context = {"inbox_url": "https://mail.google.com/mail/u/0/"}
    job._build_allowlist()
    job._check_host("https://mail.google.com/mail/u/0/#inbox")
    for url in ("https://docs.google.com/forms/d/x", "https://accounts.google.com/o/oauth2",
                "https://sites.google.com/view/x"):
        with pytest.raises(apply_run._Parked, match="allowed"):
            job._check_host(url)


def test_shared_hosting_subdomains_are_separate_sites():
    assert apply_run._site("acme.github.io") == "acme.github.io"
    assert apply_run._site("careers.acme.github.io") == "acme.github.io"
    job = _job()
    job.ats_hosts.add("acme.github.io")
    assert job._allowed_site("acme.github.io")
    assert not job._allowed_site("evil.github.io")
    assert not job._password_ok("evil.github.io")


def test_a_known_platform_met_later_is_recorded_as_the_jobs_ats(monkeypatch):
    job = _job()
    updates = []
    monkeypatch.setattr(apply_run.apply_queue, "update",
                        lambda *a, **kw: updates.append(kw.get("ats")))
    job._admit_ats_transition("https://careers.gts.example/jobs/1", job.page.url)
    job._admit_ats_transition("https://careers-gtsx.icims.com/jobs/1605/login",
                              "https://careers.gts.example/jobs/1")
    assert [u["domain"] for u in updates] == ["careers.gts.example", "careers-gtsx.icims.com"]
    assert job.entry["ats"]["system"] == "icims"
    job._admit_ats_transition("https://login.icims.com/x", "https://careers-gtsx.icims.com/")
    assert len(updates) == 2                   # the same site is not recorded twice


def test_an_advance_that_opens_a_challenge_waits_for_the_user(monkeypatch):
    job = _human_check_job(monkeypatch, [480, 480, 0])
    job.r.sleep = lambda s: None
    digest = apply_form.FormDigest("jobs.example", "Apply", "",
                                   buttons=[apply_form.Button(0, (0, "#next"), "Next")])
    monkeypatch.setattr(apply_run.apply_fill, "click",
                        lambda *a, **kw: apply_run.apply_fill.ClickResult(True, False))
    result = job._click(digest, 0, "advance", {"clicked": []})
    assert result.changed and job.last_sig is None


def test_a_whole_page_check_that_already_cleared_is_not_waited_on(monkeypatch):
    job = _human_check_job(monkeypatch, [0])
    markers = iter([("https://jobs.example/apply", "Apply for the role")])
    monkeypatch.setattr(job, "_page_marker", lambda: next(markers))
    job.r.sleep = Mock(side_effect=AssertionError("waited on a page that had moved on"))
    job._wait_for_human_check("captcha or bot check on the page",
                              before=("https://jobs.example/", "Just a moment..."))


def test_headless_a_whole_page_check_gets_a_moment_to_clear_itself(monkeypatch):
    job = _human_check_job(monkeypatch, [0], headless=True)
    seen = iter([("u", "Just a moment..."), ("u", "Just a moment..."), ("u", "Apply")])
    monkeypatch.setattr(job, "_page_marker", lambda: next(seen))
    naps = []
    job.r.sleep = naps.append
    job._wait_for_human_check("captcha or bot check on the page", before=("u", "Just a moment..."))
    assert len(naps) == 2


def test_a_queue_write_that_fails_while_recording_the_ats_never_ends_the_job(monkeypatch):
    job = _job()
    calls = []

    def _locked(*a, **kw):
        calls.append(kw.get("ats"))
        raise TimeoutError("the queue lock is held")
    monkeypatch.setattr(apply_run.apply_queue, "update", _locked)
    job._admit_ats_transition("https://careers-gtsx.icims.com/jobs/1605/login", job.page.url)
    assert len(calls) == 2                     # once, then once more after the wait
    assert job.entry["ats"]["system"] == "icims"
    assert "careers-gtsx.icims.com" in job.allowed


def test_a_challenge_that_closed_before_the_wait_is_done(monkeypatch):
    job = _human_check_job(monkeypatch, [480, 0])
    job.page.evaluate = lambda *a, **k: "the same page"
    naps = []
    job.r.sleep = naps.append
    assert job._human_check_showing()
    job._wait_for_human_check("a CAPTCHA challenge is showing")
    assert naps == [] and job.last_sig is None


def test_a_destination_that_parks_is_the_tab_the_job_keeps(monkeypatch):
    job = _job()
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    monkeypatch.setattr(apply_run.apply_queue, "update", lambda *a, **kw: None)
    job._follow_popup(Mock(url="https://careers.example/apply"))
    source = job.page
    tab = Mock(url="https://unrelated.example/collect", is_closed=lambda: False)
    with pytest.raises(apply_run._Parked, match="allowed"):
        job._follow_popup(tab)
    assert job.page is tab and job._parked_tab() is tab
    assert source is not tab


def test_every_hop_of_a_redirect_chain_is_watched(monkeypatch):
    job = _job()
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    monkeypatch.setattr(apply_run.apply_queue, "update", lambda *a, **kw: None)
    tabs = [Mock(url=f"https://careers.example/hop{i}") for i in range(4)]
    hops = iter(tabs[1:])
    monkeypatch.setattr(job, "_await_destination",
                        lambda page: (next(hops, page), {}))
    watched = []
    monkeypatch.setattr(job, "_watch", watched.append)
    job._follow_popup(tabs[0], source_url=job.page.url)
    assert job.page is tabs[3]
    assert [id(t) for t in watched] == [id(t) for t in tabs]


@pytest.mark.parametrize("flag", ["_final_advance", "_code_may_send", "_link_may_send",
                                  "_pause_sent", None])
def test_a_dropped_load_after_a_step_that_may_have_sent_is_never_loaded_again(monkeypatch, flag):
    job = _job()
    job.page = Mock(url="chrome-error://chromewebdata/")
    monkeypatch.setattr(job, "_held_load", lambda page: (
        "https://www.linkedin.com/jobs/2", "GET", "net::ERR_CONNECTION_RESET"))
    monkeypatch.setattr(apply_run, "_error_page_up", lambda *a: None)
    if flag:
        setattr(job, flag, True)
    with pytest.raises(apply_run._Parked) as p:
        job._recover_error_page(job.page)
    if flag:
        assert p.value.reason.startswith(apply_run.CHECK_SENT_REASON), p.value.reason
        job.page.goto.assert_not_called()
    else:
        assert "again after one retry" in p.value.reason
        job.page.goto.assert_called_once()


def test_an_apply_entry_whose_text_cannot_be_read_is_never_clicked(monkeypatch):
    job = _job()
    rec = {"clicked": []}
    job.pages.append(rec)
    clicked = []
    monkeypatch.setattr(apply_run, "click_entry", lambda *a, **k: clicked.append(a))
    monkeypatch.setattr(apply_run.apply_form, "live_text", lambda loc: {})
    with pytest.raises(apply_run._NotClicked, match="could not be read"):
        job._click_entry(rec, object(), "Apply", how="linkedin_handler")
    assert clicked == [] and rec["clicked"] == []


def test_a_queue_write_that_fails_while_noting_a_missing_answer_never_ends_the_job(monkeypatch):
    job = _job()
    calls = []

    def _locked(*a, **kw):
        calls.append(a)
        raise TimeoutError("the queue lock is held")
    monkeypatch.setattr(apply_run.apply_queue, "add_missing", _locked)
    job._add_missing("Years of experience", "required")
    assert len(calls) == 2
    assert job.missing[0]["question"] == "Years of experience"
