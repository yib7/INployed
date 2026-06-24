"""Per-call escalating Gemini timeout + timeout-only retry (cycle 11 SP1).

The transport must give each `generate_content` a bounded timeout that ESCALATES
across attempts (default 60->120->180s) and retry ONLY on a timeout; a hung server
must not block forever. All logic is exercised with an injected `_invoke` — no real
Gemini call, no network, no billing.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import config, llm  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Logic tests: creds always "present", sleeps instant, usage clean.
    monkeypatch.setattr(llm, "_check_creds", lambda: None)
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    llm.reset_usage()
    yield
    llm.reset_usage()


# A timeout whose TYPE NAME carries "timeout" (mirrors httpx.ReadTimeout).
class ReadTimeout(Exception):
    pass


def _ok_resp(text='{"ok": 1}'):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=2),
    )


def _invoke_recorder(fail_times, exc_factory, *, ok_text='{"ok": 1}'):
    """A fake llm._invoke that fails `fail_times` times then returns ok_resp.
    Records the timeout_s passed on each attempt."""
    seen: list[int] = []

    def _fake(system, user, model, *, json_out, temperature,
              max_output_tokens, tools, timeout_s):
        seen.append(timeout_s)
        if len(seen) <= fail_times:
            raise exc_factory()
        return _ok_resp(ok_text)

    return _fake, seen


# -- config.tailor_timeout_schedule -------------------------------------------
def test_schedule_default(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_TIMEOUTS", raising=False)
    assert config.tailor_timeout_schedule() == [60, 120, 180]


def test_schedule_env_override(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_TIMEOUTS", "30, 90, 200")
    assert config.tailor_timeout_schedule() == [30, 90, 200]


def test_schedule_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_TIMEOUTS", "abc,,-5,0")
    assert config.tailor_timeout_schedule() == [60, 120, 180]


# -- llm._is_timeout ----------------------------------------------------------
def test_is_timeout_by_type_name():
    assert llm._is_timeout(ReadTimeout("boom")) is True


def test_is_timeout_by_message():
    assert llm._is_timeout(RuntimeError("504 Deadline Exceeded")) is True
    assert llm._is_timeout(RuntimeError("the request timed out")) is True


def test_is_timeout_false_for_other_errors():
    assert llm._is_timeout(ValueError("bad json")) is False
    assert llm._is_timeout(RuntimeError("429 quota exceeded")) is False


def test_is_timeout_walks_cause_chain():
    inner = ReadTimeout("read timed out")
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert llm._is_timeout(outer) is True


# -- escalation + retry -------------------------------------------------------
def test_escalates_timeout_then_succeeds(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_TIMEOUTS", "1,2,3")
    fake, seen = _invoke_recorder(2, lambda: ReadTimeout("timed out"))
    monkeypatch.setattr(llm, "_invoke", fake)
    out = llm.call("s", "u", config.TIER_FLASH, json_out=True)
    assert out == {"ok": 1}
    assert seen == [1, 2, 3]                 # used escalating timeouts, in order
    assert llm.USAGE and llm.USAGE[0]["model"] == config.MODEL_FLASH


def test_exhausts_timeouts_raises_timeout_error(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_TIMEOUTS", "1,2,3")
    fake, seen = _invoke_recorder(99, lambda: ReadTimeout("timed out"))
    monkeypatch.setattr(llm, "_invoke", fake)
    with pytest.raises(llm.LLMError) as ei:
        llm.call("s", "u", config.TIER_FLASH, json_out=True)
    assert seen == [1, 2, 3]                  # all attempts used, then gave up
    assert "tim" in str(ei.value).lower()    # timeout-specific message


def test_non_timeout_error_surfaces_after_retries_not_as_timeout(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_TIMEOUTS", "1,2,3")
    fake, seen = _invoke_recorder(99, lambda: RuntimeError("kaboom"))
    monkeypatch.setattr(llm, "_invoke", fake)
    with pytest.raises(llm.LLMError) as ei:
        llm.call("s", "u", config.TIER_FLASH, json_out=True)
    assert len(seen) == 3
    assert "timed out" not in str(ei.value).lower()
    assert "kaboom" in str(ei.value)


# -- SDK wiring (construction only, no generate_content, no billing) ----------
def test_build_client_passes_http_options_timeout(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"gemini_auth": "api_key"})
    monkeypatch.setenv("RESUME_TAILOR_GEMINI_API_KEY", "k")
    rec = {}
    monkeypatch.setattr("google.genai.Client",
                        lambda **kwargs: rec.update(kwargs) or SimpleNamespace(**kwargs))
    llm._build_client(60)
    assert rec.get("api_key") == "k"
    assert rec["http_options"].timeout == 60_000   # seconds -> milliseconds
