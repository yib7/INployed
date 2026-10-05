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

The parts live in three modules under this one, each importing only the ones
below it: `apply_pages` (the fixture paths and synthetic data, the hermetic
patches, the fast timing, `FlowServer` and the routed pages), `apply_flows`
(the scripted judges, the pause answers, `Flow` and `FLOWS`) and
`apply_invariants` (`Sends`, `Recorder` and the invariants). This module runs
them (`run_flow`, `run_matrix`, the real judge's column, `rates`, `summary`)
and re-exports their names for reading. A test that patches a name patches
the module that defines it (`apply_flows.FLOWS`, `apply_pages.PASSWORD`):
this module reads those two through their modules, so the patch reaches it.
"""
from __future__ import annotations

import json
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import apply_flows
import apply_pages
import apply_run
import jev
import jev_doubles
from apply_pages import (bank, board_chain_routes, board_routes, _COMBINED, CONFIRMATION_HTML,  # noqa: F401
                         FAKE_SUCCESS_FLOOR, fast_timing, FixtureHTTPServer, FIXTURES_DIR,
                         FlowServer, hermetic, _host, JOB_ID, linkedin_job_routes,
                         _linkedin_routes, NOISY_SEEDS, on_linkedin, PASSWORD, Patches,
                         SIGNUP_EMAIL, SUCCESS_FLOOR, SUITE_SEEDS, unconfirmed_values,
                         write_job_folder)
from apply_flows import (flow, Flow, FLOWS, OptionalLeftBlank, _PARKED, PauseResponder, PauseSpec,  # noqa: F401
                         pausing, read_as, REAL, VerifiedReadAsConfirmation)
from apply_invariants import (Action, assert_invariants, FINAL_WORDS, invariant_breaks,  # noqa: F401
                              policy_park, Recorder, Send, Sends, submit_worded, SUBMIT_WORDS)


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
    recorder = Recorder(f, park_mode=not f.submit, password=apply_pages.PASSWORD if f.password else "")
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
    known = {f.name for f in apply_flows.FLOWS if f.known}
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
    by_name = {f.name: f for f in apply_flows.FLOWS}
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
