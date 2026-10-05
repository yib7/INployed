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
  record, the trace, the queue or the logs; nothing typed, ticked or picked
  in a read-only box or a honeypot. A flow that must end before any
  page opens (`Flow.opens_no_page`) is checked for that too. Park and
  resume: a person's answer to a pause goes only into its own
  field, on the page the run paused on, and code never types into a
  sensitive field (a date of birth, an SSN).
- `PauseSpec` / `PauseResponder`: a pause flow's answer (`Flow.pause`), given
  from a thread as the dashboard's "Waiting for you" card gives it: the
  answer file in `apply_pause.pause_dir()`.
- `run_flow` / `run_matrix` / `summary`: one flow under one judge, the whole
  registry under many, and the table with success rates.

Hermetic: the job folder, the queue, the account ledger and the logs live in
a temp dir; the answer bank is `tests/fixtures/apply_matrix_answers.json` and
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
import apply_send_words  # noqa: E402
import jev  # noqa: E402
import jev_doubles  # noqa: E402

FIXTURES_DIR = REPO / "tests" / "fixtures"
BANK_PATH = FIXTURES_DIR / "apply_matrix_answers.json"
PASSWORD = "synthetic-matrix-Pw-7Qz4"      # the master password a flow's accounts step types
SIGNUP_EMAIL = "jane.doe@example.com"
JOB_ID = "42"
NOISY_SEEDS = tuple(range(1, 21))         # the script's seeds; the suite runs the first three
SUITE_SEEDS = NOISY_SEEDS[:3]
# The share of (flow, noisy seed) runs that reach their expected end, pinned
# under what the matrix measures, with room for a few timing misses.
SUCCESS_FLOOR = 0.97                      # measured: 204 of 205 suite runs (0.995); the misses
                                          # left are a mapping dropped on both looks and a
                                          # consent tick with one look under its floor
# Under the fake judge every flow reaches its end but the known failing ones
# (`Flow.known`), which the rates leave out.
FAKE_SUCCESS_FLOOR = 1.0

_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")

# The runner tests' synthetic sheet (`apply_data.build_markdown` over their
# synthetic master and `bank()`), with the sheet's delimiters filled in at run
# time. Its Standard answers and Address are what the store renders, so the
# runner's refresh before each job leaves it as it is.
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
- **Are you willing to work on-site (in the office)?** Yes
- **Work-authorization statement (free text).** Authorized to work in the United States; no visa sponsorship required.
- **Gender (EEO self-identification).** Decline to self-identify
- **Race / ethnicity (EEO self-identification).** Decline to self-identify
- **Veteran status (EEO self-identification).** I am not a protected veteran
- **Disability status (EEO self-identification).** No, I do not have a disability and have not had one in the past
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
    """The synthetic answer store's entries (version 2): every built-in set
    and confirmed, and one custom answer set but not confirmed
    (`unconfirmed_values`)."""
    return json.loads(BANK_PATH.read_text(encoding="utf-8"))


# An unconfirmed answer shorter than this is left out of the recorder's check:
# a "Yes" or a "2" turns up in many values the run types for other reasons.
UNCONFIRMED_MIN = 6


def unconfirmed_values(answers: list[dict] | None = None) -> tuple[str, ...]:
    """The answers the bank holds that the user has not confirmed (FL-1: the
    run never fills one), each at least `UNCONFIRMED_MIN` characters."""
    answers = bank() if answers is None else answers
    out = []
    for e in answers:
        text = str(e.get("answer") or "").strip() if isinstance(e, dict) else ""
        if isinstance(e, dict) and not e.get("confirmed") and len(text) >= UNCONFIRMED_MIN:
            out.append(text)
    return tuple(out)


def _value_text(value: Any) -> str:
    """A fill's or a pick's value as text: a string, the strings of a list,
    or a `select_option` dict's values."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_value_text(v) for v in value)
    return ""


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
def hermetic(rundir: Path, *, password: bool = False, today: Any = None):
    """The run's stores in `rundir` (the queue, the account ledger, the
    answer store holding the synthetic bank, which the runner reads once per
    drain), the synthetic bank for every catalog built without one, and the
    synthetic master password when the flow signs in (else none is stored).
    With `today` (a `datetime.date`), every catalog reads it as today's date:
    the real column passes the day its cache was recorded
    (`jev_harness.RECORDED_TODAY`)."""
    import apply_run
    import ats_accounts
    from resume_tailor import apply_answers, apply_config
    p = Patches()
    try:
        p.setenv("ATS_ACCOUNTS_PATH", str(Path(rundir) / "accounts.json"))
        p.setenv("APPLY_QUEUE_PATH", str(Path(rundir) / "queue.json"))
        answers = bank()
        store = Path(rundir) / "apply_answers.json"
        p.setattr(apply_answers, "STORE_PATH", store)
        p.setattr(apply_config, "APPLY_CONFIG", Path(rundir) / "apply_config.json")
        apply_answers.save(answers, store)
        real_build = apply_run.apply_facts.build

        def _build(folder, **kw):
            kw.setdefault("answers", answers)
            if today is not None:
                kw.setdefault("today", today)
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
    ("apply_click", "SETTLE_QUIET_S"): 0.1,        # a flow with a timer keeps its own (`Flow.settle_s`)
    ("apply_click", "SETTLE_MAX_S"): 3.0,
    ("apply_click", "NETWORK_IDLE_MS"): 50,
    ("apply_click", "POLL_S"): 0.05,
    ("apply_run", "POPUP_TIMEOUT_MS"): 1_500,
    ("apply_run", "POPUP_GRACE_S"): 0.3,
    ("apply_run", "ENTRY_POLL_MS"): 50,
    ("apply_run", "GOTO_RETRY_S"): 0.1,
    ("apply_run", "CLICK_TIMEOUT_S"): 3,
    ("apply_run", "SUBMIT_SETTLE_S"): 5,
    # a quiet step click that set a request going: no flow's step answers
    # later than 5 s (`test_a_slow_step_posts_once_and_is_waited_for` answers
    # at 5 s), so 8 s keeps every wait while a dead step parks in 8 s, not 20
    ("apply_run", "STEP_SETTLE_S"): 8,
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
    # the post-submit read: the slow_post flow's answer comes SLOW_POST_S after
    # its click, inside the click's own waits and this one
    ("apply_run", "POST_SUBMIT_WAIT_S"): 10.0,
    ("apply_run", "POST_SUBMIT_POLL_S"): 0.2,
    ("apply_run", "POST_SUBMIT_QUIET_S"): 0.3,
    # an inbox page's rows: the slow inbox fixture renders them 1.5 s after
    # its load; an inbox with no row at all is read as empty after this
    ("apply_inbox", "ROWS_WAIT_MS"): 4_000,
}


@contextmanager
def fast_timing(settle_s: float | None = None, extra: tuple = ()):
    """`FAST_TIMING` on the runner's modules; `settle_s` replaces the quiet
    window for a flow whose page moves on by a timer; `extra` raises a flow's
    own caps (`Flow.timing`)."""
    import importlib
    p = Patches()
    try:
        for (mod, name), value in [*FAST_TIMING.items(), *extra]:
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


# the slow_post flow's answer: past apply_click.ACTION_TIMEOUT_MS (5 s), so the click
# times out with the post in flight; no shorter while that timeout stands
SLOW_POST_S = 6.0


def _page(name: str) -> Callable[[], str]:
    return lambda: (FIXTURES_DIR / "forms" / name).read_text(encoding="utf-8")


class FixtureHTTPServer(http.server.ThreadingHTTPServer):
    """The fixture pages' server. The handler speaks HTTP/1.0, so Chromium
    opens one connection per request; under the matrix's `--jobs 8` load the
    accept loop falls behind, and the socketserver default backlog of 5 made
    Windows refuse the sixth pending connect, which left the tab on Chrome's
    own error page ("left the allowed sites: chromewebdata")."""
    request_queue_size = 128
    daemon_threads = True


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
            "success_flash.html": (0.0, _page("success_flash_answer.html")),
            # the post answered with no redirect by a page
            # that says a link to confirm the application was emailed
            "link_after_submit": (0.0, _page("link_sent.html"))}
        self.rejects: set[str] = {"server_validation"}      # posts answered with a refusal
        self._server = None
        self._thread = None
        self.base = ""

    def start(self) -> str:
        handler = partial(_Handler, directory=str(self.root))
        server = FixtureHTTPServer(("127.0.0.1", 0), handler)
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
                        same_tab: bool = False,
                        dest: Callable[[str], str] | None = None) -> Callable[[str], dict[str, str]]:
    """Routes (a function of the server's base URL; no network) for a
    LinkedIn job page fixture `page` served on `host`, its Apply link going
    through the `/safety/go/` hop to the fixture `target` (in the job page's
    own tab with `same_tab`), or to `dest(base)` when given (a routed host:
    a tracker, a job board). The hop is the redirector, whose script sends
    the tab on after a moment as LinkedIn's does (0.4 s here, 1.5 s in the
    fixture: the run waits either way), or the safety interstitial, whose
    Continue link leads on. The hop's route is registered last, so it wins
    for its own URLs."""
    def _routes(base: str) -> dict[str, str]:
        forms = FIXTURES_DIR / "forms"
        to = dest(base) if dest is not None else f"{base}/forms/{target}"
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

# An ad tracker and a job board between LinkedIn's Apply and the form
_TRACKER_URL = "https://click.appcast.io/t/4438751519"
_BOARD_URL = "https://www.dice.com/job-detail/4438751519"


def tracker_routes(base: str) -> dict[str, str]:
    """LinkedIn's Apply through its hop to an ad tracker whose script sends
    the tab to the company's form (`lever_single.html`) 3 s later."""
    page = (FIXTURES_DIR / "forms" / "tracker_redirect.html").read_text(encoding="utf-8")
    routes = linkedin_job_routes("linkedin_posting.html", dest=lambda b: _TRACKER_URL)(base)
    routes["https://click.appcast.io/**"] = page.replace("__TARGET__",
                                                        f"{base}/forms/lever_single.html")
    return routes


def board_chain_routes(base: str, boards: tuple[str, ...] = ("www.dice.com",
                                                              "www.ziprecruiter.com"),
                       *, plain_apply: bool = False, loop: bool = False) -> dict[str, str]:
    """LinkedIn's Apply to a chain of job boards (`boards`, each a copy of
    `aggregator.html`), each board's company link to the next board and the
    last one's to the company's form (`lever_single.html`). `plain_apply`:
    the last board's only off-site control reads "Apply now".
    `loop`: the last board's company link goes back to the first board, and
    no board links to the company."""
    page = (FIXTURES_DIR / "forms" / "aggregator.html").read_text(encoding="utf-8")
    targets = [f"https://{b}/job-detail/4438751519" for b in boards[1:]]
    targets.append(f"https://{boards[0]}/job-detail/4438751519" if loop
                   else f"{base}/forms/lever_single.html")
    routes = linkedin_job_routes("linkedin_posting.html",
                                 dest=lambda b: f"https://{boards[0]}/job-detail/4438751519")(base)
    for i, (board, target) in enumerate(zip(boards, targets)):
        body = page.replace("__COMPANY__", target)
        if plain_apply and i == len(boards) - 1:
            body = body.replace('<a class="btn" href="#board-apply" id="board-apply">Apply now</a>',
                                "").replace(">Apply on company site<", ">Apply now<")
        routes[f"https://{board}/**"] = body
    return routes


def board_routes(base: str, *, company_link: bool = True) -> dict[str, str]:
    """LinkedIn's Apply through its hop to a job board's copy of the posting,
    whose "Apply on company site" leads to the company's form
    (`lever_single.html`); without `company_link` the board's own Apply is
    all it offers."""
    page = (FIXTURES_DIR / "forms" / "aggregator.html").read_text(encoding="utf-8").replace(
        "__COMPANY__", f"{base}/forms/lever_single.html")
    if not company_link:
        page = page.replace('<a class="btn" href="' + f"{base}/forms/lever_single.html"
                            + '" id="company-site">Apply on company site</a>', "")
    routes = linkedin_job_routes("linkedin_posting.html", dest=lambda b: _BOARD_URL)(base)
    routes["https://www.dice.com/**"] = page
    return routes


def _no_routes(base: str) -> dict[str, str]:
    return {}


def read_as(out: dict, state: str, conf: float, probabilities: dict | None = None, *,
            nouls: str = "coherent") -> dict:
    """A test judge's scripted reading of a page as `state` at `conf` (the
    page read is a Choice and Nouls). `nouls`: "coherent" reads the
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
    """A judge that reads every LinkedIn page as `other` at 0.30 (as a real
    run once read one, and parked) and passes the rest to the judge it wraps."""

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
    at 0.90, and passes the rest to the judge it wraps."""

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


class CommitmentUnderFloor:
    """A judge that reads a background-check box as a consent under
    `CONSENT_MIN_CONF` on every look: 0.84 of the inner judge's confidence
    (under NoisyJev, 0.63 to 0.84). The commitment floor then decides, and
    the run must park on the box, never tick it."""

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        for row in (state or {}).get("fields") or []:
            qid = f"field_{row.get('n')}_source"
            a = out.get(qid)
            if "background check" in str(row.get("label", "")).lower() and a is not None \
                    and a.choice == "consent_attest":
                conf = round(0.84 * float(a.confidence or 0.0), 4)
                out[qid] = jev.Answer(kind="choice", choice="consent_attest", confidence=conf,
                                      probabilities={"consent_attest": conf,
                                                     "leave_blank": round(1 - conf, 4)})
        return out


class HeadlineLeftBlank:
    """A judge that maps a "Headline" box to `leave_blank` at 0.95, and
    reads any value in it as not the sheet's (0.10): the sheet names no
    headline, as a real judge sees, where the fake one's word match takes a
    fact and finds "form" and "field" in every sheet. The rest goes to the
    judge it wraps."""

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        for row in (state or {}).get("fields") or []:
            qid = f"field_{row.get('n')}_source"
            if str(row.get("label", "")).strip() == "Headline" and qid in out:
                out[qid] = jev.Answer(kind="choice", choice="leave_blank", confidence=0.95,
                                      probabilities={"leave_blank": 0.95, "full_name": 0.05})
        for qid, q in questions.items():
            instructions = q.get("instructions")
            if qid.startswith("verify_") and isinstance(instructions, dict) \
                    and instructions.get("field_label") == "Headline" and qid in out:
                out[qid] = _noul(0.1)
        return out


class OptionalLeftBlank:
    """A judge whose first look leaves the optional-looking "Years of
    experience", "Portfolio URL" and "Badge number" boxes without a mapping
    (a read under the floor, which an optional box gets no second look
    for); a request that carries them as required (the repair's, after the
    form said so) is the wrapped judge's."""
    LABELS = ("Years of experience", "Portfolio URL", "Badge number")

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        for row in (state or {}).get("fields") or []:
            if str(row.get("label", "")).strip() in self.LABELS and not row.get("required"):
                out.pop(f"field_{row.get('n')}_source", None)
        return out


class PauseLeftBlank:
    """A judge that maps the boxes labelled as in `LABELS` to `leave_blank`
    at 0.95 (no fact on the sheet answers them), as a real judge reads them:
    pause_disabled.html's optional referral code, so its
    Submit stays disabled after the fill, and pause_form.html's two "Please
    explain" boxes, so the run asks both. The rest goes to the judge it
    wraps."""
    LABELS = ("Referral code", "Please explain")

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        for row in (state or {}).get("fields") or []:
            qid = f"field_{row.get('n')}_source"
            label = str(row.get("label", "")).strip().rstrip(" *")
            if label in self.LABELS and qid in out:
                out[qid] = jev.Answer(kind="choice", choice="leave_blank", confidence=0.95,
                                      probabilities={"leave_blank": 0.95})
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
class PauseSpec:
    """How a pause flow's person answers: `mode` is "fill",
    "browser" or "park", or "timeout" (no answer comes); `values` are
    (words of the question's label, the value) pairs; `save` names the
    labels whose value is kept for future runs; `by_id` are (field id, value)
    pairs, read first (two fields with the same label)."""
    mode: str
    values: tuple[tuple[str, str], ...] = ()
    save: tuple[str, ...] = ()
    by_id: tuple[tuple[str, str], ...] = ()

    def answer(self, request: dict) -> tuple[str, dict[str, str], dict[str, bool]]:
        values: dict[str, str] = {}
        save: dict[str, bool] = {}
        ids = dict(self.by_id)
        for q in request.get("questions") or []:
            label = str(q.get("label") or "").lower()
            if str(q.get("field_id") or "") in ids:
                values[str(q["key"])] = ids[str(q["field_id"])]
                continue
            for words, value in self.values:
                if words.lower() in label:
                    values[str(q["key"])] = value
                    break
            if any(words.lower() in label for words in self.save):
                save[str(q["key"])] = True
        return self.mode, values, save


class PauseResponder:
    """Answers the run's pauses from a thread as the dashboard's card does:
    each request that lands in `apply_pause.pause_dir()` gets the answer
    `spec` gives (`PauseSpec.answer`), written with `apply_pause.write_answer`.
    With a `recorder`, every value given is noted with the field it answers
    and the page the run paused on (the pause invariants), before the answer
    file lands."""

    def __init__(self, spec: PauseSpec, recorder: Recorder | None = None, *,
                 poll_s: float = 0.05):
        self.spec = spec
        self.recorder = recorder
        self.poll_s = poll_s
        self.requests: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pause-responder", daemon=True)

    def start(self) -> "PauseResponder":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def __enter__(self) -> "PauseResponder":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run(self) -> None:
        import apply_pause
        seen: set[str] = set()
        while not self._stop.is_set():
            for req in apply_pause.pending_requests():
                pid = str(req.get("pause_id") or "")
                if pid in seen:
                    continue
                seen.add(pid)
                self.requests.append(req)
                mode, values, save = self.spec.answer(req)
                if self.recorder is not None:
                    for q in req.get("questions") or []:
                        value = values.get(str(q["key"]))
                        if not value:
                            continue
                        if q.get("sensitive"):
                            self.recorder.sensitive_values.add(value)
                        else:
                            self.recorder.user_values[value] = (str(q.get("field_id") or ""),
                                                                str(req.get("page_url") or ""))
                apply_pause.write_answer(req["job"], mode, values, save, pause_id=pid)
            self._stop.wait(self.poll_s)


@contextmanager
def pausing(rundir: Path, spec: PauseSpec | None, recorder: Recorder | None = None):
    """A pause flow's run: the pause folder in `rundir`, pauses on (the
    suite's `apply_pause.NEVER_WAIT` off), a short poll, a minute of one
    second for the timeout flow, and the responder answering. Yields the
    `auto_apply_pause_minutes` the run takes: 0 (no pause) with no `spec`."""
    import apply_pause
    if spec is None:
        yield 0
        return
    p = Patches()
    responder = None
    try:
        p.setenv("LOCALAPPDATA", str(Path(rundir) / "appdata"))
        p.setattr(apply_pause, "NEVER_WAIT", False)
        p.setattr(apply_pause, "POLL_S", 0.05)
        if spec.mode == "timeout":
            p.setattr(apply_pause, "SECONDS_PER_MINUTE", 1.0)
        else:
            responder = PauseResponder(spec, recorder).start()
        yield 1 if spec.mode == "timeout" else 10
    finally:
        if responder is not None:
            responder.stop()
        p.undo()


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
    routes: Callable[[str], dict[str, Any]] = _no_routes   # a glob -> HTML or a route handler
    password: bool = False          # a synthetic master password is stored
    inbox: bool = False             # the fixture inbox is the run's inbox
    inbox_page: str = "outlook_list.html"   # which one, under tests/fixtures/inbox/
    ats: dict[str, str] = field(default_factory=dict)
    settle_s: float | None = None   # the quiet window when a page moves on by a timer
    # ((module, name), value) caps raised for this flow on top of `FAST_TIMING`:
    # a wait that ends on its condition (a placeholder clearing) is given room
    # to, so a busy machine never ends it by its cap (the skeleton fixture)
    timing: tuple = ()
    # JS the run's page evaluates after every `apply_form.extract` of it: a
    # fixture that moves on once it has been read (the skeleton)
    # waits for that condition, never for a clock
    on_read: str = ""
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
    # the page's text changes with the clock, so no two runs ask the judge
    # the same request: a replay of the real judge's answers leaves the flow
    # out (its recording's run is its real column)
    replayable: bool = True
    # the real judge's cache holds a run of it: False for a flow added after
    # the last recording (a round that records nothing adds flows too). A
    # replay leaves it out and names it; the next recording takes it in
    recorded: bool = True
    # the real judge's end, (status, a regex the reason must match), when it
    # reads what the fake cannot: the fake answers not_settled to every settle
    # read, so a reworded question the saved answers settle parks under the
    # fake and its noisy seeds and fills under the real judge
    real_end: tuple[str, str] = ()
    # the run pauses on a question it can ask, and this is how
    # the person answers (`PauseSpec`); None: the run never pauses
    pause: PauseSpec | None = None

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

    def reached(self, status: str, reason: str, final: dict, judge: str = "") -> bool:
        """The run reached this flow's expected end: `real_end` under the
        real judge when the flow names one."""
        want, pattern = (self.real_end if judge == REAL and self.real_end
                         else (self.status, self.reason))
        if status != want or not re.search(pattern, reason or ""):
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
    # --- the entry (Easy Apply, the LinkedIn job page, settling, consent) ---
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
    Flow("linkedin_interstitial_tab", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting.html", "lever_single.html",
                                    hop="linkedin_safety_interstitial_tab.html", same_tab=True),
         covers="a same-tab Apply onto the safety reminder, whose Continue opens a new tab"),
    Flow("linkedin_apply_in_list", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_apply_in_list.html", "lever_single.html"),
         covers="a top card's Apply in a list item, a rail of other jobs' Apply beside it"),
    Flow("linkedin_more_jobs_late", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=lambda base: {**linkedin_job_routes("linkedin_more_jobs_late.html",
                                                    "lever_single.html")(base),
                              "https://careers.contoso.example/**": _OTHER_JOB},
         covers="a top card rendered late beside another job's card with a company-site Apply"),
    # --- submit truth and the gate's invariants ---
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
         covers="a form post answered after the click's action timeout: the click stays "
                "clicked, the answer is read"),
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
    # --- page reading that holds up ---
    Flow("review_with_next", "review_with_next.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="a wizard step that reads as a review with only Next: the Next is clicked, the "
                "real last step reaches the gate"),
    Flow("ticker_page", "ticker_page.html", True, "needs_human", r"^page did not advance",
         covers="a clock and a posted-ago note that change every second, a Continue that brings "
                "the same step back: read as not advancing, never as new pages",
         replayable=False),
    Flow("tracker_redirect", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", routes=tracker_routes,
         covers="an ad tracker's hop after LinkedIn's Apply, sending the tab on 3 s later: "
                "waited out, never the destination"),
    Flow("aggregator_company_site", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", routes=board_routes,
         covers="a job board's copy of the posting: its link to the company's site is followed "
                "once, the board is never filled"),
    Flow("aggregator_chain", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", routes=board_chain_routes,
         covers="a job board whose company link lands on a second board: the second board's "
                "own company link is followed, neither board is filled"),
    Flow("aggregator_board_only", _LINKEDIN_JOB, True, "needs_human",
         r"^aggregator posting on www\.dice\.com: no link to the company's site",
         routes=lambda base: board_routes(base, company_link=False),
         covers="a job board's posting whose only Apply is the board's own: parks at once"),
    Flow("popup_step_park", "popup_step.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="a Next that opens the next step in a new tab: the tab is the next page"),
    Flow("popup_step", "popup_step.html", True, "submitted", _SUBMITTED,
         confirm="#received:visible",
         covers="a Next and a submit that each open a new tab: the thank-you tab is read"),
    Flow("skeleton_then_form", "skeleton_then_form.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         # the skeleton stays until the run has read it, and the form comes
         # 800 ms later: the read waits for the skeleton to clear, never for a
         # clock, so its caps sit far past any busy machine's delay
         timing=((("apply_run", "LOADING_WAIT_S"), 30.0), (("apply_run", "EMPTY_READ_MAX_S"), 30.0)),
         on_read="() => window.__appRead && window.__appRead()",
         covers="a loading skeleton (aria-busy, a Cancel) that stays until the page is read, "
                "then the form: the skeleton is never read as the page"),
    Flow("privacy_gate", "privacy_gate.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="a privacy agreement as the first screen: its I Accept is the step's advance"),
    Flow("mailto_apply", "mailto_apply.html", True, "needs_human",
         r"^apply by email to jobs@contoso\.example$",
         covers="an Apply that is an email address: parks with the address, nothing clicked"),
    # --- extraction and fill coverage (replicas of real application forms) ---
    Flow("lever_cards", "lever_cards.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="star markers in their own spans, a location typeahead with no ARIA, a "
                "question of tick boxes, a list opening on a 'Click here' placeholder"),
    Flow("ashby_yesno", "ashby_yesno.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="Yes / No button pairs with aria-pressed, radios sharing one id, a resume "
                "parser's own upload left alone"),
    # the Contoso incident's form; the marker shows only for the
    # answers the typed store holds (a citizen or permanent resident: no
    # sponsorship, authorized; relocates). The relocation label names no
    # place, so the gate reads it as the stored fact's own question and the
    # run confirms all three answers end to end
    Flow("ashby_relocation", "ashby_relocation.html", True, "submitted", _SUBMITTED,
         confirm="body[data-auth=citizen][data-sponsor=no][data-relocate=willing] "
                 "#thanks:visible",
         covers="legal authorization among qualified options only (a Yes on a work visa "
                "is no Yes), sponsorship on Yes / No buttons settled by the alias set, and "
                "relocation: it confirms authorization, sponsorship and relocation from the "
                "typed answers"),
    # the same form, its relocation label naming a
    # place ("the job location (New York)"). The gate holds the stored
    # relocation answer back from a question worded another way, and the
    # settle read asks Jev whether it answers this one. The fake leaves every
    # settle read open, so the fake run parks on it; the real judge reads it
    # with where the candidate lives (the home line) and picks
    # the willing option, which the confirmation marker checks
    Flow("ashby_relocation_place", "ashby_relocation_place.html", True, "needs_human",
         r"^required field without an answer: Are you willing to relocate to the job location "
         r"\(New York\)\?$",
         confirm="body[data-auth=citizen][data-sponsor=no][data-relocate=willing] "
                 "#thanks:visible",
         real_end=("submitted", _SUBMITTED),
         covers="the place-named relocation question: the fake parks it and sends nothing, "
                "the real judge settles it from the saved relocation answer"),
    Flow("greenhouse_react_select", "greenhouse_react_select.html", True, "submitted",
         _SUBMITTED, confirm="#thanks:visible",
         covers="react-select dropdowns (the pick shown in a sibling, a hidden required twin), "
                "a resume labelled by its group, a pronouns question of tick boxes"),
    Flow("workday_create_account", "workday_chooser.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True,
         covers="Workday's start popup, its account screen with a robots-only box, dropdowns "
                "drawn as buttons (a country list of 64), a read-only email, date parts"),
    Flow("oracle_email_terms", "oracle_email_terms.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         password=True,
         covers="an email screen whose terms box is 0 x 0 behind its label, a honeypot off the "
                "page"),
    Flow("icims_iframe_login", "icims_iframe_login.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         password=True,
         covers="a posting in a content frame, an email screen whose Next stays disabled until "
                "its privacy box is ticked"),
    Flow("teamtailor_modal", "teamtailor_modal.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a cookie dialog, the form in a modal, sr-only 'Required' markers, a question "
                "drawn as a menu button of radio items"),
    Flow("paylocity_required_span", "paylocity_required_span.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="'(required)' spans, div dropdowns (51 states), an unnamed radiogroup of native "
                "radios, a resume box hidden behind its button"),
    Flow("rippling_generic_aria", "rippling_generic_aria.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="'Search' / 'Select...' / 'textbox' aria-labels under visible questions, a "
                "role=radio question, the only submit disabled until the form is complete "
                "(its question was once 'May we text you about this application?': the "
                "live judge leaves that blank, since no fact answers it, and the job parks)"),
    Flow("bamboo_honeypot_mui", "bamboo_honeypot_mui.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="a late posting with a read-only share box, a honeypot, hidden selects behind "
                "styled triggers, a 'file-input' label"),
    Flow("ukg_shadow_apply", "ukg_shadow_apply.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="buttons and a box inside open shadow roots, the Apply and the Submit among them"),
    Flow("aria_controls", "aria_controls.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="a role=checkbox consent and a role=switch toggle, a resume box hidden inside a "
                "wrapper until 'Attach resume' is clicked"),
    Flow("typeahead_editor", "typeahead_editor.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="a City combobox whose matches come only after typing, a rich-text cover letter"),
    Flow("consent_commitment", "consent_commitment.html", True, "needs_human",
         r"^required field without an answer: I consent to a background check$",
         confirm="#thanks:visible", wrap=CommitmentUnderFloor,
         covers="a required background-check consent beside a routine privacy box, read as a "
                "consent under 0.85 on every look: the run parks on it and never ticks it"),
    # --- advancing and repair ---
    Flow("masked_phone", "masked_phone.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a phone mask that takes keys only, a phone box beside a country code that "
                "takes bare digits"),
    Flow("date_mmddyyyy", "date_mmddyyyy.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a signature date box that takes MM/DD/YYYY only"),
    Flow("upload_resets_input", "upload_resets_input.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="an upload widget that keeps the file, shows a chip and resets its input: "
                "verified by the chip, uploaded once"),
    Flow("modal_with_combobox", "modal_with_combobox.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a form in a dialog that Escape closes, a typeahead that says expanded with "
                "no menu: no Escape without a menu"),
    Flow("conditional_fields", "conditional_fields.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", settle_s=0.8,
         covers="a source answer that reveals a required box half a second later, a consent "
                "tick that reveals the only submit"),
    Flow("resume_parse_autofill", "resume_parse_autofill.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible",
         gate="body[data-values-ok='1'] #btn-submit:visible", wrap=HeadlineLeftBlank,
         covers="a resume parser that writes its guesses after the upload, a profile lookup "
                "after the email: the sheet's values stand at the gate"),
    Flow("validation_errors", "validation_errors.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", wrap=OptionalLeftBlank,
         covers="a Next the form refuses: a phone it wants in digits, a blank box it needs, a "
                "summary no control names; repaired, then the gate"),
    Flow("validation_errors_submit", "validation_errors.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible", wrap=OptionalLeftBlank,
         covers="the same, then a submit the form refuses with nothing sent: repaired once and "
                "sent through the gate again"),
    Flow("validation_in_button_box", "validation_in_button_box.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible", gate="#btn-submit:visible", wrap=OptionalLeftBlank,
         covers="a Next refused with its summary written into the Next's own box: the same "
                "button found again after the repair"),
    Flow("validation_in_button_box_submit", "validation_in_button_box.html", True, "submitted",
         _SUBMITTED, confirm="#thanks:visible", wrap=OptionalLeftBlank,
         covers="the same, and a submit refused with its message in its own box: repaired and "
                "sent through the gate again"),
    Flow("hydration_beacon", "hydration_beacon.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a first Next click before the page's script is ready, on a page that posts its "
                "own telemetry: the telemetry is no request of the click's, so the quiet click "
                "gets its retry"),
    Flow("validation_banner_only", "validation_banner_only.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible", gate="#btn-submit:visible", wrap=OptionalLeftBlank,
         covers="a Next refused with one banner no control names: the judge's mapping is the "
                "only signal of the field it wants"),
    Flow("validation_banner_only_submit", "validation_banner_only.html", True, "submitted",
         _SUBMITTED, confirm="#thanks:visible", wrap=OptionalLeftBlank,
         covers="the same in submit mode: the banner's field repaired, then sent through the "
                "gate"),
    Flow("chat_launcher", "chat_launcher.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="body:not([data-chat-started]) #btn-submit:visible",
         covers="a chat panel fixed over the step's Next: closed by its own close button, the "
                "Next clicked once more, the chat never started"),
    Flow("cookie_banner", "cookie_banner.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="body[data-consent='rejected'] #btn-submit:visible",
         covers="a consent overlay that shows on scroll, over the Next: rejected, never "
                "accepted, and the Next clicked once more"),
    Flow("next_and_feedback_submit", "next_and_feedback_submit.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible",
         gate="body:not([data-feedback]) #btn-submit:visible",
         covers="a step's Next beside a feedback box's own Submit: the Next goes on, the "
                "feedback is never sent, the gate waits for the last step"),
    Flow("two_forms", "two_forms.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="body:not([data-signin-typed]) #btn-submit:visible",
         password=True,
         covers="a sign-in and a sign-up side by side with no account in the ledger: the "
                "sign-up's boxes alone take the address and the password"),
    Flow("apply_with_linkedin", "apply_with_linkedin.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="Apply with LinkedIn beside the posting's own Apply, Continue with LinkedIn "
                "above the step's Next: neither is taken"),
    Flow("talent_beside_application", "talent_beside_application.html", True, "submitted",
         _SUBMITTED, confirm="body:not([data-talent-sent]) #thanks:visible",
         wrap=OptionalLeftBlank,
         covers="a talent box's Submit above the application's own Submit, which refuses a "
                "blank box: repaired, the application's Submit clicked again, the talent box "
                "never sent"),
    Flow("upload_profile_kept", "upload_profile_kept.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="body[data-uploads='1'] #btn-submit:visible",
         covers="a returning candidate's page showing a kept resume of the same file name: "
                "this job's resume is uploaded, exactly once"),
    Flow("upload_profile_replace", "upload_profile_replace.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible", gate="body[data-uploads='1'] #btn-submit:visible",
         covers="a widget that replaces a kept resume's chip with this upload's, of the same "
                "name: verified by the change, uploaded once"),
    Flow("form_associated_invalid", "form_associated_invalid.html", True, "needs_human",
         r"^required field without an answer: Preferred shift \(a control the run cannot "
         r"read: form-associated custom element\)$", confirm="#thanks:visible",
         covers="a form-associated control its internals mark invalid, no required "
                "attribute: named, parked on, never sent"),
    Flow("form_associated_invalid_park", "form_associated_invalid.html", False, "needs_human",
         r"^required field without an answer: Preferred shift \(a control the run cannot "
         r"read: form-associated custom element\)$", confirm="#thanks:visible",
         covers="the same in park mode: never ready_to_submit with it unanswered"),
    Flow("recaptcha_disabled_submit", "recaptcha_disabled_submit.html", True, "needs_human",
         r"^a CAPTCHA check is on the form before the submit", confirm="#received:visible",
         routes=lambda base: {"https://www.google.com/recaptcha/**": _CHECKBOX_STUB},
         covers="a submit disabled until the reCAPTCHA tick: the gate's CAPTCHA path, the "
                "person ticks it"),
    Flow("recaptcha_disabled_submit_park", "recaptcha_disabled_submit.html", False,
         "ready_to_submit", r"^auto_apply_submit is off; a CAPTCHA checkbox is on the form",
         confirm="#received:visible", gate="#btn-submit:visible",
         routes=lambda base: {"https://www.google.com/recaptcha/**": _CHECKBOX_STUB},
         covers="the same in park mode: the gate with the checkbox's note"),
    # the chaos case: the form is drawn anew (the same markup, new nodes)
    # right after the run first reads it, before any act
    Flow("rerender_after_read", "lever_single.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         on_read="() => { if (window.__redrawn) return; window.__redrawn = 1; "
                 "const a = document.getElementById('application'); a.innerHTML = a.innerHTML; }",
         covers="the form drawn anew between the read and the fill: every act finds its "
                "control again"),
    Flow("closed_shadow_controls", "closed_shadow_controls.html", False, "needs_human",
         r"^required field without an answer: Earliest start date \(a control the run cannot "
         r"read: closed shadow root\)$",
         confirm="#thanks:visible",
         covers="a required start date inside a closed shadow root and a form-associated "
                "relocation choice: named, and the job parks on the required one"),
    # --- accounts and email ---
    # served on the company's careers host: the code comes from
    # careers@fabrikam.example, and the live judge rightly reads that mail as
    # sent by someone other than 127.0.0.1 (0.09)
    Flow("otp_six_boxes", f"{_CAREERS}/apply/otp", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True, inbox=True,
         inbox_page="otp_list.html",
         routes=lambda base: {f"{_CAREERS}/**": (FIXTURES_DIR / "forms" / "otp_six_boxes.html")
                              .read_text(encoding="utf-8")},
         covers="a sign-up, then its code in six one-character boxes, the fresh code below an "
                "older one from the same sender, then the application"),
    Flow("workday_signin_modal", "workday_signin_modal.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True, inbox=True,
         inbox_page="link_list.html", ats={"system": "workday"},
         covers="Workday's start popup, a Sign In dialog whose way to an account is a Create "
                "Account button, the password rules, the account checked by a link in the "
                "email, the sign-in after it, then the wizard"),
    Flow("workday_link_pick", "workday_signin_modal.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True, inbox=True,
         inbox_page="link_pick_list.html", ats={"system": "workday"},
         covers="the account check's email holds two links whose words read as a check, the "
                "job alerts' confirmation first: the judge picks the account's link, the "
                "sign-in after it, then the wizard"),
    Flow("signup_exists", "signup_exists.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-up that says the address has an account: one sign-in instead, never a "
                "second sign-up, then the wizard"),
    Flow("password_rules", "password_rules.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-up that states its password rules beside the box: read, met by the "
                "stored password, then the wizard"),
    Flow("slow_signup", "slow_signup.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True, suite_seeds=1,
         covers="a sign-up that posts and then shows nothing for 8 s: waited for, clicked "
                "once"),
    Flow("sso_buttons", "sso_buttons.html", True, "needs_human",
         r"^sign-in only through another site \(Google, Microsoft, LinkedIn, Apple\)",
         password=True,
         covers="a portal whose only way on is a sign-in with Google, Microsoft, LinkedIn or "
                "Apple: parks at once, none clicked"),
    Flow("signin_alerts_link", "signin_alerts_link.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-in whose header carries a job-alerts sign-up link: the screen's own "
                "Create Account button makes the account, never the alerts link"),
    # --- the code and link steps after the answers ---
    Flow("link_after_submit", "link_after_submit.html", True, "submitted",
         r"^submitted \(unconfirmed\): the emailed link's page on \S+ says "
         r"'application has been received'", inbox=True, inbox_page="link_confirm_list.html",
         ats={"system": "greenhouse"},
         covers="a form post answered with no redirect by a page that says a link was emailed: "
                "the link opens in a tab of its own, its page is the confirmation, and the job's "
                "tab is never loaded again, so one post goes"),
    Flow("link_after_answers_park", "link_after_answers.html", False, "ready_to_submit",
         r"^auto_apply_submit is off; the emailed link from \S+ is the step that may send the "
         r"application", gate="#link-sent:visible", inbox=True,
         inbox_page="link_confirm_list.html", ats={"system": "greenhouse"},
         covers="the answers, then a Continue to a page that says a link was emailed: park mode "
                "stops there and never opens the link"),
    # the same in submit mode. The link's page
    # confirms the address alone: a page that says the application was
    # received would end the job submitted through the link, a send this
    # harness sees only inside the submit gate
    Flow("link_after_answers", "link_after_answers.html", True, "needs_human",
         "^" + re.escape(apply_run.CHECK_SENT_REASON) + r": the emailed link on \S+ was opened "
         r"after the application's answers went on the site, and its page shows no received "
         r"words; the job's tab was not loaded again$", inbox=True,
         inbox_page="link_email_list.html", ats={"system": "greenhouse"},
         covers="submit mode: the answers, then a Continue to a page that says a link was "
                "emailed; the link opens in a tab of its own, its page confirms only the "
                "address, and the job's tab is never loaded again: the person checks whether "
                "the application went through"),
    Flow("code_after_answers", "code_after_answers.html", True, "submitted", _SUBMITTED,
         confirm="body[data-confirmed]", inbox=True, ats={"system": "greenhouse"},
         covers="the answers, a Continue to an emailed-code step, the code typed and its Verify "
                "clicked once, then the last step sent through the gate"),
    Flow("code_after_answers_park", "code_after_answers.html", False, "ready_to_submit",
         r"^auto_apply_submit is off; the emailed code is entered and its button \(Verify\) is "
         r"the step that may send the application", gate="#btn-verify:visible", inbox=True,
         ats={"system": "greenhouse"},
         covers="the same in park mode: the code is typed and its button never clicked, "
                "whatever role the judge gave it"),
    # a master password is stored, as for every flow with an address screen: a
    # noisy read of that screen as a login wall takes the account step's way
    # through it (a 20-seed matrix run found seeds 6 and 18 so)
    Flow("email_code_first_park", "email_code_first.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", inbox=True, password=True,
         ats={"system": "greenhouse"},
         covers="an email-first start, its emailed code, then the application: the address alone "
                "is no application on the site, so park mode passes the code step and stops at "
                "the submit"),
    Flow("login_get_park", "login_get_form.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-in form sent with method=get, whose next page's query carries the "
                "password: the trace keeps each URL without its query, so PASSWORD-LEAK "
                "covers it"),
    # park and resume: two required questions no saved answer
    # holds pause the run; the person's answers go in and the run goes on
    Flow("pause_fill", "pause_form.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         pause=PauseSpec("fill", (("query language", "Datalog 2.0 (user)"),
                                  ("preferred team", "Platform"),
                                  ("conference talks", "37"))),
         covers="a pause answered in the card: each value in its own field, then the submit"),
    Flow("pause_browser", "pause_form.html?browser=1", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible", pause=PauseSpec("browser"),
         covers="a pause the person finishes in the browser: the page read again, their "
                "values kept, then the submit"),
    Flow("pause_changed_page", "pause_form.html?grow=1", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         pause=PauseSpec("fill", (("query language", "Datalog 2.0 (user)"),
                                  ("preferred team", "Platform"),
                                  ("conference talks", "37"))),
         covers="a page that changed during the pause: read and planned again before any "
                "fill, the answers put in on the new plan"),
    Flow("pause_sensitive", "pause_form.html?dob=1", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         pause=PauseSpec("fill", (("query language", "Datalog 2.0 (user)"),
                                  ("preferred team", "Platform"),
                                  ("conference talks", "37"),
                                  ("date of birth", "11/11/1911"))),
         covers="a required date of birth the person types in the browser: code never "
                "types it, the value an answer gives for it is ignored"),
    Flow("pause_timeout", "pause_form.html", True, "needs_human",
         r"^required field without an answer: ", pause=PauseSpec("timeout"),
         covers="no answer in time: the job parks as a run with no pause would"),
    Flow("pause_park", "pause_form.html", True, "needs_human",
         r"^required field without an answer: ", pause=PauseSpec("park"),
         covers="Park it: the job parks as a run with no pause would"),
    Flow("pause_dup_labels", "pause_form.html?dup=1&grow=1", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible", wrap=PauseLeftBlank,
         pause=PauseSpec("fill", (("query language", "Datalog 2.0 (user)"),
                                  ("preferred team", "Platform"),
                                  ("conference talks", "37")),
                         by_id=(("explain_a", "Alpha reason (user)"),
                                ("explain_b", "Beta reason (user)"))),
         covers="two required fields with the same label, answered in the card, on a page that "
                "changed during the pause: each answer goes in its own field on the replan"),
    Flow("pause_submit_in_browser", "pause_disabled.html?click=1", True, "needs_human",
         "^" + re.escape(apply_run.CHECK_SENT_REASON) + r": the page moved on during the pause",
         wrap=PauseLeftBlank, pause=PauseSpec("browser"),
         covers="a Submit disabled after the fill; the person fixes the page and clicks Submit "
                "in the browser during the pause: the run parks with the check-whether note, "
                "never fills the page after it, and the job is never re-queued"),
    Flow("pause_fix_kept", "pause_disabled.html?reformat=1", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible",
         gate="body:not([data-phone-retyped]) #btn-submit:not([disabled])",
         wrap=PauseLeftBlank, pause=PauseSpec("browser"),
         covers="a Submit disabled after the fill; the person rewrites the phone the run typed "
                "and fills the referral in the browser: the replan keeps both"),
    Flow("pause_submit_to_account", "pause_disabled.html?click=1&account=1", True,
         "needs_human",
         "^" + re.escape(apply_run.CHECK_SENT_REASON) + r": the page moved on during the pause "
         r"\(its 'Submit application' button is gone\)",
         wrap=PauseLeftBlank, pause=PauseSpec("browser"),
         covers="the person clicks the enabled Submit during the pause and the site shows an "
                "account form that shares the Email label, with no received words: the "
                "paused page's send button is gone, so the job may have been sent and parks "
                "with the check-whether note"),
    Flow("pause_wizard_next", "pause_disabled.html?click=1&wizard=1", True, "submitted",
         _SUBMITTED, confirm="#thanks:visible", wrap=PauseLeftBlank,
         pause=PauseSpec("browser"),
         covers="a Next disabled after the fill; the person fixes the page and clicks Next "
                "during the pause, and the address moves on to a review step: a page with no "
                "send button that moved on is planned again, and the gate sends"),
    # added after the last recording
    Flow("pause_wizard_fill", "pause_disabled.html?click=1&wizard=1&again=1", True,
         "submitted", _SUBMITTED, confirm="#thanks:visible", wrap=PauseLeftBlank,
         pause=PauseSpec("fill", (("referral code", "CARD-7 (user)"),)),
         covers="a Next disabled after the fill; the person answers the referral code in the "
                "card and also clicks Next in the browser, and the review step has its own "
                "Referral code box: the card's answer goes nowhere on the step it moved on to"),
)


def flow(name: str) -> Flow:
    return next(f for f in FLOWS if f.name == name)


# --- sends --------------------------------------------------------------------------------

BINDING = "__applyHarnessSend"
# once per document (a tab's window outlives its first blank document, so the
# mark is the document's own)
_SEND_JS = """(() => {
  if (document.__applyHarnessSendSeen) { return; }
  document.__applyHarnessSendSeen = true;
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


def _watch_document(page) -> None:
    try:
        page.evaluate(_SEND_JS)
    except Exception:       # noqa: BLE001  (a tab closed or moving on: its next page is watched)
        pass


# a request that failed before any connection was made never reached the site;
# a reset or a bare failure may have after it left
_NO_CONNECTION = re.compile(r"ERR_(?:CONNECTION_REFUSED|ADDRESS_UNREACHABLE|NAME_NOT_RESOLVED|"
                            r"NAME_RESOLUTION_FAILED|BLOCKED_BY_CLIENT)\b")


class Sends:
    """Every send of one run (see the module docstring). A send request
    that fails before any connection (`_NO_CONNECTION`) stays counted as an
    attempt and is marked not accepted."""

    def __init__(self, recorder: Recorder | None = None):
        self.recorder = recorder
        self.events: list[Send] = []
        self._requests: list[tuple[Any, Send]] = []

    def _in_gate(self) -> bool:
        return bool(self.recorder and self.recorder.gate_depth > 0)

    @property
    def count(self) -> int:
        return len(self.events)

    def install(self, context, flow: Flow, server: FlowServer | None = None) -> None:
        context.expose_binding(BINDING, self._dom)
        context.add_init_script(_SEND_JS)
        # a tab `window.open` made keeps its first blank window for the page
        # it loads, and the init script does not run again for that page: a
        # submit in such a tab went unseen (popup_step, found by the final
        # review's SUBMITTED-WITHOUT-SEND, C-M3). Each tab's loaded page gets
        # the watch too; the document's mark keeps it to one
        context.on("page", self._watch_tab)
        for glob in flow.send_urls:
            context.route(glob, self._request)
        if flow.send_urls:
            context.on("requestfailed", self._failed)
        if server is not None:
            server.on_post = lambda name: self.events.append(
                Send("post", f"/submit/{name}", self._in_gate(), name not in server.rejects))

    def uninstall(self, server: FlowServer | None = None) -> None:
        if server is not None:
            server.on_post = None

    def _watch_tab(self, page) -> None:
        page.on("domcontentloaded", _watch_document)

    def _dom(self, source, *args) -> None:
        try:
            url = str(source["frame"].url)
        except Exception:       # noqa: BLE001  (a frame gone by the time the call lands)
            url = ""
        self.events.append(Send("dom", url, self._in_gate()))

    def _request(self, route, request) -> None:
        send = Send("request", f"{request.method} {request.url}", self._in_gate())
        self.events.append(send)
        self._requests.append((request, send))
        route.fallback()

    def _failed(self, request) -> None:
        """A send request the network dropped: not accepted when no
        connection was made (a later route's abort)."""
        try:
            failure = str(request.failure or "")
        except Exception:       # noqa: BLE001  (a request gone with its page)
            return
        if not _NO_CONNECTION.search(failure):
            return
        for seen, send in self._requests:
            if seen is request:
                send.accepted = False


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
  // an icon button's words are its accessible name (a submit's menu arrow
  // whose only text is white space around an svg)
  const shown = (text || '').replace(/\s+/g, ' ').trim();
  text = (shown || el.getAttribute('aria-label') || el.getAttribute('title') || '')
    .replace(/\s+/g, ' ').trim().slice(0, 160);
  // a box no person fills, by the page's own truth, never the extractor's
  // word lists: read-only (a combobox opens on a click), a box
  // the fixture declares one (`data-harness-junk`), off the page, two pixels
  // or less, or under aria-hidden; a text box or a textarea only
  let junk = el.getAttribute('data-harness-junk') || '';
  const typing = tag === 'textarea' || (tag === 'input'
    && !['checkbox', 'radio', 'file', 'submit', 'button', 'reset', 'image', 'hidden'].includes(type));
  if (typing && !junk) {
    const r = el.getBoundingClientRect();
    const x = window.scrollX || 0, y = window.scrollY || 0;
    if (el.readOnly && el.getAttribute('role') !== 'combobox') junk = 'read-only';
    else if (r.right + x < 0 || r.bottom + y < 0 || r.left + x < -500 || r.top + y < -500) junk = 'off the page';
    else if (r.width <= 2 && r.height <= 2) junk = 'two pixels or less';
    else if (el.closest('[aria-hidden=true]')) junk = 'aria-hidden';
  }
  // a tick or a toggle: it never sends
  const role = el.getAttribute('role') || '';
  const toggle = ['checkbox', 'switch', 'radio', 'option', 'menuitemcheckbox',
                  'menuitemradio'].includes(role) || ['checkbox', 'radio'].includes(type)
    || el.hasAttribute('aria-pressed');
  return {text: text, role: role, tag: tag, type: type, toggle: toggle,
          aria: (el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 160),
          form: !!(el.form || (el.closest && el.closest('form'))),
          url: String(el.ownerDocument.location.href), junk: junk,
          name: el.getAttribute('name') || el.id || ''};
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
# The loop's own send vocabulary (`apply_send_words.SUBMIT_WORDS`, `FINAL_WORDS`).
SUBMIT_WORDS = apply_send_words.SUBMIT_WORDS
FINAL_WORDS = apply_send_words.FINAL_WORDS
_ENTER_KEYS = ("Enter", "NumpadEnter")
_TARGET_ACTIONS = {"click": "click", "dblclick": "click", "tap": "click", "fill": "fill",
                   "type": "fill", "press_sequentially": "fill", "press": "press",
                   "check": "tick", "uncheck": "tick", "set_checked": "tick",
                   "select_option": "pick", "set_input_files": "upload",
                   "dispatch_event": "event"}
_KEYBOARD_ACTIONS = {"press": "press", "down": "press", "type": "fill", "insert_text": "fill"}
_ON_LINKEDIN_FORBIDDEN = ("fill", "tick", "pick", "upload", "gate")
_CAPTCHA_TOUCH = ("click", "fill", "tick", "pick", "press", "event")   # never in a bot check


def submit_worded(text: str, *, park_mode: bool, account_step: bool = False,
                  toggle: bool = False) -> bool:
    """Does a control's live text read as sending the application, in the
    loop's words? A send word other than "apply" (a bare "Apply" is the
    posting's entry), unless the click is the account step's and the text
    names a sign-in or a code or link sent for one ("Sign in to apply", "Send
    code", "Send me a link": the account step's own exemption,
    `apply_send_words._sends_application`; everywhere else the loop routes those
    words to the submit gate); or in park mode a last-step word on anything
    but an account step (`apply_send_words._final_shaped`). A tick or a toggle
    (`toggle`: a checkbox, switch, radio or option, or an aria-pressed
    button) is left out of the last-step words only ("I confirm the
    information above is complete" is an answer the loop ticks); a toggle whose own name sends ("Submit application") still
    reads as sending."""
    words = {w.lower() for w in SUBMIT_WORDS.findall(text or "")}
    if words - {"apply"} and not (account_step and apply_send_words.SIGN_IN_WORDS.search(text or "")):
        return True
    return (park_mode and not toggle and bool(FINAL_WORDS.search(text or ""))
            and not apply_send_words._ACCOUNT_STEP_WORDS.search(text or ""))


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
    junk: str = ""      # a box no person fills: read-only, or a honeypot
    toggle: bool = False    # a tick or a toggle (a checkbox, switch, radio or option role,
                            # aria-pressed): it never sends
    secret: bool = False    # the value typed is the master password (never the value)
    unconfirmed: bool = False   # the value filled or picked holds an answer the user has
                                # not confirmed (never the value)
    account: str = ""       # the account step's kind ("login" | "signup") and call, "login#2"
    name: str = ""          # the element's name, else its id
    user: str = ""          # the person's answer to a pause the value holds, else ""

    @property
    def host(self) -> str:
        return _host(self.url)


class Recorder:
    """Every Playwright action of one run with its live element (see the
    module docstring). `recording()` installs the patches and removes them."""

    def __init__(self, flow: Flow | None = None, *, park_mode: bool = False,
                 password: str = "", unconfirmed: tuple[str, ...] | None = None):
        self.flow = flow
        self.park_mode = park_mode
        self.password = password
        # the bank's answers the user has not confirmed (`unconfirmed_values`)
        self.unconfirmed = unconfirmed_values() if unconfirmed is None else tuple(unconfirmed)
        self.actions: list[Action] = []
        self.gate_depth = 0
        self.account_depth = 0
        self.final: dict[str, Any] = {}
        self.logs: list[str] = []
        self.files: list[Path] = []       # scanned for the password after the run
        self.judge_requests: list[str] = []   # every request the judge got, as JSON
        self.app_hosts: set[str] = set()  # where the master password may be typed
        self.ledger: Path | None = None   # the run's account ledger (no password key)
        self._keyboards: dict[int, Any] = {}
        self._account_calls = 0
        self._account_kind: list[str] = []    # the running account step's "login#n" / "signup#n"
        # each answer the person gave a pause -> (the field it
        # answers, the page the run paused on); and the values given for a
        # sensitive field, which code must never type
        self.user_values: dict[str, tuple[str, str]] = {}
        self.sensitive_values: set[str] = set()

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

    def _add(self, kind: str, how: str, info: dict, key: str = "", value: Any = None) -> None:
        # whether the value typed is the master password: a boolean, never
        # the value (the recorder keeps no typed value at all)
        secret = bool(self.password) and isinstance(value, str) and value == self.password
        # whether it holds an answer the user has not confirmed: the value
        # filled or picked, or the text of the option or box ticked
        shown = _value_text(value)
        if kind in ("click", "tick") and info.get("toggle"):
            shown = f"{shown} {info.get('text', '')}"
        unconfirmed = any(u in shown for u in self.unconfirmed)
        # the person's answer to a pause the value is, word for word
        user = next((u for u in list(self.user_values) + sorted(self.sensitive_values)
                     if u and u == shown.strip()), "") if kind in ("fill", "pick") else ""
        self.actions.append(Action(kind=kind, how=how, url=str(info.get("url", "")),
                                   text=str(info.get("text", "")), role=str(info.get("role", "")),
                                   tag=str(info.get("tag", "")), type=str(info.get("type", "")),
                                   in_gate=self.gate_depth > 0, key=key,
                                   form=bool(info.get("form", False)),
                                   aria=str(info.get("aria", "")),
                                   in_account=self.account_depth > 0,
                                   junk=str(info.get("junk", "")),
                                   toggle=bool(info.get("toggle", False)), secret=secret,
                                   unconfirmed=unconfirmed,
                                   account=self._account_kind[-1] if self._account_kind else "",
                                   name=str(info.get("name", "")), user=user))

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
                        value = (a[0] if a else kw.get("value", kw.get("text", kw.get("label")))) \
                            if _kind in ("fill", "pick") else None
                        rec._add(rec._kind(_kind, _name, a, kw), f"{_label}.{_name}",
                                 rec._live(target), key, value)
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
                        value = (a[0] if a else kw.get("value", kw.get("text", kw.get("label")))) \
                            if _kind in ("fill", "pick") else None
                        rec._add(rec._kind(_kind, _name, a, kw), f"{_label}.{_name}", info, key,
                                 value)
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
                    value = (a[0] if a else kw.get("text")) if _kind == "fill" else None
                    rec._add(_kind, f"Keyboard.{_name}", info, key, value)
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
                # `_fill(page, digest, host, email, signup)`: its kind and call
                signup = a[4] if len(a) > 4 else kw.get("signup", False)
                rec._account_calls += 1
                rec._account_kind.append(f"{'signup' if signup else 'login'}#{rec._account_calls}")
                rec.account_depth += 1
                try:
                    return account_fill(accounts, *a, **kw)
                finally:
                    rec.account_depth -= 1
                    rec._account_kind.pop()
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

_TEXT_SUFFIXES = (".md", ".json", ".jsonl", ".log", ".txt", ".html", ".csv", ".yaml", ".yml")


def _text_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        path = Path(path)
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix.lower() in _TEXT_SUFFIXES:
                    yield child
        elif path.is_file():
            yield path


_NOTHING_SENT = "nothing was sent"
# the runner's check-whether end: its reason's prefix, or the same words as
# a clause of their own after "; " (the parks `_send_evidence` gives the
# check-sent note: an error banner or page, a code not accepted); a page's words quoted in a reason are never a clause
_CHECK_SENT_CLAUSE = re.compile(r"(?:^|; )" + re.escape(apply_run.CHECK_SENT_REASON)
                                + r"(?=$|[:;])")


def invariant_breaks(outcome: Any, recorder: Recorder, sends: Sends, *,
                     tab_note: str = "") -> list[str]:
    """Every invariant the run broke, one line each; [] when it held them all.
    `tab_note`: the finished queue entry's note, when the caller read it."""
    breaks: list[str] = []
    status = str(getattr(outcome, "status", ""))
    reason = str(getattr(outcome, "reason", ""))
    # a send the site accepted: a post it refused, or a
    # request that never made its connection, sent nothing
    accepted = any(s.accepted for s in sends.events)
    if sends.count > 1:
        breaks.append(f"SEND-COUNT: {sends.count} sends, at most one is allowed")
    if recorder.park_mode and sends.count:
        breaks.append(f"PARK-SENT: park mode sent {sends.count} application(s)")
    for s in sends.events:
        if not s.in_gate:
            breaks.append(f"SEND-OUTSIDE-GATE: a {s.kind} send ({s.detail}) while the "
                          "submit gate was not running")
    # the runner's own prefix, never the word anywhere in
    # a reason that may quote the page
    if status == "submitted" and not recorder.final.get("confirmed"):
        if not (reason.startswith("submitted (unconfirmed)") and accepted):
            breaks.append(f"FALSE-SUBMITTED: submitted without the confirmation marker "
                          f"({reason})")
    if status == "submitted" and not accepted:
        breaks.append(f"SUBMITTED-WITHOUT-SEND: submitted ({reason[:80]!r}) and no send the "
                      "site accepted was seen: the job would never be tried again")
    # after a send the site accepted, a park the person
    # may re-queue sends a second application; only a check-whether park
    # (which is never re-queued as is) may follow one: its reason says so,
    # or its note is the check-sent note
    if accepted and status in ("needs_human", "failed") \
            and not _CHECK_SENT_CLAUSE.search(reason) and tab_note != apply_run.CHECK_SENT_NOTE:
        breaks.append(f"REQUEUABLE-AFTER-SEND: {status} ({reason[:80]!r}) after a send the site "
                      "accepted: a re-queue would send it twice")
    if status == "ready_to_submit" and sends.count:
        breaks.append(f"READY-AFTER-SEND: ready_to_submit ({reason}) after {sends.count} "
                      "send(s): a review would send it again")
    # the run says nothing went: the submit that did not go through, a send
    # that never made its connection, a post the navigation guard stopped
    nothing_sent = reason.startswith(apply_run.NOT_SENT_REASON) or _NOTHING_SENT in reason
    if nothing_sent and any(s.accepted for s in sends.events):
        breaks.append(f"NOT-SENT-AFTER-SEND: {reason[:80]!r} after a send the site accepted: "
                      "a retry would send it twice")
    for a in recorder.actions:
        if a.kind == "click" and not a.in_gate \
                and submit_worded(a.text, park_mode=recorder.park_mode,
                                  account_step=a.in_account, toggle=a.toggle):
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
        if a.kind in ("fill", "tick", "pick", "upload") and a.junk:
            breaks.append(f"JUNK-FILL: a {a.kind} into a box no person fills ({a.junk}) on "
                          f"{a.host}")
        if a.kind == "click" and _OTHER_SITE_SIGN_IN.search(f"{a.text} {a.aria}"):
            breaks.append(f"OTHER-SITE-CLICK: clicked {a.text or a.aria!r} on {a.host}: the run "
                          "never signs in or applies with another site's account")
        if a.secret and recorder.app_hosts and a.host not in recorder.app_hosts:
            breaks.append(f"PASSWORD-OFF-SITE: the master password typed on {a.host}")
        if a.secret and (a.type != "password" or a.how.startswith("Keyboard.")):
            breaks.append(f"PASSWORD-NOT-A-PASSWORD-BOX: the master password typed into a "
                          f"{a.tag or 'focused'} {a.type or 'element'} on {a.host}")
        if a.unconfirmed:
            breaks.append(f"UNCONFIRMED-ANSWER: a {a.kind} of an answer the user has not "
                          f"confirmed, into a {a.tag or 'focused'} {a.type or 'element'} on "
                          f"{a.host}")
        breaks += _pause_breaks(a, recorder)
    breaks += _password_tries(recorder.actions)
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
    if recorder.ledger is not None and recorder.ledger.is_file():
        try:
            ledger = json.loads(recorder.ledger.read_text(encoding="utf-8"))
        except ValueError:
            ledger = {}
        keys = [str(k) for rec in (ledger.values() if isinstance(ledger, dict) else [])
                if isinstance(rec, dict) for k in rec]
        bad = sorted({k for k in keys if _PASSWORD_KEY.search(k)})
        if bad:
            breaks.append(f"LEDGER-PASSWORD-KEY: the account ledger holds {bad[0]!r}")
    return breaks


# a sign-in or an apply with another site's account (`apply_judge.THIRD_PARTY`)
_OTHER_SITE_SIGN_IN = apply_run.apply_judge.THIRD_PARTY
_PASSWORD_KEY = re.compile(r"pass|pwd|secret|token|credential", re.I)
# The master password goes into one sign-in per site (a second is a
# rejected password: typing it again moves toward a lockout), a sign-up's
# boxes twice at most (the one re-type of a form the site emptied),
# and no more than this many boxes on one site in all
PASSWORD_TYPINGS_MAX = 6


def _pause_breaks(a: Action, recorder: Recorder) -> list[str]:
    """A person's answer to a pause goes only into its own
    field, on the page the run paused on; code never types into a sensitive
    field, and a value the answer gave for one is never typed anywhere."""
    out = []
    if a.user in recorder.sensitive_values:
        out.append(f"SENSITIVE-ANSWER-TYPED: the value given for a sensitive field went into "
                   f"a {a.tag or 'focused'} {a.type or 'element'} on {a.host}")
    elif a.user:
        field_id, page_url = recorder.user_values[a.user]
        if a.name != field_id or a.url != page_url:
            out.append(f"USER-ANSWER-ELSEWHERE: an answer for {field_id!r} went into "
                       f"{a.name or a.tag or 'an element'!r} on {a.url}; it belongs on "
                       f"{page_url}")
    if a.kind == "fill" and a.name and apply_run.apply_judge.is_sensitive_field(
            re.sub(r"[_\-\[\]]+", " ", a.name)):
        out.append(f"SENSITIVE-TYPED: code typed into the sensitive field {a.name!r} on "
                   f"{a.host}")
    return out


def _password_tries(actions: list[Action]) -> list[str]:
    """The password invariants over the typings: one sign-in per site, two
    sign-ups per site, `PASSWORD_TYPINGS_MAX` boxes per site."""
    out: list[str] = []
    calls: dict[tuple[str, str], set[str]] = {}
    boxes: dict[str, int] = {}
    for a in actions:
        if not a.secret:
            continue
        boxes[a.host] = boxes.get(a.host, 0) + 1
        if a.account:
            kind = a.account.split("#", 1)[0]
            calls.setdefault((a.host, kind), set()).add(a.account)
    for (host, kind), seen in sorted(calls.items()):
        allowed = 1 if kind == "login" else 2
        if len(seen) > allowed:
            out.append(f"PASSWORD-RETRY: the master password went into {len(seen)} {kind} "
                       f"screens on {host} (at most {allowed})")
    for host, n in sorted(boxes.items()):
        if n > PASSWORD_TYPINGS_MAX:
            out.append(f"PASSWORD-RETRY: the master password was typed {n} times on {host} (at "
                       f"most {PASSWORD_TYPINGS_MAX})")
    return out


def assert_invariants(outcome: Any, recorder: Recorder, sends: Sends, *,
                      tab_note: str = "") -> None:
    breaks = invariant_breaks(outcome, recorder, sends, tab_note=tab_note)
    assert not breaks, "invariant breaks:\n" + "\n".join(breaks)


# The parks the user's policy allows: the park-mode submit, a required
# question the data cannot answer, a sensitive question, a payment, a check
# nobody solved, a dead page, and the window or tab the user closed; on
# LinkedIn, an Easy Apply job (the user applies there, by the user's call), a
# job already applied to, a closed posting, and LinkedIn signed out (dead ends
# the run cannot pass).
_POLICY_PARKS = tuple(re.compile(p) for p in (
    r"^auto_apply_submit is off(; |$)", r"^required field without an answer",
    # an optional answer that failed its check and that the
    # run could not take out again (a radio group): the person removes it
    r"^a wrong answer could not be removed: ",
    r"^asks for .*which auto-apply never fills", r"^payment requested",
    # a bot check nobody solved: the loop's park and the run's own CAPTCHA
    # reasons, anchored (an unanchored word matched a reads list's
    # "captcha_or_bot_check 0.17" inside another park's evidence)
    r"^captcha or bot check on the page", r"^a CAPTCHA (?:challenge|check) ",
    # its one producer's shape (`_JobRun._click_advance`), anchored so a park
    # whose evidence quotes the sentence never reads as it
    r"^the \S+ button \(.*\) did nothing \(.*clicked twice\); a CAPTCHA checkbox",
    r"^error or dead page",
    "^" + re.escape(apply_run.CLOSED_REASON), "^" + re.escape(apply_run.TAB_CLOSED_REASON),
    "^" + re.escape(apply_run.EASY_APPLY_REASON) + "$",
    "^" + re.escape(apply_run.apply_linkedin.APPLIED_REASON),
    "^" + re.escape(apply_run.apply_linkedin.CLOSED_REASON),
    "^" + re.escape(apply_run.apply_linkedin.SIGNED_OUT_REASON),
    # the site's own dead ends: a job it says was applied to before, a
    # posting it says is closed
    "^" + re.escape(apply_run.ALREADY_APPLIED_REASON),
    "^" + re.escape(apply_run.CLOSED_POSTING_REASON),
    # a posting the run cannot apply to itself: a job board's with no link to
    # the company's site, an Apply that is an email address
    "^" + re.escape(apply_run.AGGREGATOR_REASON) + " on ",
    "^" + re.escape(apply_run.MAILTO_REASON) + " to ",
    # a real dead end: a way on still disabled once every
    # field is answered, and no field the form or the plan names as blank
    r"^the .{1,80} button stays disabled after the fill( \(|$)",
    # the account dead ends: a portal whose only way on is a sign-in with another
    # site's account, which the run never uses; a site whose
    # password rules the stored master password cannot meet
    "^" + re.escape(apply_run.SSO_REASON) + " ",
    "^" + re.escape(apply_run.PASSWORD_RULE_REASON) + " on ",
    # a queue entry the run cannot work, which the user fixes
    "^" + re.escape(apply_run.MALFORMED_REASON) + ": ",
    # a judge that stays down after the submit click or the code step: the
    # job is never re-queued once something may have been
    # sent, and the run cannot read on, a dead end the user checks. The
    # window or the tab the user closed there, and the link and the
    # final-worded steps, end the same way (`_stopped_after_send`); a pause's
    # wait that failed with the page still open ends as a close too
    # (`_pause_closed`)
    "^" + re.escape(apply_run.CHECK_SENT_REASON) + r": the run stopped after the "
    r"(?:submit click|code step|link step|final-worded step|pause) \((?:"
    + re.escape(apply_run.JUDGE_DOWN_REASON) + ": |" + re.escape(apply_run.CLOSED_REASON)
    + r"\)|" + re.escape(apply_run.TAB_CLOSED_REASON) + r"\)|"
    + re.escape(apply_run.PAUSE_UNANSWERED_REASON) + r"\))",
    # the page moved on while the run waited for the person,
    # who may have sent it in the browser; the user checks it
    "^" + re.escape(apply_run.CHECK_SENT_REASON) + r": the page moved on during the pause \(",
    # the judge down under the same job a second time, a
    # failure the job's own request may cause: parked so the queue moves on.
    # Only after the judge answered in the drain: a park while it
    # answered nothing (an outage for every job) is outside the policy. Only
    # for an error a request can cause: a 5xx other than 503 and
    # 529, a 408, or an error with no status (a timeout), never a dropped
    # connection, most often the network's
    "^" + re.escape(apply_run.JUDGE_DOWN_REASON) + r": (?!Connection|BrokenPipe)\S+"
    r"(?: (?:408|5(?!03|29)\d\d))? (?:at .+ )?after [1-9]\d* answers? in this drain; "
    + re.escape(apply_run.OUTAGES_PARKED) + "$"))


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
    replay_misses: int = 0          # the requests a replay judge's cache did not hold
    reads: list[dict] = field(default_factory=list)     # per traced page, `trace_reads`


def trace_reads(trace_dir: str | Path) -> list[dict]:
    """Per page of a run's trace (`page-<n>.json`), the combined read the
    run acted on (`state`, `conf`) and the judge's own pick
    (`judged`, `judged_conf`): what the page-read floors are tuned on."""
    rows = []
    if not trace_dir:
        return rows
    pages = sorted(Path(trace_dir).glob("page-*.json"),
                   key=lambda p: int(p.stem.split("-")[1]) if p.stem.split("-")[1].isdigit()
                   else 0)
    for p in pages:
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        answers = entry.get("answers") or {}
        read = answers.get("page_state") or {}
        judged = answers.get("page_state_judged") or read
        rows.append({"n": entry.get("n"), "state": entry.get("state"),
                     "conf": entry.get("confidence"), "read": read.get("choice"),
                     "read_conf": read.get("confidence"), "judged": judged.get("choice"),
                     "judged_conf": judged.get("confidence")})
    return rows


def judges(seeds: Iterable[int] = SUITE_SEEDS, *, fake: bool = True,
           real: Any = None) -> list[tuple[str, Any]]:
    """("fake", FakeJev()) and ("noisy-<seed>", NoisyJev(FakeJev(), seed)) per
    seed, then ("real", `real`) when given (a `RealJudge`'s judge)."""
    out: list[tuple[str, Any]] = [("fake", jev.FakeJev())] if fake else []
    out += [(f"noisy-{s}", jev_doubles.NoisyJev(jev.FakeJev(), s)) for s in seeds]
    if real is not None:
        out.append((REAL, real))
    return out


# --- the real judge's column -----------------------------------------------------------
#
# One run per flow under the live model through its own replay cache
# (`REAL_CACHE`, committed: the flows are synthetic pages). `record` asks the
# live model on a miss through `jev.SpendCap`, so the column stops at the
# recording's cap; `replay` never leaves the machine and a miss parks the
# run as failed, naming `JevUnavailable`; `dry` answers with the fake at
# each request's estimated size into a temp copy of the cache (the request
# count and the spend a recording would make, and the cap at work).

REAL = "real"
REAL_MODES = ("record", "replay", "dry")
REAL_CACHE = FIXTURES_DIR / "jev_cache" / "matrix_cache.json"


@dataclass
class RealJudge:
    mode: str
    judge: Any                      # the ReplayJev a run gets
    cache: Path                     # where the answers are read and written
    cap: Any = None                 # the jev.SpendCap in `record` and `dry`

    @property
    def capped(self) -> bool:
        return bool(self.cap is not None and self.cap.reached)


def replay_only(judge: Any) -> bool:
    """A replay judge that never asks anyone (`ReplayJev(None, ...)`)."""
    return isinstance(judge, jev.ReplayJev) and judge.inner is None


def real_judge(mode: str, cache: Path = REAL_CACHE, cap_usd: float | None = None,
               *, live: Callable[[], Any] | None = None) -> RealJudge:
    """The real judge's column's judge for `mode` (`REAL_MODES`). `live`
    makes the live judge in `record` (`jev.TypeSafeJev` when None); the cap
    is `cap_usd`, else `AUTO_APPLY_RECORD_USD_CAP` (`jev.record_cap`: a
    live recording without it is refused, a dry run takes
    `jev.DRY_RECORD_CAP_USD`)."""
    if mode not in REAL_MODES:
        raise ValueError(f"unknown real-judge mode {mode!r}; expected one of "
                         f"{', '.join(REAL_MODES)}")
    cache = Path(cache)
    if mode == "replay":
        return RealJudge(mode, jev.ReplayJev(None, cache), cache)
    cap_usd = jev.record_cap(live=mode == "record") if cap_usd is None else float(cap_usd)
    if mode == "dry":
        import jev_harness
        cache = jev_harness.dry_copy(cache)
        inner: Any = jev_doubles.DryRun()
    else:
        inner = live() if live is not None else jev.TypeSafeJev()
    cap = jev.SpendCap(inner, cap_usd)
    return RealJudge(mode, jev.ReplayJev(cap, cache), cache, cap)


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
    site. A route's own fetch (`Route.fetch`, which no route sees: the run
    fetches an emailed link's pages so) is held to the local hosts too."""
    from playwright.sync_api import Browser, BrowserType, Route
    p = Patches()
    new_context, new_page = Browser.new_context, Browser.new_page
    persistent = BrowserType.launch_persistent_context
    fetch = Route.fetch

    def _fetch(self, **kw):
        if _host(kw.get("url") or self.request.url) not in _LOCAL_HOSTS:
            raise RuntimeError("offline: a route's fetch off the local hosts")
        return fetch(self, **kw)

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
        p.setattr(Route, "fetch", _fetch)
        yield
    finally:
        p.undo()


# The flows' inbox has a host of its own (the application's is the fixture
# server's): the master password is never typed on the inbox's site, and a
# link in a message is the application's only by its host
INBOX_HOST = "mail.fixtures.test"


def inbox_url(page: str) -> str:
    return f"http://{INBOX_HOST}/inbox/{page}"


def _inbox_server(base: str) -> Callable[[Any], None]:
    """A route handler serving `tests/fixtures/inbox/<name>` on `INBOX_HOST`,
    each message's links to `../forms/` pointed at the application's host
    (`base`)."""
    from urllib.parse import urlsplit

    def _handle(route) -> None:
        name = urlsplit(route.request.url).path.split("/inbox/", 1)[-1]
        path = FIXTURES_DIR / "inbox" / name
        if not name or "/" in name or not path.is_file():
            route.fulfill(status=404, body="not found", content_type="text/plain")
            return
        body = path.read_text(encoding="utf-8").replace('href="../forms/', f'href="{base}/forms/')
        route.fulfill(body=body, content_type="text/html")
    return _handle


def _fulfiller(body: str) -> Callable[[Any], None]:
    """A route handler serving `body` (one parameter: Playwright passes the
    request too to a handler that takes two)."""
    def _handle(route) -> None:
        route.fulfill(body=body, content_type="text/html")
    return _handle


@contextmanager
def _after_each_read(js: str):
    """`apply_form.extract` followed by `js` in the page it read (`Flow.on_read`)."""
    import apply_form
    p = Patches()
    real = apply_form.extract

    def _extract(page, *a, **kw):
        digest = real(page, *a, **kw)
        try:
            page.evaluate(js)
        except Exception:       # noqa: BLE001  (a page mid-navigation)
            pass
        return digest
    try:
        p.setattr(apply_form, "extract", _extract)
        yield
    finally:
        p.undo()


def run_flow(f: Flow, judge: Any, judge_name: str, *, browser, server: FlowServer,
             workdir: Path, fast: bool = True, pause: PauseSpec | None = None) -> RunResult:
    """`f` once under `judge`: a fresh context, queue, ledger and job folder;
    the drain of that one job; the invariants. `pause`: how the person
    answers a flow with no `Flow.pause` of its own (pauses on and parked at
    once, every other flow must end as it did)."""
    import apply_queue

    rundir = Path(tempfile.mkdtemp(prefix=f"{f.name}-{judge_name}-", dir=str(workdir)))
    folder = write_job_folder(rundir / "job")
    queue = rundir / "queue.json"
    recorder = Recorder(f, park_mode=not f.submit, password=PASSWORD if f.password else "")
    recorder.app_hosts = f.app_hosts(server.base)
    sends = Sends(recorder)
    start = time.monotonic()
    with ExitStack() as stack:
        # the real column's cache keys carry the catalog's date: it reads the
        # day the cache was recorded, the fake and the noisy seeds the real one
        today = None
        if judge_name == REAL:
            import jev_harness
            today = jev_harness.RECORDED_TODAY
        stack.enter_context(hermetic(rundir, password=f.password, today=today))
        if fast:
            stack.enter_context(fast_timing(f.settle_s, f.timing))
        if f.on_read:
            stack.enter_context(_after_each_read(f.on_read))
        pause_minutes = stack.enter_context(pausing(rundir, f.pause or pause, recorder))
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
        context.route(f"http://{INBOX_HOST}/**", _inbox_server(server.base))
        for glob, body in f.routes(server.base).items():
            # a page's HTML, or a route handler of its own (a load the
            # network drops once)
            context.route(glob, body if callable(body) else _fulfiller(body))
        sends.install(context, f, server)
        stack.enter_context(recorder.recording())
        inbox = inbox_url(f.inbox_page) if f.inbox else "https://mail.example.com/inbox"
        replay, misses = judge, getattr(judge, "misses", None)
        if f.wrap is not None:
            judge = f.wrap(judge)
        runner = apply_run.Runner(
            jev=recorder.watch(judge), queue_path=queue, profile_dir=rundir / "profile",
            settings={"auto_apply_submit": f.submit, "auto_apply_headless": True,
                      "auto_apply_jev_mode": "fake", "auto_apply_batch_cap": 1,
                      "auto_apply_generate": True, "auto_apply_pause_minutes": pause_minutes},
            context=context, run_context={"signup_email": SIGNUP_EMAIL, "inbox_url": inbox},
            sleep=lambda s: None, drain_report=False)
        outcomes = runner.drain(cap=1)
    # the record, the trace, the queue and the ledger: none may hold the
    # master password, and the ledger no password-shaped key
    recorder.ledger = rundir / "accounts.json"
    recorder.files = [folder, queue, recorder.ledger]
    seconds = round(time.monotonic() - start, 2)
    missed = replay.misses - misses if isinstance(misses, int) else 0
    if not outcomes:
        return RunResult(f.name, judge_name, "", "no outcome", False,
                         ["NO-OUTCOME: the drain ran no job"], sends.count, 0, seconds,
                         replay_misses=missed)
    out = outcomes[0]
    entry = next((e for e in apply_queue.load(queue).get("jobs", [])
                  if str(e.get("job_posting_id")) == JOB_ID), {})
    breaks = invariant_breaks(out, recorder, sends, tab_note=str(entry.get("tab_note") or ""))
    if f.opens_no_page and opened:
        breaks.append(f"PAGE-OPENED: {len(opened)} page(s) opened for a job that must end "
                      "before any page")
    traces = sorted((folder / "apply_trace").glob("attempt-*"))
    return RunResult(f.name, judge_name, out.status, out.reason,
                     f.reached(out.status, out.reason, recorder.final, judge_name), breaks,
                     sends.count,
                     out.pages, seconds, str(traces[-1]) if traces else "",
                     policy_park(out.status, out.reason), list(recorder.actions),
                     len(recorder.judge_requests), missed,
                     trace_reads(traces[-1]) if traces else [])


def run_matrix(flows: Iterable[Flow], judge_list: list[tuple[str, Any]], *, browser,
               server: FlowServer, workdir: Path, fast: bool = True,
               progress: Callable[[RunResult], None] | None = None,
               pause: PauseSpec | None = None) -> list[RunResult]:
    """Each flow under each judge (`run_flow`); `pause` as `run_flow` takes it."""
    results = []
    extra = {"pause": pause} if pause is not None else {}
    for f in flows:
        for name, judge in judge_list:
            if name == REAL and not (f.replayable and f.recorded) and replay_only(judge):
                # its text changes with the clock, or no recording holds it
                # yet: no replay can hit
                continue
            r = run_flow(f, judge, name, browser=browser, server=server, workdir=workdir,
                         fast=fast, **extra)
            results.append(r)
            if progress is not None:
                progress(r)
    return results


@dataclass
class RealColumn:
    results: list[RunResult]
    unrecorded: list[str]           # flows the cap stopped mid-run or before they started
    stopped: str = ""               # the cap's reason, when it stopped a request


def run_real(flows: Iterable[Flow], rj: RealJudge, *, browser, server: FlowServer,
             workdir: Path, fast: bool = True,
             progress: Callable[[RunResult], None] | None = None) -> RealColumn:
    """The real judge's column: one run per flow, in turn (a recording
    writes one cache, so its runs never go in parallel). Once the cap stops
    a request, no new flow starts: the flow it stopped and every flow after
    it are listed as unrecorded and left out of the results (a stopped run's
    end is the cap's, never the judge's). The answers recorded before the
    stop stay in the cache."""
    results: list[RunResult] = []
    unrecorded: list[str] = []
    for f in flows:
        if rj.capped:
            unrecorded.append(f.name)
            continue
        r = run_flow(f, rj.judge, REAL, browser=browser, server=server, workdir=workdir,
                     fast=fast)
        if rj.capped:
            unrecorded.append(f.name)
            continue
        results.append(r)
        if progress is not None:
            progress(r)
    return RealColumn(results, unrecorded, rj.cap.reason if rj.capped else "")


def rates(results: list[RunResult]) -> dict[str, Any]:
    """Success rates under the fake and under the noisy judges over the
    flows that are not known failing (the floors read these), per flow, and
    the known flows' rates apart."""
    def rate(rows):
        return (sum(1 for r in rows if r.ok) / len(rows)) if rows else 1.0

    def noisy(r):
        return r.judge not in ("fake", REAL)

    def row(rows):
        out = {"fake": rate([r for r in rows if r.judge == "fake"]),
               "noisy": rate([r for r in rows if noisy(r)]), "runs": len(rows)}
        real = [r for r in rows if r.judge == REAL]
        if real:                    # the real judge's column, when it ran on the flow
            out["real"] = rate(real)
        return out
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
            "noisy": rate([r for r in counted if noisy(r)]),
            "real": rate([r for r in counted if r.judge == REAL]),
            "real_runs": sum(1 for r in counted if r.judge == REAL),
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
    # the columns the run has: the fake and noisy ones always (today's
    # layout), the real judge's when it ran; a real-only run shows it alone
    real = any(r.judge == REAL for r in results)
    cols = ("real",) if real and all(r.judge == REAL for r in results) else \
        ("fake", "noisy", "real") if real else ("fake", "noisy")
    lines.append("")
    def cell(row, c):
        return f"{row[c]:>6.0%} " if c in row else f"{'-':>6} "
    lines.append(f"{'flow':<22} " + "".join(f"{c:>6} " for c in cols) + "runs")
    for name, row in rt["per_flow"].items():
        lines.append(f"{name:<22} " + "".join(cell(row, c) for c in cols) + f"{row['runs']}")
    lines.append("")
    by_name = {f.name: f for f in FLOWS}
    for name, row in rt["known"].items():
        tag = by_name[name].known.split(":")[0]
        rated = ", ".join(f"{c} {row[c]:.0%}" if c in row else f"{c} -" for c in cols)
        lines.append(f"known failing, {tag}: {name} ({rated}; left out of the rates below)")
    parts = [f"{c} {rt[c]:.1%}" for c in cols if c != "real"]
    if real:
        parts.append(f"real {rt['real']:.1%} over {rt['real_runs']} flows")
    lines.append(f"success: {', '.join(parts)}, all {rt['all']:.1%} "
                 f"over {len(results)} runs; invariant breaks: {rt['breaks']}; parks outside "
                 f"the policy: {rt['outside_policy']} of {rt['parks']}; designed ends off the "
                 f"policy list: {rt['designed']}")
    return "\n".join(lines)
