"""A `claude` CLI whose sign-in has lapsed.

Claude Code 2.1.292, signed out, answers every `claude -p` with exit 1, an
empty stderr and a JSON envelope on stdout whose `result` reads "Failed to
authenticate: OAuth session expired and could not be refreshed". The envelope
opens with usage counters, so its first 400 characters never reach `result`.
2.1.283 in the same state also printed `[claude-code:unrecognized_model]` for
claude-sonnet-5-5, which must not swap the model: the fallback is signed out
too.

Every test fakes `claude_cli.subprocess.run` and `claude_cli.find_claude`: the
real `claude` binary is never invoked.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

import claude_cli  # noqa: E402

AUTH = "Failed to authenticate: OAuth session expired and could not be refreshed"
SONNET_NEW = "claude-sonnet-5-5"


def _envelope(result, *, is_error=True):
    # The live envelope's field order: counters first, `result` near the end.
    return json.dumps({
        "duration_api_ms": 0, "stop_reason": "stop_sequence", "total_cost_usd": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation": {
            "ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}},
        "modelUsage": {}, "permission_denials": [], "terminal_reason": "api_error",
        "subagent_stats": {"spawned": 0, "completed": 0, "failed": 0},
        "is_error": is_error, "num_turns": 1, "subtype": "success",
        "result": result, "type": "result", "duration_ms": 326,
    })


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def fake_exe(monkeypatch):
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")


def _fake_cli(monkeypatch, proc):
    models: list[str] = []

    def fake_run(argv, **kwargs):
        models.append(argv[argv.index("--model") + 1])
        return proc

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    return models


@pytest.mark.parametrize("text", [
    AUTH,
    "Not logged in. Please run /login",
    "Invalid API key · Please run /login",
    "OAuth token has expired. Please obtain a new token or refresh your existing token.",
])
def test_is_signed_out_message_matches_the_cli_sign_in_failures(text):
    assert claude_cli.is_signed_out_message(text) is True


@pytest.mark.parametrize("text", [
    None, "", "some unrelated failure", "Rate limit reached", "API Error: 500",
    "the job asks for OAuth experience",
])
def test_is_signed_out_message_ignores_other_failures(text):
    assert claude_cli.is_signed_out_message(text) is False


def test_a_signed_out_exit_raises_signed_out_and_names_the_real_error(
        monkeypatch, fake_exe):
    models = _fake_cli(monkeypatch, _proc(returncode=1, stdout=_envelope(AUTH)))
    with pytest.raises(claude_cli.ClaudeCLIError) as ei:
        claude_cli.run_claude("sys", "user", SONNET_NEW)
    assert ei.value.kind == "signed_out"
    assert AUTH in str(ei.value)
    assert models == [SONNET_NEW]                # no fallback: it is signed out too


def test_the_unrecognized_marker_beside_a_sign_in_failure_swaps_nothing(
        monkeypatch, fake_exe):
    marker = f'[claude-code:unrecognized_model] {{"model":"{SONNET_NEW}","query_source":"sdk"}}'
    models = _fake_cli(monkeypatch, _proc(returncode=1, stdout=_envelope(AUTH), stderr=marker))
    with pytest.raises(claude_cli.ClaudeCLIError) as ei:
        claude_cli.run_claude("sys", "user", SONNET_NEW)
    assert ei.value.kind == "signed_out"
    assert models == [SONNET_NEW]
    assert claude_cli.active_swaps() == {}


def test_a_signed_out_error_envelope_on_exit_0_raises_signed_out(monkeypatch, fake_exe):
    _fake_cli(monkeypatch, _proc(stdout=_envelope(AUTH)))
    with pytest.raises(claude_cli.ClaudeCLIError) as ei:
        claude_cli.run_claude("sys", "user", "claude-sonnet-5")
    assert ei.value.kind == "signed_out"


def test_a_failed_exit_shows_the_envelope_result_over_its_counters(monkeypatch, fake_exe):
    """Any failed exit with an envelope on stdout names its `result`, which the
    first 400 characters of the envelope never reach."""
    _fake_cli(monkeypatch, _proc(returncode=1, stdout=_envelope("API Error: 500 boom")))
    with pytest.raises(claude_cli.ClaudeCLIError) as ei:
        claude_cli.run_claude("sys", "user", "claude-sonnet-5")
    assert "API Error: 500 boom" in str(ei.value)
    assert "duration_api_ms" not in str(ei.value)


def test_the_pool_never_retries_a_signed_out_cli(monkeypatch):
    calls = []

    def fake_run_claude(system, user, model, **kwargs):
        calls.append(model)
        raise claude_cli.ClaudeCLIError(AUTH, kind="signed_out")

    async def no_sleep(_s):
        raise AssertionError("a signed-out CLI must not be retried")

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    monkeypatch.setattr(claude_cli.asyncio, "sleep", no_sleep)
    pool = claude_cli.ClaudePool(max_procs=1)
    config = SimpleNamespace(system_instruction="sys", response_mime_type=None,
                             response_schema=None)
    with pytest.raises(claude_cli.ClaudeCLIError):
        asyncio.run(pool.generate(model="claude-sonnet-5", contents="hi", config=config))
    assert calls == ["claude-sonnet-5"]
