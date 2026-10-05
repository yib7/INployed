"""SP7: accounts and email.

- The ledger's account for a sign-in host that names no tenant, found by
  the job's own hosts (ACC-13).
- The account check by code in one-character boxes (ACC-06), never with a
  code older than the job (ACC-07), or by a link in the email opened on an
  allowed host only (ACC-05); the Workday account path to the gate.
- A create-account button (ACC-01), one sign-in without a ledger entry
  (ACC-02), an account that exists (ACC-03), password rules (ACC-04), a
  slow sign-up (ACC-09), an account step's error (ACC-10), a sign-in only
  with another site (ACC-11), a sign-up shown again (ACC-12).
- The password invariants the harness asserts on every run.

Headless Chromium through the module-scoped test browser for the flows; no
network, no judge but `FakeJev`, `NoisyJev` or a scripted one; the master
password is the harness's synthetic one."""
import dataclasses
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_harness as h  # noqa: E402
import apply_pages  # noqa: E402
import apply_run  # noqa: E402
import ats_accounts  # noqa: E402
import jev  # noqa: E402

pytest_plugins = ["conftest_browser"]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_ACCOUNTS_PATH", str(tmp_path / "accounts.json"))
    return tmp_path / "accounts.json"


def _job_run(tmp_path, *hosts: str) -> "apply_run._JobRun":
    """A `_JobRun` with no page, its admitted ATS hosts `hosts`."""
    runner = apply_run.Runner(jev=jev.FakeJev(), profile_dir=tmp_path / "profile",
                              settings={}, context=object(),
                              run_context={"signup_email": "jane.doe@example.com",
                                           "inbox_url": "https://mail.example.com/inbox"})
    run = apply_run._JobRun(runner, None, {"job_posting_id": "42"})
    run.ats_hosts.update(hosts)
    return run


# === the ledger by tenant (ACC-13) =======================================================================

def test_a_shared_sign_in_host_finds_the_account_made_on_the_jobs_careers_host(ledger, tmp_path):
    ats_accounts.record("careers-gtsx.icims.com", "jane.doe@example.com")
    run = _job_run(tmp_path, "careers-gtsx.icims.com")
    assert run._account_for("login.icims.com")["email"] == "jane.doe@example.com"
    # another tenant's job never takes this tenant's account
    other = _job_run(tmp_path, "careers-other.icims.com")
    assert other._account_for("login.icims.com") is None


def test_an_account_made_on_a_shared_sign_in_host_is_kept_under_the_jobs_tenant(ledger, tmp_path):
    run = _job_run(tmp_path, "careers-gtsx.icims.com")
    run._record_account("login.icims.com", "jane.doe@example.com")
    assert list(ats_accounts.list_accounts()) == ["careers-gtsx.icims.com"]
    # a host that names its tenant keeps the account under its own name
    run._record_account("cboe.wd1.myworkdayjobs.com", "jane.doe@example.com")
    assert "cboe.wd1.myworkdayjobs.com" in ats_accounts.list_accounts()


# === codes and links from the inbox (ACC-05, ACC-06, ACC-07) ===============================================

def _events(r, kind: str) -> list[dict]:
    import json
    out = []
    for p in sorted(Path(r.trace).glob("*.json")):
        out += [e for e in json.loads(p.read_text(encoding="utf-8")).get("events", [])
                if e.get("kind") == kind]
    return out


def test_a_code_in_six_boxes_is_typed_from_the_first_and_the_fresh_code_is_used(
        _browser, flow_server, tmp_path):
    # ACC-06: the six one-character boxes are one code field, typed from its
    # first box; ACC-07: the older code above it in the inbox is never read
    r = h.run_flow(h.flow("otp_six_boxes"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    codes = _events(r, "code")
    assert len(codes) == 1 and codes[0]["boxes"] == 6, codes
    record = (Path(r.trace).parent.parent / "apply_record.md").read_text(encoding="utf-8")
    assert "482915" not in record and "739104" not in record


_BOXES = ('<div role="group" aria-label="Security code">'
          + "".join(f'<input type="text" maxlength="1" id="c{i}" aria-label="Character {i}">'
                    for i in range(1, 7)) + "</div>")
_ADVANCE_JS = ("<script>const bs = Array.from(document.querySelectorAll('input'));"
               "bs.forEach((b, i) => b.addEventListener('input', () => { if (b.value && bs[i + 1]) "
               "bs[i + 1].focus(); }));</script>")


@pytest.mark.parametrize("moves_on", [True, False])
def test_the_six_boxes_hold_the_code_whether_or_not_they_move_the_focus_on(
        browser_page, tmp_path, moves_on):
    # ACC-06: typed from the first box; a widget that keeps the focus in the
    # first box gets each character in its own box
    import apply_form
    browser_page.set_content(f"<body>{_BOXES}{_ADVANCE_JS if moves_on else ''}</body>")
    digest = apply_form.extract(browser_page)
    assert [(f.label, f.widget, len(f.option_locators)) for f in digest.fields] == [
        ("Security code", "otp", 6)]
    run = _job_run(tmp_path)
    run.page = browser_page
    run._fill_otp(digest.fields[0], "7K4Q2Z")
    got = browser_page.evaluate("Array.from(document.querySelectorAll('input'), b => b.value)")
    assert "".join(got) == "7K4Q2Z"


def test_a_code_longer_than_the_boxes_parks_before_typing(browser_page, tmp_path):
    import apply_form
    browser_page.set_content(f"<body>{_BOXES}</body>")
    run = _job_run(tmp_path)
    run.page = browser_page
    with pytest.raises(apply_run._Parked, match="the code has 8 characters and the page 6 boxes"):
        run._fill_otp(apply_form.extract(browser_page).fields[0], "7K4Q2Z9X")
    assert browser_page.evaluate("Array.from(document.querySelectorAll('input'), b => b.value)"
                                 ".join('')") == ""


def test_the_workday_account_path_reaches_the_gate(_browser, flow_server, tmp_path):
    # the checkpoint: the start popup, the Create Account button (ACC-01),
    # the account checked by the emailed link (ACC-05), the sign-in, the
    # wizard; the link is opened in a tab of its own and never logged
    r = h.run_flow(h.flow("workday_signin_modal"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    links = _events(r, "inbox_link")
    assert len(links) == 1 and links[0]["found"] and links[0]["host"] == "127.0.0.1", links
    clicks = [a.text for a in r.actions if a.kind == "click"]
    assert "Create Account" in clicks and "Resend Account Verification" not in clicks, clicks
    job = Path(r.trace).parent.parent
    for path in job.rglob("*"):
        if path.is_file() and path.suffix in (".md", ".json"):
            assert "6f1c2e9a0b7d4e35" not in path.read_text(encoding="utf-8"), path


# === the account step's own way on ======================================================================

def _signin_digest(*buttons: str) -> "apply_run.apply_form.FormDigest":
    form = apply_run.apply_form
    return form.FormDigest("127.0.0.1", "Sign In", "Sign In", fields=[
        form.Field(0, (0, "#email"), "Email Address", "text", False),
        form.Field(1, (0, "#password"), "Password", "other", False, id_or_name="password",
                   secret=True)],
        buttons=[form.Button(i, (0, f"#b{i}"), t) for i, t in enumerate(buttons)])


@pytest.mark.parametrize("judged", ["Forgot your password?", "Resend Account Verification",
                                    "Cancel", "Back to Job Posting"])
def test_a_control_beside_the_step_judged_its_way_on_is_never_clicked(judged):
    # a noisy judge rated the reset link the advance and the Sign In other;
    # the step's own Sign In is the click (a reset link would mail a reset)
    from apply_judge import FillPlan
    digest = _signin_digest("Sign In", judged, "Create Account")
    plan = FillPlan(buttons={"advance": (1, 0.93), "other": (0, 0.94)})
    assert apply_run.account_advance(digest, plan, signup=False) == (
        0, apply_run.apply_judge.BUTTON_ADVANCE_MIN_CONF)


# === a sign-in with no account in the ledger and no sign-up (ACC-02) =====================================

_NO_SIGNUP = (h.FIXTURES_DIR / "forms" / "login_wall.html").read_text(encoding="utf-8").replace(
    '<p><a href="signup.html">Create an account</a></p>', "")
_WRONG_PASSWORD = _NO_SIGNUP.replace(
    "document.body.setAttribute('data-logged-in', '1');\n    window.location.href = 'ashby_steps.html';",
    "document.getElementById('login').insertAdjacentHTML('afterbegin', "
    "'<p role=alert>Wrong email or password.</p>');")


def _signin_flow(name: str, body: str) -> h.Flow:
    return h.Flow(name, f"{name}.html", False, "ready_to_submit", h._PARKED,
                  confirm="#received:visible", gate="#btn-submit:visible", password=True,
                  routes=lambda base: {f"{base}/forms/{name}.html": body})


def test_a_sign_in_with_no_sign_up_gets_one_try_and_its_account_joins_the_ledger(
        _browser, flow_server, tmp_path):
    import json
    assert "Create an account" not in _NO_SIGNUP
    r = h.run_flow(_signin_flow("login_no_signup", _NO_SIGNUP), jev.FakeJev(), "fake",
                   browser=_browser, server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    ledger = json.loads((Path(r.trace).parents[2] / "accounts.json").read_text(encoding="utf-8"))
    assert {k: v["email"] for k, v in ledger.items()} == {"127.0.0.1": h.SIGNUP_EMAIL}
    assert "signed in" in ledger["127.0.0.1"]["note"]


def test_a_sign_in_the_one_try_does_not_pass_parks_with_what_to_do(
        _browser, flow_server, tmp_path):
    assert "Wrong email or password" in _WRONG_PASSWORD
    r = h.run_flow(_signin_flow("login_wrong_password", _WRONG_PASSWORD), jev.FakeJev(), "fake",
                   browser=_browser, server=flow_server, workdir=tmp_path)
    assert (r.status, r.breaks) == ("needs_human", []), r.reason
    assert r.reason.startswith("no account on 127.0.0.1 took the master password (one sign-in "
                               "was tried"), r.reason
    fills = [a for a in r.actions if a.kind == "fill" and a.type == "password"]
    assert len(fills) == 1                  # typed once: never again toward a lockout
    assert not (Path(r.trace).parents[2] / "accounts.json").exists()


def test_an_account_step_that_raises_names_its_error_type_and_step(
        _browser, flow_server, tmp_path, monkeypatch):
    # ACC-10: the park says the account step failed and how (the exception's
    # type and the step), never the exception's words (they may quote a value)
    real = apply_run.apply_form.resolve

    def _resolve(page, locator):
        if locator[1] == "#signup_password":
            raise RuntimeError("synthetic failure quoting jane.doe@example.com")
        return real(page, locator)
    monkeypatch.setattr(apply_run.apply_form, "resolve", _resolve)
    r = h.run_flow(h.flow("signup_park"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.status == "needs_human" and not r.breaks, (r.reason, r.breaks)
    assert r.reason.startswith("account signup needed (") and r.reason.endswith(
        "; the account step failed: RuntimeError at accounts.fill)"), r.reason
    assert "synthetic failure" not in r.reason
    errors = _events(r, "error")
    assert errors == [{"kind": "error", "t": errors[0]["t"], "step": "accounts.fill",
                       "error": "RuntimeError"}], errors


class _Steps:
    """An accounts hook that notes which step the run asked for."""

    def __init__(self):
        self.calls: list[str] = []

    def login(self, page, digest, host):
        self.calls.append("login")
        return True

    def signup(self, page, digest, host):
        self.calls.append("signup")
        return True


def _boxes_digest(*boxes: tuple[str, str], forgot: bool = False):
    form = apply_run.apply_form
    fields = [form.Field(0, (0, "#email"), "Email", "email", False)]
    fields += [form.Field(i + 1, (0, f"#p{i}"), label, "other", False, autocomplete=auto,
                          secret=True)
               for i, (label, auto) in enumerate(boxes)]
    buttons = [form.Button(0, (0, "#go"), "Continue")]
    if forgot:
        buttons.append(form.Button(1, (0, "#forgot"), "Forgot your password?"))
    return form.FormDigest("127.0.0.1", "Account", "Account", fields=fields, buttons=buttons)


@pytest.mark.parametrize("read, boxes, forgot, step", [
    # a sign-up read as a sign-in: two boxes, or a new-password box
    ("login_wall", [("Password", ""), ("Confirm password", "")], False, "signup"),
    ("login_wall", [("Password", "new-password")], False, "signup"),
    # a sign-in read as a sign-up: its current-password box, or its one
    # box beside "Forgot your password?"
    ("signup_form", [("Password", "current-password")], False, "login"),
    ("signup_form", [("Password", "")], True, "login"),
    # boxes that say nothing leave the read as it is
    ("signup_form", [("Password", "")], False, "signup"),
    ("login_wall", [("Password", "")], False, "login"),
])
def test_the_password_boxes_decide_the_account_step_over_a_misread(tmp_path, read, boxes, forgot,
                                                                   step):
    from types import SimpleNamespace
    run = _job_run(tmp_path, "127.0.0.1")
    run.accounts = _Steps()
    run.page = SimpleNamespace(url="http://127.0.0.1/account")
    run._account_step(read, _boxes_digest(*boxes, forgot=forgot))
    assert run.accounts.calls == [step]


def _workday(name: str, **kw) -> h.Flow:
    base = h.flow("workday_signin_modal")
    import dataclasses
    return dataclasses.replace(base, name=name, **kw)


def test_a_verification_link_outside_the_allowed_hosts_is_named_and_never_opened(
        _browser, flow_server, tmp_path):
    f = _workday("workday_link_outside", inbox_page="link_outside_list.html",
                 status="needs_human", reason=r"^emailed verification link needed: ")
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.reason == ("emailed verification link needed: the email's link goes to "
                        "click.mailtrack.invalid, outside the application's sites; it was never "
                        "opened")
    assert _events(r, "inbox_link")[0]["refused"] == ["click.mailtrack.invalid"]


def test_a_verification_link_the_site_refuses_parks_with_its_words(_browser, flow_server,
                                                                  tmp_path):
    # the link's page comes from the server (the run fetches a link's page
    # past the test's routes): the message points it at the expired page
    message = (h.FIXTURES_DIR / "inbox" / "link_message.html").read_text(encoding="utf-8")
    assert "../forms/workday_account_verified.html?" in message
    f = _workday("workday_link_expired", status="needs_human",
                 reason=r"^emailed verification link needed: ",
                 routes=lambda base: {h.inbox_url("link_message.html"): message.replace(
                     "../forms/workday_account_verified.html?", "../forms/workday_link_expired.html?"
                 ).replace('href="../forms/', f'href="{base}/forms/')})
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.reason == ("emailed verification link needed: the site refused the emailed link "
                        "('link has expired')")


class _LinkPick:
    """FakeJev, with each request's question ids kept, and the link pick
    answered `pick` when one is given."""

    def __init__(self, pick: str | None = None):
        self.pick, self.asked = pick, []

    def judge(self, state, questions):
        self.asked.append(set(questions))
        out = jev.FakeJev().judge(state, questions)
        if self.pick and "link_pick" in out:
            out["link_pick"] = jev.Answer(kind="choice", choice=self.pick,
                                          probabilities={self.pick: 1.0}, confidence=1.0)
        return out


def test_the_judge_picks_the_accounts_link_from_a_message_that_holds_two(
        _browser, flow_server, tmp_path):
    # ACC-05's link pick in a run (SP8b): the message holds a job alerts'
    # confirmation first and the account's check second, both on the
    # application's site; the judge's pick is the link opened
    judge = _LinkPick()
    r = h.run_flow(h.flow("workday_link_pick"), judge, "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert [q for q in judge.asked if "link_pick" in q] == [{"link_pick"}]
    opened = [e for e in _events(r, "decision") if e.get("what") == "verify_link"]
    assert len(opened) == 1
    assert opened[0]["shown"].startswith("Your account has been verified"), opened


def test_a_link_pick_that_opens_the_other_link_parks_once_the_site_still_asks(
        _browser, flow_server, tmp_path):
    # the other side: a pick of the job alerts' link verifies nothing, and the
    # site's second ask for the link parks (one link per site, never a loop)
    f = dataclasses.replace(h.flow("workday_link_pick"), status="needs_human",
                            reason=r"^emailed verification link needed: the emailed link was "
                                   r"opened and ")
    r = h.run_flow(f, _LinkPick("link_0"), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    opened = [e for e in _events(r, "decision") if e.get("what") == "verify_link"]
    assert [e["shown"] for e in opened] == [
        "Your job alerts are on. We will email you new Analytics roles at Fabrikam each week."]


class _RedirectingSite:
    """A real local server (a redirect a route fulfils can behave otherwise):
    on 127.0.0.1, `/go` answers with a 302 to `/landed` on `localhost` (the
    other host), `/stay` with a 302 to its own `/landed` and a cookie,
    `/loop` with 302s back and forth, `/meta` and `/js` move to the other
    host from the page. `/landed` sets a cookie, asks for `/pixel`, opens
    a popup and sets a cookie from its script; `/opener` is a verified page
    that opens popups on both hosts. `/jsloc` and `/dataloc` answer with a
    302 to an address with no host. `/cf` is a bot check (a 403 with
    `cf-mitigated: challenge`) whose script, once it runs, sets a cookie and
    goes on to `/landed` (the link used); `/hopcf` is a 302 to it; `/busy`
    and `/down` answer 429 and 503, `/maint` a 503 for maintenance,
    `/denied` and `/empty403` a bare 403; `/bot403` is a 403 whose page
    asks for a bot check; `/gone` is a 403 that says the link expired and
    `/already` one that says the address is verified already; `/human` is
    a 200 page of a bot check, `/hophuman` a 302 to it, `/hopdown` a 302 to
    `/down`.
    `/verified_widget` and `/verified_box` are verified pages whose sign-in
    carries a CAPTCHA widget in a frame or a "Verify you are human" box.
    Every path asked for is kept in `asked`."""

    def __init__(self):
        import http.server
        import threading
        site = self

        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                site.asked.append(f"{self.headers.get('Host', '').split(':')[0]}{self.path}")
                other = f"http://localhost:{site.port}/landed"
                own = f"http://127.0.0.1:{site.port}/landed"
                moves = {"/go": other, "/stay": own, "/loop": "/loop2", "/loop2": "/loop",
                         "/jsloc": f"javascript:location.href='{other}'",
                         "/dataloc": f"data:text/html,<script>location.href='{other}'</script>",
                         "/hopcf": f"http://127.0.0.1:{site.port}/cf",
                         "/hophuman": f"http://127.0.0.1:{site.port}/human",
                         "/hopdown": f"http://127.0.0.1:{site.port}/down"}
                if self.path in moves:
                    self.send_response(302)
                    self.send_header("Location", moves[self.path])
                    self.send_header("Set-Cookie", "hop=1; Path=/")
                    self.end_headers()
                    return
                checks = {"/cf": (403, "<h1>Just a moment...</h1><script>document.cookie = "
                                       "'solved=1'; location.href = '/landed';</script>"),
                          "/busy": (429, "<h1>Too many requests</h1>"),
                          "/down": (503, "<h1>Service unavailable</h1>"),
                          "/gone": (403, "<h1>This link has expired</h1>"),
                          "/maint": (503, "<h1>Down for maintenance</h1><p>Back soon.</p>"),
                          "/denied": (403, "<h1>Access denied</h1>"),
                          "/empty403": (403, ""),
                          "/bot403": (403, "<h1>Please verify you are human</h1><p>Press and "
                                           "hold the button.</p>"),
                          "/already": (403, "<h1>This email address has already been "
                                            "verified.</h1>")}
                if self.path in checks:
                    status, page = checks[self.path]
                    data = f"<!doctype html><html><body>{page}</body></html>".encode()
                    self.send_response(status)
                    if self.path == "/cf":
                        self.send_header("cf-mitigated", "challenge")
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                body = {"/meta": f'<meta http-equiv="refresh" content="0;url={other}">Moving',
                        "/js": f"<script>location.href = '{other}';</script>Moving",
                        "/landed": '<h1>Your account is verified</h1><img src="/pixel">'
                                   "<script>window.open('/popup'); document.cookie = 'ran=1';"
                                   "</script>",
                        "/opener": "<h1>Your account is verified</h1><script>"
                                   f"window.open('http://localhost:{site.port}/popup');"
                                   "window.open('/popup');"
                                   "var a = document.createElement('a');"
                                   f"a.href = 'http://localhost:{site.port}/popup';"
                                   "a.target = '_blank'; document.body.appendChild(a); a.click();"
                                   "</script>",
                        "/human": "<h1>Verify you are human</h1><p>Complete the check below."
                                  "</p>",
                        "/verified_widget": "<h1>Your email address is verified</h1><p>Sign in "
                                            "to go on with your application.</p><form><input "
                                            "name=u><input type=password name=p><iframe srcdoc=\""
                                            "<input type=checkbox><label>I'm not a robot</label>"
                                            "\"></iframe><button>Sign in</button></form>",
                        "/verified_box": "<h1>Email verified</h1><p>Sign in to continue.</p>"
                                         "<form><input name=u><input type=password name=p><div><label>"
                                         "<input type=checkbox> Verify you are human</label>"
                                         "</div><button>Sign in</button></form>",
                        }.get(self.path, "")
                data = f"<!doctype html><html><body>{body}</body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Set-Cookie", "landed=1; Path=/")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.asked: list[str] = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def redirecting_site():
    site = _RedirectingSite()
    yield site
    site.close()


def _link_run(tmp_path, page, monkeypatch) -> tuple["apply_run._JobRun", list[str]]:
    """A `_JobRun` on `page` whose one allowed host is 127.0.0.1, and the
    URLs of every tab whose text the run read."""
    run = _job_run(tmp_path, "127.0.0.1")
    run.page = page
    read: list[str] = []
    real = apply_run.apply_fill.page_text

    def _page_text(tab, *a, **kw):
        read.append(tab.url)
        return real(tab, *a, **kw)
    monkeypatch.setattr(apply_run.apply_fill, "page_text", _page_text)
    return run, read


def _left_behind(page) -> dict:
    """What a link's tab left in the job's context: its other pages, and
    the hosts holding a cookie."""
    page.wait_for_timeout(500)          # a late popup or cookie has had its chance
    return {"pages": [p.url for p in page.context.pages if p is not page],
            "cookies": sorted({(c["domain"], c["name"]) for c in page.context.cookies()})}


def test_a_verification_link_a_server_redirects_to_another_host_is_never_read(
        browser_page, tmp_path, monkeypatch, redirecting_site):
    # I1 (SP7 review): the link is on the allowed host, and its server's 302
    # sends the tab to another host. That host is never asked for (so its
    # page never runs, sets no cookie, opens no popup), and the park names it
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    with pytest.raises(apply_run._Parked, match=r"the emailed link went on to localhost, outside "
                                                r"the application's sites; the run stopped it"):
        run._open_link(f"http://127.0.0.1:{redirecting_site.port}/go")
    assert read == []
    assert not [p for p in redirecting_site.asked if p.startswith("localhost")], (
        redirecting_site.asked)
    assert _left_behind(browser_page) == {"pages": [], "cookies": [("127.0.0.1", "hop")]}


@pytest.mark.parametrize("path", ["/meta", "/js"])
def test_a_verification_link_whose_page_moves_to_another_host_is_stopped(
        browser_page, tmp_path, monkeypatch, redirecting_site, path):
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    with pytest.raises(apply_run._Parked, match=r"went on to localhost, outside"):
        run._open_link(f"http://127.0.0.1:{redirecting_site.port}{path}")
    assert not [u for u in read if "localhost" in u], read
    assert not [p for p in redirecting_site.asked if p.startswith("localhost")]
    assert _left_behind(browser_page)["pages"] == []


def test_a_verification_link_redirected_on_its_own_host_is_read(
        browser_page, tmp_path, monkeypatch, redirecting_site):
    # the hop on the allowed host is taken (its cookie kept), the landed
    # page read; its popup loads nothing and is closed
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    assert run._open_link(f"http://127.0.0.1:{redirecting_site.port}/stay") == (
        "Your account is verified")
    assert read == [f"http://127.0.0.1:{redirecting_site.port}/landed"]
    assert [p for p in redirecting_site.asked if "popup" in p] == []
    assert _left_behind(browser_page) == {
        "pages": [], "cookies": [("127.0.0.1", "hop"), ("127.0.0.1", "landed"),
                                 ("127.0.0.1", "ran")]}


def test_a_verification_pages_popups_load_nothing_and_are_closed(
        browser_page, tmp_path, monkeypatch, redirecting_site):
    # N3 (SP7 review): a popup of the link's tab on any host, by a script's
    # open or a link with a target, is never asked for and never left open
    run, _ = _link_run(tmp_path, browser_page, monkeypatch)
    assert run._open_link(f"http://127.0.0.1:{redirecting_site.port}/opener") == (
        "Your account is verified")
    assert redirecting_site.asked == ["127.0.0.1/opener"], redirecting_site.asked
    assert _left_behind(browser_page)["pages"] == []


def test_a_verification_link_redirected_without_end_does_not_open(
        browser_page, tmp_path, monkeypatch, redirecting_site):
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    with pytest.raises(apply_run._Parked, match=r"the emailed link did not open "
                                                r"\(TooManyRedirects\)"):
        run._open_link(f"http://127.0.0.1:{redirecting_site.port}/loop")
    assert read == []
    assert len(redirecting_site.asked) == apply_run.LINK_MOVES_MAX


@pytest.mark.parametrize("path, scheme", [("/jsloc", "javascript"), ("/dataloc", "data")])
def test_a_verification_link_redirected_to_an_address_with_no_host_names_its_scheme(
        browser_page, tmp_path, monkeypatch, redirecting_site, path, scheme):
    # R3-M1 (SP7 review): a Location with no host (a javascript: or data:
    # address) is stopped like any other, and the park names its scheme
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    with pytest.raises(apply_run._Parked, match=rf"the emailed link went on to a {scheme}: "
                                                r"address, outside the application's sites; "
                                                r"the run stopped it$"):
        run._open_link(f"http://127.0.0.1:{redirecting_site.port}{path}")
    assert read == []
    assert not [p for p in redirecting_site.asked if p.startswith("localhost")]


@pytest.mark.parametrize("path, said", [
    ("/cf", "cf-mitigated: challenge"),
    ("/bot403", "HTTP 403; the page says 'verify you are human'"),
    ("/human", "the page says 'Verify you are human'")])
def test_a_verification_link_answered_by_a_bot_check_parks_for_the_person(
        browser_page, tmp_path, monkeypatch, redirecting_site, path, said):
    # R3-M2 (SP7 review): the site's bot check in place of the link's page.
    # A check's answer to the fetch never reaches the tab, so its script
    # never runs and never uses the link; the park asks the person to open
    # the link
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    with pytest.raises(apply_run._Parked) as parked:
        run._open_link(f"http://127.0.0.1:{redirecting_site.port}{path}")
    assert parked.value.reason == ("emailed verification link needed: the emailed link's page "
                                   f"asked for a bot check ({said})")
    assert parked.value.tab_note == apply_run.LINK_BOT_NOTE
    assert not [p for p in redirecting_site.asked if p.endswith("/landed")], redirecting_site.asked
    assert ("127.0.0.1", "solved") not in _left_behind(browser_page)["cookies"]
    if path != "/human":
        assert read == []


@pytest.mark.parametrize("path, said", [
    ("/hopcf", "asked for a bot check (cf-mitigated: challenge)"),
    ("/hophuman", "asked for a bot check (the page says 'Verify you are human')"),
    ("/hopdown", "answered HTTP 503 (the page says 'Service unavailable')")])
def test_a_bot_check_after_the_links_own_address_answered_says_the_link_may_have_been_used(
        browser_page, tmp_path, monkeypatch, redirecting_site, path, said):
    # R4-I2 (SP7 review): the link's own address answered with a redirect,
    # so the site may have taken the link's token before the check; the
    # park says so and asks for a Re-queue first. A status that is no check
    # on that hop says the same (R4-M1)
    run, _ = _link_run(tmp_path, browser_page, monkeypatch)
    with pytest.raises(apply_run._Parked) as parked:
        run._open_link(f"http://127.0.0.1:{redirecting_site.port}{path}")
    assert parked.value.reason == (
        f"emailed verification link needed: the emailed link's page {said} after the link's "
        "own address had answered, so the link may have been used")
    assert parked.value.tab_note == apply_run.LINK_USED_NOTE
    assert not [p for p in redirecting_site.asked if p.endswith("/landed")], redirecting_site.asked


@pytest.mark.parametrize("path, shown", [("/verified_widget", "Your email address is verified"),
                                         ("/verified_box", "Email verified")])
def test_a_verified_page_whose_sign_in_carries_a_captcha_is_read(
        browser_page, tmp_path, monkeypatch, redirecting_site, path, shown):
    # R4-I2 (SP7 review): the link verified the address, and the page's
    # sign-in carries its own CAPTCHA (a widget in a frame, a "Verify you
    # are human" box). A check's words are read from the main frame of a
    # page with no box to fill, so this page is read and the run goes on
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    assert run._open_link(f"http://127.0.0.1:{redirecting_site.port}{path}").startswith(shown)
    assert read == [f"http://127.0.0.1:{redirecting_site.port}{path}"]


@pytest.mark.parametrize("path, said", [
    ("/busy", "HTTP 429 (the page says 'Too many requests')"),
    ("/down", "HTTP 503 (the page says 'Service unavailable')"),
    ("/maint", "HTTP 503 (the page says 'Down for maintenance')"),
    ("/denied", "HTTP 403"), ("/empty403", "HTTP 403")])
def test_a_verification_link_answered_by_a_bare_status_parks_naming_it(
        browser_page, tmp_path, monkeypatch, redirecting_site, path, said):
    # R4-M1 (SP7 review): a 403, 429 or 503 is a bot check only with a
    # check's header or words; any other is named by its status and the
    # page's words when it says the site is down or busy. Its page never
    # runs, and the person opens the link again
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    with pytest.raises(apply_run._Parked) as parked:
        run._open_link(f"http://127.0.0.1:{redirecting_site.port}{path}")
    assert parked.value.reason == ("emailed verification link needed: the emailed link's page "
                                   f"answered {said}")
    assert parked.value.tab_note == apply_run.LINK_NOTE
    assert read == []


def test_a_verification_link_answered_by_a_403_that_says_the_address_is_verified_is_read(
        browser_page, tmp_path, monkeypatch, redirecting_site):
    # R4-M1: the page's own words first. A 403 that says the address is
    # verified already is the link's work done: it is read as the link's
    # page and the run goes on to the job's page
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    assert run._open_link(f"http://127.0.0.1:{redirecting_site.port}/already") == (
        "This email address has already been verified.")
    assert read == [f"http://127.0.0.1:{redirecting_site.port}/already"]


def test_a_verification_link_the_site_answers_with_a_403_that_names_the_link_is_refused(
        browser_page, tmp_path, monkeypatch, redirecting_site):
    # a 403 that says the link expired is the site's word on the link, not
    # a bot check
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    with pytest.raises(apply_run._Parked, match=r"the site refused the emailed link "
                                                r"\('link has expired'\)"):
        run._open_link(f"http://127.0.0.1:{redirecting_site.port}/gone")
    assert read == [f"http://127.0.0.1:{redirecting_site.port}/gone"]


def test_a_verification_link_behind_a_bot_check_is_not_recorded_as_opened(
        browser_page, tmp_path, monkeypatch, redirecting_site):
    # R3-M2: no decision says the link was opened, and the site is not one
    # whose link was followed
    from types import SimpleNamespace
    run, _ = _link_run(tmp_path, browser_page, monkeypatch)
    link = f"http://127.0.0.1:{redirecting_site.port}/cf"
    run.inbox = SimpleNamespace(fetch_link=lambda page, host, inbox_url: link, refused=[])
    decided: list[str] = []
    monkeypatch.setattr(run, "_decide", lambda what, why, **kw: decided.append(what))
    rec: dict = {"clicked": []}
    with pytest.raises(apply_run._Parked, match=r"asked for a bot check"):
        run._verify_link(apply_run.apply_form.FormDigest("127.0.0.1", "Verify your email", ""),
                         rec)
    assert decided == [] and rec["clicked"] == [] and run._links_followed == set()


@pytest.mark.parametrize("path, why, note", [
    ("/cf", "the emailed link's page asked for a bot check (cf-mitigated: challenge)",
     "LINK_HELD_NOTE"),
    ("/hopcf", "the emailed link's page asked for a bot check (cf-mitigated: challenge) after "
               "the link's own address had answered, so the link may have been used",
     "CHECK_SENT_NOTE"),
    ("/gone", "the site refused the emailed link ('link has expired')", "CHECK_SENT_NOTE"),
    ("/loop", "the emailed link did not open (TooManyRedirects)", "CHECK_SENT_NOTE")])
def test_a_link_that_parks_after_the_answers_asks_the_person_to_check_and_never_to_requeue(
        browser_page, tmp_path, monkeypatch, redirecting_site, path, why, note):
    # final review A R2-M3: in submit mode, once the application's answers
    # are on the site the emailed link may be the step that sends it. A park
    # of the link's tab then carries the check-sent reason and no Re-queue:
    # a link held on its own address is the person's to open, then Mark
    # applied; one whose address answered may have sent the application
    from types import SimpleNamespace
    run, _ = _link_run(tmp_path, browser_page, monkeypatch)
    link = f"http://127.0.0.1:{redirecting_site.port}{path}"
    run.inbox = SimpleNamespace(fetch_link=lambda page, host, inbox_url: link, refused=[])
    run.form_filled = True
    with pytest.raises(apply_run._Parked) as parked:
        run._verify_link(apply_run.apply_form.FormDigest("127.0.0.1", "Verify your email", ""),
                         {"clicked": []})
    assert parked.value.status == "needs_human"
    assert parked.value.reason == (f"{apply_run.CHECK_SENT_REASON}: {apply_run.LINK_REASON}, and "
                                   f"the link may send the application: {why}")
    assert parked.value.tab_note == getattr(apply_run, note)
    assert parked.value.tab_note not in (apply_run.LINK_NOTE, apply_run.LINK_BOT_NOTE,
                                         apply_run.LINK_USED_NOTE)
    assert run._maybe_sent()


@pytest.mark.parametrize("fields, text, said", [
    ([], "Verify Your Account. We sent a verification email to your address. Click the link in "
         "the email to activate your account.", "verification email"),
    ([("I agree", "checkbox")], "Check your inbox and open the link we sent.", "Check your inbox"),
    # a sign-up that says it will mail a link is a form; a code box is a code step
    ([("Email", "email")], "We will send a verification email.", ""),
    ([("Verification code", "text")], "Check your email for the code.", ""),
    # a thank-you that mentions its confirmation email; a posting's link to apply
    ([], "Thank you for applying! We sent a confirmation email to your address.", ""),
    ([], "About the role. To apply, click the link below.", ""),
])
def test_a_page_that_says_a_link_was_emailed_reads_as_the_account_check(fields, text, said):
    import apply_judge
    form = apply_run.apply_form
    buttons = [form.Button(0, (0, "#apply"), "Apply now")] if "To apply" in text else []
    digest = form.FormDigest("127.0.0.1", "Account", text, fields=[
        form.Field(i, (0, f"#f{i}"), label, kind, False) for i, (label, kind) in enumerate(fields)],
        buttons=buttons)
    assert apply_judge.link_sent(digest) == said
    facts = apply_judge.page_facts(digest)
    if said:
        assert apply_judge.structural_kind(facts, strict=True) == "code_gate"
        assert apply_run.unsure_acts("code_gate", digest)
    else:
        assert not facts.link_sent



# === an account that exists, password rules, re-typing, a slow sign-up (ACC-03, 04, 09, 12) ===========

def _run(f, tmp_path, _browser, flow_server, judge=None):
    return h.run_flow(f, judge or jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                      workdir=tmp_path)


def test_a_sign_up_that_says_the_account_exists_signs_in_instead(_browser, flow_server, tmp_path):
    r = _run(h.flow("signup_exists"), tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    decided = [e["what"] for e in _events(r, "decision")]
    assert "account_exists" in decided
    secret = [(a.account.split("#")[0], a.type) for a in r.actions if a.secret]
    # two boxes of the one sign-up, then one sign-in: never a second sign-up
    assert secret == [("signup", "password"), ("signup", "password"), ("login", "password")], secret


def test_an_existing_account_with_another_password_parks_after_one_sign_in(
        _browser, flow_server, tmp_path):
    import dataclasses
    body = (h.FIXTURES_DIR / "forms" / "signup_exists.html").read_text(encoding="utf-8").replace(
        "<body>", "<body data-reject>", 1)
    f = dataclasses.replace(h.flow("signup_exists"), name="signup_exists_other_password",
                            status="needs_human", reason=r"^an account exists",
                            routes=lambda base: {f"{base}/forms/signup_exists.html": body})
    r = _run(f, tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.reason == ("an account exists on 127.0.0.1 with another password: reset it to the "
                        "master password, then Re-queue")
    assert sum(1 for a in r.actions if a.secret and a.account.startswith("login")) == 1


def test_a_sign_ups_password_rules_are_read_and_met(_browser, flow_server, tmp_path):
    r = _run(h.flow("password_rules"), tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    rules = [e for e in _events(r, "decision") if e["what"] == "password_rules"]
    assert len(rules) == 1 and rules[0]["unmet"] == 0, rules
    assert rules[0]["rules"] == ["digit", "lower", "max_length", "min_length", "special", "upper"]


def test_a_stored_password_that_misses_a_rule_parks_before_anything_is_typed(
        _browser, flow_server, tmp_path, monkeypatch):
    import dataclasses
    monkeypatch.setattr(apply_pages, "PASSWORD", "Short-Pw1")        # nine characters
    f = dataclasses.replace(h.flow("password_rules"), status="needs_human",
                            reason=r"^the master password does not meet the password rules")
    r = _run(f, tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.reason.startswith("the master password does not meet the password rules on "
                               "127.0.0.1: it misses 1 of the rules the site states (the site "
                               "asks: Your "
                               "password must be 12 to 64 characters long"), r.reason
    assert not any(a.secret for a in r.actions)
    assert not any(a.kind == "click" for a in r.actions)


_REFUSE_ONCE = (
    "    if (!window.__refused) { window.__refused = 1;"
    " document.getElementById('signup_password').value = '';"
    " document.getElementById('signup_confirm').value = '';"
    " document.getElementById('signup').insertAdjacentHTML('afterbegin',"
    " '<p role=alert>Something went wrong, try again.</p>'); return; }\n")
_REFUSE_ALWAYS = (
    "    document.getElementById('signup_password').value = '';"
    " document.getElementById('signup_confirm').value = '';"
    " window.__n = (window.__n || 0) + 1;"
    " document.getElementById('signup').insertAdjacentHTML('afterbegin',"
    " '<p role=alert>Something went wrong (' + window.__n + '), try again.</p>'); return;\n")
_LOGGED_IN = "    document.body.setAttribute('data-logged-in', '1');"


def _refusing_signup(name: str, refuse: str, **kw) -> h.Flow:
    import dataclasses
    body = (h.FIXTURES_DIR / "forms" / "signup.html").read_text(encoding="utf-8")
    assert body.count(_LOGGED_IN) == 1
    body = body.replace(_LOGGED_IN, refuse + _LOGGED_IN)
    return dataclasses.replace(h.flow("signup_park"), name=name,
                               routes=lambda base: {f"{base}/forms/signup.html": body}, **kw)


def test_a_sign_up_shown_again_with_its_boxes_emptied_takes_the_password_once_more(
        _browser, flow_server, tmp_path):
    # ACC-12: the first Create account is refused for another reason (the
    # boxes emptied, a note shown); the second goes through
    r = _run(_refusing_signup("signup_retype", _REFUSE_ONCE), tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    retyped = [e for e in _events(r, "decision") if e["what"] == "password_retyped"]
    assert len(retyped) == 1 and "Something went wrong, try again." in retyped[0]["why"]
    assert sum(1 for a in r.actions if a.secret) == 4


def test_a_sign_up_refused_twice_parks_with_what_the_page_says(_browser, flow_server, tmp_path):
    f = _refusing_signup("signup_refused", _REFUSE_ALWAYS, status="needs_human",
                         reason=r"^the signup on 127\.0\.0\.1")
    r = _run(f, tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.reason == ("the signup on 127.0.0.1 did not take the master password (the page "
                        "says 'Something went wrong (2), try again.')"), r.reason
    assert sum(1 for a in r.actions if a.secret) == 4     # typed twice, never a third time


def test_a_slow_sign_up_is_waited_for_and_clicked_once(_browser, flow_server, tmp_path):
    r = _run(h.flow("slow_signup"), tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert [a.text for a in r.actions if a.kind == "click"].count("Create account") == 1
    settles = _events(r, "step_settle")
    assert len(settles) == 1 and settles[0]["changed"], settles


# === a sign-in only with another site (ACC-11) ================================================================

def test_a_portal_that_signs_in_only_with_another_site_parks_and_clicks_none(
        _browser, flow_server, tmp_path):
    r = _run(h.flow("sso_buttons"), tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.reason == ("sign-in only through another site (Google, Microsoft, LinkedIn, "
                        "Apple); the run never signs in with another site")
    assert r.policy is True
    assert not [a for a in r.actions if a.kind == "click"]


_SSO_CHROME = (h.FIXTURES_DIR / "forms" / "sso_buttons.html").read_text(encoding="utf-8").replace(
    "<p>By signing in you agree to our Terms of Use.</p>",
    '<p>By signing in you agree to our Terms of Use. <button type="button">Learn more</button></p>'
    '<div><button type="button">English</button> <button type="button">Contact us</button></div>'
    '<div role="region" aria-label="Cookies"><p>We use cookies.</p>'
    '<button type="button">Accept all</button></div>')


def test_a_portal_with_page_chrome_beside_its_sso_buttons_parks_as_sso(
        _browser, flow_server, tmp_path):
    # N2 (SP7 review): "Learn more", a language, "Contact us" and a cookie
    # banner's "Accept all" are no way on; the park is the SSO one (in the
    # policy), never a login wall
    import dataclasses
    assert _SSO_CHROME.count("Accept all") == 1
    f = dataclasses.replace(h.flow("sso_buttons"), name="sso_buttons_chrome",
                            routes=lambda base: {f"{base}/forms/sso_buttons.html": _SSO_CHROME})
    r = _run(f, tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.reason == ("sign-in only through another site (Google, Microsoft, LinkedIn, "
                        "Apple); the run never signs in with another site")
    assert r.policy is True
    assert not [a for a in r.actions if a.kind == "click"]


_SSO_SKIP = (h.FIXTURES_DIR / "forms" / "sso_buttons.html").read_text(encoding="utf-8").replace(
    "<p>By signing in you agree to our Terms of Use.</p>",
    '<p>By signing in you agree to our Terms of Use.</p><button type="button" id="skip">Skip for '
    "now</button><script>document.getElementById('skip').addEventListener('click', function () "
    "{ document.body.setAttribute('data-skipped', '1'); });</script>")


class _SignInAsLoginWall(jev.FakeJev):
    """The fake, reading the screen of sign-ins with other sites as a login
    wall at 0.90, as a judge that sees a sign-in screen would."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        text = str((state.get("page") or {}).get("headline_text") or "")
        if "page_state" in out and "Fabrikam" in text:
            h.read_as(out, "login_wall", 0.90)
        return out


def test_a_portal_whose_other_control_leads_nowhere_ends_with_the_sso_park(
        _browser, flow_server, tmp_path):
    # R3-I1 (SP7 review): "Skip for now" is a control the run does not know,
    # so the screen is not read as SSO-only at once; the account step finds
    # no box to sign in with, and its login-wall park falls back to the SSO
    # reason, its own words kept (R4-I1). No sign-in with another site is
    # clicked and nothing is sent
    import dataclasses
    f = dataclasses.replace(h.flow("sso_buttons"), name="sso_buttons_skip",
                            routes=lambda base: {f"{base}/forms/sso_buttons.html": _SSO_SKIP})
    assert apply_run.sso_only(apply_run.apply_form.FormDigest(
        "127.0.0.1", "Sign in", "", buttons=[apply_run.apply_form.Button(0, (0, "#b"), t)
                                             for t in ("Sign in with Google", "Skip for now")])) == []
    r = _run(f, tmp_path, _browser, flow_server, _SignInAsLoginWall())
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.reason.startswith("sign-in only through another site (Google, Microsoft, LinkedIn, "
                               "Apple); the run never signs in with another site, and nothing "
                               "else on the screen took it on; the account step's park: login "
                               "wall (read as login_wall "), r.reason
    assert r.policy is True
    assert not [a for a in r.actions
                if a.kind == "click" and apply_run._THIRD_PARTY.search(a.text or "")], r.actions
    assert r.sends == 0
    fallback = [e for e in _events(r, "decision") if e["what"] == "sso_fallback"]
    assert len(fallback) == 1 and fallback[0]["why"].startswith("login wall ("), fallback


def _sso_with(control: str) -> str:
    """sso_buttons.html with one more control, `control`, that does nothing."""
    return (h.FIXTURES_DIR / "forms" / "sso_buttons.html").read_text(encoding="utf-8").replace(
        "<p>By signing in you agree to our Terms of Use.</p>",
        f'<p>By signing in you agree to our Terms of Use.</p><button type="button">{control}'
        "</button>")


def _careers(title: str, main: str, script: str = "") -> str:
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><title>" + title
            + "</title></head><body><header><a href='#'>Fabrikam Careers</a></header><main>"
            + main + "</main>" + script + "</body></html>")


_REVIEW_STEP = ("<h1>Step 3 of 4: Review your application</h1><p>Analytics Engineer. Check your "
                "details, then go on.</p><button type='button'>Back</button><button type='button' "
                "id='nx'>Next</button><button type='button'>Sign in with Google to save your "
                "progress</button>")
# a dead end beside sign-ins with other sites, and an account park on a
# screen with a way on of its own: the fake's end, which is its own park
_DEAD_ENDS = {
    # sso_buttons.html with one more control that does nothing
    "skip": (_sso_with("Skip for now"), r"^page did not advance \(read as application_form "),
    "candidate_login": (_sso_with("Candidate login"),
                        r"^no way forward on this page \(buttons: .*Candidate login other"),
    "create_one": (_sso_with("Create one"),
                   r"^the advance button \(Create one\) did nothing \(judged advance [\d.]+, "
                   r"clicked twice\)$"),
    # R4-I1 (SP7 review): the review's four screens with a way on of their own
    "review_next": (_careers("Review - Fabrikam Careers", _REVIEW_STEP),
                    r"^the advance button \(Next\) did nothing \(judged advance [\d.]+, clicked "
                    r"twice\)$"),
    "review_next_saves": (_careers(
        "Review - Fabrikam Careers", _REVIEW_STEP,
        "<script>document.getElementById('nx').addEventListener('click', function () { "
        "fetch('/api/step', {method: 'POST', body: 'x'}).catch(function () {}); });</script>"),
        r"^the advance button \(Next\) did nothing \(judged advance [\d.]+, its request left: "
        r"POST \S+/api/step; it was not clicked again\)$"),
    "posting_apply": (_careers(
        "Analytics Engineer - Fabrikam Careers",
        "<h1>Analytics Engineer</h1><p>Remote. Build the data models behind our reporting.</p>"
        "<button type='button'>Apply now</button><button type='button'>Sign in with "
        "LinkedIn</button>"),
        r"^login wall \(read as login_wall .*Apply now apply_entry"),
    "portal_email": (_careers(
        "Sign in - Fabrikam Careers",
        "<h1>Sign in to apply</h1><p>Continue your application for Analytics Engineer.</p>"
        "<button type='button'>Continue with Google</button><button type='button'>Continue "
        "with Microsoft</button><button type='button'>Continue with email</button>"),
        r"^the advance button \(Continue with email\) did nothing \(judged advance [\d.]+, "
        r"clicked twice\)$"),
}


@pytest.mark.parametrize("name", list(_DEAD_ENDS))
def test_a_dead_end_beside_sign_ins_with_other_sites_keeps_its_own_park(
        _browser, flow_server, tmp_path, name):
    # R4-I1 (SP7 review): a dead end (the page did not advance, no way
    # forward, a way on that did nothing, a step save whose request left)
    # keeps its own park and reason, and so does an account park on a screen
    # with a way on of its own ("Apply now"): the words of a dead control
    # cannot tell the page's own way on from a sign-in's. The harness counts
    # each outside the policy, so the matrix sees it. No sign-in with
    # another site is clicked and nothing is sent
    import dataclasses
    page, reason = _DEAD_ENDS[name]
    f = dataclasses.replace(h.flow("sso_buttons"), name=f"sso_dead_end_{name}", reason=reason,
                            routes=lambda base: {f"{base}/forms/sso_buttons.html": page})
    r = _run(f, tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.status == "needs_human"
    assert r.policy is False, r.reason
    assert not [a for a in r.actions
                if a.kind == "click" and apply_run._THIRD_PARTY.search(a.text or "")], r.actions
    assert r.sends == 0
    assert not [e for e in _events(r, "decision") if e["what"] == "sso_fallback"]


_G = "Sign in with Google"
_WALL = "login wall (read as login_wall 0.90; master password stored: no; boxes: none)"
_SIGNUP = "account signup needed (read as signup 0.90; the create-account link led nowhere)"
# R4-I1: the account parks and dead ends `_account_park` is handed, over the
# screens of the review's probes: the SSO park only where the screen's only
# way on is a sign-in with another site
_ACCOUNT_PARKS = [
    # beside the sign-ins, the site's own sign-in or sign-up, chrome, or a
    # control the run does not know: the SSO park
    (["Sign in", _G], _WALL, True),
    (["Skip for now", _G], _WALL, True),
    (["Candidate login", "Create one", "Help", _G], _SIGNUP, True),
    (["Register", "Forgot password?", "Continue with Google"], _SIGNUP, True),
    # a way on of its own: an Apply, a Next or a Continue, a send, another
    # way to sign in
    (["Next", _G], _WALL, False),
    (["Continue", "Continue with Google"], _WALL, False),
    (["Apply now", "Sign in with LinkedIn"], _WALL, False),
    (["Continue with email", "Continue with Google", "Continue with Microsoft"], _WALL, False),
    (["Use your phone number", _G], _WALL, False),
    (["Email me a sign-in link", _G], _WALL, False),
    (["More sign-in options", _G], _WALL, False),
    (["Submit", _G], _SIGNUP, False),
    (["I agree", _G], _SIGNUP, False),
    # a dead end keeps its own park, whatever the screen holds
    (["Skip for now", _G], "page did not advance (read as application_form 0.60 again after Skip "
                           "for now (apply_entry))", False),
    (["Candidate login", _G], "no way forward on this page (buttons: Sign in with Google advance "
                              "1.00; Candidate login other 1.00)", False),
    (["Create one", _G], "the advance button (Create one) did nothing (judged advance 1.00, "
                         "clicked twice)", False),
    (["Continue", "Continue with Google"], "the advance button (Continue) did nothing (judged "
                                           "advance 0.90, its request left: POST "
                                           "https://ats.example/api/step; it was not clicked "
                                           "again)", False),
]


@pytest.mark.parametrize("buttons, reason, relabelled", _ACCOUNT_PARKS,
                         ids=[f"{'+'.join(b)}|{r.split(' (')[0]}" for b, r, _ in _ACCOUNT_PARKS])
def test_an_account_park_falls_back_to_sso_only_where_the_screen_has_no_way_on_of_its_own(
        tmp_path, buttons, reason, relabelled):
    import dataclasses
    form = apply_run.apply_form
    tick = form.Field(0, (0, "#t"), "Remember me", "checkbox", False)
    digest = form.FormDigest("127.0.0.1", "Sign in", "", fields=[tick], buttons=[
        form.Button(i, (0, f"#b{i}"), t) for i, t in enumerate(buttons)])
    run = _job_run(tmp_path, "127.0.0.1")
    parked = run._account_park(digest, reason)
    site = "LinkedIn" if "LinkedIn" in " ".join(buttons) else "Google"
    if relabelled:
        assert parked.reason == (f"sign-in only through another site ({site}); the run never "
                                 "signs in with another site, and nothing else on the screen "
                                 f"took it on; the account step's park: {reason}")
        assert parked.tab_note == apply_run.SSO_NOTE
    else:
        assert (parked.reason, parked.tab_note) == (reason, apply_run.LOGIN_NOTE)
    # a box to fill is the account step's to fill: never the SSO park
    boxed = dataclasses.replace(digest, fields=[form.Field(1, (0, "#p"), "Password", "password",
                                                           True, secret=True)])
    assert run._account_park(boxed, reason).reason == reason


@pytest.mark.parametrize("guard", ["submit_clicked", "_code_sent", "linkedin", "captcha"])
def test_an_account_park_after_a_send_may_have_gone_or_on_linkedin_keeps_its_own_words(
        tmp_path, monkeypatch, guard):
    # R4-I1: the main SSO check's guards. After the submit or a code step
    # was clicked a send may have gone, so no park invites a Re-queue there;
    # LinkedIn's own pages are never an ATS sign-in. R4-M2: a CAPTCHA box
    # or challenge waiting for the person may be what held the screen
    form = apply_run.apply_form
    digest = form.FormDigest("127.0.0.1", "Sign in", "", buttons=[
        form.Button(0, (0, "#b0"), "Sign in"), form.Button(1, (0, "#b1"), _G)])
    run = _job_run(tmp_path, "127.0.0.1")
    assert run._account_park(digest, _WALL).tab_note == apply_run.SSO_NOTE
    if guard == "linkedin":
        monkeypatch.setattr(run, "_on_linkedin", lambda: True)
    elif guard == "captcha":
        monkeypatch.setattr(run, "_human_check_showing", lambda checkbox=False: checkbox)
    else:
        setattr(run, guard, True)
    parked = run._account_park(digest, _WALL)
    assert (parked.reason, parked.tab_note) == (_WALL, apply_run.LOGIN_NOTE)


# ACC-11: the controls beside two sign-ins with other sites, over every list
# the SP7 reviews probed (M4, N2, R3-I1, R3-M4). Known page chrome leaves the
# screen SSO-only; any other control may be a way on, and a screen read so
# goes on to its account step (R3-I1: a false SSO park loses a job)
_SSO_CHROME_CONTROLS = [
    # round 1 (M4): help, a way back, a cancel, a close, a notice
    "Help", "Back", "Cancel", "Close", "Privacy policy", "Cookie settings",
    # round 2 (N2): page chrome
    "Learn more", "Accept all", "English", "Contact us", "Accept all cookies", "Reject all",
    "Fran\u00e7ais", "FAQ", "Terms of use", "Accessibility",
    # round 3 (R3-M4): a way to reach the site, an account's recovery
    "Email us", "Email support", "Phone support", "Forgot password?", "Code of conduct",
    "English (US)", "Language: Deutsch", "Can't sign in?", "Reset your password", "Learn more \u203a",
]
_SSO_WAY_ON_CONTROLS = [
    # round 1 (M4): a control that may show the screen's own way on
    "More options", "Use another method", "Show more", "Other ways to sign in",
    # round 2 (N2): another way to sign in or apply
    "Use email", "Continue with email", "Create account", "Sign up", "Use a password instead",
    "Email me a sign-in link", "More sign-in options", "Next", "I agree", "Sign in with email",
    "Apply",
    # round 3 (R3-I1): a control the run does not know
    "Skip", "Skip for now", "Skip this step", "Not now", "Maybe later", "Proceed", "Get started",
    "Start", "Go", "Upload resume", "Upload your resume", "Enter details manually",
    "Fill out the form", "Use my resume", "I don't have an account", "Create one",
    "First time here?", "New here? Get started", "Continue without signing in",
    "Apply manually", "Candidate login", "Register",
    # round 3 (R3-M4): left as ways on, the safe side
    "Show all", "Join our talent community",
    # an aside's word beside a way on's
    "Go back and use email", "Help me apply",
]
_SSO_TABLE = [
    (["Sign in with Google", "Continue with Microsoft"], [], ["Google", "Microsoft"]),
    (["Sign in with Google"], [("Email", "email")], []),         # a box to fill
    (["Apply with LinkedIn", "Apply"], [], []),                  # a posting's own Apply
    (["Log in using your SSO account", "Help"], [], ["SSO"]),
    (["Sign in with Google", "Help", "Back", "Cancel", "Close", "Privacy policy",
      "Cookie settings", "Learn more", "Email us"], [], ["Google"]),
    (["Sign in with Google", "Learn more"], [("Password", "other")], []),    # a password box
    *[(["Sign in with Google", "Continue with Microsoft", c], [], ["Google", "Microsoft"])
      for c in _SSO_CHROME_CONTROLS],
    *[(["Sign in with Google", "Continue with Microsoft", c], [], []) for c in _SSO_WAY_ON_CONTROLS],
]


@pytest.mark.parametrize("buttons, fields, sites", _SSO_TABLE)
def test_sso_only_reads_a_screen_whose_one_way_on_is_another_sites_sign_in(buttons, fields,
                                                                         sites):
    form = apply_run.apply_form
    digest = form.FormDigest("127.0.0.1", "Sign in", "Sign in to apply", fields=[
        form.Field(i, (0, f"#f{i}"), label, kind, False) for i, (label, kind) in enumerate(fields)],
        buttons=[form.Button(i, (0, f"#b{i}"), t) for i, t in enumerate(buttons)])
    assert apply_run.sso_only(digest) == sites


# --- the same two rules where the application's own form makes the account (ACC-04, ACC-12) -------

_FORM_ACCOUNT = """<!doctype html><html><head><title>Apply - Fabrikam Careers</title></head><body>
<h1>Apply for Analytics Engineer</h1>
<div id="step1">
<p id="note"></p>
<label>First name * <input name="first" required></label>
<label>Last name * <input name="last" required></label>
<label>Email * <input type="email" name="email" required></label>
<label>Resume * <input type="file" name="resume" required></label>
<label>Choose a password * <input type="password" id="pw" name="pw" autocomplete="new-password"
  required></label>
<p>__RULES__</p>
<button type="button" id="next">Next</button>
</div>
<div id="step2" style="display: none"><h2>Review your application</h2>
<p>Please review your application before you submit it.</p>
<button type="button" id="btn-submit">Submit application</button></div>
<script>
  var refused = __REFUSE__;
  document.getElementById('next').addEventListener('click', function () {
    var pw = document.getElementById('pw');
    if (!pw.value) return;
    if (refused > 0) {
      refused -= 1;
      pw.value = '';
      // in words: a page's signature leaves its numbers out
      window.__tries = (window.__tries || 0) + 1;
      document.getElementById('note').textContent = 'Something went wrong on the '
        + ['first', 'second', 'third', 'fourth', 'fifth'][window.__tries - 1] + ' try.';
      return;
    }
    document.getElementById('step1').style.display = 'none';
    document.getElementById('step2').style.display = 'block';
  });
  document.getElementById('btn-submit').addEventListener('click', function () {
    document.body.dataset.submitted = 1;
    document.body.innerHTML = '<h1 id=received>Application received</h1>';
  });
</script></body></html>"""


def _form_account(name: str, *, rules: str = "", refuse: int = 0, **kw) -> h.Flow:
    careers = "https://careers.fabrikam.example"
    body = _FORM_ACCOUNT.replace("__RULES__", rules).replace("__REFUSE__", str(refuse))
    fields = dict(confirm="#received:visible", gate="#btn-submit:visible", password=True,
                  routes=lambda base: {f"{careers}/**": body})
    fields.update(kw)
    return h.Flow(name, f"{careers}/apply/42", False, fields.pop("status", "ready_to_submit"),
                  fields.pop("reason", h._PARKED), **fields)


def test_an_application_form_that_makes_the_account_checks_the_password_rules_first(
        _browser, flow_server, tmp_path):
    f = _form_account("form_account_rules", rules="Your password must be at least 30 characters.",
                      status="needs_human",
                      reason=r"^the master password does not meet the password rules on "
                             r"careers\.fabrikam\.example: it misses 1 of the rules the site "
                             r"states")
    r = _run(f, tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert not any(a.secret for a in r.actions)


def test_an_application_form_shown_again_with_its_password_emptied_takes_it_once_more(
        _browser, flow_server, tmp_path):
    r = _run(_form_account("form_account_retype", refuse=1), tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert sum(1 for a in r.actions if a.secret) == 2
    retyped = [e for e in _events(r, "decision") if e["what"] == "password_retyped"]
    assert len(retyped) == 1, retyped


def test_an_application_form_asking_for_the_password_a_third_time_parks(
        _browser, flow_server, tmp_path):
    f = _form_account("form_account_refused", refuse=5, status="needs_human",
                      reason=r"^the form on careers\.fabrikam\.example asked for the master "
                             r"password again \(the page says 'Something went wrong on the "
                             r"second try\.'\)$")
    r = _run(f, tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert sum(1 for a in r.actions if a.secret) == 2


@pytest.mark.parametrize("text, help_, rules", [
    # Workday's list under its heading, each rule a line of its own
    ("Create Account\nPassword\nVerify New Password\nPassword Requirements:\na lowercase character\n"
     "an uppercase character\na numeric character\na special character\n"
     "a minimum of 8 characters", "",
     {"lower": True, "upper": True, "digit": True, "special": True, "min_length": 8}),
    ("Password\nMust be 12 to 64 characters long.", "",
     {"min_length": 12, "max_length": 64}),
    ("Password\nConfirm password", "Use at least 10 characters. Maximum of 20 characters.",
     {"min_length": 10, "max_length": 20}),
    ("Password\nYour password cannot contain the following characters: < > &", "",
     {"forbidden": "&<>"}),
    # a phone box's label beside the password is no rule; words far below are not read
    ("Password\nConfirm password\nPhone number\n\n\n\n\n\n\n\n\nOur office has 12 characters", "",
     {}),
])
def test_password_rules_read_a_sign_ups_stated_rules(text, help_, rules):
    form = apply_run.apply_form
    digest = form.FormDigest("127.0.0.1", "Create Account", text, fields=[
        form.Field(0, (0, "#p"), "Password", "other", True, id_or_name="password", help=help_,
                   secret=True),
        form.Field(1, (0, "#c"), "Confirm password", "other", True, id_or_name="confirm",
                   secret=True),
        form.Field(2, (0, "#t"), "Phone number", "tel", False)])
    got, said = apply_run.password_rules(digest)
    assert got == rules, said


# I2 (SP7 review): the phrasings the review's probe read as rules the site
# never set; each is read as the site means it
_OVER_READ = [
    # a count of the classes: any three of the four
    ("Password\nMust contain at least 3 of the following: an uppercase letter, a lowercase "
     "letter, a number, a special character",
     {"upper": True, "lower": True, "digit": True, "special": True, "classes_needed": 3}),
    # another field's hint below the password's boxes
    ("Password\nConfirm password\nPreferred name\nUp to 20 characters", {}),
    ("Password\nConfirm password\nTell us about yourself\nAt least 50 characters", {}),
    # the commas between the characters are no character of the list
    ("Password\nCannot contain the following characters: <, >, &", {"forbidden": "&<>"}),
    ("Password\nMust not include the characters \"<\", \">\" or \"&\".", {"forbidden": "&<>"}),
    # a phone's line: its "number" is no digit rule
    ("Password\nConfirm password\nMobile phone\nEnter a number we can reach you at", {}),
]


def _rules_digest(text: str, *others: tuple[str, str]):
    form = apply_run.apply_form
    return form.FormDigest("127.0.0.1", "Create Account", text, fields=[
        form.Field(0, (0, "#p"), "Password", "other", True, id_or_name="password", secret=True),
        form.Field(1, (0, "#c"), "Confirm password", "other", True, id_or_name="confirm",
                   secret=True),
        *[form.Field(i + 2, (0, f"#o{i}"), label, kind, False)
          for i, (label, kind) in enumerate(others)]])


@pytest.mark.parametrize("text, rules", _OVER_READ)
@pytest.mark.parametrize("labelled", [False, True])
def test_password_rules_never_read_a_rule_the_site_did_not_set(text, rules, labelled):
    # with the other field's words as its label and as a heading alone
    others = [("Preferred name", "text"), ("Tell us about yourself", "textarea"),
              ("Mobile phone", "tel")] if labelled else []
    got, said = apply_run.password_rules(_rules_digest(text, *others))
    assert got == rules, said


@pytest.mark.parametrize("text, password", [
    (_OVER_READ[0][0], "lowercase-and-12"),         # three of the four: no uppercase
    (_OVER_READ[0][0], "Lowercase12345"),           # three of the four: no special
    (_OVER_READ[1][0], "Twenty-Four-Chars-Long-1"),
    (_OVER_READ[2][0], "Short-Pass-12"),
    (_OVER_READ[3][0], "Comma,Pass-12"),
    (_OVER_READ[5][0], "No-Digits-Here"),
])
def test_a_screen_that_would_take_the_password_never_parks_on_a_misread_rule(
        tmp_path, monkeypatch, text, password):
    # the run parked with "does not meet the password rules" on each of these
    # screens (a park the harness counts inside the policy); the site takes
    # the password. The value is a test's own, never the stored one
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: password)
    run = _job_run(tmp_path, "127.0.0.1")
    run._check_password_rules(_rules_digest(text), "127.0.0.1")


def test_a_count_of_classes_the_password_misses_still_parks(tmp_path, monkeypatch):
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "onlylowercase-")
    run = _job_run(tmp_path, "127.0.0.1")
    digest = _rules_digest(_OVER_READ[0][0])
    assert ats_accounts.unmet_rules(apply_run.password_rules(digest)[0]) == [
        "at least 3 of: an uppercase letter, a lowercase letter, a digit, a special character"]
    with pytest.raises(apply_run._Parked, match=r"it misses 1 of the rules the site states "
                                                r"\(the site asks"):
        run._check_password_rules(digest, "127.0.0.1")


# N1 (SP7 review round 2): a prohibition, a choice and another field's hint,
# each read as the site means it; a rule left unclear is no rule
_READ_AS_MEANT = [
    # a prohibition names no class the password needs
    ("Password\nYour password must not contain your name, email address or phone number", {}),
    ("Password\nMust not contain spaces or special characters", {}),
    ("Password\nMust contain a number and must not contain your phone number", {"digit": True}),
    ("Password\nCan't contain your phone number. Must include a number.", {"digit": True}),
    # classes joined by "or": any one of them
    ("Password\nMust include at least one number or special character",
     {"digit": True, "special": True, "classes_needed": 1}),
    ("Password\nMust contain a number, a symbol, or an uppercase letter",
     {"upper": True, "digit": True, "special": True, "classes_needed": 1}),
    # a choice beside classes it requires: the required ones, the choice the site's
    ("Password\nAt least one uppercase letter, one lowercase letter, and one number or special "
     "character", {"upper": True, "lower": True}),
    # another field's label with its own hint ends the password's rules
    ("Password\n8+ characters\nNickname (up to 20 characters)", {"min_length": 8}),
    ("Password\nBio: at least 50 characters", {}),
    # a heading that names the password keeps its rules
    ("Password requirements: at least 12 characters", {"min_length": 12}),
    ("Password\nConfirm password\nPassword requirements:\nAt least 12 characters\n"
     "One uppercase letter", {"min_length": 12, "upper": True}),
    # the username's rule is no password rule; a prohibition naming it is one
    ("Forgot your password?\nNew users must register with at least 3 characters in the username",
     {}),
    ("Password\nMust be at least 8 characters and must not match your username",
     {"min_length": 8}),
    # a typographic apostrophe is the same prohibition
    ("Password\nCan\u2019t contain your phone number or any symbol. Must include a number.",
     {"digit": True}),
]


@pytest.mark.parametrize("text, rules", _READ_AS_MEANT)
@pytest.mark.parametrize("labelled", [False, True])
def test_password_rules_read_a_prohibition_a_choice_and_a_hint_as_the_site_means_them(
        text, rules, labelled):
    others = [("Nickname", "text"), ("Bio", "textarea"), ("Username", "text")] if labelled else []
    got, said = apply_run.password_rules(_rules_digest(text, *others))
    assert got == rules, said


@pytest.mark.parametrize("text, password", [
    (_READ_AS_MEANT[0][0], "No-Digits-Here"),            # the phone number is no digit rule
    (_READ_AS_MEANT[1][0], "Plainpassword"),              # nor its special characters a need
    (_READ_AS_MEANT[4][0], "Only-Symbols-Here"),          # a number or a special character
    (_READ_AS_MEANT[4][0], "OnlyDigits1234"),
    (_READ_AS_MEANT[5][0], "only-symbols-here"),          # a number, a symbol or an uppercase
    (_READ_AS_MEANT[7][0], "Twenty-Four-Chars-Long-1"),  # the nickname's 20 is no maximum
    (_READ_AS_MEANT[8][0], "Short-Pass-12"),              # the bio's 50 is no minimum
])
def test_a_screen_whose_rule_the_password_meets_as_meant_never_parks(
        tmp_path, monkeypatch, text, password):
    # each parked with "does not meet the password rules" before; the value
    # is a test's own, never the stored one
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: password)
    run = _job_run(tmp_path, "127.0.0.1")
    run._check_password_rules(_rules_digest(text), "127.0.0.1")


def test_a_choice_of_classes_the_password_holds_none_of_still_parks(tmp_path, monkeypatch):
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: "onlylowercaseletters")
    run = _job_run(tmp_path, "127.0.0.1")
    digest = _rules_digest(_READ_AS_MEANT[4][0])
    assert ats_accounts.unmet_rules(apply_run.password_rules(digest)[0]) == [
        "at least 1 of: a digit, a special character"]
    with pytest.raises(apply_run._Parked, match=r"it misses 1 of the rules the site states "
                                                r"\(the site asks"):
        run._check_password_rules(digest, "127.0.0.1")


# R3-M3 (SP7 review round 3): the pre-check blocks only on a rule it reads
# with certainty. A rule's phrase with a choice or advice word ("or",
# "and/or", "a mix of", "any", "avoid", "recommended", "should", "(0-9) or")
# adds no class the password needs; the certain rules beside it still hold.
# Every phrasing of the round-3 probe, in both directions
_READ_WITH_CERTAINTY = [
    # a choice the reader does not read whole, advice: no class needed
    ("Password\nMust contain an uppercase letter and/or a number", {}),
    ("Password\nUse 8 or more characters with a mix of letters, numbers & symbols",
     {"min_length": 8}),
    ("Password\nMust contain a digit (0-9) or a special character", {}),
    ("Password\nAvoid using your phone number", {}),
    ("Password\nA symbol is recommended", {}),
    ("Password\nUse any combination of uppercase letters, numbers and symbols", {}),
    ("Password\nYour password should include a number", {}),
    ("Password\nShould contain numbers or symbols. Must contain an uppercase letter.",
     {"upper": True}),
    # a choice read whole: any one of it, and the certain classes beside it
    ("Password\nMust contain letters and numbers or symbols",
     {"digit": True, "special": True, "classes_needed": 1}),
    ("Password\nMust include one uppercase letter, and either a number or a symbol",
     {"upper": True}),
    # "8 or more" is a count, no choice: the class beside it holds
    ("Password\nMust be 8 or more characters and contain a number",
     {"min_length": 8, "digit": True}),
    # a prohibition beside a rule: the rule after "and must" holds
    ("Password\nMust not be the same as your email and must include a symbol",
     {"special": True}),
    # read looser than meant, which the site judges: a prohibition's clause
    # that runs on, "No fewer than", a head that is no rule
    ("Password\nNo special characters required, but must contain a number", {}),
    ("Password\nNo fewer than 8 characters, including one uppercase letter", {"min_length": 8}),
    ("Password\nNote: must contain an uppercase letter", {}),
    # another field's label with its own hint ends the password's rules
    ("Password\nMinimum 8 characters\nDisplay name (shown to recruiters)", {"min_length": 8}),
    ("Password\nNickname (up to 20 characters)", {}),
]


@pytest.mark.parametrize("text, rules", _READ_WITH_CERTAINTY)
def test_password_rules_read_only_what_the_site_says_with_certainty(text, rules):
    got, said = apply_run.password_rules(_rules_digest(text, ("Nickname", "text")))
    assert got == rules, said


@pytest.mark.parametrize("text, password", [
    (_READ_WITH_CERTAINTY[0][0], "only-lowercase-here"),  # and/or: an uppercase, a number
    (_READ_WITH_CERTAINTY[1][0], "OnlyLettersHere"),      # a mix of: a number, a symbol
    (_READ_WITH_CERTAINTY[2][0], "OnlyLettersHere"),      # (0-9) or: a digit, a symbol
    (_READ_WITH_CERTAINTY[3][0], "No-Digits-Here"),       # avoid: the phone's number
    (_READ_WITH_CERTAINTY[4][0], "NoSymbolsHere1"),       # recommended: a symbol
    (_READ_WITH_CERTAINTY[5][0], "lowercaseonly"),        # any combination of three
    (_READ_WITH_CERTAINTY[6][0], "No-Digits-Here"),       # should: a number
])
def test_a_rule_read_without_certainty_never_parks(tmp_path, monkeypatch, text, password):
    # each parked with "does not meet the password rules" before; the value
    # is a test's own, never the stored one
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: password)
    run = _job_run(tmp_path, "127.0.0.1")
    run._check_password_rules(_rules_digest(text), "127.0.0.1")


@pytest.mark.parametrize("text, password, needs", [
    (_READ_WITH_CERTAINTY[7][0], "no-uppercase-12", "an uppercase letter"),
    (_READ_WITH_CERTAINTY[10][0], "No-Digits-Here", "a digit"),
])
def test_a_certain_rule_beside_advice_or_a_count_still_parks(tmp_path, monkeypatch, text,
                                                             password, needs):
    monkeypatch.setattr(ats_accounts, "_get_master_password", lambda: password)
    run = _job_run(tmp_path, "127.0.0.1")
    assert ats_accounts.unmet_rules(apply_run.password_rules(_rules_digest(text))[0]) == [needs]
    with pytest.raises(apply_run._Parked, match=r"it misses 1 of the rules the site states "
                                                r"\(the site asks"):
        run._check_password_rules(_rules_digest(text), "127.0.0.1")


# === the password invariants (SP7) ===========================================================================

def _recorder(tmp_path):
    rec = h.Recorder(h.flow("signup_park"), park_mode=True, password=h.PASSWORD)
    rec.final = {"confirmed": False, "at_gate": True}
    rec.app_hosts = {"127.0.0.1"}
    return rec, h.Sends(rec)


class _Parked:
    status = "ready_to_submit"
    reason = "auto_apply_submit is off"


_APP = "http://127.0.0.1:8000/forms/signup.html"


@pytest.mark.parametrize("plant, code", [
    ("typed_into_a_text_box", "PASSWORD-NOT-A-PASSWORD-BOX"),
    ("typed_by_the_keyboard", "PASSWORD-NOT-A-PASSWORD-BOX"),
    ("typed_in_the_inbox", "PASSWORD-OFF-SITE"),
    ("two_sign_ins", "PASSWORD-RETRY"),
    ("three_sign_ups", "PASSWORD-RETRY"),
    ("seven_typings", "PASSWORD-RETRY"),
    ("in_the_trace", "PASSWORD-LEAK"),
    ("in_the_ledger", "PASSWORD-LEAK"),
    ("a_ledger_password_key", "LEDGER-PASSWORD-KEY"),
    ("a_jsonl_file", "PASSWORD-LEAK"),
    ("a_judge_request", "PASSWORD-TO-JUDGE"),
    ("a_log_line", "PASSWORD-LEAK"),
    ("a_sign_in_with_google", "OTHER-SITE-CLICK"),
])
def test_each_password_invariant_fails_on_its_planted_breach(tmp_path, plant, code):
    rec, sends = _recorder(tmp_path)

    def typed(url=_APP, type_="password", how="Locator.fill", account=""):
        rec.actions.append(h.Action("fill", url, tag="input", type=type_, how=how, secret=True,
                                    account=account))
    if plant == "typed_into_a_text_box":
        typed(type_="text")
    elif plant == "typed_by_the_keyboard":
        typed(how="Keyboard.type")
    elif plant == "typed_in_the_inbox":
        typed(url=h.inbox_url("link_list.html"))
    elif plant == "two_sign_ins":
        typed(account="login#1")
        typed(account="login#2")
    elif plant == "three_sign_ups":
        for n in (1, 2, 3):
            typed(account=f"signup#{n}")
    elif plant == "seven_typings":
        for _ in range(7):
            typed()
    elif plant == "in_the_trace":
        trace = tmp_path / "job" / "apply_trace" / "attempt-1"
        trace.mkdir(parents=True)
        (trace / "page-2.json").write_text('{"value": "%s"}' % h.PASSWORD, encoding="utf-8")
        rec.files = [tmp_path / "job"]
    elif plant == "in_the_ledger":
        rec.ledger = tmp_path / "accounts.json"
        rec.ledger.write_text('{"127.0.0.1": {"note": "%s"}}' % h.PASSWORD, encoding="utf-8")
        rec.files = [rec.ledger]
    elif plant == "a_ledger_password_key":
        rec.ledger = tmp_path / "accounts.json"
        rec.ledger.write_text('{"127.0.0.1": {"email": "a@b.c", "password_hint": "x"}}',
                              encoding="utf-8")
    elif plant == "a_jsonl_file":
        (tmp_path / "outcomes.jsonl").write_text(h.PASSWORD, encoding="utf-8")
        rec.files = [tmp_path]
    elif plant == "a_judge_request":
        rec.judge_requests.append('{"fields": [{"value": "%s"}]}' % h.PASSWORD)
    elif plant == "a_log_line":
        rec.logs.append(f"filled {h.PASSWORD}")
    elif plant == "a_sign_in_with_google":
        rec.actions.append(h.Action("click", _APP, text="Sign in with Google", tag="button"))
    breaks = h.invariant_breaks(_Parked(), rec, sends)
    assert code in {b.split(":")[0] for b in breaks}, breaks


def test_the_typings_a_sign_up_path_makes_hold_the_password_invariants(tmp_path):
    # one sign-up (two boxes), its one re-type (two), one sign-in: within
    rec, sends = _recorder(tmp_path)
    for account in ("signup#1", "signup#1", "signup#2", "signup#2", "login#3"):
        rec.actions.append(h.Action("fill", _APP, tag="input", type="password", secret=True,
                                    account=account))
    assert h.invariant_breaks(_Parked(), rec, sends) == []


def test_the_recorder_marks_the_master_passwords_typings_and_no_other(
        _browser, flow_server, tmp_path):
    # the Workday path end to end: the sign-up's two boxes and the sign-in's
    # one carry the flag, every other typing (the address, the names) none
    r = _run(h.flow("workday_signin_modal"), tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    secret = [(a.account.split("#")[0], a.type, a.host) for a in r.actions if a.secret]
    assert secret == [("signup", "password", "127.0.0.1")] * 2 + [
        ("login", "password", "127.0.0.1")], secret
    assert any(a.kind == "fill" and not a.secret for a in r.actions)


_SIGN_IN_WITH_BUTTONS = """<body><h1>Sign In</h1>
<label>Email Address <input id="email" type="text"></label>
<label>Password <input id="pw" type="password"></label>
<button type="button" id="go">Sign In</button>
<button type="button" onclick="document.body.dataset.took = 'alerts'">Sign up for job alerts</button>
<button type="button" onclick="document.body.dataset.took = 'create';
  document.body.insertAdjacentHTML('beforeend', '<h2>Create Account</h2>')">Create Account</button>
</body>"""


def test_the_create_account_button_is_the_one_that_makes_an_account(browser_page, tmp_path):
    # ACC-01: of a sign-in screen's sign-up words, never a job-alert sign-up
    import apply_form
    browser_page.route("http://127.0.0.1/signin", lambda route: route.fulfill(
        body=_SIGN_IN_WITH_BUTTONS, content_type="text/html"))
    browser_page.goto("http://127.0.0.1/signin")
    run = _job_run(tmp_path, "127.0.0.1")
    run.page = browser_page
    digest = apply_form.extract(browser_page)
    assert run.accounts._signup_button(browser_page, digest) is True
    assert browser_page.evaluate("document.body.dataset.took") == "create"


_SIGN_IN_ALERTS_LINK = """<body><header><a href="/alerts">Sign up for job alerts</a></header>
<h1>Sign In</h1>
<label>Email Address <input id="email" type="text"></label>
<label>Password <input id="pw" type="password"></label>
<button type="button" id="go">Sign In</button>
<button type="button" onclick="document.body.dataset.took = 'create';
  document.body.insertAdjacentHTML('beforeend', '<h2>Create Account</h2>')">Create Account</button>
</body>"""


def test_a_header_job_alerts_link_never_shadows_the_create_account_button(
        browser_page, tmp_path, monkeypatch, ledger):
    # I3 (SP7 review): the header's "Sign up for job alerts" link was taken
    # for the sign-in's create-account link; the run went to the alerts page
    # and parked as a login wall, the Create Account button never clicked
    import apply_form
    monkeypatch.setattr(ats_accounts, "has_password", lambda: True)
    for path, body in (("signin", _SIGN_IN_ALERTS_LINK), ("alerts", "<h1>Job alerts</h1>")):
        browser_page.route(f"http://127.0.0.1/{path}", h._fulfiller(body))
    browser_page.goto("http://127.0.0.1/signin")
    run = _job_run(tmp_path, "127.0.0.1")
    run.page = browser_page
    run.catalog = apply_run.apply_facts.FactCatalog()
    digest = apply_form.extract(browser_page)
    assert run.accounts.login(browser_page, digest, "127.0.0.1") is True
    assert browser_page.evaluate("document.body.dataset.took") == "create"
    assert browser_page.url == "http://127.0.0.1/signin"


def test_a_sign_in_whose_header_carries_a_job_alerts_link_makes_the_account(
        _browser, flow_server, tmp_path):
    r = _run(h.flow("signin_alerts_link"), tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    clicks = [a.text for a in r.actions if a.kind == "click"]
    assert "Create Account" in clicks and "Sign up for job alerts" not in clicks, clicks
    assert not [a for a in r.actions if a.secret and a.account.startswith("login")]


def test_a_job_alerts_link_leaves_the_one_sign_in_to_a_screen_with_no_sign_up(
        _browser, flow_server, tmp_path):
    # no Create Account button: the alerts link is no sign-up, and the one
    # sign-in without a ledger entry (ACC-02) is tried
    import json
    body = (h.FIXTURES_DIR / "forms" / "signin_alerts_link.html").read_text(encoding="utf-8")
    body = body.replace('<p><button type="button" id="btn-create-account">Create Account</button>'
                        '</p>', "").replace(
        "document.body.setAttribute('data-signed-in', 'refused');",
        "window.location.href = 'lever_single.html'; return;")
    assert "btn-create-account\"" not in body and "lever_single.html'; return;" in body
    f = h.Flow("signin_alerts_only", "signin_alerts_only.html", False, "ready_to_submit",
               h._PARKED, confirm="#thanks:visible", gate="#btn-submit:visible", password=True,
               routes=lambda base: {f"{base}/forms/signin_alerts_only.html": body})
    r = _run(f, tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    ledger = json.loads((Path(r.trace).parents[2] / "accounts.json").read_text(encoding="utf-8"))
    assert "signed in" in ledger["127.0.0.1"]["note"]


def test_the_account_steps_own_buttons_are_never_a_job_alerts_sign_up():
    # a noisy judge rated the job-alerts sign-up the advance of a sign-up
    # screen; the screen's own Create Account is the click
    from apply_judge import FillPlan
    form = apply_run.apply_form
    digest = form.FormDigest("127.0.0.1", "Create Account", "Create Account", fields=[
        form.Field(0, (0, "#email"), "Email Address", "email", False),
        form.Field(1, (0, "#p"), "Password", "other", False, autocomplete="new-password",
                   secret=True),
        form.Field(2, (0, "#c"), "Verify New Password", "other", False,
                   autocomplete="new-password", secret=True)],
        buttons=[form.Button(0, (0, "#b0"), "Sign up for job alerts"),
                 form.Button(1, (0, "#b1"), "Create Account")])
    plan = FillPlan(buttons={"advance": (0, 0.93), "other": (1, 0.94)})
    assert apply_run.account_advance(digest, plan, signup=True) == (
        1, apply_run.apply_judge.BUTTON_ADVANCE_MIN_CONF)


def test_the_way_to_the_sign_in_is_never_a_job_alerts_sign_in(browser_page, tmp_path):
    # ACC-03's way from a sign-up that says the account exists to the sign-in
    import apply_form
    browser_page.route("http://127.0.0.1/signup", lambda route: route.fulfill(
        content_type="text/html", body="""<body><h1>Create Account</h1>
<p>An account with this email already exists.</p>
<button type="button" onclick="document.body.dataset.took = 'alerts'">Sign in to manage job
alerts</button>
<button type="button" onclick="document.body.dataset.took = 'signin';
  document.body.insertAdjacentHTML('beforeend', '<h2>Sign In</h2>')">Sign In</button></body>"""))
    browser_page.goto("http://127.0.0.1/signup")
    run = _job_run(tmp_path, "127.0.0.1")
    run.page = browser_page
    digest = apply_form.extract(browser_page)
    assert run.accounts._to_sign_in(browser_page, digest, "127.0.0.1", "exists") is True
    assert browser_page.evaluate("document.body.dataset.took") == "signin"


def test_a_sign_ins_box_that_came_back_is_never_typed_again(tmp_path):
    # ACC-12 re-types a sign-up's emptied boxes only: a sign-in's box that came
    # back is a rejected password, and a second typing moves toward a lockout
    class _Emptied:
        """A password box the site emptied."""

        def evaluate(self, js, *a, **kw):
            return 0
    run = _job_run(tmp_path, "127.0.0.1")
    assert run.accounts._retype("127.0.0.1", [_Emptied()],
                                _boxes_digest(("Password", "new-password"))) is True
    run = _job_run(tmp_path, "127.0.0.1")
    digest = _boxes_digest(("Password", "current-password"))
    assert run.accounts._retype("127.0.0.1", [_Emptied()], digest) is False
    assert run.accounts.retyped == set()


def test_a_sign_up_form_drafts_nothing_with_the_generate_setting_off(
        _browser, flow_server, tmp_path, monkeypatch):
    # every plan the run makes on a sign-up flow reads the person's
    # auto_apply_generate; none falls back to drafting by default
    import apply_judge
    real_init, real_plan = apply_run.Runner.__init__, apply_judge.plan
    seen: list = []

    def init(self, *a, **k):
        k["settings"] = {**k["settings"], "auto_apply_generate": False}
        real_init(self, *a, **k)

    def plan(*a, **k):
        seen.append(k.get("generation_enabled", "default"))
        return real_plan(*a, **k)
    monkeypatch.setattr(apply_run.Runner, "__init__", init)
    monkeypatch.setattr(apply_judge, "plan", plan)
    _run(h.flow("password_rules"), tmp_path, _browser, flow_server)
    assert seen and set(seen) == {False}, seen
