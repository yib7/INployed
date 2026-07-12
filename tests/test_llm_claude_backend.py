"""Claude provider config — tier->model resolution + timeout schedule (SP2)
plus llm.call() dispatch and the _call_claude retry envelope (SP3).

Mirror of test_llm_backend.py: tailor_provider(), claude_model_for(), and
claude_timeout_schedule() resolve live from env > config.json > defaults.
All tests hermetic: no real Claude CLI invocation, no network — `_call_claude`
lanes are exercised via the injected `llm._invoke_claude` seam and a stubbed
`llm._claude_cli()` fake module (mirrors llm._invoke / _call_gemini's own
test style in test_llm_rate_limit.py / test_llm_timeout.py).
"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))
sys.path.insert(0, str(REPO))          # claude_cli.py lives at the repo root

from resume_tailor import config  # noqa: E402
from resume_tailor import llm  # noqa: E402
import claude_cli  # noqa: E402


# -- tailor_provider resolution -----------------------------------------------
def test_tailor_provider_default_is_gemini(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROVIDER", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.tailor_provider() == "gemini"


def test_tailor_provider_from_config_json(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROVIDER", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"tailor_provider": "claude"})
    assert config.tailor_provider() == "claude"


def test_tailor_provider_env_overrides_config(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_PROVIDER", "claude")
    monkeypatch.setattr(config, "_config_json", lambda: {"tailor_provider": "gemini"})
    assert config.tailor_provider() == "claude"


def test_tailor_provider_unknown_falls_back_to_gemini(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_PROVIDER", "chatgpt")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.tailor_provider() == "gemini"


def test_tailor_provider_case_insensitive_claude(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_PROVIDER", "CLAUDE")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.tailor_provider() == "claude"


def test_tailor_provider_whitespace_stripped(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_PROVIDER", "  claude  ")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.tailor_provider() == "claude"


def test_tailor_provider_whitespace_stripped_gemini(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_PROVIDER", "  GEMINI  ")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.tailor_provider() == "gemini"


def test_tailor_provider_vertex_falls_back_to_gemini(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_PROVIDER", "vertex")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.tailor_provider() == "gemini"


# -- Claude tier -> model resolution ------------------------------------------
def test_claude_model_defaults_are_correct():
    assert config.CLAUDE_MODEL_FLASH_LITE == "claude-haiku-4-5"
    assert config.CLAUDE_MODEL_FLASH == "claude-sonnet-5"
    assert config.CLAUDE_MODEL_PRO == "claude-opus-4-8"


def test_claude_model_for_returns_tier_default(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", raising=False)
    monkeypatch.delenv("RESUME_TAILOR_CLAUDE_MODEL_FLASH", raising=False)
    monkeypatch.delenv("RESUME_TAILOR_CLAUDE_MODEL_PRO", raising=False)
    assert config.claude_model_for(config.TIER_FLASH_LITE) == "claude-haiku-4-5"
    assert config.claude_model_for(config.TIER_FLASH) == "claude-sonnet-5"
    assert config.claude_model_for(config.TIER_PRO) == "claude-opus-4-8"


def test_claude_model_for_flash_lite_picks_up_env_change_live(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", "claude-custom-lite")
    assert config.claude_model_for(config.TIER_FLASH_LITE) == "claude-custom-lite"


def test_claude_model_for_flash_picks_up_env_change_live(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_MODEL_FLASH", "claude-custom-flash")
    assert config.claude_model_for(config.TIER_FLASH) == "claude-custom-flash"


def test_claude_model_for_pro_picks_up_env_change_live(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_MODEL_PRO", "claude-custom-pro")
    assert config.claude_model_for(config.TIER_PRO) == "claude-custom-pro"


def test_claude_model_for_unknown_tier_defaults_to_flash(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_CLAUDE_MODEL_FLASH", raising=False)
    assert config.claude_model_for("unknown_tier") == "claude-sonnet-5"


# -- claude_timeout_schedule --------------------------------------------------
def test_claude_timeout_schedule_default(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_CLAUDE_TIMEOUTS", raising=False)
    assert config.claude_timeout_schedule() == [180, 300]


def test_claude_timeout_schedule_env_override(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_TIMEOUTS", "60,120")
    assert config.claude_timeout_schedule() == [60, 120]


def test_claude_timeout_schedule_env_override_spaced(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_TIMEOUTS", "60, 120, 300")
    assert config.claude_timeout_schedule() == [60, 120, 300]


def test_claude_timeout_schedule_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_TIMEOUTS", "abc,,-5,0")
    assert config.claude_timeout_schedule() == [180, 300]


def test_claude_timeout_schedule_empty_string_falls_back(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_TIMEOUTS", "")
    assert config.claude_timeout_schedule() == [180, 300]


# -- Pinned schedule tests: verify tailor_timeout_schedule stays green ---------
def test_schedule_default_still_green(monkeypatch):
    """Pinned: tailor_timeout_schedule() default must stay [60, 120, 180]."""
    monkeypatch.delenv("RESUME_TAILOR_TIMEOUTS", raising=False)
    assert config.tailor_timeout_schedule() == [60, 120, 180]


def test_schedule_env_override_still_green(monkeypatch):
    """Pinned: tailor_timeout_schedule() env override must still work."""
    monkeypatch.setenv("RESUME_TAILOR_TIMEOUTS", "30, 90, 200")
    assert config.tailor_timeout_schedule() == [30, 90, 200]


def test_schedule_garbage_falls_back_still_green(monkeypatch):
    """Pinned: tailor_timeout_schedule() junk must fall back to [60, 120, 180]."""
    monkeypatch.setenv("RESUME_TAILOR_TIMEOUTS", "abc,,-5,0")
    assert config.tailor_timeout_schedule() == [60, 120, 180]


# -- llm.call() dispatch -------------------------------------------------------
def test_call_dispatches_to_claude_when_provider_is_claude(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROVIDER", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"tailor_provider": "claude"})
    captured = {}
    monkeypatch.setattr(
        llm, "_call_claude",
        lambda system, user, model, **k: captured.update(model=model) or "C",
    )
    assert llm.call("s", "u", config.TIER_PRO) == "C"
    assert captured["model"] == config.CLAUDE_MODEL_PRO


def test_call_dispatches_to_gemini_by_default(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROVIDER", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    captured = {}
    monkeypatch.setattr(
        llm, "_call_gemini",
        lambda system, user, model, **k: captured.update(model=model) or "G",
    )
    assert llm.call("s", "u", config.TIER_PRO) == "G"
    assert captured["model"] == config.MODEL_PRO


# -- _call_claude retry envelope ------------------------------------------------
@pytest.fixture
def claude_env(monkeypatch):
    """Sleeps recorded (not slept), usage clean, a fake claude_cli module wired
    behind llm._claude_cli() so find_claude() always reports the CLI present
    unless a test overrides it."""
    recorded: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: recorded.append(s))
    fake_module = types.SimpleNamespace(find_claude=lambda: "claude.exe")
    monkeypatch.setattr(llm, "_claude_cli", lambda: fake_module)
    llm.reset_usage()
    yield recorded, fake_module
    llm.reset_usage()


class ClaudeCLIErrorLike(Exception):
    """Stands in for claude_cli.ClaudeCLIError — llm._call_claude reads
    `.kind` via getattr, so any exception with that attribute works."""

    def __init__(self, msg, *, kind="error"):
        super().__init__(msg)
        self.kind = kind


def _invoke_claude_seq(excs, *, ok_text='{"ok": 1}', in_tok=11, out_tok=22,
                        cache_read=3, cache_write=4):
    """Fake llm._invoke_claude raising each exception in `excs` in turn, then
    returning a CLIResult-shaped SimpleNamespace. Records each timeout_s."""
    seen: list[int] = []
    pending = list(excs)

    def _fake(system, user, model, *, json_out, tools, timeout_s):
        seen.append(timeout_s)
        if pending:
            raise pending.pop(0)
        return claude_cli.CLIResult(ok_text, in_tok, out_tok, cache_read, cache_write)

    return _fake, seen


def test_call_claude_happy_path_usage_row(monkeypatch, claude_env):
    recorded, _fake_module = claude_env
    fake, seen = _invoke_claude_seq([])
    monkeypatch.setattr(llm, "_invoke_claude", fake)
    out = llm._call_claude("sys", "user", "claude-sonnet-5")
    assert out == '{"ok": 1}'
    assert seen == [180]                     # first schedule slot, no retries
    assert recorded == []
    assert llm.USAGE and llm.USAGE[0] == {
        "model": "claude:claude-sonnet-5",
        "in": 11, "out": 22, "cache_read": 3, "cache_write": 4,
    }


def test_call_claude_json_out_runs_extract_json(monkeypatch, claude_env):
    fake, _seen = _invoke_claude_seq([], ok_text='```json\n{"a": 1}\n```')
    monkeypatch.setattr(llm, "_invoke_claude", fake)
    out = llm._call_claude("sys", "user", "claude-sonnet-5", json_out=True)
    assert out == {"a": 1}


def test_call_claude_rate_limit_reuses_slot_within_budget(monkeypatch, claude_env):
    recorded, _ = claude_env
    excs = [ClaudeCLIErrorLike("usage limit", kind="rate_limit")] * (
        llm.RATE_LIMIT_MAX_RETRIES - 1
    )
    fake, seen = _invoke_claude_seq(excs)
    monkeypatch.setattr(llm, "_invoke_claude", fake)
    out = llm._call_claude("sys", "user", "claude-sonnet-5")
    assert out == '{"ok": 1}'
    # every rate-limit attempt reuses the same (first) schedule slot
    assert seen == [180] * llm.RATE_LIMIT_MAX_RETRIES
    assert len(recorded) == llm.RATE_LIMIT_MAX_RETRIES - 1


def test_call_claude_rate_limit_exhausts_budget_raises(monkeypatch, claude_env):
    recorded, _ = claude_env
    excs = [ClaudeCLIErrorLike("usage limit", kind="rate_limit")] * 99
    fake, _seen = _invoke_claude_seq(excs)
    monkeypatch.setattr(llm, "_invoke_claude", fake)
    with pytest.raises(llm.LLMError, match="[Rr]ate.*limit"):
        llm._call_claude("sys", "user", "claude-sonnet-5")
    assert len(recorded) == llm.RATE_LIMIT_MAX_RETRIES


def test_call_claude_timeout_escalates_then_raises(monkeypatch, claude_env):
    recorded, _ = claude_env
    excs = [ClaudeCLIErrorLike("timed out", kind="timeout")] * 99
    fake, seen = _invoke_claude_seq(excs)
    monkeypatch.setattr(llm, "_invoke_claude", fake)
    with pytest.raises(llm.LLMError) as ei:
        llm._call_claude("sys", "user", "claude-sonnet-5")
    assert seen == [180, 300]           # default schedule, escalated fully
    assert recorded == []               # timeouts never sleep
    assert "300" in str(ei.value)       # names the last timeout


def test_call_claude_timeout_then_succeeds(monkeypatch, claude_env):
    excs = [ClaudeCLIErrorLike("timed out", kind="timeout")]
    fake, seen = _invoke_claude_seq(excs)
    monkeypatch.setattr(llm, "_invoke_claude", fake)
    out = llm._call_claude("sys", "user", "claude-sonnet-5")
    assert out == '{"ok": 1}'
    assert seen == [180, 300]


def test_call_claude_transient_error_sleeps_and_advances(monkeypatch, claude_env):
    recorded, _ = claude_env
    excs = [ClaudeCLIErrorLike("500 boom", kind="error")]
    fake, seen = _invoke_claude_seq(excs)
    monkeypatch.setattr(llm, "_invoke_claude", fake)
    out = llm._call_claude("sys", "user", "claude-sonnet-5")
    assert out == '{"ok": 1}'
    assert seen == [180, 300]
    assert recorded == [1.5]            # 1.5 * (idx + 1), idx=0 on first attempt


def test_call_claude_transient_exhausts_schedule_raises(monkeypatch, claude_env):
    recorded, _ = claude_env
    excs = [ClaudeCLIErrorLike("500 boom", kind="error")] * 99
    fake, seen = _invoke_claude_seq(excs)
    monkeypatch.setattr(llm, "_invoke_claude", fake)
    with pytest.raises(llm.LLMError):
        llm._call_claude("sys", "user", "claude-sonnet-5")
    assert seen == [180, 300]
    assert recorded == [1.5, 3.0]


def test_call_claude_missing_cli_raises_immediately_no_sleeps(monkeypatch, claude_env):
    recorded, fake_module = claude_env
    fake_module.find_claude = lambda: None
    invoke_calls = []

    def _should_not_be_called(*a, **k):
        invoke_calls.append(1)
        raise AssertionError("_invoke_claude must not be called when the CLI is missing")

    monkeypatch.setattr(llm, "_invoke_claude", _should_not_be_called)
    with pytest.raises(llm.LLMError, match="PATH|install|Install"):
        llm._call_claude("sys", "user", "claude-sonnet-5")
    assert recorded == []
    assert invoke_calls == []
