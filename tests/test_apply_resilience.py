"""SP8a: the auto-apply run's resilience (the offline half of SP8).

- Chrome's own error page (`chrome-error://chromewebdata/`, a load the
  network dropped) reads as a failed load: one retry of a GET on the allowed
  sites, never "left the allowed sites: chromewebdata". After the submit
  click only the page the send led to is loaded again: the GET that carried
  the send and any POST never are (at most one send per job).

Headless Chromium through the module-scoped test browser, the flow harness
(`apply_harness.run_flow`) and the fake judge; no network but the local
server and routed hosts."""
import dataclasses
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_harness as h  # noqa: E402
import jev  # noqa: E402

pytest_plugins = ["conftest_browser"]


# --- Chrome's error page ------------------------------------------------------------------

def _drop_first(error: str = "connectionrefused", *, body: str = ""):
    """A route handler that drops the first load of its address on the
    network (`error`, as a refused connection would) and lets every later one
    through, or answers it with `body`; `.seen` lists the methods it saw."""
    seen: list[str] = []

    def _handle(route, request) -> None:
        seen.append(str(request.method))
        if len(seen) == 1:
            route.abort(error)
        elif body:
            route.fulfill(body=body, content_type="text/html")
        else:
            route.fallback()
    _handle.seen = seen
    return _handle


def _trace_text(result) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in Path(result.trace).glob("*") if p.suffix in (".json", ".log"))


def _run(f, browser, server, tmp_path):
    return h.run_flow(f, jev.FakeJev(), "fake", browser=browser, server=server,
                      workdir=tmp_path)


def test_a_destination_the_network_drops_once_is_loaded_again_and_the_run_goes_on(
        _browser, flow_server, tmp_path):
    # the matrix flake (linkedin_gts_other): LinkedIn's Apply led to the
    # company's form, whose load the network dropped
    base = h.flow("linkedin_gts_other")
    drop = _drop_first()
    f = dataclasses.replace(base, routes=lambda b: {**base.routes(b),
                                                    f"{b}/forms/lever_single.html": drop})
    r = _run(f, _browser, flow_server, tmp_path)
    assert "chromewebdata" not in r.reason, r
    assert r.ok, (r.status, r.reason)
    assert r.breaks == []
    assert drop.seen == ["GET", "GET"]
    assert "error_page_retry" in _trace_text(r)


def test_a_redirect_hop_the_network_drops_once_is_loaded_again_and_its_destination_admitted(
        _browser, flow_server, tmp_path):
    # LinkedIn's /safety/go/ hop itself dropped: its retry redirects on to the
    # company's form, which is then admitted as the application's site
    base = h.flow("linkedin_gts_other")
    hop = "https://www.linkedin.com/safety/go/**"
    drop = _drop_first(body=base.routes(flow_server.base)[hop])
    f = dataclasses.replace(base, routes=lambda b: {**base.routes(b), hop: drop})
    r = _run(f, _browser, flow_server, tmp_path)
    assert "chromewebdata" not in r.reason, r
    assert r.ok, (r.status, r.reason)
    assert r.breaks == []
    assert drop.seen == ["GET", "GET"]


def test_a_load_the_network_drops_twice_parks_as_an_error_page_inside_the_policy(
        _browser, flow_server, tmp_path):
    base = h.flow("linkedin_gts_other")
    seen: list[str] = []

    def _always(route, request) -> None:
        seen.append(str(request.method))
        route.abort("connectionreset")
    f = dataclasses.replace(base, routes=lambda b: {**base.routes(b),
                                                    f"{b}/forms/lever_single.html": _always})
    r = _run(f, _browser, flow_server, tmp_path)
    assert r.status == "needs_human", r
    assert r.reason.startswith("error or dead page: GET "), r.reason
    assert "again after one retry" in r.reason
    assert "chromewebdata" not in r.reason
    assert h.policy_park(r.status, r.reason) is True
    assert seen == ["GET", "GET"]
    assert r.breaks == []


def test_after_the_send_the_get_that_carried_it_is_never_loaded_again(
        _browser, flow_server, tmp_path):
    # the submit_code noisy-14 end: the submit's own GET (the harness counts
    # it as the send) was dropped; loading it again would send twice
    base = h.flow("submit_code")
    drop = _drop_first()
    f = dataclasses.replace(base, routes=lambda b: {"**/forms/code_gate.html": drop})
    r = _run(f, _browser, flow_server, tmp_path)
    assert r.status == "submitted", r
    assert r.reason.startswith("submitted (unconfirmed): error or dead page: GET "), r.reason
    assert "carried the send" in r.reason
    assert "chromewebdata" not in r.reason
    assert drop.seen == ["GET"]
    assert r.sends == 1
    assert r.breaks == []


_POST_SITE = "https://careers.fabrikam.example"
_POST_FORM = """<!doctype html><html><head><title>Apply for Analytics Engineer</title></head>
<body><h1>Apply for Analytics Engineer</h1><p>Fabrikam, Remote</p>
<form method="post" action="/post/submit">
<label>First name * <input name="first_name" required></label>
<label>Last name * <input name="last_name" required></label>
<label>Email * <input type="email" name="email" required></label>
<button type="submit" id="btn-submit">Submit application</button>
</form></body></html>"""


# the POST's answer sends the tab on to the thanks page (a route's own 303
# would skip the routes, so the page's script does it)
_SENT = ("<!doctype html><html><head><title>Sending</title></head><body><p>Sending</p>"
         f"<script>location.replace('{_POST_SITE}/post/thanks')</script></body></html>")


def _answer_the_post(route) -> None:
    route.fulfill(body=_SENT, content_type="text/html")


def _post_flow(submit, thanks) -> h.Flow:
    return h.Flow("post_redirect", f"{_POST_SITE}/post/apply", True, "submitted",
                  r"^confirmation page", confirm="body[data-confirmed]",
                  send_urls=("**/post/submit",),
                  routes=lambda b: {f"{_POST_SITE}/post/apply": _POST_FORM,
                                    f"{_POST_SITE}/post/submit": submit,
                                    f"{_POST_SITE}/post/thanks": thanks})


def test_after_the_send_the_page_it_led_to_is_loaded_again_and_reads_as_the_confirmation(
        _browser, flow_server, tmp_path):
    # the POST went through; the GET its answer led to was dropped
    thanks = _drop_first(body=h.CONFIRMATION_HTML)
    posts: list[str] = []

    def _submit(route, request) -> None:
        posts.append(str(request.method))
        _answer_the_post(route)
    r = _run(_post_flow(_submit, thanks), _browser, flow_server, tmp_path)
    assert r.status == "submitted", r
    assert r.ok, (r.status, r.reason)
    assert posts == ["POST"]
    assert thanks.seen == ["GET", "GET"]
    assert r.sends == 1
    assert r.breaks == []


def test_after_the_send_a_post_the_network_dropped_is_never_sent_again(
        _browser, flow_server, tmp_path):
    posts: list[str] = []

    def _submit(route, request) -> None:
        posts.append(str(request.method))
        route.abort("connectionreset")
    r = _run(_post_flow(_submit, h.CONFIRMATION_HTML), _browser, flow_server, tmp_path)
    assert r.status == "submitted", r
    assert r.reason.startswith("submitted (unconfirmed): error or dead page: POST "), r.reason
    assert "never sent again" in r.reason
    assert posts == ["POST"]
    assert r.sends == 1
    assert r.breaks == []


# --- a malformed queue entry (RES-09) -------------------------------------------------------

def _queue(tmp_path, *entries):
    """A queue file holding `entries` as written, hand-edited ones included."""
    import json

    import apply_queue
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"version": 1, "jobs": list(entries)}), encoding="utf-8")
    return path, apply_queue


def _plain(jid: str, folder: Path, **extra) -> dict:
    import apply_queue
    e = apply_queue.new_entry(jid, company="Fabrikam", title="Analytics Engineer",
                              apply_url="https://careers.fabrikam.example/apply")
    e["artifacts"]["folder"] = str(folder)
    e["artifacts"]["apply_md"] = str(folder / "apply.md")
    e.update(extra)
    return e


def _runner_for(queue, ctx, tmp_path):
    import apply_run
    return apply_run.Runner(jev=jev.FakeJev(), queue_path=queue, profile_dir=tmp_path / "p",
                            settings={"auto_apply_headless": True, "auto_apply_jev_mode": "fake"},
                            context=ctx, run_context={"inbox_url": ""}, sleep=lambda s: None)


def test_a_malformed_entry_ends_that_job_failed_and_the_drain_goes_on(_browser, tmp_path,
                                                                        monkeypatch):
    import apply_run
    monkeypatch.chdir(tmp_path)
    bad = _plain("bad", tmp_path / "bad")
    bad["artifacts"]["apply_md"] = {"path": "x"}         # hand-edited into a mapping
    good = _plain("good", tmp_path / "good")               # no apply.md: fails fast
    queue, apply_queue = _queue(tmp_path, bad, good)
    ctx = _browser.new_context()
    try:
        outcomes = _runner_for(queue, ctx, tmp_path).drain(cap=5)
    finally:
        ctx.close()
    assert [o.job_id for o in outcomes] == ["bad", "good"]
    first = outcomes[0]
    assert first.status == "failed"
    assert first.reason == (f"{apply_run.MALFORMED_REASON}: artifacts.apply_md is a dict, "
                            f"not text")
    assert outcomes[1].reason == "no apply.md"
    jobs = {e["job_posting_id"]: e for e in apply_queue.load(queue)["jobs"]}
    assert jobs["bad"]["status"] == "failed" and jobs["good"]["status"] == "failed"
    # nothing written where the malformed path would point (the working dir)
    assert not (tmp_path / "apply_trace").exists()
    assert not list(tmp_path.glob("application_record*"))


def test_a_malformed_attempts_count_is_claimed_as_a_first_attempt(tmp_path):
    bad = _plain("bad", tmp_path / "bad", attempts="twice")
    queue, apply_queue = _queue(tmp_path, bad)
    got = apply_queue.claim("apply_run", path=queue)
    assert got["job_posting_id"] == "bad"
    assert got["status"] == "in_progress" and got["attempts"] == 1


def test_an_entry_the_run_cannot_set_up_ends_failed_and_the_drain_goes_on(_browser, tmp_path,
                                                                          monkeypatch):
    import apply_run
    queue, apply_queue = _queue(tmp_path, _plain("a", tmp_path / "a"),
                                _plain("b", tmp_path / "b"))
    real = apply_run._JobRun.__init__

    def _init(self, runner, ctx, entry):
        if entry["job_posting_id"] == "a":
            raise RuntimeError("the set-up broke at /secret/path")
        real(self, runner, ctx, entry)
    monkeypatch.setattr(apply_run._JobRun, "__init__", _init)
    ctx = _browser.new_context()
    try:
        outcomes = _runner_for(queue, ctx, tmp_path).drain(cap=5)
    finally:
        ctx.close()
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "failed"), ("b", "failed")]
    assert outcomes[0].reason == "RuntimeError while the job was set up"
    jobs = {e["job_posting_id"]: e for e in apply_queue.load(queue)["jobs"]}
    assert jobs["a"]["status"] == "failed"
    assert "secret" not in jobs["a"]["notes"]


def test_a_malformed_entry_is_a_dead_end_inside_the_policy():
    import apply_run
    assert h.policy_park("failed", f"{apply_run.MALFORMED_REASON}: ats is a list, "
                                   f"not a mapping") is True


# --- an unexpected error's reason (RES-05) -----------------------------------------------------

_CALL_LOG = ("Locator.fill: Timeout 5000ms exceeded.\nCall log:\n  - waiting for "
             "locator(\"#first_name\")\n  - fill(\"Jane Doe\") on <input id=first_name>")


def test_an_unexpected_error_names_its_type_and_step_and_never_its_message(
        _browser, flow_server, tmp_path, monkeypatch):
    import apply_fill

    def _broken(*a, **kw):
        raise RuntimeError(_CALL_LOG)
    monkeypatch.setattr(apply_fill, "apply", _broken)
    r = _run(h.flow("lever_single_park"), _browser, flow_server, tmp_path)
    assert r.status == "failed", r
    assert r.reason == "RuntimeError at fill_and_verify (page 1)", r.reason
    run = (Path(r.trace) / "run.json").read_text(encoding="utf-8")
    pages = "".join(p.read_text(encoding="utf-8") for p in Path(r.trace).glob("page-*.json"))
    for text in (run, pages, r.reason):
        assert "Jane Doe" not in text and "Call log" not in text
    # the step and the code's frames in the trace and the job's log, the message nowhere
    assert '"step": "fill_and_verify"' in pages + run
    assert "apply_run.py:" in pages + run
    log = (Path(r.trace) / "job.log").read_text(encoding="utf-8")
    assert "RuntimeError at fill_and_verify" in log and "apply_run.py:" in log
    assert "Jane Doe" not in log and "Call log" not in log


# --- the judge request's size (RES-03) ----------------------------------------------------------

_COUNTRIES = [f"Country number {i} of the long list" for i in range(40)]


def _long_form(n_fields: int = 80, n_buttons: int = 30):
    """A long application: `n_fields` fields with help text, a third of them
    lists of 40 options, and `n_buttons` buttons, a third in the site's header."""
    import apply_form
    fields = []
    for n in range(n_fields):
        options = list(_COUNTRIES) if n % 3 == 0 else []
        fields.append(apply_form.Field(
            n, (0, f"#q{n}"), f"Question number {n} about your background and experience",
            "select" if options else "text", n % 2 == 0,
            help=("Tell us in your own words what you did, where, for how long and with whom; "
                  "a few lines are enough. ") * 2,
            options=options, id_or_name=f"question_{n}_answer", section=f"Part {n // 10}"))
    buttons = [apply_form.Button(n_fields + i, (0, f"#b{i}"), f"Header link {i}", chrome=i % 3 == 0)
               for i in range(n_buttons)]
    return apply_form.FormDigest("careers.fabrikam.example", "Apply for Analytics Engineer",
                                 "Apply for Analytics Engineer. " * 200, fields=fields,
                                 buttons=buttons)


def _catalog(tmp_path):
    import apply_facts
    return apply_facts.build(h.write_job_folder(tmp_path / "job"), answers=h.bank())


def test_an_80_field_form_is_asked_in_requests_that_fit_the_judges_limits(tmp_path):
    import apply_judge
    digest, catalog = _long_form(), _catalog(tmp_path)
    whole = apply_judge.page_questions(digest, catalog, {})
    assert not jev.request_fits(*whole)     # asked as one, it would not fit
    requests = apply_judge.page_requests(digest, catalog, {})
    for state, questions in requests:
        longest, total = jev.request_size(state, questions)
        assert longest <= jev.STATE_TOKENS_MAX * jev.SIZE_MARGIN, (longest, total)
        assert total <= jev.REQUEST_TOKENS_MAX * jev.SIZE_MARGIN, (longest, total)
    asked = [q for _, qs in requests for q in qs]
    # every field asked once, in the part whose `fields` holds it
    assert sorted(int(q.split("_")[1]) for q in asked if q.endswith("_source")) == list(range(80))
    for state, questions in requests:
        held = {f["n"] for f in state["fields"]}
        assert {int(q.split("_")[1]) for q in questions if q.endswith("_source")} == held
    # the page's own buttons asked once; the header's left out
    roles = sorted(int(q.split("_")[1]) for q in asked if q.endswith("_role"))
    assert roles == [b.n for b in digest.buttons if not b.chrome]


def test_a_form_that_fits_is_asked_as_it_was(tmp_path):
    import apply_judge
    digest, catalog = _long_form(12, 6), _catalog(tmp_path)
    assert apply_judge.page_requests(digest, catalog, {}) == [
        apply_judge.page_questions(digest, catalog, {})]


def test_the_parts_answers_read_as_one_and_a_yes_to_the_prohibited_question_holds():
    import apply_judge
    no = jev.Answer(kind="noul", noul=0.1)
    yes = jev.Answer(kind="noul", noul=0.8)
    pick = jev.Answer(kind="choice", choice="email", probabilities={"email": 0.9},
                      confidence=0.9)
    got = apply_judge.merge_answers([{"asks_for_prohibited": no, "field_1_source": pick},
                                     {"asks_for_prohibited": yes},
                                     {"asks_for_prohibited": no}])
    assert got == {"asks_for_prohibited": yes, "field_1_source": pick}


def test_the_run_maps_a_long_form_in_parts_and_plans_every_field(tmp_path):
    from unittest.mock import Mock

    import apply_run

    sizes: list[tuple[int, int]] = []

    class Sized(jev.FakeJev):
        def judge(self, state, questions):
            sizes.append(jev.request_size(state, questions))
            return super().judge(state, questions)
    runner = apply_run.Runner(jev=Sized(), context=Mock(), run_context={}, sleep=lambda s: None)
    run = apply_run._JobRun(runner, Mock(), {"job_posting_id": "s",
                                             "apply_url": "https://x.example/1"})
    run.catalog = _catalog(tmp_path)
    run.page = Mock(url="https://careers.fabrikam.example/apply")
    answers = run._map(_long_form(), {}, "application_form", discover=False, own_page=False)
    assert len(sizes) > 1
    for longest, total in sizes:
        assert longest <= jev.STATE_TOKENS_MAX * jev.SIZE_MARGIN
        assert total <= jev.REQUEST_TOKENS_MAX * jev.SIZE_MARGIN
    assert all(f"field_{n}_source" in answers for n in range(80))
    assert "asks_for_prohibited" in answers


def test_the_module_docstring_names_the_wall_clock_by_its_constant():
    # RES-08: the docstring once said eight minutes while the constant said 15
    import re

    import apply_run
    doc = apply_run.__doc__ or ""
    assert "JOB_WALL_CLOCK_S" in doc
    assert not re.search(r"\b(?:eight|8)[- ]minute", doc, re.I)
    assert apply_run.JOB_WALL_CLOCK_S == 15 * 60


# --- the judge's outage (RES-02) ---------------------------------------------------------------

class _Busy(Exception):
    """A service error the way the TypeSafe SDK raises one: `status`, and
    `retry_after_ms` on a 429; its message quotes the request."""

    def __init__(self, status: int, retry_after_ms: float | None = None):
        super().__init__(f"{status}: the request for Jane Doe was not answered")
        self.status = status
        self.retry_after_ms = retry_after_ms


class _Flaky:
    """Raises `errors` in turn, then answers as `inner` (the fake judge)."""

    def __init__(self, errors, inner=None):
        self.errors = list(errors)
        self.inner = inner if inner is not None else jev.FakeJev()
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.inner.judge(state, questions)


class _Answers:
    def judge(self, state, questions):
        return {q: jev.Answer(kind="noul", noul=0.9) for q in questions}


def test_a_busy_judge_is_tried_again_after_its_retry_after_and_answers():
    sleeps: list[float] = []
    inner = _Flaky([_Busy(429, retry_after_ms=20_000), _Busy(503), ConnectionError("reset")],
                   inner=_Answers())
    guarded = jev.Guarded(inner, sleep=sleeps.append)
    got = guarded.judge({"page": "x"}, {"q": {"type": "noul", "instructions": "x"}})
    assert got["q"].noul == 0.9
    # the service's 20 s over the first 5 s delay, then the run's own 15 s and 40 s
    assert sleeps == [20.0, 15.0, 40.0] and inner.calls == 4
    assert guarded.down == ""


def test_a_judge_that_stays_down_opens_the_breaker_and_later_requests_fail_at_once():
    sleeps: list[float] = []
    inner = _Flaky([_Busy(529)] * 9)
    guarded = jev.Guarded(inner, sleep=sleeps.append)
    with pytest.raises(jev.JudgeOutage) as got:
        guarded.judge({}, {"q": {"type": "noul", "instructions": "x"}})
    assert got.value.kind == "_Busy 529" and "Jane" not in str(got.value)
    assert sleeps == list(jev.RETRY_DELAYS_S) and inner.calls == 4
    assert guarded.down == "_Busy 529"
    with pytest.raises(jev.JudgeOutage):
        guarded.judge({}, {"q": {"type": "noul", "instructions": "x"}})
    assert inner.calls == 4 and len(sleeps) == 3      # no request while it is open


def test_a_refused_key_or_a_long_retry_after_opens_the_breaker_without_a_wait():
    for error in (_Busy(401), _Busy(403), _Busy(429, retry_after_ms=600_000)):
        sleeps: list[float] = []
        guarded = jev.Guarded(_Flaky([error]), sleep=sleeps.append)
        with pytest.raises(jev.JudgeOutage):
            guarded.judge({}, {})
        assert sleeps == [] and guarded.down == f"_Busy {error.status}"


def test_a_request_the_service_rejects_passes_through_without_a_retry():
    sleeps: list[float] = []
    guarded = jev.Guarded(_Flaky([_Busy(400), ValueError("a bug")]), sleep=sleeps.append)
    for kind in (_Busy, ValueError):
        with pytest.raises(kind):
            guarded.judge({}, {})
    assert sleeps == [] and guarded.down == ""


def test_a_runner_holds_its_judge_behind_the_guard_whoever_sets_it():
    import apply_run
    judge = jev.FakeJev()
    runner = apply_run.Runner(jev=judge, context=object(), run_context={})
    assert isinstance(runner.jev, jev.Guarded) and runner.jev.inner is judge
    other = _Flaky([])
    runner.jev = other
    assert runner.jev.inner is other and runner.jev.calls == 0     # its attributes read through


def test_a_judge_busy_for_a_while_lets_the_run_go_on(_browser, flow_server, tmp_path):
    flaky = _Flaky([_Busy(529), _Busy(429, retry_after_ms=1_000)])
    r = h.run_flow(h.flow("lever_single_park"), flaky, "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and r.breaks == [], (r.status, r.reason)
    assert flaky.calls > 2


def _two_jobs(judge, browser, server, tmp_path, sleeps):
    """A drain of two queued jobs on the fixture form under `judge`: the
    outcomes, the queue's entries by id, and the tabs left open."""
    import apply_queue
    import apply_run
    queue = tmp_path / "queue.json"
    url = f"{server.base}/forms/lever_single.html"
    with h.hermetic(tmp_path), h.fast_timing():
        for jid in ("a", "b"):
            folder = h.write_job_folder(tmp_path / jid)
            apply_queue.enqueue(apply_queue.new_entry(jid, company="Fabrikam",
                                                      title="Analytics Engineer", apply_url=url),
                                path=queue)
            apply_queue.set_artifacts(jid, {"folder": str(folder),
                                            "apply_md": str(folder / "apply.md"),
                                            "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")},
                                      path=queue)
        ctx = browser.new_context()
        h.offline(ctx)
        try:
            runner = apply_run.Runner(
                jev=judge, queue_path=queue, profile_dir=tmp_path / "p",
                settings={"auto_apply_headless": True, "auto_apply_jev_mode": "fake",
                          "auto_apply_submit": False},
                context=ctx, run_context={"inbox_url": ""}, sleep=sleeps.append)
            outcomes = runner.drain(cap=5)
            left = [p for p in ctx.pages if not p.is_closed()]
        finally:
            ctx.close()
    return outcomes, {e["job_posting_id"]: e for e in apply_queue.load(queue)["jobs"]}, left


def test_a_judge_that_stays_down_hands_the_job_back_and_stops_the_drain(
        _browser, flow_server, tmp_path):
    import json

    import apply_run
    sleeps: list[float] = []
    outcomes, jobs, left = _two_jobs(_Flaky([_Busy(529)] * 20), _browser, flow_server,
                                     tmp_path, sleeps)
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "queued")], outcomes
    reason = outcomes[0].reason
    assert reason.startswith(f"{apply_run.JUDGE_DOWN_REASON}: _Busy 529 at "), reason
    assert reason.endswith(apply_run.REQUEUED_NOTE) and "Jane" not in reason
    assert outcomes[0].judge_down
    assert sleeps == list(jev.RETRY_DELAYS_S)
    # the job is back in the queue as if never claimed, the next one untouched
    a, b = jobs["a"], jobs["b"]
    assert (a["status"], a["attempts"], a["claimed_by"], a["started_at"]) == ("queued", 0, "", "")
    assert a["notes"] == reason
    assert (b["status"], b["attempts"]) == ("queued", 0)
    # no park: no tab left open, no record; the trace ends with the reason
    assert left == []
    assert not list((tmp_path / "a").glob("application_record*"))
    run = json.loads((tmp_path / "a" / "apply_trace" / "attempt-1" / "run.json")
                     .read_text(encoding="utf-8"))
    assert (run["status"], run["reason"]) == ("queued", reason)
    assert "re-queued 1" in apply_run.summary_line(outcomes)


def test_an_outage_at_the_grounding_gate_reaches_the_run():
    import types

    import apply_answergen

    class Down:
        def judge(self, state, questions):
            raise jev.JudgeOutage("_Busy 529")
    field = types.SimpleNamespace(label="Why this role?", help="", placeholder="")
    catalog = types.SimpleNamespace(sheet_excerpt=lambda: "Jane built ingestion pipelines.")
    with pytest.raises(jev.JudgeOutage):
        apply_answergen.attempt(field, catalog, Down(), budget=1,
                                llm_call=lambda *a, **k: "I built ingestion pipelines.")


def test_an_outage_while_the_inbox_is_read_reaches_the_run(browser_page, fixtures_server):
    import apply_inbox

    class Down:
        def judge(self, state, questions):
            raise jev.JudgeOutage("_Busy 529")
    with pytest.raises(jev.JudgeOutage):
        apply_inbox.fetch_code(browser_page, "127.0.0.1",
                               fixtures_server + "/inbox/outlook_list.html", jev=Down(), polls=2,
                               sleep=lambda s: None)
    assert len(browser_page.context.pages) == 1     # its tab closed all the same


# --- a tab the site closes (RES-06), one tab per job (RES-07) ---------------------------------

_CAREERS = "https://careers.fabrikam.example"
_FORMS = REPO / "tests" / "fixtures" / "forms"
_LINKEDIN_JOB = "https://www.linkedin.com/jobs/view/4438751519/"


def _posting(to: str) -> str:
    """The fixture posting, its Apply opening `to` in a new tab."""
    return (_FORMS / "job_posting.html").read_text(encoding="utf-8").replace(
        'href="ashby_steps.html"', f'href="{to}"')


# A popup's first step that hands the flow back to the tab that opened it
# and closes itself (a `window.close()` after a step, as after an OAuth)
_HANDBACK_STEP = f"""<!doctype html><html><head><title>Apply</title></head><body>
<h1>Apply for Analytics Engineer</h1>
<label>First name * <input name="first" required></label>
<button type="button" onclick="window.opener.location.href = '{_CAREERS}/apply/next';
  window.close()">Continue</button></body></html>"""
_THANKS = """<!doctype html><html><head><title>Application received</title></head><body>
<h1>Thank you for applying</h1><p>Your application has been received.</p></body></html>"""


def _handback_form(posts: list) -> str:
    """The Lever fixture's form, whose Submit posts, sends the tab that
    opened it to /apply/next and closes itself; `posts` counts its sends."""
    form = (_FORMS / "lever_single.html").read_text(encoding="utf-8")
    script = form[form.index("<script>"):form.index("</script>") + len("</script>")]
    return form.replace(script, (
        "<script>document.getElementById('btn-submit').addEventListener('click', function () {"
        " fetch('/apply/post', {method: 'POST', body: 'a'}).then(function () {"
        f" window.opener.location.href = '{_CAREERS}/apply/next'; window.close(); }});"
        " });</script>"))


def _counter(posts: list):
    def _handle(route) -> None:
        posts.append(route.request.method)
        route.fulfill(body="ok", content_type="text/plain")
    return _handle


def _drain_jobs(browser, tmp_path, jobs, routes, *, submit=False):
    """A drain of `jobs` ((id, url) pairs) under the fake judge on an offline
    context with `routes` (a glob -> HTML or a handler, the later winning):
    the outcomes, and the URLs of the tabs left open."""
    import apply_queue
    import apply_run
    queue = tmp_path / "queue.json"
    with h.hermetic(tmp_path), h.fast_timing():
        for jid, url in jobs:
            folder = h.write_job_folder(tmp_path / jid)
            apply_queue.enqueue(apply_queue.new_entry(jid, company="Fabrikam",
                                                      title="Analytics Engineer", apply_url=url),
                                path=queue)
            apply_queue.set_artifacts(jid, {"folder": str(folder),
                                            "apply_md": str(folder / "apply.md"),
                                            "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")},
                                      path=queue)
        ctx = browser.new_context()
        h.offline(ctx)
        for glob, body in routes.items():
            ctx.route(glob, body if callable(body) else h._fulfiller(body))
        try:
            runner = apply_run.Runner(
                jev=jev.FakeJev(), queue_path=queue, profile_dir=tmp_path / "p",
                settings={"auto_apply_headless": True, "auto_apply_jev_mode": "fake",
                          "auto_apply_submit": submit, "auto_apply_generate": True},
                context=ctx, run_context={"signup_email": h.SIGNUP_EMAIL, "inbox_url": ""},
                sleep=lambda s: None)
            outcomes = runner.drain(cap=5)
            left = [str(p.url) for p in ctx.pages if not p.is_closed()]
        finally:
            ctx.close()
    return outcomes, left


def _trace_of(tmp_path, jid: str) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in (tmp_path / jid / "apply_trace").rglob("*.json"))


def test_a_popup_that_hands_the_flow_back_and_closes_goes_on_in_the_tab_that_opened_it(
        _browser, tmp_path):
    outcomes, left = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/jobs/a")], {
        f"{_CAREERS}/jobs/a": _posting(f"{_CAREERS}/apply/start"),
        f"{_CAREERS}/apply/start": _HANDBACK_STEP,
        f"{_CAREERS}/apply/next": (_FORMS / "lever_single.html").read_text(encoding="utf-8")})
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "ready_to_submit")], outcomes
    assert left == [f"{_CAREERS}/apply/next"]         # the job's one tab, at the gate
    assert "tab_taken_over" in _trace_of(tmp_path, "a")


def test_a_popup_that_sends_then_hands_back_is_confirmed_by_the_tab_that_opened_it(
        _browser, tmp_path):
    posts: list = []
    outcomes, left = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/jobs/a")], {
        f"{_CAREERS}/jobs/a": _posting(f"{_CAREERS}/apply/start"),
        f"{_CAREERS}/apply/start": _handback_form(posts),
        f"{_CAREERS}/apply/post": _counter(posts),
        f"{_CAREERS}/apply/next": _THANKS}, submit=True)
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "submitted")], outcomes
    assert outcomes[0].reason.startswith("confirmation page (in the tab that opened"), outcomes
    assert "received" in outcomes[0].reason
    assert posts == ["POST"] and left == []           # one send; a confirmation closes its tab


def test_after_a_send_the_tab_handed_back_to_is_only_read_never_filled_or_sent_again(
        _browser, tmp_path):
    # the tab that opened the closed one shows a form again: the run never
    # takes it over after the submit click (at most one send per job)
    posts: list = []
    again = (_FORMS / "lever_single.html").read_text(encoding="utf-8").replace(
        "document.body.setAttribute('data-submitted', '1');",
        "fetch('/apply/post', {method: 'POST', body: 'b'});")
    outcomes, left = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/jobs/a")], {
        f"{_CAREERS}/jobs/a": _posting(f"{_CAREERS}/apply/start"),
        f"{_CAREERS}/apply/start": _handback_form(posts),
        f"{_CAREERS}/apply/post": _counter(posts),
        f"{_CAREERS}/apply/next": again}, submit=True)
    assert posts == ["POST"], posts
    assert outcomes[0].status == "submitted", outcomes
    assert outcomes[0].reason.startswith("submitted (unconfirmed): the job's tab was closed"), \
        outcomes
    assert "tab_taken_over" not in _trace_of(tmp_path, "a")
    assert left == [f"{_CAREERS}/apply/next"]         # the job's last open tab stays


def test_the_linkedin_tab_closes_once_the_company_tab_is_adopted_and_each_job_keeps_one_tab(
        _browser, flow_server, tmp_path):
    routes = h.linkedin_job_routes("linkedin_posting_button.html",
                                   "lever_single.html")(flow_server.base)
    outcomes, left = _drain_jobs(_browser, tmp_path, [
        ("a", _LINKEDIN_JOB), ("b", f"{flow_server.base}/forms/job_posting.html")], routes)
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "ready_to_submit"),
                                                        ("b", "ready_to_submit")], outcomes
    # one tab per job, each at its form: no LinkedIn tab, no posting behind a popup
    assert sorted(left) == sorted([f"{flow_server.base}/forms/lever_single.html",
                                   f"{flow_server.base}/forms/ashby_steps.html"]), left
    assert "source_tab_closed" in _trace_of(tmp_path, "a")
