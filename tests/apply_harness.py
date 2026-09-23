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
  a confirmation page (`body[data-confirmed]`), or with the flow's own answer
  (`FlowServer.answers`: `slow_post` after `SLOW_POST_S`, `server_validation`
  with the same form marked invalid, a post back to `postback_emptied.html`
  with a note above the same form emptied). A post the server refuses
  (`FlowServer.rejects`) is counted as a send it did not accept.
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
  with a send counted; never `ready_to_submit` after a send (a click that
  dispatched and then timed out stays clicked: a review would send twice);
  never "the submit did not go through" after a send the site accepted;
  no click whose live text reads as a submit outside the gate; nothing
  clicked, typed or ticked inside a bot-check provider's frame; no fill,
  tick, pick, upload or gate on a `*.linkedin.com` page; no click on an Easy
  Apply control (its text or aria-label); the master password nowhere in the
  record, the trace, the queue or the logs. A flow that must end before any
  page opens (`Flow.opens_no_page`) is checked for that too.
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
# one run below what the matrix measures. Later phases raise it to 0.95.
SUCCESS_FLOOR = 0.88                      # SP3 fix round 1: 119 of 133 suite runs (0.895)
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
    ("apply_run", "POPUP_GRACE_S"): 0.3,
    ("apply_run", "ENTRY_POLL_MS"): 50,
    ("apply_run", "GOTO_RETRY_S"): 0.1,
    ("apply_run", "CLICK_TIMEOUT_S"): 3,
    ("apply_run", "SUBMIT_SETTLE_S"): 5,
    ("apply_run", "REDIRECT_TIMEOUT_S"): 6,
    # the empty-read and top-card waits keep room for the fixtures that render
    # late (0.8 s and 2.5 s after `load`)
    ("apply_run", "EMPTY_READ_MAX_S"): 4.0,
    ("apply_run", "EMPTY_READ_STABLE_S"): 1.2,
    ("apply_run", "EMPTY_READ_POLL_S"): 0.1,
    ("apply_run", "LINKEDIN_READY_S"): 5.0,
    ("apply_run", "LINKEDIN_POLL_MS"): 100,
    ("apply_run", "LINKEDIN_EASY_RECHECK_S"): 1.0,
    ("apply_run", "CONSENT_WAIT_S"): 1.5,
    # the post-submit read: the slow_post flow's answer comes 8 s after its
    # click, inside the click's own waits and this one
    ("apply_run", "POST_SUBMIT_WAIT_S"): 10.0,
    ("apply_run", "POST_SUBMIT_POLL_S"): 0.2,
    ("apply_run", "POST_SUBMIT_QUIET_S"): 0.3,
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


SLOW_POST_S = 6.0                   # the slow_post flow's answer: past the 5 s action timeout


def _page(name: str) -> Callable[[], str]:
    return lambda: (FIXTURES_DIR / "forms" / name).read_text(encoding="utf-8")


class _Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "FlowServer"

    def log_message(self, format, *args):    # noqa: A002  (the base class's signature)
        pass

    def do_POST(self):                       # noqa: N802  (the base class's naming)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)          # the posted answers are never kept
        path = self.path.split("?")[0].rstrip("/")
        answers = self.server.flow_server.answers
        if path.startswith("/submit/"):
            name = path[len("/submit/"):]
        elif path.startswith("/forms/") and path[len("/forms/"):] in answers:
            name = path[len("/forms/"):]       # a form that posts back to its own page
        else:
            self.send_error(404)
            return
        self.server.flow_server._posted(name)
        delay, answer = self.server.flow_server.answers.get(name, (0.0, None))
        if delay:
            time.sleep(delay)                # counted on arrival, answered late
        body = (answer() if callable(answer) else answer or CONFIRMATION_HTML).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FlowServer:
    """`tests/fixtures/` over http; `POST /submit/<name>` counts a send and
    answers with `CONFIRMATION_HTML`, or with `answers[name]` (a delay and a
    page)."""

    def __init__(self, root: Path = FIXTURES_DIR):
        self.root = Path(root)
        self.posts: dict[str, int] = {}
        self.on_post: Callable[[str], None] | None = None
        # name -> (seconds before the answer, the page or a function giving it)
        self.answers: dict[str, tuple[float, Any]] = {
            "slow_post": (SLOW_POST_S, None),
            "server_validation": (0.0, _page("server_validation_errors.html")),
            "postback_emptied.html": (0.0, _page("postback_emptied_answer.html")),
            "success_flash.html": (0.0, _page("success_flash_answer.html"))}
        self.rejects: set[str] = {"server_validation"}      # posts answered with a refusal
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


def linkedin_job_routes(page: str = "linkedin_posting.html", target: str = "ashby_steps.html",
                        *, hop: str = "linkedin_redirect.html", host: str = "www.linkedin.com",
                        same_tab: bool = False) -> Callable[[str], dict[str, str]]:
    """Routes (a function of the server's base URL; no network) for a
    LinkedIn job page fixture `page` served on `host`, its Apply link going
    through the `/safety/go/` hop to the fixture `target` (in the job page's
    own tab with `same_tab`). The hop is the redirector, whose script sends
    the tab on after a moment as LinkedIn's does (0.4 s here, 1.5 s in the
    fixture: the run waits either way), or the safety interstitial, whose
    Continue link leads on. The hop's route is registered last, so it wins
    for its own URLs."""
    def _routes(base: str) -> dict[str, str]:
        forms = FIXTURES_DIR / "forms"
        to = f"{base}/forms/{target}"
        hop_url = f"https://{host}/safety/go/?url={to}"
        posting = (forms / page).read_text(encoding="utf-8").replace(
            'href="linkedin_redirect.html"', f'href="{hop_url}"').replace(
            "window.open('linkedin_redirect.html'", f"window.open('{hop_url}'")
        if same_tab:
            posting = posting.replace(' target="_blank" rel="opener"', "")
        hop_page = (forms / hop).read_text(encoding="utf-8").replace(
            "location.replace('ashby_steps.html'); }, 1500)",
            f"location.replace('{to}'); }}, 400)").replace(
            'href="lever_single.html"', f'href="{to}"')
        return {f"https://{host}/**": posting, f"https://{host}/safety/go/**": hop_page}
    return _routes


_linkedin_routes = linkedin_job_routes()


def _no_routes(base: str) -> dict[str, str]:
    return {}


def read_as(out: dict, state: str, conf: float, probabilities: dict | None = None, *,
            nouls: str = "coherent") -> dict:
    """A test judge's scripted reading of a page as `state` at `conf` (SP4:
    the page read is a Choice and Nouls). `nouls`: "coherent" reads the
    Nouls with it (`state`'s yes, every other kind's no), as a judge that
    took the page for `state` would; "neutral" sets them to 0.5 (no word
    either way); "keep" leaves the wrapped judge's. Only the answers the
    request asked are set; `out` is returned."""
    if "page_state" in out:
        out["page_state"] = jev.Answer(kind="choice", choice=state, confidence=conf,
                                       probabilities=dict(probabilities or {state: conf}))
    if nouls == "keep":
        return out
    for kind, qids in jev.PAGE_KIND_NOULS.items():
        for qid in qids:
            if qid in out:
                p = 0.5 if nouls == "neutral" else (0.9 if kind == state else 0.1)
                out[qid] = jev.Answer(kind="noul", noul=p)
    return out


class LinkedInReadAsOther:
    """A judge that reads every LinkedIn page as `other` at 0.30 (the GTS
    park of 2026-09-22) and passes the rest to the judge it wraps."""

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        host = str(((state or {}).get("page") or {}).get("url_host") or "")
        if "page_state" in out and on_linkedin(f"https://{host}/"):
            out["page_state"] = jev.Answer(
                kind="choice", choice="other", confidence=0.30,
                probabilities={"other": 0.30, "job_posting": 0.28, "application_form": 0.22,
                               "login_wall": 0.20})
        return out


def _button_texts(state: Any) -> list[tuple[Any, str]]:
    """(n, text) of a request's buttons (the page read's and the mapping's)."""
    return [(b.get("n"), str(b.get("text") or "")) for b in (state or {}).get("buttons") or []]


def _noul(p: float) -> Any:
    return jev.Answer(kind="noul", noul=p)


class ModalReadAsForm:
    """A judge that reads Workday's start dialog (a page with an "Apply
    Manually" button) as an application form at 0.90, its Nouls with it (a
    form's details yes, a posting's no), and "Apply Manually" as the advance
    at 0.90 (the audit's INV-01 shape), and passes the rest to the judge it
    wraps."""

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        buttons = _button_texts(state)
        if not any(text == "Apply Manually" for _, text in buttons):
            return out
        if "page_state" in out:
            out["page_state"] = jev.Answer(
                kind="choice", choice="application_form", confidence=0.90,
                probabilities={"application_form": 0.90, "job_posting": 0.10})
            for qid, p in (("page_applicant_details", 0.9), ("page_job_description", 0.1),
                           ("page_apply_entry", 0.1)):
                if qid in out:
                    out[qid] = _noul(p)
        for n, text in buttons:
            qid = f"button_{n}_role"
            if text == "Apply Manually" and qid in out:
                out[qid] = jev.Answer(kind="choice", choice="advance", confidence=0.90,
                                      probabilities={"advance": 0.90, "apply_entry": 0.10})
        return out


class VerifiedReadAsConfirmation:
    """A judge that reads an email-verified page as a confirmation at 0.90,
    its received Noul yes (the I5 shape: "Your email is verified, thank
    you")."""

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        text = str(((state or {}).get("page") or {}).get("headline_text") or "")
        if "page_state" in out and "email is verified" in text:
            out["page_state"] = jev.Answer(kind="choice", choice="confirmation", confidence=0.90,
                                           probabilities={"confirmation": 0.90, "other": 0.10})
            if "page_received" in out:
                out["page_received"] = _noul(0.9)
        return out


# local stand-ins for a bot-check provider's frames (the flows route the
# provider's URL here; nothing reaches the provider)
_CHECKBOX_STUB = ("<!doctype html><html><body><div role=\"checkbox\" aria-checked=\"false\">"
                  "I'm not a robot</div></body></html>")
_CHALLENGE_STUB = ("<!doctype html><html><body><p>Select every image with a bus</p>"
                   "<button type=\"button\">Verify</button></body></html>")


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
    easy_apply: bool = False        # the queue entry's `is_easy_apply`
    wrap: Callable[[Any], Any] | None = None    # wraps every judge the flow runs under
    opens_no_page: bool = False     # the run must end before any page opens
    suite_seeds: int | None = None  # the suite runs this many noisy seeds (a slow flow: fewer)
    # the run ends before the judge reads a page (LinkedIn's job page is
    # decided by its handler alone): when the fake run asked the judge
    # nothing, every noisy seed's run is that run, and the suite copies it
    # (`judge_requests` 0 is checked); the script runs every seed
    judge_free: bool = False

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


# another company's job, where a run that took another job's Apply would land
_OTHER_JOB = """<!doctype html><html><head><title>Data Analyst at Contoso</title></head><body>
<h1>Data Analyst</h1><p>Contoso, New York</p>
<label>Full name * <input name="name" required></label>
<button type="button" onclick="document.body.dataset.wrongJob = 1">Submit application</button>
</body></html>"""

_SUBMITTED = r"^confirmation page"
_PARKED = r"^auto_apply_submit is off$"
_EASY_APPLY = "^" + re.escape(apply_run.EASY_APPLY_REASON) + "$"

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
         confirm="#thanks:visible", covers="a company page embedding the form in an iframe"),
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
    # --- SP2: the entry (Easy Apply, the LinkedIn job page, settling, consent) ---
    Flow("linkedin_easy_apply", _LINKEDIN_JOB, True, "needs_human", _EASY_APPLY,
         routes=linkedin_job_routes("linkedin_easy_apply.html", "lever_single.html"),
         judge_free=True,
         covers="a job page whose only Apply is Easy Apply (its aria-label) stops unclicked"),
    Flow("linkedin_easy_apply_modal", _LINKEDIN_JOB, True, "needs_human", _EASY_APPLY,
         routes=linkedin_job_routes("linkedin_easy_apply_modal.html", "lever_single.html"),
         judge_free=True,
         covers="LinkedIn's own form open in a modal: nothing filled, the run stops"),
    Flow("linkedin_easy_apply_flag", _LINKEDIN_JOB, True, "needs_human", _EASY_APPLY,
         routes=linkedin_job_routes(), easy_apply=True, opens_no_page=True,
         judge_free=True,
         covers="an Easy Apply queue entry ends before any page opens"),
    Flow("linkedin_posting_late", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting_late.html", "lever_single.html"),
         covers="a top card that renders 2.5 s after load, then the company's form"),
    Flow("linkedin_posting_noise", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting_noise.html", "lever_single.html"),
         covers="an alert switch, a feedback Submit, the messaging search and a hidden "
                "sign-in form beside the offsite Apply"),
    Flow("linkedin_gts_other", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting_noise.html", "lever_single.html"),
         wrap=LinkedInReadAsOther,
         covers="the GTS park: a posting with stray controls the judge reads as other at "
                "0.30 still reaches the company's form"),
    Flow("linkedin_applied", _LINKEDIN_JOB, True, "needs_human", r"^already applied: ",
         routes=linkedin_job_routes("linkedin_applied.html", "lever_single.html"),
         judge_free=True,
         covers="a job LinkedIn shows as applied is not applied to again"),
    Flow("linkedin_closed", _LINKEDIN_JOB, True, "needs_human", r"^closed: ",
         routes=linkedin_job_routes("linkedin_closed.html", "lever_single.html"),
         judge_free=True,
         covers="a posting that no longer accepts applications"),
    Flow("linkedin_signed_out", _LINKEDIN_JOB, True, "needs_human", r"^LinkedIn is signed out",
         routes=linkedin_job_routes("linkedin_signed_out.html", "lever_single.html"),
         judge_free=True,
         covers="a sign-in dialog and no offsite Apply: the user signs in to LinkedIn"),
    Flow("linkedin_safety_interstitial", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting.html", "lever_single.html",
                                    hop="linkedin_safety_interstitial.html"),
         covers="the safety reminder on the hop, which waits for its Continue"),
    Flow("spa_late_render", "spa_late_render.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a posting that renders 800 ms after load, then its same-tab Apply"),
    Flow("consent_overlay", "consent_overlay.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a cookie dialog over the posting, declined through its input button"),
    # --- SP2 review round 1 ---
    Flow("consent_wrapper", "consent_wrapper.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a consent vendor's sizeless wrapper holding a fixed banner and a page filter"),
    Flow("linkedin_two_pane", "https://www.linkedin.com/jobs/search/?currentJobId=4438751519"
         "&keywords=analytics", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_two_pane.html", "lever_single.html"),
         covers="the two-pane view: Easy Apply pills and cards, the job's pane rendered late"),
    Flow("linkedin_button_popup", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting_button.html", "lever_single.html"),
         covers="a signed-in page whose offsite Apply is a button opening a tab by script"),
    # --- SP2 review round 2 ---
    Flow("linkedin_interstitial_tab", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting.html", "lever_single.html",
                                    hop="linkedin_safety_interstitial_tab.html", same_tab=True),
         covers="a same-tab Apply onto the safety reminder, whose Continue opens a new tab"),
    Flow("linkedin_apply_in_list", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_apply_in_list.html", "lever_single.html"),
         covers="a top card's Apply in a list item, a rail of other jobs' Apply beside it"),
    # --- SP2 review round 3 ---
    Flow("linkedin_more_jobs_late", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=lambda base: {**linkedin_job_routes("linkedin_more_jobs_late.html",
                                                    "lever_single.html")(base),
                              "https://careers.contoso.example/**": _OTHER_JOB},
         covers="a top card rendered late beside another job's card with a company-site Apply"),
    # --- SP3: submit truth and the gate's invariants ---
    Flow("native_required_submit", "native_required_submit.html", True, "needs_human",
         r"^required field without an answer: Cover letter", confirm="#received:visible",
         covers="a hidden required box behind a rich-text editor: the gate reads the form's "
                "validity and stops before the click"),
    Flow("server_validation", "server_validation.html", True, "needs_human",
         r"^the submit did not go through: validation errors", confirm="#received:visible",
         covers="the server answers the post with the same form marked invalid: nothing sent"),
    Flow("submit_then_challenge", "submit_then_challenge.html", True, "needs_human",
         r"^a CAPTCHA challenge appeared after the submit click", confirm="#received:visible",
         routes=lambda base: {"https://hcaptcha.com/**": _CHALLENGE_STUB},
         covers="the submit raises a bot-check challenge: the person solves it"),
    Flow("slow_post", "slow_post.html", True, "submitted", _SUBMITTED,
         confirm="body[data-confirmed]", suite_seeds=1,
         covers="a form post answered after 8 s: the click stays clicked, the answer is read"),
    Flow("posting_with_alert_box", "posting_with_alert_box.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="a job-alert box beside the posting's Apply: the Apply is the entry"),
    Flow("workday_start_modal", "workday_start_modal.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible", wrap=ModalReadAsForm,
         covers="Workday's start dialog read as a form: Apply Manually opens it, never the gate"),
    Flow("recaptcha_checkbox", "recaptcha_checkbox.html", True, "needs_human",
         r"^a CAPTCHA check is on the form before the submit", confirm="#received:visible",
         routes=lambda base: {"https://www.google.com/recaptcha/**": _CHECKBOX_STUB},
         covers="a 78 px reCAPTCHA checkbox with an empty token: the form is filled, then the "
                "person ticks it"),
    # --- SP3 review round 1 ---
    Flow("recaptcha_checkbox_park", "recaptcha_checkbox.html", False, "ready_to_submit",
         r"^auto_apply_submit is off; a CAPTCHA checkbox is on the form",
         confirm="#received:visible", gate="#btn-submit:visible",
         routes=lambda base: {"https://www.google.com/recaptcha/**": _CHECKBOX_STUB},
         covers="park mode fills the form and stops at the gate; the person ticks the box"),
    Flow("postback_emptied", "postback_emptied.html", True, "needs_human",
         r"^check whether the application went through: a request left",
         covers="the post back shows a note above the same form emptied: the send may have "
                "gone, never read as not sent"),
    Flow("ajax_reset", "ajax_reset.html", True, "needs_human",
         r"^check whether the application went through: a request left",
         covers="a fetch send, then the form reset: never read as not sent"),
    # --- SP3 review round 2 ---
    Flow("success_flash_emptied", "success_flash.html", True, "needs_human",
         r"^check whether the application went through: a request left",
         covers="a success flash (role=alert) above the same form emptied after the post: "
                "never read as not sent"),
    Flow("reset_aria_invalid", "reset_aria_invalid.html", True, "needs_human",
         r"^check whether the application went through: a request left",
         covers="a fetch send, then a reset form with aria-invalid and its message on every "
                "box: never read as not sent"),
    Flow("email_verify_thanks", "verify_email_code.html", True, "needs_human",
         r"^(check whether the application went through: after the emailed code"
         r"|a confirmation page before any submit)",
         inbox=True, ats={"system": "greenhouse"}, wrap=VerifiedReadAsConfirmation,
         covers="a sign-up's email Verify, then a thanks for it: never read as submitted"),
    # --- SP4: page reading that holds up ---
    Flow("review_with_next", "review_with_next.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="a wizard step that reads as a review with only Next: the Next is clicked, the "
                "real last step reaches the gate"),
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
    accepted: bool = True       # the site took it (a refused post is still a send)


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
                Send("post", f"/submit/{name}", self._in_gate(), name not in server.rejects))

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
          aria: (el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 160),
          form: !!(el.form || (el.closest && el.closest('form'))),
          url: String(el.ownerDocument.location.href)};
}"""
# The focused element of a frame's document, for a key press or typed text
# without a target; `frame: true` when the focus sits in a child frame (the
# element is then read in that frame).
_FOCUS_JS = "() => { const el = document.activeElement; " \
            "if (!el || el === document.body) return {url: String(location.href)}; " \
            "if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') " \
            "return {url: String(location.href), frame: true}; " \
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
_CAPTCHA_TOUCH = ("click", "fill", "tick", "pick", "press", "event")   # never in a bot check


def submit_worded(text: str, *, park_mode: bool, account_step: bool = False) -> bool:
    """Does a control's live text read as sending the application, in the
    loop's words? A send word other than "apply" (a bare "Apply" is the
    posting's entry), unless the click is the account step's and the text
    names a sign-in or a code or link sent for one ("Sign in to apply", "Send
    code", "Send me a link": the account step's own exemption,
    `apply_run._sends_application`; everywhere else the loop routes those
    words to the submit gate); or in park mode a last-step word on anything
    but an account step (`apply_run._final_shaped`)."""
    words = {w.lower() for w in SUBMIT_WORDS.findall(text or "")}
    if words - {"apply"} and not (account_step and apply_run._SIGN_IN_WORDS.search(text or "")):
        return True
    return (park_mode and bool(FINAL_WORDS.search(text or ""))
            and not apply_run._ACCOUNT_STEP_WORDS.search(text or ""))


_EASY_APPLY_WORDS = re.compile(r"easy\s*apply", re.I)


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
    aria: str = ""      # the element's aria-label
    in_account: bool = False    # made by the account step (`_Accounts._fill`)

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
        self.account_depth = 0
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
                                   form=bool(info.get("form", False)),
                                   aria=str(info.get("aria", "")),
                                   in_account=self.account_depth > 0))

    @staticmethod
    def focused(page) -> dict:
        """The focused element's live info, read in the frame that holds the
        focus: a key pressed while an input inside an iframe has the focus
        goes to that input and its form (N4), so the main document's
        `activeElement` (the `<iframe>`) is followed down, frame by frame."""
        try:
            info = dict(page.evaluate(_FOCUS_JS))
        except Exception:   # noqa: BLE001
            return {}
        frame = page.main_frame
        for _ in range(8):
            if not info.get("frame"):
                return info
            found = None
            for child in frame.child_frames:
                try:
                    if child.frame_element().evaluate("el => el === document.activeElement"):
                        found = child
                        break
                except Exception:   # noqa: BLE001  (a detached frame)
                    continue
            if found is None:
                return info
            frame = found
            try:
                info = dict(frame.evaluate(_FOCUS_JS))
            except Exception:   # noqa: BLE001
                return info
        return info

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
                    info = rec.focused(page) if page is not None else {}
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

            account_fill = apply_run._Accounts._fill

            def _account(accounts, *a, **kw):
                rec.account_depth += 1
                try:
                    return account_fill(accounts, *a, **kw)
                finally:
                    rec.account_depth -= 1
            p.setattr(apply_run._Accounts, "_fill", _account)

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
    if status == "ready_to_submit" and sends.count:
        breaks.append(f"READY-AFTER-SEND: ready_to_submit ({reason}) after {sends.count} "
                      "send(s): a review would send it again")
    if reason.startswith(apply_run.NOT_SENT_REASON) and any(s.accepted for s in sends.events):
        breaks.append(f"NOT-SENT-AFTER-SEND: {reason[:80]!r} after a send the site accepted: "
                      "a retry would send it twice")
    for a in recorder.actions:
        if a.kind == "click" and not a.in_gate \
                and submit_worded(a.text, park_mode=recorder.park_mode,
                                  account_step=a.in_account):
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
        if a.kind == "click" and _EASY_APPLY_WORDS.search(f"{a.text} {a.aria}"):
            breaks.append(f"EASY-APPLY-CLICK: clicked {a.text or a.aria!r} on {a.host}")
        if a.kind in _CAPTCHA_TOUCH and apply_run._is_captcha_url(a.url):
            breaks.append(f"CAPTCHA-TOUCH: a {a.kind} inside the bot check on {a.host}")
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
# nobody solved, a dead page, and the window or tab the user closed; on
# LinkedIn, an Easy Apply job (the user applies there, by the user's call), a
# job already applied to, a closed posting, and LinkedIn signed out (dead ends
# the run cannot pass).
_POLICY_PARKS = tuple(re.compile(p) for p in (
    r"^auto_apply_submit is off(; |$)", r"^required field without an answer",
    r"^asks for .*which auto-apply never fills", r"^payment requested",
    r"(?i)captcha|bot check", r"^error or dead page",
    "^" + re.escape(apply_run.CLOSED_REASON), "^" + re.escape(apply_run.TAB_CLOSED_REASON),
    "^" + re.escape(apply_run.EASY_APPLY_REASON) + "$",
    "^" + re.escape(apply_run.apply_linkedin.APPLIED_REASON),
    "^" + re.escape(apply_run.apply_linkedin.CLOSED_REASON),
    "^" + re.escape(apply_run.apply_linkedin.SIGNED_OUT_REASON),
    # the site's own dead ends (SP4): a job it says was applied to before, a
    # posting it says is closed
    "^" + re.escape(apply_run.ALREADY_APPLIED_REASON),
    "^" + re.escape(apply_run.CLOSED_POSTING_REASON)))


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
    actions: list[Action] = field(default_factory=list, repr=False)  # what the run did
    judge_requests: int = 0         # the requests the judge got


def judges(seeds: Iterable[int] = SUITE_SEEDS, *, fake: bool = True) -> list[tuple[str, Any]]:
    """("fake", FakeJev()) and ("noisy-<seed>", NoisyJev(FakeJev(), seed)) per seed."""
    out: list[tuple[str, Any]] = [("fake", jev.FakeJev())] if fake else []
    out += [(f"noisy-{s}", jev.NoisyJev(jev.FakeJev(), s)) for s in seeds]
    return out


_LOCAL_HOSTS = ("127.0.0.1", "localhost")


_OFFLINE_MARK = "_apply_harness_offline"


def offline(context) -> None:
    """Keep a test context off the network: a request to anything but the
    local fixture server is aborted, unless a route registered after this one
    (a flow's fake host, LinkedIn's fixture) answers it first. Register it
    before the flow's routes: Playwright tries the latest route first. A
    context that has the guard is left as it is."""
    if getattr(context, _OFFLINE_MARK, False):
        return

    def _guard(route) -> None:
        if _host(route.request.url) in _LOCAL_HOSTS:
            route.continue_()
        else:
            route.abort("blockedbyclient")
    context.route("**/*", _guard)
    try:
        setattr(context, _OFFLINE_MARK, True)
    except Exception:       # noqa: BLE001  (a context that takes no attribute gets a second guard)
        pass


@contextmanager
def offline_contexts():
    """Every browser context made while this is on gets the `offline` guard
    at birth, whoever makes it: `Browser.new_context`, `Browser.new_page`'s
    own context, and `BrowserType.launch_persistent_context`. The browser
    tests' conftest turns it on for each module's browser, so a route a
    test forgot, or a fix reverted during a RED proof, cannot reach a real
    site."""
    from playwright.sync_api import Browser, BrowserType
    p = Patches()
    new_context, new_page = Browser.new_context, Browser.new_page
    persistent = BrowserType.launch_persistent_context

    def _new_context(self, *a, **kw):
        ctx = new_context(self, *a, **kw)
        offline(ctx)
        return ctx

    def _new_page(self, *a, **kw):
        page = new_page(self, *a, **kw)
        offline(page.context)
        return page

    def _persistent(self, *a, **kw):
        ctx = persistent(self, *a, **kw)
        offline(ctx)
        return ctx
    try:
        p.setattr(Browser, "new_context", _new_context)
        p.setattr(Browser, "new_page", _new_page)
        p.setattr(BrowserType, "launch_persistent_context", _persistent)
        yield
    finally:
        p.undo()


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
        if f.easy_apply:
            apply_queue.update(JOB_ID, path=queue, is_easy_apply=True)
        context = browser.new_context()
        stack.callback(context.close)
        stack.callback(sends.uninstall, server)
        opened: list = []
        context.on("page", lambda p: opened.append(p))
        offline(context)
        for glob, body in f.routes(server.base).items():
            context.route(glob, _fulfiller(body))
        sends.install(context, f, server)
        stack.enter_context(recorder.recording())
        inbox = f"{server.base}/inbox/outlook_list.html" if f.inbox \
            else "https://mail.example.com/inbox"
        if f.wrap is not None:
            judge = f.wrap(judge)
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
    if f.opens_no_page and opened:
        breaks.append(f"PAGE-OPENED: {len(opened)} page(s) opened for a job that must end "
                      "before any page")
    traces = sorted((folder / "apply_trace").glob("attempt-*"))
    return RunResult(f.name, judge_name, out.status, out.reason,
                     f.reached(out.status, out.reason, recorder.final), breaks, sends.count,
                     out.pages, seconds, str(traces[-1]) if traces else "",
                     policy_park(out.status, out.reason), list(recorder.actions),
                     len(recorder.judge_requests))


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
    # a flow designed to end off the policy list (the server's validation
    # answer, a "check whether" end) reaching that end is no miss: it is
    # counted apart, and the policy count shows only the misses
    return {"fake": rate([r for r in counted if r.judge == "fake"]),
            "noisy": rate([r for r in counted if r.judge != "fake"]),
            "all": rate(counted),
            "breaks": sum(len(r.breaks) for r in results),
            "parks": len(parks),
            "outside_policy": sum(1 for r in parks if not r.policy and not r.ok),
            "designed": sum(1 for r in parks if not r.policy and r.ok),
            "per_flow": {name: row(rows) for name, rows in per_flow.items()},
            "known": {name: row(rows) for name, rows in per_flow.items() if name in known}}


def summary(results: list[RunResult], *, width: int = 70) -> str:
    """The matrix as text: one row per run, then per-flow and overall rates,
    the known failing flows, the parks outside the user's policy that missed
    their flow's end, and apart from them the runs that reached a designed
    end off the policy list."""
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
                 f"the policy: {rt['outside_policy']} of {rt['parks']}; designed ends off the "
                 f"policy list: {rt['designed']}")
    return "\n".join(lines)
