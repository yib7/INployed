"""The runner against a hostile page (Phase 4 slice B probes, kept as
regression tests): the job's ATS account is pinned, a password goes only over
https, Chrome keeps its sandbox and takes no downloads, an emailed code comes
only from the job's own senders, the queue never loses jobs to a read error,
and a page's boxes are capped.

Synthetic pages and data only: FakeJev, routed hosts, tmp_path files."""
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_form  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_judge  # noqa: E402
import apply_queue  # noqa: E402
import apply_run  # noqa: E402
import jev  # noqa: E402

pytest_plugins = ["conftest_browser"]

_OWN = "https://cboe.wd1.myworkdayjobs.com/en-US/External/job/42/apply"
_OTHER = "https://othercorp.wd5.myworkdayjobs.com/en-US/External/job/99/apply"


def _job(apply_url: str = _OWN, ats: dict | None = None,
         inbox_url: str = "https://mail.google.com/mail/u/0/#inbox") -> "apply_run._JobRun":
    """A `_JobRun` on a Workday job (by default), its allowlist built, its
    page on the apply URL."""
    runner = apply_run.Runner(jev=jev.FakeJev(), settings={}, context=Mock(),
                              run_context={"signup_email": "jane.doe@example.com",
                                           "inbox_url": inbox_url},
                              sleep=lambda seconds: None)
    entry = {"job_posting_id": "42", "apply_url": apply_url, "company": "Fabrikam",
             "ats": ats if ats is not None else {"domain": _OWN.split("/")[2],
                                                 "system": "workday"}}
    run = apply_run._JobRun(runner, Mock(), entry)
    run._build_allowlist()
    run.page = Mock(url=apply_url)
    return run


# === the job's ATS account is pinned ======================================================

_HOP = f"""<!doctype html><html><head><title>Apply</title></head><body><h1>Loading</h1>
<script>location.replace({_OTHER!r});</script></body></html>"""


def test_b3_a_page_moving_the_job_to_another_workday_tenant_parks(_browser, flow_server,
                                                                  tmp_path):
    f = h.Flow("b3_tenant_switch", _OWN, False, "needs_human",
               re.escape(apply_run.TENANT_REASON), password=True,
               ats={"system": "workday", "domain": "cboe.wd1.myworkdayjobs.com"},
               routes=lambda base: {"https://cboe.wd1.myworkdayjobs.com/**": _HOP,
                                    "https://othercorp.wd5.myworkdayjobs.com/**": h._COMBINED})
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.status == "needs_human", r.reason
    assert apply_run.TENANT_REASON in r.reason and "othercorp.wd5" in r.reason
    assert not [a for a in r.actions if a.secret]
    assert not [a for a in r.actions if a.host == "othercorp.wd5.myworkdayjobs.com"
                and a.kind == "fill"]


def test_b3_the_jobs_own_tenant_and_shared_hosts_stay_allowed():
    run = _job()
    for host in ("cboe.wd1.myworkdayjobs.com", "cboe.wd5.myworkdayjobs.com",
                 "wd5.myworkdaysite.com", "cboe.wd1.myworkdaysite.com"):
        assert run._tenant_departure(host) == "", host
        assert run._allowed_site(host), host
        assert run._password_ok(f"https://{host}/login"), host
    run._check_host("https://cboe.wd5.myworkdayjobs.com/en-US/External/login")


@pytest.mark.parametrize("url", [
    "https://othercorp.wd5.myworkdayjobs.com/en-US/External/login",   # another company
    "https://othercorp.wd1.myworkdaysite.com/recruiting/othercorp",   # same vendor, other site
    "https://careers-othercorp.icims.com/jobs/1/login",               # another platform
    "https://boards.greenhouse.io/othercorp/jobs/1",                  # another platform
])
def test_b3_another_tenant_or_platform_departs_from_the_pinned_account(url):
    run = _job()
    host = apply_run._host(url)
    assert run._tenant_departure(host) == "cboe.wd1.myworkdayjobs.com"
    assert not run._allowed_site(host)
    assert not run._password_ok(url)
    assert not run._link_ok(host)
    with pytest.raises(apply_run._Parked, match=re.escape(apply_run.TENANT_REASON)):
        run._check_host(url)


def test_b3_a_move_to_another_tenant_parks_unless_linkedin_or_the_board_led_there():
    run = _job()
    with pytest.raises(apply_run._Parked, match=re.escape(apply_run.TENANT_REASON)):
        run._admit_ats_transition(_OTHER, _OWN)
    assert "othercorp.wd5.myworkdayjobs.com" not in run.ats_hosts
    # LinkedIn's Apply naming the job's account is where the job lives
    led = _job(apply_url="https://www.linkedin.com/jobs/view/42",
               ats={"domain": "www.linkedin.com", "system": "linkedin"})
    led._admit_ats_transition(_OWN, "https://www.linkedin.com/jobs/view/42")
    assert "cboe.wd1.myworkdayjobs.com" in led.ats_hosts
    with pytest.raises(apply_run._Parked, match=re.escape(apply_run.TENANT_REASON)):
        led._check_host(_OTHER)


def test_b3_the_first_ats_page_a_careers_site_leads_to_is_pinned():
    run = _job(apply_url="https://careers.fabrikam.example/jobs/42",
               ats={"domain": "careers.fabrikam.example", "system": "other"})
    assert not run._pinned_hosts()
    # an unpinned job may meet any platform (the careers site's own ATS)
    assert run._tenant_departure("careers-fabrikam.icims.com") == ""
    run._pin_first_tenant("https://careers-fabrikam.icims.com/jobs/42/login")
    assert run._pinned_hosts() == ["careers-fabrikam.icims.com"]
    assert run._tenant_departure("login.icims.com") == ""
    assert run._tenant_departure("careers-othercorp.icims.com") == "careers-fabrikam.icims.com"


def test_b3_a_tenant_on_its_url_path_leaves_the_platform_shared():
    """Greenhouse and Lever name the company on the path: a host names no
    tenant there, so every host of the platform stays the job's."""
    run = _job(apply_url="https://boards.greenhouse.io/fabrikam/jobs/42",
               ats={"domain": "boards.greenhouse.io", "system": "greenhouse"})
    assert run._tenant_departure("job-boards.greenhouse.io") == ""
    assert run._tenant_departure("jobs.lever.co")       # another platform still departs


# === a password goes only over https ======================================================

def test_a2_insecure_is_http_off_this_machine():
    assert apply_run._insecure("http://careers.fabrikam.example/apply")
    assert not apply_run._insecure("https://careers.fabrikam.example/apply")
    for url in ("http://127.0.0.1:8000/a", "http://localhost/a", "http://[::1]:9/a",
                "http://app.localhost/a", "about:blank", "", "careers.fabrikam.example"):
        assert not apply_run._insecure(url), url


def test_a2_the_password_check_refuses_an_http_page_frame_or_form_action():
    run = _job(apply_url="https://careers.fabrikam.example/jobs/42",
               ats={"domain": "careers.fabrikam.example", "system": "other"})
    assert run._password_ok("https://careers.fabrikam.example/login")
    assert not run._password_ok("http://careers.fabrikam.example/login")
    why = run._secret_refusal(["https://careers.fabrikam.example/login",
                               "http://careers.fabrikam.example/session"])
    assert why.startswith(apply_run.PASSWORD_HTTP_REASON)
    assert "careers.fabrikam.example" in why
    assert run._secret_refusal(["https://careers.fabrikam.example/login"]) == ""
    # this machine's own loopback is exempt from the https rule
    assert not run._secret_refusal(["http://127.0.0.1:8000/login"]).startswith(
        apply_run.PASSWORD_HTTP_REASON)


def test_a2_a_signup_over_http_parks_with_no_password_typed(_browser, flow_server, tmp_path):
    start = "http://careers.fabrikam.example/apply/42"
    f = h.Flow("a2_http_signup", start, False, "needs_human", "unencrypted", password=True,
               ats={"system": "other", "domain": "careers.fabrikam.example"},
               routes=lambda base: {"http://careers.fabrikam.example/**": h._COMBINED})
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.status == "needs_human", r.reason
    assert apply_run.PASSWORD_HTTP_REASON in r.reason
    assert not [a for a in r.actions if a.secret]


def test_a2_the_same_signup_over_https_still_types_the_password(_browser, flow_server, tmp_path):
    """The user's rule: the job's own careers site gets the master password."""
    start = "https://careers.fabrikam.example/apply/42"
    f = h.Flow("a2_https_signup", start, False, "ready_to_submit", ".*", password=True,
               ats={"system": "other", "domain": "careers.fabrikam.example"},
               routes=lambda base: {"https://careers.fabrikam.example/**": h._COMBINED})
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert {a.host for a in r.actions if a.secret} == {"careers.fabrikam.example"}


# === Chrome keeps its sandbox and takes no downloads =====================================

def test_b1_every_launch_keeps_the_sandbox_and_refuses_downloads(tmp_path, monkeypatch):
    seen: list[dict] = []

    class _Chromium:
        def launch_persistent_context(self, user_data_dir, **kw):
            seen.append(kw)
            raise RuntimeError("no browser here")

    monkeypatch.setattr(apply_run, "_sweep_check_copies", lambda *a: None)
    with pytest.raises(RuntimeError):
        apply_run.launch_profile(SimpleNamespace(chromium=_Chromium()), tmp_path / "profile",
                                 headless=True)
    assert len(seen) == 2                     # installed Chrome, then the bundled build
    for kw in seen:
        assert kw.get("chromium_sandbox") is True
        assert kw.get("accept_downloads") is False


def test_b1_the_probe_browser_keeps_the_sandbox_and_refuses_downloads(monkeypatch):
    launches: list[dict] = []
    contexts: list[dict] = []

    class _Browser:
        def new_context(self, **kw):
            contexts.append(kw)
            raise RuntimeError("stop here")

        def close(self):
            pass

    class _Chromium:
        def launch(self, **kw):
            launches.append(kw)
            return _Browser()

    class _PW:
        chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import playwright.sync_api as sync_api
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _PW())
    with pytest.raises(RuntimeError, match="stop here"):
        apply_run.probe("https://careers.fabrikam.example/jobs/42")
    assert launches and all(kw.get("chromium_sandbox") is True for kw in launches)
    assert contexts and all(kw.get("accept_downloads") is False for kw in contexts)


@pytest.mark.skipif(sys.platform != "win32", reason="reads the process list on Windows")
def test_b1_the_live_browser_runs_without_no_sandbox(_browser, tmp_path):
    """Chromium started with the runner's launch options: its command line
    carries no `--no-sandbox`, and the pages still load."""
    profile = tmp_path / f"sandbox-{tmp_path.name}"
    ctx = _browser.browser_type.launch_persistent_context(
        str(profile), headless=True, viewport=apply_run.VIEWPORT, **apply_run.LAUNCH_HARDENING)
    try:
        page = ctx.new_page()
        page.set_content("<p id=x>sandboxed</p>")
        assert page.inner_text("#x") == "sandboxed"
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
              f"'*{profile.name}*' }} | ForEach-Object {{ $_.CommandLine }}")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60).stdout
    finally:
        ctx.close()
    lines = [line for line in out.splitlines() if "--user-data-dir" in line]
    assert lines, "the browser process was not found"
    assert not any("--no-sandbox" in line for line in lines)


# === an emailed code comes only from the job's own senders ================================

@pytest.mark.parametrize("url,system", [
    ("https://linkedin-careers.evil.example/apply/42", "other"),
    ("https://workday-jobs.evil.example/apply", "other"),
    ("https://greenhouse.evil.example/apply", "other"),
    ("https://icims.evil.example/apply", "other"),
    ("https://evil.example/linkedin.com/jobs", "other"),
    ("https://linkedin.com.evil.example/x", "other"),
    ("https://objectstorage.us-ashburn-1.oraclecloud.com/n/x/b/y/o/apply.html", "other"),
    ("https://www.linkedin.com/jobs/view/42", "linkedin"),
    ("https://cboe.wd1.myworkdayjobs.com/en-US/External/job/42", "workday"),
    ("https://boards.greenhouse.io/fabrikam/jobs/42", "greenhouse"),
    ("https://jobs.lever.co/fabrikam/42", "lever"),
    ("https://careers-fabrikam.icims.com/jobs/42", "icims"),
    ("https://ehxx.fa.us2.oraclecloud.com/hcmUI/CandidateExperience", "oracle"),
    ("https://user@careers-fabrikam.icims.com:443/jobs/42", "icims"),
])
def test_b4_infer_ats_reads_the_site_never_a_substring(url, system):
    assert apply_queue.infer_ats(url)["system"] == system


_MESSAGES = [
    {"n": 0, "sender": "security-noreply@linkedin.com",
     "subject": "Your LinkedIn verification code",
     "preview": "Use this verification code to sign in to LinkedIn: 482913"},
    {"n": 1, "sender": "deals@shopfront.example", "subject": "Sale", "preview": "50% off"},
]


def test_b4_read_inbox_never_picks_a_refused_sender():
    """A judge that answers yes for the LinkedIn code (the probe's FakeJev
    did, for a host named `linkedin-careers.evil.example`): the sender check
    alone keeps it."""
    yes, no = jev.Answer("noul", noul=0.95), jev.Answer("noul", noul=0.05)
    answers = {"msg_0_from_site": yes, "msg_0_has_code": yes,
               "msg_1_from_site": no, "msg_1_has_code": no}
    assert apply_judge.read_inbox(answers, _MESSAGES, "code") == 0     # the judge alone
    run = _job(apply_url="https://careers.fabrikam.example/jobs/42",
               ats={"domain": "careers.fabrikam.example", "system": "other"})
    assert apply_judge.read_inbox(answers, _MESSAGES, "code",
                                  refuse=run._sender_refused) is None


@pytest.mark.parametrize("sender,refused", [
    ("security-noreply@linkedin.com", True),
    ("LinkedIn", True),                                    # a row with a name alone
    ("no-reply@accounts.google.com", True),                # the inbox provider (Gmail)
    ("noreply@okta.com", True),                            # an identity provider
    ("no-reply@us.greenhouse-mail.io", False),             # unknown to the lists
    ("no-reply@greenhouse.io", True),                      # another ATS than the job's
    ("careers-othercorp@icims.com", True),
    ("noreply@myworkday.com", False),                      # the job's own platform
    ("Fabrikam Recruiting <talent@fabrikam.example>", False),
    ("Workday", False),
])
def test_b4_the_sender_check_on_a_workday_job(sender, refused):
    assert _job()._sender_refused(sender) is refused


def test_b4_an_ats_sender_is_allowed_while_the_jobs_ats_is_unknown():
    run = _job(apply_url="https://careers.fabrikam.example/jobs/42",
               ats={"domain": "careers.fabrikam.example", "system": "other"},
               inbox_url="https://outlook.office.com/mail/")
    assert not run._sender_refused("no-reply@greenhouse.io")
    assert run._sender_refused("no-reply@microsoft.com")      # the Outlook inbox's provider
    assert run._sender_refused("security-noreply@linkedin.com")


def test_b4_the_runners_inbox_passes_the_sender_check(monkeypatch):
    run = _job()
    seen = {}

    def fake_fetch(page, site, inbox_url, **kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(apply_run.apply_inbox, "fetch_code", fake_fetch)
    monkeypatch.setattr(apply_run.apply_inbox, "fetch_link", fake_fetch)
    inbox = apply_run._Inbox(run)
    inbox.fetch_code(Mock(), "cboe.wd1.myworkdayjobs.com", "https://mail.google.com/mail/u/0/")
    assert seen["refuse"] == run._sender_refused
    seen.clear()
    inbox.fetch_link(Mock(), "cboe.wd1.myworkdayjobs.com", "https://mail.google.com/mail/u/0/")
    assert seen["refuse"] == run._sender_refused


# === the queue never loses jobs to a read error ===========================================

def test_c5_a_locked_mutation_raises_on_an_unreadable_queue_and_writes_nothing(
        tmp_path, monkeypatch):
    qp = tmp_path / "apply_queue.json"
    qp.write_text(json.dumps({"version": 1, "jobs": [
        apply_queue.new_entry("111", apply_url="https://example.com/a"),
        apply_queue.new_entry("222", apply_url="https://example.com/b")]}), encoding="utf-8")
    before = qp.read_bytes()
    real = Path.read_text

    def flaky(self, *a, **kw):
        if self == qp:
            raise PermissionError(32, "sharing violation (synthetic)")
        return real(self, *a, **kw)

    monkeypatch.setattr(apply_queue, "_READ_RETRY", 0)
    monkeypatch.setattr(Path, "read_text", flaky)
    with pytest.raises(apply_queue.QueueUnreadable):
        apply_queue.enqueue(apply_queue.new_entry("333", apply_url="https://example.com/c"),
                            path=qp)
    with pytest.raises(apply_queue.QueueLockTimeout):     # every lock handler catches it
        apply_queue.claim("apply_run", path=qp)
    # a lock-free reader still gets an empty queue, the file untouched
    with pytest.warns(RuntimeWarning):
        assert apply_queue.load(qp)["jobs"] == []
    monkeypatch.setattr(Path, "read_text", real)
    assert qp.read_bytes() == before
    assert [e["job_posting_id"] for e in apply_queue.load(qp)["jobs"]] == ["111", "222"]
    assert sorted(p.name for p in tmp_path.iterdir() if ".corrupt" in p.name) == []


# === a page's boxes are capped =============================================================

def _fields(n: int) -> list:
    return [apply_form.Field(n=i, locator=(0, f"#f{i}"), label=f"Box {i}", type="text",
                             required=False) for i in range(n)]


def test_b_low_page_requests_ask_about_at_most_fields_max(monkeypatch):
    seen: list[int] = []

    def questions(digest, catalog, job=None, *, fields=True):
        seen.append(len(digest.fields))
        return {"fields": []}, {"q": {}}

    monkeypatch.setattr(apply_judge, "page_questions", questions)
    digest = apply_form.FormDigest("careers.fabrikam.example", "Apply", "",
                                   fields=_fields(apply_judge.FIELDS_MAX + 50))
    apply_judge.page_requests(digest, apply_judge.FactCatalog(), {})
    assert seen == [apply_judge.FIELDS_MAX]


def test_b_low_the_extractor_keeps_one_field_past_the_cap():
    raw = {"fields": [{"css": f"#f{i}", "label": f"Box {i}", "type": "text",
                       "required": False} for i in range(apply_judge.FIELDS_MAX + 40)],
           "buttons": [], "text": ""}
    frame = Mock(url="https://careers.fabrikam.example/apply")
    frame.evaluate.return_value = raw
    page = Mock(url="https://careers.fabrikam.example/apply", frames=[frame],
                main_frame=frame)
    page.title.return_value = "Apply"
    digest = apply_form.extract(page)
    assert len(digest.fields) == apply_judge.FIELDS_MAX + 1


def test_b_low_a_page_with_more_boxes_than_the_cap_parks_before_the_judge():
    run = _job()
    run.r.jev = Mock(judge=Mock(side_effect=AssertionError("never asked")))
    digest = apply_form.FormDigest("cboe.wd1.myworkdayjobs.com", "Apply", "",
                                   fields=_fields(apply_judge.FIELDS_MAX + 1))
    with pytest.raises(apply_run._Parked, match=re.escape(apply_run.FIELDS_MAX_REASON)):
        run._map(digest, {}, "application_form")


# === sites ================================================================================

def test_b_low_oracle_cloud_counts_only_its_recruiting_hosts():
    assert apply_run._site("ehxx.fa.us2.oraclecloud.com") == "oraclecloud.com"
    user = "objectstorage.us-ashburn-1.oraclecloud.com"
    assert apply_run._site(user) == user
    assert apply_run._site(user) not in apply_run.ATS_SITES


@pytest.mark.parametrize("host", ["evil.weebly.com", "evil.myshopify.com", "evil.gitlab.io",
                                  "evil.squarespace.com", "evil.substack.com"])
def test_b_low_shared_hosting_subdomains_are_separate_sites(host):
    assert apply_run._site(host) == host
    assert apply_run._site(host) != apply_run._site("other." + host.split(".", 1)[1])


def test_b_low_shared_google_and_office_hosts_are_their_own_sites():
    for host in ("sites.google.com", "docs.google.com", "forms.office.com"):
        assert apply_run._site(host) == host


def test_b_low_the_password_box_check_reads_the_frame_resolve_uses():
    """The frames reordered since the read: the check and the fill both use
    the frame the box was read in (by its address)."""
    main = Mock(url="https://careers.fabrikam.example/apply")
    login = Mock(url="https://login.fabrikam.example/frame")
    other = Mock(url="https://ads.example/frame")
    page = Mock(url=main.url)
    page.frames = [main, login, other]
    page.main_frame = main
    page.title.return_value = "Apply"
    for f in page.frames:
        f.evaluate.return_value = {"fields": [], "buttons": [], "text": ""}
    apply_form.extract(page)
    page.frames = [main, other, login]                  # reordered after the read
    assert apply_form.resolve_frame(page, (1, "#pw")) is login
    run = _job()
    assert run._box_frame(page, (1, "#pw"), page.frames) is login


# === a career-site front end hands the job on =============================================

_FRONT = "https://cboe.eightfold.ai/careers/job/42"
_HANDOFF = f"""<!doctype html><html><head><title>Apply</title></head><body><h1>Apply</h1>
<script>location.replace({_OWN!r});</script></body></html>"""


def _front_end_job() -> "apply_run._JobRun":
    return _job(apply_url=_FRONT, ats={"domain": "cboe.eightfold.ai", "system": "other"})


def test_b3_a_front_end_is_never_the_pin_and_the_platform_it_hands_on_to_is():
    run = _front_end_job()
    assert "cboe.eightfold.ai" in run.ats_hosts and not run._pinned_hosts()
    assert run._tenant_departure("cboe.wd1.myworkdayjobs.com") == ""
    run._check_host(_OWN)
    run._pin_first_tenant(_OWN)
    assert run._pinned_hosts() == ["cboe.wd1.myworkdayjobs.com"]
    # the front end stays one of the job's hosts; another company's account departs
    assert run._tenant_departure("cboe.eightfold.ai") == ""
    assert run._allowed_site("cboe.eightfold.ai")
    with pytest.raises(apply_run._Parked, match=re.escape(apply_run.TENANT_REASON)):
        run._check_host(_OTHER)
    assert not run._sender_refused("no-reply@eightfold.ai")      # the job's own front end


def test_b3_a_front_end_landed_on_pins_nothing():
    run = _job(apply_url="https://careers.fabrikam.example/jobs/42",
               ats={"domain": "careers.fabrikam.example", "system": "other"})
    run._pin_first_tenant("https://fabrikam.phenompeople.com/us/en/job/42")
    assert "fabrikam.phenompeople.com" in run.ats_hosts and not run._pinned_hosts()
    run._pin_first_tenant("https://careers-fabrikam.icims.com/jobs/42/login")
    assert run._pinned_hosts() == ["careers-fabrikam.icims.com"]


def test_b3_a_front_end_handing_off_to_a_workday_tenant_does_not_park(_browser, flow_server,
                                                                     tmp_path):
    f = h.Flow("b3_front_end_handoff", _FRONT, False, "ready_to_submit", ".*", password=True,
               ats={"system": "other", "domain": "cboe.eightfold.ai"},
               routes=lambda base: {"https://cboe.eightfold.ai/**": _HANDOFF,
                                    "https://cboe.wd1.myworkdayjobs.com/**": h._COMBINED})
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.status == "ready_to_submit", r.reason
    assert {a.host for a in r.actions if a.secret} == {"cboe.wd1.myworkdayjobs.com"}


def test_b3_the_handed_on_workday_tenant_moving_to_another_parks(_browser, flow_server,
                                                                tmp_path):
    f = h.Flow("b3_front_end_then_switch", _FRONT, False, "needs_human",
               re.escape(apply_run.TENANT_REASON), password=True,
               ats={"system": "other", "domain": "cboe.eightfold.ai"},
               routes=lambda base: {"https://cboe.eightfold.ai/**": _HANDOFF,
                                    "https://cboe.wd1.myworkdayjobs.com/**": _HOP,
                                    "https://othercorp.wd5.myworkdayjobs.com/**": h._COMBINED})
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.status == "needs_human", r.reason
    assert "othercorp.wd5" in r.reason
    assert not [a for a in r.actions if a.secret]
