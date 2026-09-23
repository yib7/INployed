"""The auto-apply flow harness: every multi-page fixture flow, run end to end
through `apply_run.Runner` under a judge, with the run's safety invariants
checked on every run.

Parts (the matrix test, `scripts/apply_matrix.py` and later phases use them):

- `FLOWS`, the flow registry. A `Flow` names its start page (a fixture under
  `tests/fixtures/forms/`, or a routed fake host), any `context.route` pages,
  the run's mode (park or submit), the end it must reach (a status and a
  reason pattern), the selector of its confirmation marker, the selector that
  shows the park-mode run stopped at the submit, and how a send shows.
- `FlowServer`: `tests/fixtures/` over http on a free localhost port. A POST
  to `/submit/<name>` is an application sent: it is counted and answered with
  a confirmation page (`body[data-confirmed]`).
- `Sends`: every send of one run, whatever shape it takes: the fixture's own
  marker (a script setting `body[data-submitted]`, reported through an init
  script and a binding), a request the flow names (the GET a submit button
  navigates to), a POST to the server. Each send notes whether the submit gate
  was running.
- `Recorder`: every click, fill, tick, pick and upload the run makes through
  Playwright, with the element's live text, role and host at that moment and
  whether `_JobRun._submit_gate` was running; the gate's own entries; the
  page the run ended on (its confirmation and gate markers).
- `invariant_breaks(outcome, recorder, sends)` / `assert_invariants(...)`:
  at most one send; park mode never sends; only the gate sends; `submitted`
  only with the confirmation marker showing, or "submitted (unconfirmed)"
  with a send counted; no click whose live text reads as a submit outside the
  gate; no fill, tick, pick, upload or gate on a `*.linkedin.com` page; the
  master password nowhere in the record, the trace, the queue or the logs.
- `run_flow` / `run_matrix` / `summary`: one flow under one judge, the whole
  registry under many, and the table with success rates.

Hermetic: the job folder, the queue, the account ledger and the logs live in
a temp dir; the answer bank is `tests/fixtures/apply_matrix/answers.json` and
the sheet is built here (no `resume_tailor` import, so the matrix script can
run outside pytest); the master password is a synthetic string; the judge is
`FakeJev` or `NoisyJev`. No network but the local server and routed hosts.

A pytest module gets the server through `conftest_browser`'s `flow_server`
fixture (session scope) and imports the rest from here.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "local") not in sys.path:
    sys.path.insert(0, str(REPO / "local"))

import apply_run  # noqa: E402
import jev  # noqa: E402

FIXTURES_DIR = REPO / "tests" / "fixtures"
BANK_PATH = FIXTURES_DIR / "apply_matrix" / "answers.json"
PASSWORD = "synthetic-matrix-Pw-7Qz4"      # the master password a flow's accounts step types
SIGNUP_EMAIL = "jane.doe@example.com"
JOB_ID = "42"
NOISY_SEEDS = tuple(range(1, 21))         # the script's seeds; the suite runs the first three
SUITE_SEEDS = NOISY_SEEDS[:3]
# The share of (flow, noisy seed) runs that reach their expected end, pinned
# at the SP1 baseline. Later phases raise it to 0.95.
SUCCESS_FLOOR = 0.64                      # SP1 fix round 1: 30 of 45 suite runs (0.667)
# Under the fake judge every flow reaches its end but the known failing ones
# (`Flow.known`), which the rates leave out.
FAKE_SUCCESS_FLOOR = 1.0

_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")

# The runner tests' synthetic sheet (`apply_data.build_markdown` over their
# synthetic master), with the sheet's delimiters filled in at run time.
_SHEET = """# Apply sheet: Analytics Engineer @ Fabrikam
Generated 2026-09-23.

## Candidate
- **Name:** Jane Doering
- **Email:** jane.doe@example.com
- **Phone:** 555-555-0100
- **Location:** Anytown, CA
- **LinkedIn:** https://linkedin.com/in/janedoe
- **GitHub / Portfolio:** https://github.com/janedoe

### Address
- **Full:** 123 Main Street, Anytown, California 12345, United States
- **Street:** 123 Main Street
- **City:** Anytown
- **State / Province:** California
- **ZIP / Postal:** 12345
- **Country:** United States

## Education
- State University {dash} B.S., Computer Science {dot} 2020 - 2024

## Work experience

**Acme Corp** {dash} Software Engineer {dot} Anytown, CA {dot} 2024-06 / present

- Built the ingestion pipeline.


## Cover letter

Dear hiring team,

I am writing to apply.

## Standard answers
- **Are you legally authorized to work in the US?** Yes
- **Will you now or in the future require visa sponsorship?** No
- **How many years of relevant experience do you have?** 2
- **Are you willing to relocate?** Yes
- **Work-authorization statement (free text).** Authorized to work in the United States; no visa sponsorship required.
- **Gender (EEO self-identification).** Decline to self-identify
- **Race / ethnicity (EEO self-identification).** Decline to self-identify
- **Veteran status (EEO self-identification).** I am not a veteran
- **Disability status (EEO self-identification).** No, I do not have a disability
- **How did you hear about us?** LinkedIn

## Electronic signature (use at the end, where the form asks; do not submit)
- **Signature (type):** Jane Doering
- **Date:** use today's date (the day you apply)

<!-- inployed-apply-meta: {{"job_posting_id": "42", "company": "Fabrikam", "title": "Analytics Engineer", "url": "https://example.com/job/42"}} -->
"""


def sheet_text() -> str:
    import apply_facts
    return _SHEET.format(dash=apply_facts._EM_DASH, dot=apply_facts._DOT)


def bank() -> list[dict]:
    return json.loads(BANK_PATH.read_text(encoding="utf-8"))


def write_job_folder(folder: Path) -> Path:
    """The job folder a queue entry points at: the sheet and a resume PDF."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "apply.md").write_text(sheet_text(), encoding="utf-8")
    (folder / "Jane_Doe_Resume.pdf").write_bytes(_PDF)
    return folder


# --- patches ----------------------------------------------------------------------------

class Patches:
    """Attribute patches undone in reverse on `undo()` (the harness runs
    outside pytest too, so it keeps its own monkeypatch)."""

    def __init__(self):
        self._undo: list[Callable[[], None]] = []

    def setattr(self, obj: Any, name: str, value: Any) -> None:
        old = getattr(obj, name)
        setattr(obj, name, value)
        self._undo.append(lambda: setattr(obj, name, old))

    def setenv(self, key: str, value: str) -> None:
        old = os.environ.get(key)
        os.environ[key] = value

        def _restore():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._undo.append(_restore)

    def undo(self) -> None:
        while self._undo:
            self._undo.pop()()


@contextmanager
def hermetic(rundir: Path, *, password: bool = False):
    """The run's stores in `rundir` (the queue, the account ledger), the
    synthetic answer bank for every catalog, and the synthetic master
    password when the flow signs in (else none is stored)."""
    import apply_run
    import ats_accounts
    p = Patches()
    try:
        p.setenv("ATS_ACCOUNTS_PATH", str(Path(rundir) / "accounts.json"))
        p.setenv("APPLY_QUEUE_PATH", str(Path(rundir) / "queue.json"))
        answers = bank()
        real_build = apply_run.apply_facts.build

        def _build(folder, **kw):
            kw.setdefault("answers", answers)
            return real_build(folder, **kw)
        p.setattr(apply_run.apply_facts, "build", _build)
        p.setattr(ats_accounts, "_get_master_password",
                  (lambda: PASSWORD) if password else (lambda: None))
        yield
    finally:
        p.undo()


# The fixtures are static pages: a short quiet window reads them as well as the
# production one, and a dead click or a missing popup then costs a few seconds.
FAST_TIMING = {
    ("apply_fill", "SETTLE_QUIET_S"): 0.1,        # a flow with a timer keeps its own (`Flow.settle_s`)
    ("apply_fill", "SETTLE_MAX_S"): 3.0,
    ("apply_fill", "NETWORK_IDLE_MS"): 50,
    ("apply_fill", "POLL_S"): 0.05,
    ("apply_run", "POPUP_TIMEOUT_MS"): 1_500,
    ("apply_run", "CLICK_TIMEOUT_S"): 3,
    ("apply_run", "SUBMIT_SETTLE_S"): 5,
    ("apply_run", "REDIRECT_TIMEOUT_S"): 6,
}


@contextmanager
def fast_timing(settle_s: float | None = None):
    """`FAST_TIMING` on the runner's modules; `settle_s` replaces the quiet
    window for a flow whose page moves on by a timer."""
    import importlib
    p = Patches()
    try:
        for (mod, name), value in FAST_TIMING.items():
            if name == "SETTLE_QUIET_S" and settle_s is not None:
                value = settle_s
            p.setattr(importlib.import_module(mod), name, value)
        yield
    finally:
        p.undo()


# --- the server -----------------------------------------------------------------------

CONFIRMATION_HTML = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>Application received</title></head><body data-confirmed=\"1\">"
    "<h1>Thank you for applying</h1><p>Your application has been received. The hiring "
    "team will review it and reach out if there is a match.</p></body></html>")


class _Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "FlowServer"

    def log_message(self, format, *args):    # noqa: A002  (the base class's signature)
        pass

    def do_POST(self):                       # noqa: N802  (the base class's naming)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)          # the posted answers are never kept
        name = self.path.split("?")[0].rstrip("/")
        if not name.startswith("/submit/"):
            self.send_error(404)
            return
        self.server.flow_server._posted(name[len("/submit/"):])
        body = CONFIRMATION_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FlowServer:
    """`tests/fixtures/` over http; `POST /submit/<name>` counts a send and
    answers with `CONFIRMATION_HTML`."""

    def __init__(self, root: Path = FIXTURES_DIR):
        self.root = Path(root)
        self.posts: dict[str, int] = {}
        self.on_post: Callable[[str], None] | None = None
        self._server = None
        self._thread = None
        self.base = ""

    def start(self) -> str:
        handler = partial(_Handler, directory=str(self.root))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        server.flow_server = self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="flow-server",
                                        daemon=True)
        self._thread.start()
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        return self.base

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _posted(self, name: str) -> None:
        self.posts[name] = self.posts.get(name, 0) + 1
        listener = self.on_post
        if listener is not None:
            listener(name)

    def url(self, name: str) -> str:
        return f"{self.base}/forms/{name}"


# --- the registry -------------------------------------------------------------------------

_LINKEDIN_JOB = "https://www.linkedin.com/jobs/view/4438751519/"
_CAREERS = "https://careers.fabrikam.example"
_COMBINED = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>Create your candidate account and apply</h1>
<label>First name * <input name="first" required></label>
<label>Last name * <input name="last" required></label>
<label>Email * <input type="email" name="email" required></label>
<label>Phone <input type="tel" name="phone"></label>
<label>Resume * <input type="file" name="resume" required></label>
<label>Password * <input type="password" name="pw" autocomplete="new-password" required></label>
<button type="button" onclick="document.body.dataset.submitted = 1; document.body.innerHTML =
  '<h1 id=received>Application received</h1><p>Thank you for applying.</p>'">Create account and apply</button>
</body></html>"""


def _linkedin_routes(base: str) -> dict[str, str]:
    """LinkedIn's job page and its `/safety/go/` redirector (routed; no
    network): the Apply link goes through the redirector, whose script sends
    the tab on to the fixture form after a moment, as LinkedIn's does (0.4 s
    here, 1.5 s in the fixture: the run waits for the hop either way). The
    redirector's route is registered last, so it wins for its own URLs."""
    forms = FIXTURES_DIR / "forms"
    target = f"{base}/forms/ashby_steps.html"
    posting = (forms / "linkedin_posting.html").read_text(encoding="utf-8").replace(
        'href="linkedin_redirect.html"',
        f'href="https://www.linkedin.com/safety/go/?url={target}"')
    redirect = (forms / "linkedin_redirect.html").read_text(encoding="utf-8").replace(
        "location.replace('ashby_steps.html'); }, 1500)",
        f"location.replace('{target}'); }}, 400)")
    return {"https://www.linkedin.com/**": posting,
            "https://www.linkedin.com/safety/go/**": redirect}


def _no_routes(base: str) -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class Flow:
    """One flow of the registry (see the module docstring)."""
    name: str
    start: str                      # a fixture under forms/, or an absolute URL
    submit: bool                    # auto_apply_submit
    status: str                     # the expected end
    reason: str                     # a regex the end's reason must match
    confirm: str = ""               # the confirmation marker (a Playwright selector)
    gate: str = ""                  # park mode: shown on the page the run stopped at
    send_urls: tuple[str, ...] = ()  # requests that are the send (globs)
    routes: Callable[[str], dict[str, str]] = _no_routes
    password: bool = False          # a synthetic master password is stored
    inbox: bool = False             # the fixture inbox is the run's inbox
    ats: dict[str, str] = field(default_factory=dict)
    settle_s: float | None = None   # the quiet window when a page moves on by a timer
    covers: str = ""                # what the flow exercises
    # "<phase>: why": the fake judge does not reach the end yet; the matrix
    # reports the flow apart and leaves it out of the rates the floors read
    known: str = ""

    def start_url(self, base: str) -> str:
        return self.start if "://" in self.start else f"{base}/forms/{self.start}"

    def app_hosts(self, base: str) -> set[str]:
        """Where the application lives, the only hosts the master password may
        be typed on: the fixture server, and the start page's host off
        LinkedIn."""
        hosts = {_host(base)}
        start = _host(self.start_url(base))
        if not on_linkedin(self.start_url(base)):
            hosts.add(start)
        return hosts

    def reached(self, status: str, reason: str, final: dict) -> bool:
        """The run reached this flow's expected end."""
        if status != self.status or not re.search(self.reason, reason or ""):
            return False
        if status == "submitted" and self.confirm and not final.get("confirmed"):
            return False
        if status == "ready_to_submit" and self.gate and not final.get("at_gate"):
            return False
        return True


_SUBMITTED = r"^confirmation page"
_PARKED = r"^auto_apply_submit is off$"

FLOWS: tuple[Flow, ...] = (
    Flow("ashby_wizard", "ashby_steps.html", True, "submitted", _SUBMITTED,
         confirm="#received:visible", covers="a three-step wizard to its confirmation"),
    Flow("ashby_wizard_park", "ashby_steps.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="the wizard in park mode stops at its submit"),
    Flow("native_wizard", "native_submit_steps.html", True, "submitted", _SUBMITTED,
         confirm="#received:visible", covers="type=submit Continue, then the final submit"),
    Flow("posting_popup_park", "job_posting.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="a posting whose Apply opens the form in a new tab"),
    Flow("linkedin_posting", _LINKEDIN_JOB, True, "submitted", _SUBMITTED,
         confirm="#received:visible", routes=_linkedin_routes,
         covers="LinkedIn's job page, its Apply link through the redirector, the form"),
    Flow("greenhouse_embed", "greenhouse_embed.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible", covers="a company page embedding the form in an iframe",
         known="SP3: after the in-frame submit the embed's hidden file boxes still read as a "
               "form, so the confirmation is recorded submitted (unconfirmed)"),
    Flow("lever_single_park", "lever_single.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a one-page form in park mode"),
    Flow("login_wall_park", "login_wall.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-in screen, then the wizard"),
    Flow("login_two_step_park", "login_email_first.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible", password=True,
         covers="the address screen, the password screen, then the wizard"),
    Flow("signup_park", "signup.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-up screen, then the wizard"),
    Flow("combined_signup", f"{_CAREERS}/apply/42", True, "submitted", _SUBMITTED,
         confirm="#received:visible", password=True,
         routes=lambda base: {f"{_CAREERS}/**": _COMBINED},
         covers="one page that makes the account and sends the application"),
    Flow("submit_code", "submit_code.html", True, "submitted", _SUBMITTED,
         confirm="body[data-submitted]", send_urls=("**/forms/code_gate.html",), inbox=True,
         ats={"system": "greenhouse"},
         covers="the submit, the emailed-code gate read from the inbox, the confirmation"),
    Flow("review_steps", "review_steps.html", True, "submitted", _SUBMITTED,
         confirm="#received:visible", covers="a form step, a review page, the submit"),
    Flow("review_steps_park", "review_steps.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="the review page in park mode"),
    Flow("post_form", "post_form.html", True, "submitted", _SUBMITTED,
         confirm="body[data-confirmed]", covers="a real form POST the server counts"),
    Flow("captcha", "captcha.html", True, "needs_human", r"^captcha or bot check",
         covers="a bot check nobody solves parks"),
)


def flow(name: str) -> Flow:
    return next(f for f in FLOWS if f.name == name)


# --- sends --------------------------------------------------------------------------------

BINDING = "__applyHarnessSend"
_SEND_JS = """(() => {
  const report = () => { try { window.%s(); } catch (e) {} };
  const seen = new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === 'attributes' && r.attributeName === 'data-submitted'
          && r.target === document.body) { report(); }
    }
  });
  seen.observe(document, {attributes: true, attributeFilter: ['data-submitted'], subtree: true});
})();""" % BINDING


@dataclass
class Send:
    kind: str           # "dom" | "request" | "post"
    detail: str
    in_gate: bool


class Sends:
    """Every send of one run (see the module docstring)."""

    def __init__(self, recorder: Recorder | None = None):
        self.recorder = recorder
        self.events: list[Send] = []

    def _in_gate(self) -> bool:
        return bool(self.recorder and self.recorder.gate_depth > 0)

    @property
    def count(self) -> int:
        return len(self.events)

    def install(self, context, flow: Flow, server: FlowServer | None = None) -> None:
        context.expose_binding(BINDING, self._dom)
        context.add_init_script(_SEND_JS)
        for glob in flow.send_urls:
            context.route(glob, self._request)
        if server is not None:
            server.on_post = lambda name: self.events.append(
                Send("post", f"/submit/{name}", self._in_gate()))

    def uninstall(self, server: FlowServer | None = None) -> None:
        if server is not None:
            server.on_post = None

    def _dom(self, source, *args) -> None:
        try:
            url = str(source["frame"].url)
        except Exception:       # noqa: BLE001  (a frame gone by the time the call lands)
            url = ""
        self.events.append(Send("dom", url, self._in_gate()))

    def _request(self, route, request) -> None:
        self.events.append(Send("request", f"{request.method} {request.url}", self._in_gate()))
        route.fallback()


# --- the action recorder --------------------------------------------------------------------

_LIVE_JS = r"""el => {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  let text = '';
  if (tag === 'input') {
    if (['submit', 'button', 'reset', 'image'].includes(type)) text = el.value || '';
  } else if (tag !== 'textarea' && tag !== 'select') {
    text = el.innerText || el.textContent || '';
  }
  text = (text || el.getAttribute('aria-label') || el.getAttribute('title') || '')
    .replace(/\s+/g, ' ').trim().slice(0, 160);
  return {text: text, role: el.getAttribute('role') || '', tag: tag, type: type,
          form: !!(el.form || (el.closest && el.closest('form'))),
          url: String(el.ownerDocument.location.href)};
}"""
# The focused element of a page, for a key press or typed text without a target.
_FOCUS_JS = "() => { const el = document.activeElement; " \
            "if (!el || el === document.body) return {url: String(location.href)}; " \
            "return (" + _LIVE_JS + ")(el); }"
LIVE_TIMEOUT_MS = 1_000
# The loop's own send vocabulary (`apply_run.SUBMIT_WORDS`, `FINAL_WORDS`).
SUBMIT_WORDS = apply_run.SUBMIT_WORDS
FINAL_WORDS = apply_run.FINAL_WORDS
_ENTER_KEYS = ("Enter", "NumpadEnter")
_TARGET_ACTIONS = {"click": "click", "dblclick": "click", "tap": "click", "fill": "fill",
                   "type": "fill", "press_sequentially": "fill", "press": "press",
                   "check": "tick", "uncheck": "tick", "set_checked": "tick",
                   "select_option": "pick", "set_input_files": "upload",
                   "dispatch_event": "event"}
_KEYBOARD_ACTIONS = {"press": "press", "down": "press", "type": "fill", "insert_text": "fill"}
_ON_LINKEDIN_FORBIDDEN = ("fill", "tick", "pick", "upload", "gate")


def submit_worded(text: str, *, park_mode: bool) -> bool:
    """Does a control's live text read as sending the application, in the
    loop's words? A send word other than "apply" (a bare "Apply" is the
    posting's entry), or in park mode a last-step word on anything but an
    account step (`apply_run._final_shaped`, `_sends_application`)."""
    words = {w.lower() for w in SUBMIT_WORDS.findall(text or "")}
    if words - {"apply"}:
        return True
    return (park_mode and bool(FINAL_WORDS.search(text or ""))
            and not apply_run._ACCOUNT_STEP_WORDS.search(text or ""))


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return (urlsplit(str(url or "")).hostname or "").lower()


def on_linkedin(url: str) -> bool:
    host = _host(url)
    return host == "linkedin.com" or host.endswith(".linkedin.com")


@dataclass
class Action:
    kind: str           # click | fill | press | tick | pick | upload | event | gate
    url: str
    text: str = ""
    role: str = ""
    tag: str = ""
    type: str = ""
    in_gate: bool = False
    how: str = ""       # the Playwright call
    key: str = ""       # a key press's key
    form: bool = False  # the element (or the focused one) sits in a form

    @property
    def host(self) -> str:
        return _host(self.url)


class Recorder:
    """Every Playwright action of one run with its live element (see the
    module docstring). `recording()` installs the patches and removes them."""

    def __init__(self, flow: Flow | None = None, *, park_mode: bool = False,
                 password: str = ""):
        self.flow = flow
        self.park_mode = park_mode
        self.password = password
        self.actions: list[Action] = []
        self.gate_depth = 0
        self.final: dict[str, Any] = {}
        self.logs: list[str] = []
        self.files: list[Path] = []       # scanned for the password after the run
        self.judge_requests: list[str] = []   # every request the judge got, as JSON
        self.app_hosts: set[str] = set()  # where the master password may be typed
        self._keyboards: dict[int, Any] = {}

    def _live(self, target) -> dict:
        try:
            if hasattr(target, "page") and not hasattr(target, "as_element"):
                return dict(target.evaluate(_LIVE_JS, timeout=LIVE_TIMEOUT_MS))
            return dict(target.evaluate(_LIVE_JS))
        except Exception:       # noqa: BLE001  (a detached or ambiguous element)
            for owner in ("page", "owner_frame"):
                try:
                    found = getattr(target, owner)
                    return {"url": str((found() if callable(found) else found).url)}
                except Exception:   # noqa: BLE001
                    continue
            return {}

    def _add(self, kind: str, how: str, info: dict, key: str = "") -> None:
        self.actions.append(Action(kind=kind, how=how, url=str(info.get("url", "")),
                                   text=str(info.get("text", "")), role=str(info.get("role", "")),
                                   tag=str(info.get("tag", "")), type=str(info.get("type", "")),
                                   in_gate=self.gate_depth > 0, key=key,
                                   form=bool(info.get("form", False))))

    @staticmethod
    def _kind(kind: str, name: str, args: tuple, kw: dict) -> str:
        if kind == "event":
            event = args[0] if args else kw.get("type", "")
            return "click" if str(event).lower() == "click" else "event"
        return kind

    @contextmanager
    def recording(self):
        from playwright.sync_api import ElementHandle, Frame, Keyboard, Locator, Page

        p = Patches()
        rec = self
        try:
            for cls, label in ((Locator, "Locator"), (ElementHandle, "ElementHandle")):
                for name, kind in _TARGET_ACTIONS.items():
                    orig = getattr(cls, name, None)
                    if orig is None:
                        continue

                    def _on(target, *a, _orig=orig, _kind=kind, _name=name, _label=label, **kw):
                        key = str(a[0] if a else kw.get("key", "")) if _kind == "press" else ""
                        rec._add(rec._kind(_kind, _name, a, kw), f"{_label}.{_name}",
                                 rec._live(target), key)
                        return _orig(target, *a, **kw)
                    p.setattr(cls, name, _on)
            for cls, label in ((Frame, "Frame"), (Page, "Page")):
                for name, kind in _TARGET_ACTIONS.items():
                    orig = getattr(cls, name, None)
                    if orig is None or name == "press_sequentially":
                        continue

                    def _sel(owner, selector, *a, _orig=orig, _kind=kind, _name=name,
                             _label=label, **kw):
                        try:
                            info = rec._live(owner.locator(selector).first)
                        except Exception:       # noqa: BLE001
                            info = {}
                        info.setdefault("url", str(getattr(owner, "url", "")))
                        key = str(a[0] if a else kw.get("key", "")) if _kind == "press" else ""
                        rec._add(rec._kind(_kind, _name, a, kw), f"{_label}.{_name}", info, key)
                        return _orig(owner, selector, *a, **kw)
                    p.setattr(cls, name, _sel)

            keyboard_prop = Page.keyboard

            def _keyboard(page):
                kb = keyboard_prop.fget(page)
                rec._keyboards[id(kb)] = page
                return kb
            p.setattr(Page, "keyboard", property(_keyboard))
            for name, kind in _KEYBOARD_ACTIONS.items():
                orig = getattr(Keyboard, name, None)
                if orig is None:
                    continue

                def _key(kb, *a, _orig=orig, _kind=kind, _name=name, **kw):
                    page = rec._keyboards.get(id(kb))
                    try:
                        info = dict(page.evaluate(_FOCUS_JS)) if page is not None else {}
                    except Exception:   # noqa: BLE001
                        info = {}
                    key = str(a[0] if a else kw.get("key", "")) if _kind == "press" else ""
                    rec._add(_kind, f"Keyboard.{_name}", info, key)
                    return _orig(kb, *a, **kw)
                p.setattr(Keyboard, name, _key)

            gate = apply_run._JobRun._submit_gate

            def _gate(job, *a, **kw):
                url = ""
                try:
                    url = str(job.page.url)
                except Exception:   # noqa: BLE001
                    pass
                rec.actions.append(Action(kind="gate", url=url, in_gate=True,
                                          how="_JobRun._submit_gate"))
                rec.gate_depth += 1
                try:
                    return gate(job, *a, **kw)
                finally:
                    rec.gate_depth -= 1
            p.setattr(apply_run._JobRun, "_submit_gate", _gate)

            finish = apply_run._JobRun._finish

            def _finish(job, *a, **kw):
                rec.final = rec._final_state(job.page)
                return finish(job, *a, **kw)
            p.setattr(apply_run._JobRun, "_finish", _finish)

            handler = _ListHandler(self.logs)
            loggers = [logging.getLogger(n) for n in (
                "apply_run", "apply_fill", "apply_form", "apply_judge", "apply_inbox",
                "ats_accounts", "jev", "apply_trace")]
            levels = [lg.level for lg in loggers]
            for lg in loggers:
                lg.addHandler(handler)
                lg.setLevel(logging.INFO)       # the level a real run logs at
            try:
                yield self
            finally:
                for lg, level in zip(loggers, levels):
                    lg.removeHandler(handler)
                    lg.setLevel(level)
        finally:
            p.undo()

    def watch(self, judge: Any) -> Any:
        """`judge`, with every request it gets kept (for the password check)."""
        return _WatchedJudge(judge, self)

    def _final_state(self, page) -> dict[str, Any]:
        out: dict[str, Any] = {"url": "", "confirmed": False, "at_gate": False}
        if page is None:
            return out
        try:
            out["url"] = str(page.url)
            frames = list(page.frames)
        except Exception:       # noqa: BLE001  (the page is gone)
            return out

        def shown(selector: str) -> bool:
            for frame in frames:
                try:
                    if frame.locator(selector).count() > 0:
                        return True
                except Exception:   # noqa: BLE001
                    continue
            return False
        if self.flow is not None:
            out["confirmed"] = bool(self.flow.confirm) and shown(self.flow.confirm)
            out["at_gate"] = bool(self.flow.gate) and shown(self.flow.gate)
        return out


class _WatchedJudge:
    def __init__(self, inner: Any, recorder: Recorder):
        self.inner = inner
        self.recorder = recorder

    def judge(self, state: Any, questions: dict) -> dict:
        try:
            self.recorder.judge_requests.append(
                json.dumps({"state": state, "questions": questions}, default=str))
        except Exception:       # noqa: BLE001
            pass
        return self.inner.judge(state, questions)


class _ListHandler(logging.Handler):
    def __init__(self, sink: list[str]):
        super().__init__(logging.DEBUG)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.append(record.getMessage())
        except Exception:       # noqa: BLE001
            pass


# --- the invariants -------------------------------------------------------------------------

def _text_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        path = Path(path)
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix.lower() in (".md", ".json", ".log", ".txt"):
                    yield child
        elif path.is_file():
            yield path


def invariant_breaks(outcome: Any, recorder: Recorder, sends: Sends) -> list[str]:
    """Every invariant the run broke, one line each; [] when it held them all."""
    breaks: list[str] = []
    status = str(getattr(outcome, "status", ""))
    reason = str(getattr(outcome, "reason", ""))
    if sends.count > 1:
        breaks.append(f"SEND-COUNT: {sends.count} sends, at most one is allowed")
    if recorder.park_mode and sends.count:
        breaks.append(f"PARK-SENT: park mode sent {sends.count} application(s)")
    for s in sends.events:
        if not s.in_gate:
            breaks.append(f"SEND-OUTSIDE-GATE: a {s.kind} send ({s.detail}) while the "
                          "submit gate was not running")
    if status == "submitted" and not recorder.final.get("confirmed"):
        if not ("unconfirmed" in reason and sends.count):
            breaks.append(f"FALSE-SUBMITTED: submitted without the confirmation marker "
                          f"({reason})")
    for a in recorder.actions:
        if a.kind == "click" and not a.in_gate \
                and submit_worded(a.text, park_mode=recorder.park_mode):
            breaks.append(f"CLICK-OUTSIDE-GATE: clicked {a.text!r} ({a.role or a.tag}) on "
                          f"{a.host} outside the submit gate")
        enter = a.kind == "press" and a.key in _ENTER_KEYS
        if enter and not a.in_gate and (a.form or submit_worded(a.text,
                                                                 park_mode=recorder.park_mode)):
            breaks.append(f"ENTER-OUTSIDE-GATE: Enter on {a.tag or 'the page'} "
                          f"({'in a form' if a.form else repr(a.text)}) on {a.host} outside "
                          "the submit gate")
        if on_linkedin(a.url) and (a.kind in _ON_LINKEDIN_FORBIDDEN or enter):
            breaks.append(f"LINKEDIN-{'PRESS' if enter else a.kind.upper()}: a "
                          f"{a.kind} on {a.host}")
        if a.kind == "fill" and a.type == "password" and recorder.app_hosts \
                and a.host not in recorder.app_hosts:
            breaks.append(f"PASSWORD-OFF-SITE: a password box filled on {a.host}")
    if recorder.password:
        for path in _text_files(recorder.files):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if recorder.password in text:
                breaks.append(f"PASSWORD-LEAK: the master password is in {path.name}")
        if any(recorder.password in line for line in recorder.logs):
            breaks.append("PASSWORD-LEAK: the master password is in the log")
        if any(recorder.password in blob for blob in recorder.judge_requests):
            breaks.append("PASSWORD-TO-JUDGE: the master password reached the judge")
    return breaks


def assert_invariants(outcome: Any, recorder: Recorder, sends: Sends) -> None:
    breaks = invariant_breaks(outcome, recorder, sends)
    assert not breaks, "invariant breaks:\n" + "\n".join(breaks)


# The parks the user's policy allows: the park-mode submit, a required
# question the data cannot answer, a sensitive question, a payment, a check
# nobody solved, a dead page, and the window or tab the user closed.
_POLICY_PARKS = tuple(re.compile(p) for p in (
    r"^auto_apply_submit is off$", r"^required field without an answer",
    r"^asks for .*which auto-apply never fills", r"^payment requested",
    r"(?i)captcha|bot check", r"^error or dead page",
    "^" + re.escape(apply_run.CLOSED_REASON), "^" + re.escape(apply_run.TAB_CLOSED_REASON)))


def policy_park(status: str, reason: str) -> bool | None:
    """True for a park inside the user's policy, False for any other stop
    (a failure included), None for a submit."""
    if status == "submitted":
        return None
    return any(p.search(reason or "") for p in _POLICY_PARKS)


# --- running flows ----------------------------------------------------------------------------

@dataclass
class RunResult:
    flow: str
    judge: str
    status: str
    reason: str
    ok: bool
    breaks: list[str]
    sends: int
    pages: int
    seconds: float
    trace: str = ""
    policy: bool | None = None      # `policy_park` of the end


def judges(seeds: Iterable[int] = SUITE_SEEDS, *, fake: bool = True) -> list[tuple[str, Any]]:
    """("fake", FakeJev()) and ("noisy-<seed>", NoisyJev(FakeJev(), seed)) per seed."""
    out: list[tuple[str, Any]] = [("fake", jev.FakeJev())] if fake else []
    out += [(f"noisy-{s}", jev.NoisyJev(jev.FakeJev(), s)) for s in seeds]
    return out


def _fulfiller(body: str) -> Callable[[Any], None]:
    """A route handler serving `body` (one parameter: Playwright passes the
    request too to a handler that takes two)."""
    def _handle(route) -> None:
        route.fulfill(body=body, content_type="text/html")
    return _handle


def run_flow(f: Flow, judge: Any, judge_name: str, *, browser, server: FlowServer,
             workdir: Path, fast: bool = True) -> RunResult:
    """`f` once under `judge`: a fresh context, queue, ledger and job folder;
    the drain of that one job; the invariants."""
    import apply_queue

    rundir = Path(tempfile.mkdtemp(prefix=f"{f.name}-{judge_name}-", dir=str(workdir)))
    folder = write_job_folder(rundir / "job")
    queue = rundir / "queue.json"
    recorder = Recorder(f, park_mode=not f.submit, password=PASSWORD if f.password else "")
    recorder.app_hosts = f.app_hosts(server.base)
    sends = Sends(recorder)
    start = time.monotonic()
    with ExitStack() as stack:
        stack.enter_context(hermetic(rundir, password=f.password))
        if fast:
            stack.enter_context(fast_timing(f.settle_s))
        url = f.start_url(server.base)
        apply_queue.enqueue(apply_queue.new_entry(JOB_ID, company="Fabrikam",
                                                  title="Analytics Engineer", apply_url=url),
                            path=queue)
        apply_queue.set_artifacts(JOB_ID, {"folder": str(folder),
                                           "apply_md": str(folder / "apply.md"),
                                           "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")},
                                  path=queue)
        if f.ats:
            apply_queue.update(JOB_ID, path=queue, ats=f.ats)
        context = browser.new_context()
        stack.callback(context.close)
        stack.callback(sends.uninstall, server)
        for glob, body in f.routes(server.base).items():
            context.route(glob, _fulfiller(body))
        sends.install(context, f, server)
        stack.enter_context(recorder.recording())
        inbox = f"{server.base}/inbox/outlook_list.html" if f.inbox \
            else "https://mail.example.com/inbox"
        runner = apply_run.Runner(
            jev=recorder.watch(judge), queue_path=queue, profile_dir=rundir / "profile",
            settings={"auto_apply_submit": f.submit, "auto_apply_headless": True,
                      "auto_apply_jev_mode": "fake", "auto_apply_batch_cap": 1,
                      "auto_apply_generate": True},
            context=context, run_context={"signup_email": SIGNUP_EMAIL, "inbox_url": inbox},
            sleep=lambda s: None)
        outcomes = runner.drain(cap=1)
    recorder.files = [folder, queue]
    seconds = round(time.monotonic() - start, 2)
    if not outcomes:
        return RunResult(f.name, judge_name, "", "no outcome", False,
                         ["NO-OUTCOME: the drain ran no job"], sends.count, 0, seconds)
    out = outcomes[0]
    breaks = invariant_breaks(out, recorder, sends)
    traces = sorted((folder / "apply_trace").glob("attempt-*"))
    return RunResult(f.name, judge_name, out.status, out.reason,
                     f.reached(out.status, out.reason, recorder.final), breaks, sends.count,
                     out.pages, seconds, str(traces[-1]) if traces else "",
                     policy_park(out.status, out.reason))


def run_matrix(flows: Iterable[Flow], judge_list: list[tuple[str, Any]], *, browser,
               server: FlowServer, workdir: Path, fast: bool = True,
               progress: Callable[[RunResult], None] | None = None) -> list[RunResult]:
    results = []
    for f in flows:
        for name, judge in judge_list:
            r = run_flow(f, judge, name, browser=browser, server=server, workdir=workdir,
                         fast=fast)
            results.append(r)
            if progress is not None:
                progress(r)
    return results


def rates(results: list[RunResult]) -> dict[str, Any]:
    """Success rates under the fake and under the noisy judges over the
    flows that are not known failing (the floors read these), per flow, and
    the known flows' rates apart."""
    def rate(rows):
        return (sum(1 for r in rows if r.ok) / len(rows)) if rows else 1.0

    def row(rows):
        return {"fake": rate([r for r in rows if r.judge == "fake"]),
                "noisy": rate([r for r in rows if r.judge != "fake"]), "runs": len(rows)}
    known = {f.name for f in FLOWS if f.known}
    counted = [r for r in results if r.flow not in known]
    per_flow: dict[str, list[RunResult]] = {}
    for r in results:
        per_flow.setdefault(r.flow, []).append(r)
    parks = [r for r in results if r.policy is not None]
    return {"fake": rate([r for r in counted if r.judge == "fake"]),
            "noisy": rate([r for r in counted if r.judge != "fake"]),
            "all": rate(counted),
            "breaks": sum(len(r.breaks) for r in results),
            "parks": len(parks), "outside_policy": sum(1 for r in parks if not r.policy),
            "per_flow": {name: row(rows) for name, rows in per_flow.items()},
            "known": {name: row(rows) for name, rows in per_flow.items() if name in known}}


def summary(results: list[RunResult], *, width: int = 70) -> str:
    """The matrix as text: one row per run, then per-flow and overall rates,
    the known failing flows, and the parks outside the user's policy."""
    lines = [f"{'flow':<22} {'judge':<9} {'ok':<3} {'end':<16} {'reason':<{width}} breaks"]
    for r in results:
        reason = (r.reason or "")[:width]
        lines.append(f"{r.flow:<22} {r.judge:<9} {'yes' if r.ok else 'NO':<3} "
                     f"{r.status:<16} {reason:<{width}} {len(r.breaks)}")
        for b in r.breaks:
            lines.append(f"    ! {b}")
    rt = rates(results)
    lines.append("")
    lines.append(f"{'flow':<22} {'fake':>6} {'noisy':>6} runs")
    for name, row in rt["per_flow"].items():
        lines.append(f"{name:<22} {row['fake']:>6.0%} {row['noisy']:>6.0%} {row['runs']}")
    lines.append("")
    by_name = {f.name: f for f in FLOWS}
    for name, row in rt["known"].items():
        tag = by_name[name].known.split(":")[0]
        lines.append(f"known failing, {tag}: {name} (fake {row['fake']:.0%}, noisy "
                     f"{row['noisy']:.0%}; left out of the rates below)")
    lines.append(f"success: fake {rt['fake']:.1%}, noisy {rt['noisy']:.1%}, all {rt['all']:.1%} "
                 f"over {len(results)} runs; invariant breaks: {rt['breaks']}; parks outside "
                 f"the policy: {rt['outside_policy']} of {rt['parks']}")
    return "\n".join(lines)
