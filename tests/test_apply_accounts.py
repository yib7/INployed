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
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_harness as h  # noqa: E402
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
        form.Field(1, (0, "#password"), "Password", "other", False, id_or_name="password")],
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
    fields += [form.Field(i + 1, (0, f"#p{i}"), label, "other", False, autocomplete=auto)
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
    expired = ("<!doctype html><title>Account</title><h1>This link has expired.</h1>"
               "<p>Ask for a new one from the sign-in page.</p>")
    f = _workday("workday_link_expired", status="needs_human",
                 reason=r"^emailed verification link needed: ",
                 routes=lambda base: {f"{base}/forms/workday_account_verified.html*": expired})
    r = h.run_flow(f, jev.FakeJev(), "fake", browser=_browser, server=flow_server,
                   workdir=tmp_path)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.reason == ("emailed verification link needed: the site refused the emailed link "
                        "('link has expired')")


class _RedirectingSite:
    """A real local server (a redirect a route fulfils can behave otherwise):
    on 127.0.0.1, `/go` answers with a 302 to `/landed` on `localhost` (the
    other host), `/stay` with a 302 to its own `/landed`, `/meta` and `/js`
    move to the other host from the page; `/landed` asks for `/pixel`.
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
                if self.path in ("/go", "/stay"):
                    self.send_response(302)
                    self.send_header("Location", other if self.path == "/go" else own)
                    self.end_headers()
                    return
                body = {"/meta": f'<meta http-equiv="refresh" content="0;url={other}">Moving',
                        "/js": f"<script>location.href = '{other}';</script>Moving",
                        "/landed": '<h1>Your account is verified</h1><img src="/pixel">',
                        }.get(self.path, "")
                data = f"<!doctype html><html><body>{body}</body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
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


def test_a_verification_link_a_server_redirects_to_another_host_is_never_read(
        browser_page, tmp_path, monkeypatch, redirecting_site):
    # I1 (SP7 review): the link is on the allowed host, and its server's 302
    # sends the tab to another host. That page is never read, nothing more
    # of it loads, and the park names the host
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    with pytest.raises(apply_run._Parked, match=r"the emailed link went on to localhost, outside "
                                                r"the application's sites; the run stopped it"):
        run._open_link(f"http://127.0.0.1:{redirecting_site.port}/go")
    assert read == []
    assert "localhost/pixel" not in redirecting_site.asked, redirecting_site.asked


@pytest.mark.parametrize("path", ["/meta", "/js"])
def test_a_verification_link_whose_page_moves_to_another_host_is_stopped(
        browser_page, tmp_path, monkeypatch, redirecting_site, path):
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    with pytest.raises(apply_run._Parked, match=r"went on to localhost, outside"):
        run._open_link(f"http://127.0.0.1:{redirecting_site.port}{path}")
    assert not [u for u in read if "localhost" in u], read
    assert not [p for p in redirecting_site.asked if p.startswith("localhost")]


def test_a_verification_link_redirected_on_its_own_host_is_read(
        browser_page, tmp_path, monkeypatch, redirecting_site):
    run, read = _link_run(tmp_path, browser_page, monkeypatch)
    assert run._open_link(f"http://127.0.0.1:{redirecting_site.port}/stay") == (
        "Your account is verified")
    assert read == [f"http://127.0.0.1:{redirecting_site.port}/landed"]


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
    assert len(rules) == 1 and rules[0]["unmet"] == [], rules
    assert rules[0]["rules"] == ["digit", "lower", "max_length", "min_length", "special", "upper"]


def test_a_stored_password_that_misses_a_rule_parks_before_anything_is_typed(
        _browser, flow_server, tmp_path, monkeypatch):
    import dataclasses
    monkeypatch.setattr(h, "PASSWORD", "Short-Pw1")        # nine characters
    f = dataclasses.replace(h.flow("password_rules"), status="needs_human",
                            reason=r"^the master password does not meet the password rules")
    r = _run(f, tmp_path, _browser, flow_server)
    assert r.ok and not r.breaks, (r.status, r.reason, r.breaks)
    assert r.reason.startswith("the master password does not meet the password rules on "
                               "127.0.0.1: it needs at least 12 characters (the site asks: Your "
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


@pytest.mark.parametrize("buttons, fields, sites", [
    (["Sign in with Google", "Continue with Microsoft"], [], ["Google", "Microsoft"]),
    (["Sign in with Google", "Sign in with email"], [], []),     # an own way on
    (["Sign in with Google"], [("Email", "email")], []),         # a box to fill
    (["Apply with LinkedIn", "Apply"], [], []),                  # a posting's own Apply
    (["Log in using your SSO account", "Help"], [], ["SSO"]),
])
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
                             r"careers\.fabrikam\.example: it needs at least 30 characters")
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
        form.Field(0, (0, "#p"), "Password", "other", True, id_or_name="password", help=help_),
        form.Field(1, (0, "#c"), "Confirm password", "other", True, id_or_name="confirm"),
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
        form.Field(0, (0, "#p"), "Password", "other", True, id_or_name="password"),
        form.Field(1, (0, "#c"), "Confirm password", "other", True, id_or_name="confirm"),
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
    with pytest.raises(apply_run._Parked, match=r"it needs at least 3 of: an uppercase letter, "
                                                r"a lowercase letter, a digit, a special "
                                                r"character \(the site asks"):
        run._check_password_rules(_rules_digest(_OVER_READ[0][0]), "127.0.0.1")


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
    assert run.accounts._signup_button(browser_page, digest, "127.0.0.1") is True
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
        form.Field(1, (0, "#p"), "Password", "other", False, autocomplete="new-password"),
        form.Field(2, (0, "#c"), "Verify New Password", "other", False,
                   autocomplete="new-password")],
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
