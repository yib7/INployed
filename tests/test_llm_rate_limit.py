"""429/quota handling in the Gemini transport (batch-run resilience).

A 14-job parallel tailor slams the API and free-tier RPM limits are tiny
(5-15 RPM), so rate limits are EXPECTED during batches — they must translate
into patient waiting, not failed jobs. Rate-limit errors get their own retry
budget (RATE_LIMIT_MAX_RETRIES), independent of the escalating timeout
schedule, with exponential backoff + jitter and the server's retryDelay hint
honored when present. All exercised with an injected `_invoke` — no real
Gemini call, no billing.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import llm  # noqa: E402


@pytest.fixture
def sleeps(monkeypatch):
    """Creds present, sleeps recorded (not slept), jitter zeroed."""
    recorded: list[float] = []
    monkeypatch.setattr(llm, "_check_creds", lambda: None)
    monkeypatch.setattr(llm.time, "sleep", lambda s: recorded.append(s))
    monkeypatch.setattr(llm.random, "uniform", lambda a, b: 0.0)
    llm.reset_usage()
    yield recorded
    llm.reset_usage()


def _ok_resp(text='{"ok": 1}'):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=2),
    )


class Quota429(Exception):
    pass


def _invoke_seq(excs):
    """Fake _invoke raising each exception in `excs` in turn, then succeeding.
    Records the timeout_s of every attempt."""
    seen: list[int] = []
    pending = list(excs)

    def _fake(system, user, model, *, json_out, temperature,
              max_output_tokens, tools, timeout_s):
        seen.append(timeout_s)
        if pending:
            raise pending.pop(0)
        return _ok_resp()

    return _fake, seen


def test_429_survives_more_failures_than_the_timeout_schedule(monkeypatch, sleeps):
    # 4 consecutive 429s > the 3-slot timeout schedule: the old code (which
    # burned a schedule attempt per 429) failed here; the budgeted path waits.
    fake, seen = _invoke_seq([Quota429("429 RESOURCE_EXHAUSTED")] * 4)
    monkeypatch.setattr(llm, "_invoke", fake)
    out = llm.call("sys", "user", "quick", json_out=True)
    assert out == {"ok": 1}
    assert len(sleeps) == 4
    # 429 retries reuse the SAME schedule slot (no timeout escalation burned).
    assert seen == [60, 60, 60, 60, 60]


def test_429_backoff_grows_exponentially_and_caps(monkeypatch, sleeps):
    fake, _seen = _invoke_seq([Quota429("429 rate limit")] * 5)
    monkeypatch.setattr(llm, "_invoke", fake)
    llm.call("sys", "user", "quick")
    assert sleeps == [30.0, 60.0, 120.0, 240.0, 300.0]  # base 30 doubling, capped


def test_429_budget_exhaustion_raises_clear_error(monkeypatch, sleeps):
    fake, _seen = _invoke_seq([Quota429("429 quota exceeded")] * 99)
    monkeypatch.setattr(llm, "_invoke", fake)
    with pytest.raises(llm.LLMError, match="[Rr]ate limit"):
        llm.call("sys", "user", "quick")
    assert len(sleeps) == llm.RATE_LIMIT_MAX_RETRIES


def test_429_honors_server_retry_delay_hint(monkeypatch, sleeps):
    err = Quota429("429 RESOURCE_EXHAUSTED details: {'retryDelay': '17s'}")
    fake, _seen = _invoke_seq([err])
    monkeypatch.setattr(llm, "_invoke", fake)
    llm.call("sys", "user", "quick")
    assert sleeps == [17.0]


def test_429_does_not_consume_timeout_escalation(monkeypatch, sleeps):
    # 429 -> same 60s slot; then a timeout -> escalate to 120s; then ok.
    class ReadTimeout(Exception):
        pass

    fake, seen = _invoke_seq([Quota429("429 quota"), ReadTimeout("t")])
    monkeypatch.setattr(llm, "_invoke", fake)
    llm.call("sys", "user", "quick")
    assert seen == [60, 60, 120]


def test_transient_errors_keep_schedule_bounded_behavior(monkeypatch, sleeps):
    # Non-429, non-timeout transients still consume schedule attempts and
    # raise after the schedule is exhausted (pre-existing contract).
    fake, seen = _invoke_seq([RuntimeError("503 flaky")] * 3)
    monkeypatch.setattr(llm, "_invoke", fake)
    with pytest.raises(llm.LLMError):
        llm.call("sys", "user", "quick")
    assert len(seen) == 3


def test_retry_delay_hint_parser():
    assert llm._retry_delay_hint("{'retryDelay': '22s'}") == 22.0
    assert llm._retry_delay_hint('"retryDelay": "7.5s"') == 7.5
    assert llm._retry_delay_hint("retry-delay: 90") == 90.0
    assert llm._retry_delay_hint("no hint here") is None
    # Absurd server hints are capped by the caller; parser just parses.
