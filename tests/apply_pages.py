"""The flow harness's base layer: the fixture paths and synthetic data (the
answer bank, the sheet, the master password), the hermetic patches and the
fast timing, the fixture server (`FixtureHTTPServer`, `FlowServer`) and the
routed pages a flow starts from.

Part of the auto-apply flow harness; `apply_harness` re-exports it. A test
that changes the master password patches `apply_pages.PASSWORD`.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "local") not in sys.path:
    sys.path.insert(0, str(REPO / "local"))


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
    ("apply_limits", "POPUP_TIMEOUT_MS"): 1_500,
    ("apply_limits", "POPUP_GRACE_S"): 0.3,
    ("apply_limits", "ENTRY_POLL_MS"): 50,
    ("apply_limits", "GOTO_RETRY_S"): 0.1,
    ("apply_limits", "CLICK_TIMEOUT_S"): 3,
    ("apply_limits", "SUBMIT_SETTLE_S"): 5,
    # a quiet step click that set a request going: no flow's step answers
    # later than 5 s (`test_a_slow_step_posts_once_and_is_waited_for` answers
    # at 5 s), so 8 s keeps every wait while a dead step parks in 8 s, not 20
    ("apply_limits", "STEP_SETTLE_S"): 8,
    ("apply_limits", "REDIRECT_TIMEOUT_S"): 6,
    # the empty-read and top-card waits keep room for the fixtures that render
    # late (0.8 s and 2.5 s after `load`)
    ("apply_limits", "EMPTY_READ_MAX_S"): 4.0,
    ("apply_limits", "EMPTY_READ_STABLE_S"): 1.2,
    ("apply_limits", "EMPTY_READ_POLL_S"): 0.1,
    ("apply_limits", "LINKEDIN_READY_S"): 5.0,
    ("apply_limits", "LINKEDIN_POLL_MS"): 100,
    ("apply_limits", "LINKEDIN_EASY_RECHECK_S"): 1.0,
    ("apply_limits", "CONSENT_WAIT_S"): 1.5,
    # the post-submit read: the slow_post flow's answer comes SLOW_POST_S after
    # its click, inside the click's own waits and this one
    ("apply_limits", "POST_SUBMIT_WAIT_S"): 10.0,
    ("apply_limits", "POST_SUBMIT_POLL_S"): 0.2,
    ("apply_limits", "POST_SUBMIT_QUIET_S"): 0.3,
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


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return (urlsplit(str(url or "")).hostname or "").lower()


def on_linkedin(url: str) -> bool:
    host = _host(url)
    return host == "linkedin.com" or host.endswith(".linkedin.com")
