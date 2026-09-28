"""The `jev_judge` fixture and its hooks (the SP8 record / replay harness).

A runner test module opts in with

    pytest_plugins = ["conftest_jev"]

and requests `jev_judge` (the autouse `_hermetic` fixture in
`test_apply_run.py` does it for every test there). The fixture yields the
judge factory, which is also `jev_harness.judge()` for helpers without fixture
access. `AUTO_APPLY_TEST_JEV` picks the mode; see `jev_harness` for the
contract. In `fake` mode the hooks below do nothing.

In `record` and `replay` mode the fixture also pins the fact catalog's date
to the day the cache was recorded (`jev_harness.RECORDED_TODAY`), so the
committed cache replays on any later day.

Hooks, active in `record` and `replay` mode only:

- collection: in `record` mode, one line with the number of tests that use
  the fixture, the number of requests already in the cache (they replay for
  free) and the spend cap.
- makereport (call phase): a test whose live request the spend cap stopped
  (`jev.SpendCap`) skips with the cap's reason; a replay miss on a test
  marked `jev_unrecorded` skips with `jev_harness.UNRECORDED_REASON`; any
  other replay miss recorded on the test becomes a failure whose text names the fixture, the test and the
  re-record command; a failed `AssertionError` becomes an xfail carrying the
  divergence; either way the test's record goes to `outcomes.jsonl`.
- terminal summary: replay hits and misses, live requests and spend, the
  outcomes path, and the cap stop if it happened.
- session finish: the shared replay's used keys are written to
  `jev_harness.used_keys_path` (SP8, gitignored); in `replay` mode, when
  `AUTO_APPLY_JEV_PRUNE` is set, the cache is then pruned to those keys, or
  refused with a reason (`jev_harness.prune_if_asked`, `jev.prune_cache`).
"""
from __future__ import annotations

import pytest

import jev
import jev_harness

SESSION_KEY = pytest.StashKey[jev_harness.Session]()


def _xdist_active(config) -> bool:
    """True on an xdist worker's own config (`config.workerinput` is set by
    `xdist/remote.py` before any hook runs there) or on the controller about
    to fork workers (`-n`/`--numprocesses` is already resolved from "auto" to
    a positive count by the time `pytest_configure` fires, and only the
    controller's own config lacks `workerinput`).

    Deliberately NOT `os.environ.get("PYTEST_XDIST_WORKER")`: that var is set
    on the whole WORKER PROCESS, so a `pytester.runpytest_inprocess(...)` call
    nested inside a real worker (as `test_jev_harness.py`'s fixture/hook tests
    do) inherits it even though that nested, single-process pytest run is
    never itself distributed -- an env-var check raised a false UsageError
    there, caught by running this file under `-n 2` (SP3.5 fix round 1).
    `config.workerinput` is per-`Config`, not per-process, so the inner run's
    own fresh `Config` never carries it."""
    if hasattr(config, "workerinput"):
        return True
    numprocesses = getattr(config.option, "numprocesses", None)
    return bool(numprocesses)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", f"{jev_harness.UNRECORDED_MARK}: {jev_harness.UNRECORDED_REASON} (a replay "
        f"miss skips)")
    if SESSION_KEY in config.stash:
        return
    try:
        session = jev_harness.Session.from_env()
    except ValueError as e:
        # an unknown mode, a cap that is no amount above 0, or a live
        # recording with no cap: nothing runs
        raise pytest.UsageError(str(e)) from None
    config.stash[SESSION_KEY] = session
    if session.soft:
        # SP3.5 review finding 1: record/replay read-modify-write a shared
        # repo-tree cache (jev.ReplayJev) and truncate/append a shared
        # outcomes.jsonl with no cross-process lock, so every xdist worker
        # races every other one. Refuse before any worker starts (this fires
        # on the controller too, which stops the whole run before it forks).
        if _xdist_active(config):
            raise pytest.UsageError(
                f"{jev_harness.MODE_ENV}={session.mode} writes a shared cache and outcomes "
                f"file with no cross-process lock: record and replay run serially, never "
                f"under -n/--numprocesses. Run: {jev_harness.serial_command(session.mode)}")
        session.writer.reset()


def _session(config) -> jev_harness.Session | None:
    return config.stash.get(SESSION_KEY, None)


def pytest_collection_modifyitems(config, items):
    session = _session(config)
    if session is None or session.mode != "record":
        return
    n = sum(1 for it in items if jev_harness.FIXTURE in getattr(it, "fixturenames", ()))
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        dry = (f" (dry run: the fake answers at each request's estimated size, into a temp "
               f"copy of {session.source_cache})" if session.dry else "")
        reporter.write_line(
            f"jev record: {n} test(s) use {jev_harness.FIXTURE}; {session.cached_count()} "
            f"request(s) already cached replay for free; live spend stops at cap "
            f"{session.cap_usd:.2f} USD ({jev_harness.CAP_ENV}); cache {session.cache_path}{dry}")


@pytest.fixture
def jev_judge(request, monkeypatch):
    """The judge factory for this test, per `AUTO_APPLY_TEST_JEV`."""
    session = _session(request.config)
    reason = session.skip_reason()
    if reason:
        pytest.skip(reason)
    record = session.begin(request.node.nodeid)
    jev_harness.activate(session)
    if session.soft:
        _capture_outcomes(monkeypatch, record)
        _pin_today(monkeypatch)
    try:
        yield session.judge
    finally:
        jev_harness.deactivate()
        session.end(request.node.nodeid)


def _pin_today(monkeypatch):
    """Every catalog the test builds reads today as the day the cache was
    recorded (`jev_harness.RECORDED_TODAY`): the `today` fact is in each
    request that lists the facts, so the real date would miss the cache on
    every day after the recording."""
    import apply_facts
    monkeypatch.setattr(apply_facts, "build", jev_harness.pinned_build(apply_facts.build))


def _capture_outcomes(monkeypatch, record):
    """Every terminal outcome the runner reaches during the test lands on its
    record, through `_JobRun._finish`, the one path every status takes."""
    import apply_run
    real = apply_run._JobRun._finish

    def wrapped(self, *args, **kwargs):
        out = real(self, *args, **kwargs)
        record.outcomes.append({"job_id": out.job_id, "status": out.status,
                                "reason": out.reason, "pages": out.pages})
        return out
    monkeypatch.setattr(apply_run._JobRun, "_finish", wrapped)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    session = _session(item.config)
    if session is None or not session.soft or rep.when != "call":
        return
    record = session.records.get(item.nodeid)
    if record is None:
        return
    if record.capped:
        # the spend cap stopped a live request: the test never saw its
        # answers, so it is neither a divergence nor a miss
        rep.outcome = "skipped"
        rep.longrepr = (str(item.path), item.location[1] or 0, f"Skipped: {record.capped}")
    elif record.misses and item.get_closest_marker(jev_harness.UNRECORDED_MARK):
        # a later phase records this test (`jev_harness.UNRECORDED_MARK`)
        rep.outcome = "skipped"
        rep.longrepr = (str(item.path), item.location[1] or 0,
                        f"Skipped: {jev_harness.UNRECORDED_REASON}")
    elif record.misses:
        rep.outcome = "failed"
        rep.longrepr = session.miss_text(record) + "\n\n" + str(rep.longrepr or "")
    elif rep.failed and call.excinfo is not None and call.excinfo.errisinstance(AssertionError):
        record.divergence = call.excinfo.exconly()
        rep.outcome = "skipped"
        rep.wasxfail = (f"jev {session.mode}: the outcome diverges from the fake: "
                        f"{record.divergence}")
    record.result = "xfail" if getattr(rep, "wasxfail", None) else rep.outcome
    session.writer.write(record)


def pytest_terminal_summary(terminalreporter, config):
    session = _session(config)
    if session is None or not session.soft:
        return
    usage = jev.usage()
    replay = session.replay
    hits = replay.hits if replay else 0
    misses = replay.misses if replay else 0
    diverged = sum(1 for r in session.records.values() if r.divergence)
    terminalreporter.write_sep("-", f"jev {session.mode}{' (dry run)' if session.dry else ''}")
    live = "estimated live" if session.dry else "live"
    terminalreporter.write_line(
        f"replay hits {hits}, misses {misses}; {live} requests {usage['requests']}, "
        f"{usage['input_tokens']} input tokens, {usage['usd']:.4f} USD; "
        f"{len(session.records)} test(s) recorded, {diverged} diverged from the fake")
    terminalreporter.write_line(f"outcomes: {session.writer.path}")
    if session.stopped:
        terminalreporter.write_line(session.stopped)


def pytest_sessionfinish(session, exitstatus):
    """SP8: write the shared replay's used keys beside `outcomes.jsonl`
    (`jev_harness.write_used_keys`), then, in `replay` mode with
    `AUTO_APPLY_JEV_PRUNE` set, prune the cache to them or print the refusal
    (`jev_harness.prune_if_asked`). `session.testsfailed` is the run's own
    failure count: a replay miss that is not marked `jev_unrecorded` already
    turned its test's report into a failure above, so it counts here too.
    (`session` here is pytest's own `Session`, the hookspec's name for it --
    not `jev_harness.Session`, which `_session(session.config)` returns.)"""
    jsession = _session(session.config)
    if jsession is None or not jsession.soft:
        return
    jev_harness.write_used_keys(jsession)
    line = jev_harness.prune_if_asked(jsession, session.testsfailed)
    if not line:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(line)
    else:
        print(line)      # no terminal reporter: some other plugin took over reporting
