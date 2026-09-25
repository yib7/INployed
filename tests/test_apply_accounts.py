"""SP7: accounts and email.

- The ledger's account for a sign-in host that names no tenant, found by
  the job's own hosts (ACC-13).

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


@pytest.mark.parametrize("fields, text, said", [
    ([], "Verify Your Account. We sent a verification email to your address. Click the link in "
         "the email to activate your account.", "verification email"),
    ([("I agree", "checkbox")], "Check your inbox and open the link we sent.", "Check your inbox"),
    # a sign-up that says it will mail a link is a form; a code box is a code step
    ([("Email", "email")], "We will send a verification email.", ""),
    ([("Verification code", "text")], "Check your email for the code.", ""),
])
def test_a_page_that_says_a_link_was_emailed_reads_as_the_account_check(fields, text, said):
    import apply_judge
    form = apply_run.apply_form
    digest = form.FormDigest("127.0.0.1", "Account", text, fields=[
        form.Field(i, (0, f"#f{i}"), label, kind, False) for i, (label, kind) in enumerate(fields)])
    assert apply_judge.link_sent(digest) == said
    facts = apply_judge.page_facts(digest)
    if said:
        assert apply_judge.structural_kind(facts, strict=True) == "code_gate"
        assert apply_run.unsure_acts("code_gate", digest)
    else:
        assert not facts.link_sent
