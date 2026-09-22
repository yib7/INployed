"""The `jev_judge` fixture and its hooks (the SP8 record / replay harness).

A runner test module opts in with

    pytest_plugins = ["conftest_jev"]

and requests `jev_judge` (the autouse `_hermetic` fixture in
`test_apply_run.py` does it for every test there). The fixture yields the
judge factory, which is also `jev_harness.judge()` for helpers without fixture
access. `AUTO_APPLY_TEST_JEV` picks the mode; see `jev_harness` for the
contract. In `fake` mode the hooks below do nothing.

Hooks, active in `record` and `replay` mode only:

- collection: in `record` mode, one line with the number of tests that use
  the fixture, the number of requests already in the cache (they replay for
  free) and the spend cap.
- makereport (call phase): a replay miss recorded on the test becomes a
  failure whose text names the fixture, the test and the re-record command; a
  failed `AssertionError` becomes an xfail carrying the divergence; either way
  the test's record goes to `outcomes.jsonl`.
- terminal summary: replay hits and misses, live requests and spend, the
  outcomes path, and the cap stop if it happened.
"""
from __future__ import annotations

import pytest

import jev
import jev_harness

SESSION_KEY = pytest.StashKey[jev_harness.Session]()


def pytest_configure(config):
    if SESSION_KEY in config.stash:
        return
    session = jev_harness.Session.from_env()
    config.stash[SESSION_KEY] = session
    if session.soft:
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
        reporter.write_line(
            f"jev record: {n} test(s) use {jev_harness.FIXTURE}; {session.cached_count()} "
            f"request(s) already cached replay for free; live spend stops at cap "
            f"{session.cap_usd:.2f} USD ({jev_harness.CAP_ENV}); cache {session.cache_path}")


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
    try:
        yield session.judge
    finally:
        jev_harness.deactivate()
        session.end(request.node.nodeid)


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
    if record.misses:
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
    terminalreporter.write_sep("-", f"jev {session.mode}")
    terminalreporter.write_line(
        f"replay hits {hits}, misses {misses}; live requests {usage['requests']}, "
        f"{usage['input_tokens']} input tokens, {usage['usd']:.4f} USD; "
        f"{len(session.records)} test(s) recorded, {diverged} diverged from the fake")
    terminalreporter.write_line(f"outcomes: {session.writer.path}")
    if session.stopped:
        terminalreporter.write_line(session.stopped)
