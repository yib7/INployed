"""`apply_run` fails closed: the submit switch, the gate's reads and the
steps around a send park the job whenever the run cannot be sure, over the
same local fixtures, `FakeJev` and temp queue as `test_apply_run.py` (whose
fixtures this module reuses). No real store, profile or site is touched."""
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_queue  # noqa: E402
import apply_run  # noqa: E402
import apply_send_words  # noqa: E402
import settings  # noqa: E402
import test_apply_run as base  # noqa: E402

# `test_apply_run`'s helpers, and its fixtures by name (two of them autouse)
_enqueue, _entry, _runner = base._enqueue, base._entry, base._runner
_ok_plan, _ok_verification = base._ok_plan, base._ok_verification
_fast_timing, _hermetic = base._fast_timing, base._hermetic
catalog_builder, context, job_folder = base.catalog_builder, base.context, base.job_folder

pytest_plugins = ["conftest_browser", "conftest_jev"]


# --- the submit switch: only the boolean True sends ------------------------------------

def _config_bytes(raw: bytes) -> Path:
    path = settings.target_path("config")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


@pytest.mark.parametrize("raw, why", [
    (b'{"auto_apply_submit": false,', "config.json is not valid JSON"),
    # PowerShell 5.1's Out-File writes UTF-16 with a BOM: no JSON reader here takes it
    ('{"auto_apply_submit": false}'.encode("utf-16"), "config.json is not valid JSON"),
    (b'["auto_apply_submit"]', "config.json does not hold a settings object"),
])
def test_an_unreadable_config_runs_in_park_mode_and_says_why(raw, why, caplog):
    _config_bytes(raw)
    assert settings.read_problem("config") == why
    cfg = apply_run.load_settings()
    assert cfg["auto_apply_submit"] is False
    assert why in cfg[apply_run.PARK_MODE_KEY]
    assert "park mode" in caplog.text
    assert not apply_run.submit_on(cfg)


@pytest.mark.parametrize("value", ["false", "true", 1, 0, None, "yes"])
def test_a_submit_switch_that_is_not_a_boolean_parks(value):
    _config_bytes(json.dumps({"auto_apply_submit": value}).encode())
    assert settings.read_problem("config") == ""
    cfg = apply_run.load_settings()
    assert cfg["auto_apply_submit"] is False
    assert "auto_apply_submit holds" in cfg[apply_run.PARK_MODE_KEY]


@pytest.mark.parametrize("raw, on", [(None, True), (b'{"auto_apply_submit": true}', True),
                                     (b'\xef\xbb\xbf{"auto_apply_submit": true}', True),
                                     (b'{"auto_apply_submit": false}', False)])
def test_a_readable_switch_is_itself_with_no_park_note(raw, on):
    if raw is not None:
        _config_bytes(raw)
    cfg = apply_run.load_settings()
    assert cfg["auto_apply_submit"] is on
    assert apply_run.PARK_MODE_KEY not in cfg


def test_a_settings_module_that_fails_parks(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("broken")
    monkeypatch.setattr(settings, "load", _boom)
    cfg = apply_run.load_settings()
    assert cfg["auto_apply_submit"] is False
    assert "RuntimeError" in cfg[apply_run.PARK_MODE_KEY]


@pytest.mark.parametrize("value", ["false", "true", 1, None])
def test_can_submit_accepts_only_the_boolean_true(value):
    ver = _ok_verification()
    assert apply_run.can_submit(_ok_plan(), ver, {"auto_apply_submit": value}) == (
        False, "auto_apply_submit is off")
    assert apply_run.can_submit(_ok_plan(), ver, {})[0] is False
    assert apply_run.can_submit(_ok_plan(), ver, {"auto_apply_submit": True}) == (True, "")


def test_a_runner_given_a_string_switch_parks_and_reports_why(
        context, fixture_url, job_folder, catalog_builder, tmp_path, capsys):
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    runner = _runner(context, tmp_path, auto_apply_submit="false")
    runner.drain_report = True
    out = runner.drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    assert out.reason == "auto_apply_submit is off"
    page = next(p for p in context.pages if not p.is_closed())
    assert page.locator("body[data-submitted]").count() == 0
    why = runner.park_mode_why
    assert why.startswith("Park mode: auto_apply_submit holds str")
    assert capsys.readouterr().out.count(why) == 1
    report = next(tmp_path.glob(f"{apply_run.DRAIN_REPORT_PREFIX}*.md"))
    assert why in report.read_text(encoding="utf-8")


# --- `one`: a job whose browser never opened goes back to the queue ------------------

def test_one_gives_the_job_back_when_the_browser_never_opens(monkeypatch, job_folder,
                                                             tmp_path, capsys):
    import jev_harness
    import jev_switch
    monkeypatch.setattr(apply_run, "_load_env", lambda: None)
    monkeypatch.setattr(apply_run, "load_settings", lambda: dict(apply_run.DEFAULT_SETTINGS))
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: True)
    monkeypatch.setattr(apply_run.jev, "get", lambda mode="": jev_harness.judge())

    def _no_browser(self):
        raise ModuleNotFoundError("No module named 'playwright'")
    monkeypatch.setattr(apply_run.Runner, "_launch", _no_browser)
    _enqueue(job_folder, "https://boards.greenhouse.io/acme/jobs/1")
    assert apply_run.main(["one", "42", "--jev", "typesafe",
                           "--profile", str(tmp_path / "profile")]) == 1
    entry = _entry()
    assert entry["status"] == "queued"
    assert int(entry.get("attempts") or 0) == 0
    assert apply_queue.outages(entry) == 0


# --- `_site`: a suffix that holds many owners never groups them ---------------------

@pytest.mark.parametrize("a, b", [
    ("careers.acme.ltd.uk", "evil.ltd.uk"), ("jobs.agency.gc.ca", "other.gc.ca"),
    ("x.gob.mx", "y.gob.mx"), ("emploi.gouv.fr", "phish.gouv.fr"), ("a.govt.nz", "b.govt.nz"),
    ("city.tx.us", "evil.tx.us"), ("bucket.s3.amazonaws.com", "other.s3.amazonaws.com"),
    ("jane.me.uk", "john.me.uk"), ("acme.plc.uk", "evil.plc.uk")])
def test_hosts_under_a_shared_suffix_are_different_sites(a, b):
    assert apply_run._site(a) != apply_run._site(b)


@pytest.mark.parametrize("host, site", [
    ("jobs.lever.co", "lever.co"), ("boards.greenhouse.io", "greenhouse.io"),
    ("acme.wd5.myworkdayjobs.com", "myworkdayjobs.com"), ("jobs.example.co.uk", "example.co.uk"),
    ("careers.example.com", "example.com"), ("uk.linkedin.com", "linkedin.com"),
    ("app.jobright.ai", "jobright.ai"), ("x.clarity.ms", "clarity.ms"),
    ("acme.personio.de", "personio.de"), ("127.0.0.1", "127.0.0.1"),
    ("careers.acme.de", "careers.acme.de")])
def test_site_keeps_the_known_sites_and_the_generic_second_levels(host, site):
    assert apply_run._site(host) == site


# --- the account step's sign-in exemption covers a sign-in and nothing more ----------

@pytest.mark.parametrize("text, sends", [
    ("Sign in to apply", False), ("Log in", False), ("Send code", False),
    ("Send me a link", False), ("Log in and submit application", True),
    ("Sign in and finish", True), ("Sign in and send application", True),
    ("Sign in and complete application", True)])
def test_a_sign_in_worded_send_still_reads_as_a_send(text, sends):
    import apply_form
    digest = apply_form.FormDigest(url_host="x.example", title="", text="",
                                   buttons=[apply_form.Button(0, (0, "button"), text, "submit")])
    assert apply_send_words._send_worded(text, account=True) is sends
    assert apply_send_words._sends_application(digest, 0, account_only=True) is sends


# --- a create-account link with no address of its own is clicked --------------------

@pytest.mark.parametrize("name", ["login_signup_hash_link.html", "login_signup_js_link.html"])
def test_a_scripted_signup_link_is_clicked_and_reaches_the_signup_form(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch, name):
    import ats_accounts
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    _enqueue(job_folder, fixture_url(name))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    assert ats_accounts.lookup("127.0.0.1")["email"] == "jane.doe@example.com"


@pytest.mark.parametrize("target, scripted", [
    ("http://h.example/login#", True), ("http://h.example/login#signup", True),
    ("javascript:void(0)", True), ("mailto:x@example.com", True),
    ("http://h.example/signup", False), ("http://h.example/signup#top", False)])
def test_scripted_link(target, scripted):
    assert apply_run._scripted_link(target, "http://h.example/login") is scripted


# --- after the submit click, a standing form needs received words that are new ------

def _confirmation_digest():
    import apply_form
    return apply_form.FormDigest(
        "jobs.example", "Apply", "We have received your application. Please fix the errors.",
        fields=[], buttons=[apply_form.Button(0, (0, "button"), "Submit application", "submit")])


def _confirmation_answers():
    import jev
    return {"page_state": jev.Answer(kind="choice", choice="confirmation", confidence=0.9,
                                     probabilities={"confirmation": 0.9})}


def test_received_words_shown_before_the_click_do_not_confirm_a_standing_form():
    digest = _confirmation_digest()
    step, why, _ = apply_run.confirmation_step(
        digest, _confirmation_answers(), 0.9, submit_clicked=True,
        before="Apply. We have received your application. Fill the form.")
    assert step == "park" and why.startswith(apply_run.CHECK_SENT_REASON), why
    step, _, _ = apply_run.confirmation_step(digest, _confirmation_answers(), 0.9,
                                             submit_clicked=True, before="Apply. Fill the form.")
    assert step == "submitted"


def test_received_words_after_a_code_step_with_a_form_park_as_check_sent():
    step, why, _ = apply_run.confirmation_step(_confirmation_digest(), _confirmation_answers(),
                                               0.9, submit_clicked=False, code_sent=True)
    assert step == "park" and why.startswith(apply_run.CHECK_SENT_REASON), why
    assert "after the emailed code" in why


# --- the sign-in's navigation guard stays on while a challenge is solved -------------

def test_the_navigation_guard_stays_on_through_a_sign_in_challenge(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    import dataclasses

    import ats_accounts
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "synthetic-password")
    on: list[bool] = []
    seen: list[bool] = []
    armed: list[bool] = []
    start, stop = apply_run._NavGuard.start, apply_run._NavGuard.stop
    monkeypatch.setattr(apply_run._NavGuard, "start", lambda g: (on.append(True), start(g)))
    monkeypatch.setattr(apply_run._NavGuard, "stop", lambda g: (on.append(False), stop(g)))
    click = apply_run._Accounts._click

    def _quiet(self, page, digest, n):
        # the sign-up's click lands and the page answers nothing yet
        result = click(self, page, digest, n)
        quiet = any(f.secret for f in digest.fields)
        armed.append(quiet)
        return dataclasses.replace(result, changed=False) if quiet else result

    def _wait(self, why, **kw):
        seen.append(on[-1])
        raise apply_run._Parked("needs_human", why)
    monkeypatch.setattr(apply_run._Accounts, "_click", _quiet)
    monkeypatch.setattr(apply_run._JobRun, "_human_check_showing",
                        lambda self, checkbox=False: bool(armed and armed[-1]) and not checkbox)
    monkeypatch.setattr(apply_run._JobRun, "_wait_for_human_check", _wait)
    _enqueue(job_folder, fixture_url("login_signup_hash_link.html"))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert seen == [True], (out, on)
    assert out.reason.startswith("a CAPTCHA challenge appeared at sign-in"), out
    assert on[-1] is False


# --- the LinkedIn page's wait ends with the job's time ------------------------------

def test_the_linkedin_wait_ends_with_the_jobs_deadline(monkeypatch):
    import apply_linkedin
    monkeypatch.setattr(apply_linkedin, "read", lambda page, **kw: apply_linkedin.View())
    now = [100.0]

    class _Page:
        waits: list[int] = []

        def wait_for_timeout(self, ms):
            self.waits.append(ms)
            now[0] += ms / 1000

    page = _Page()
    apply_run.linkedin_view(page, wait_s=20, clock=lambda: now[0], deadline=101.0)
    assert 0 < sum(page.waits) <= 1000 + apply_run.LINKEDIN_POLL_MS
    page.waits.clear()
    apply_run.linkedin_view(page, wait_s=20, clock=lambda: now[0], deadline=now[0] - 5)
    assert page.waits == []


# --- the probe's words follow the loop on SSO-only and emailed-link pages ------------

def test_the_probe_parks_a_screen_whose_only_way_on_is_another_sites_sign_in():
    import apply_form
    from apply_judge import FillPlan
    digest = apply_form.FormDigest("jobs.example", "Sign in", "Sign in to apply", buttons=[
        apply_form.Button(0, (0, "#b0"), "Sign in with Google"),
        apply_form.Button(1, (0, "#b1"), "Continue with Microsoft")])
    step = apply_run.loop_step("https://jobs.example/login", digest, FillPlan(),
                               "login_wall", 0.9)
    assert step.startswith(f"park: {apply_run.SSO_REASON} (Google, Microsoft)"), step


@pytest.mark.parametrize("state", ["application_form", "confirmation", "signup_form"])
def test_the_probe_takes_a_page_that_says_a_link_was_emailed_as_the_link_step(state):
    import apply_form
    from apply_judge import FillPlan
    digest = apply_form.FormDigest("jobs.example", "Account",
                                   "Check your inbox and open the link we sent.", fields=[
                                       apply_form.Field(0, (0, "#f0"), "I agree", "checkbox",
                                                        False)])
    step = apply_run.loop_step("https://jobs.example/verify", digest, FillPlan(), state, 0.9)
    assert step.endswith("the code step (the emailed code from the inbox)"), step


# --- a job's cost survives a reset of the resettable counter -------------------------

def test_a_jobs_cost_counts_every_request_across_a_usage_reset(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    import jev

    def _drive(self):
        jev.count_usage(100)
        jev.reset_usage()           # another caller in the process zeroes `usage()`
        jev.count_usage(50)
        raise apply_run._Parked("needs_human", "synthetic stop")
    monkeypatch.setattr(apply_run._JobRun, "_drive", _drive)
    _enqueue(job_folder, fixture_url("ashby_steps.html"))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.reason == "synthetic stop", out
    assert out.jev_usage["requests"] == 2 and out.jev_usage["input_tokens"] == 150


# --- the CLI: a cap under one is refused; output survives a narrow console -----------

@pytest.mark.parametrize("cap", ["0", "-1", "two"])
def test_a_drain_cap_under_one_is_refused(cap, capsys):
    with pytest.raises(SystemExit) as e:
        apply_run.main(["drain", "--cap", cap])
    assert e.value.code == 2
    assert "--cap" in capsys.readouterr().err


def test_probe_lines_survive_a_console_that_cannot_show_them():
    import io
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    apply_run._line("  title: Ingénieur → 日本", out=out)
    out.flush()
    assert raw.getvalue().decode("cp1252").startswith("  title: Ingénieur ? ")


# --- the trace's screenshots mask the sensitive boxes ---------------------------------

def test_the_screenshots_mask_every_sensitive_box(
        context, fixture_url, job_folder, catalog_builder, tmp_path, monkeypatch):
    import apply_trace
    masked: list[set[str]] = []
    shot = apply_trace.Trace.screenshot

    def _shot(self, page, name, *, extra_mask=()):
        ids: set[str] = set()
        for loc in apply_trace.mask_locators(page, extra_mask):
            try:
                ids |= set(loc.evaluate_all("els => els.map(e => e.id)"))
            except Exception:       # noqa: BLE001  (a frame gone)
                continue
        masked.append(ids)
        return shot(self, page, name, extra_mask=extra_mask)
    monkeypatch.setattr(apply_trace.Trace, "screenshot", _shot)
    _enqueue(job_folder, fixture_url("sensitive_optional.html"))
    out = _runner(context, tmp_path, auto_apply_submit=False).drain(cap=1)[0]
    assert out.status == "ready_to_submit", out
    assert masked and all({"ssn", "birth"} <= ids for ids in masked), masked
    assert all("first_name" not in ids for ids in masked)
