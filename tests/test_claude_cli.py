"""Hermetic tests for the root claude_cli.py headless-CLI transport.

Every test fakes `claude_cli.subprocess.run` and `claude_cli.find_claude` --
the real `claude` binary is NEVER invoked and no network call is made. Retry
tests monkeypatch the module-level `asyncio.sleep` so nothing actually waits.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import claude_cli  # noqa: E402


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _envelope(result, *, is_error=False, usage=None):
    body = {"result": result, "is_error": is_error}
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body)


# --------------------------------------------------------------------------
# find_claude / is_rate_limit_message
# --------------------------------------------------------------------------

def test_find_claude_delegates_to_shutil_which(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: f"/bin/{name}")
    assert claude_cli.find_claude() == "/bin/claude"


def test_find_claude_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    assert claude_cli.find_claude() is None


@pytest.mark.parametrize("text", [
    "You've hit your usage limit for this session",
    "Rate limit exceeded, please retry",
    "429 Too Many Requests",
    "Overloaded, try again later",
    "Limit reached; resets at 5pm",
])
def test_is_rate_limit_message_true_cases(text):
    assert claude_cli.is_rate_limit_message(text) is True


def test_is_rate_limit_message_false_for_ordinary_error():
    assert claude_cli.is_rate_limit_message("some unrelated failure") is False
    assert claude_cli.is_rate_limit_message(None) is False
    assert claude_cli.is_rate_limit_message("") is False


# --------------------------------------------------------------------------
# run_claude: argv shape + flags
# --------------------------------------------------------------------------

@pytest.fixture
def fake_exe(monkeypatch):
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "C:/bin/claude.exe")
    return "C:/bin/claude.exe"


def test_run_claude_argv_shape_and_call_kwargs(monkeypatch, fake_exe):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _proc(stdout=_envelope("hi", usage={"input_tokens": 1, "output_tokens": 2}))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    res = claude_cli.run_claude("sys prompt", "user prompt", "claude-sonnet-5")

    argv = captured["argv"]
    assert argv[0] == fake_exe
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert argv[argv.index("--system-prompt") + 1] == "sys prompt"
    assert "--exclude-dynamic-system-prompt-sections" in argv

    kwargs = captured["kwargs"]
    assert kwargs["input"] == "user prompt"
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["timeout"] == claude_cli.DEFAULT_TIMEOUT_S
    # temp cwd, not the repo (no CLAUDE.md / skills leak into the child)
    assert kwargs["cwd"] != str(REPO)
    assert kwargs["creationflags"] == claude_cli._NO_WINDOW

    assert res.text == "hi"
    assert res.input_tokens == 1
    assert res.output_tokens == 2


def test_run_claude_json_mode_appends_only_json_suffix(monkeypatch, fake_exe):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _proc(stdout=_envelope("{}"))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    claude_cli.run_claude("sys", "user", "claude-haiku-4-5", json_mode=True)

    sys_prompt = captured["argv"][captured["argv"].index("--system-prompt") + 1]
    assert sys_prompt.startswith("sys")
    assert "ONLY valid JSON" in sys_prompt


def test_run_claude_json_mode_false_leaves_system_prompt_untouched(monkeypatch, fake_exe):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _proc(stdout=_envelope("hi"))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    claude_cli.run_claude("sys prompt", "user", "claude-haiku-4-5", json_mode=False)
    assert captured["argv"][captured["argv"].index("--system-prompt") + 1] == "sys prompt"


def test_run_claude_websearch_flag_adds_allowed_tools(monkeypatch, fake_exe):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _proc(stdout=_envelope("hi"))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    claude_cli.run_claude("sys", "user", "m", allow_websearch=True)
    argv = captured["argv"]
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch"


def test_run_claude_no_websearch_flag_by_default(monkeypatch, fake_exe):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _proc(stdout=_envelope("hi"))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    claude_cli.run_claude("sys", "user", "m")
    assert "--allowedTools" not in captured["argv"]


# --------------------------------------------------------------------------
# run_claude: envelope -> CLIResult (incl. cache token parsing)
# --------------------------------------------------------------------------

def test_run_claude_parses_cache_tokens_when_present(monkeypatch, fake_exe):
    usage = {
        "input_tokens": 10, "output_tokens": 20,
        "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7,
    }
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda argv, **kw: _proc(stdout=_envelope("hello", usage=usage)))
    res = claude_cli.run_claude("sys", "user", "m")
    assert res.text == "hello"
    assert res.input_tokens == 10
    assert res.output_tokens == 20
    assert res.cache_read_tokens == 5
    assert res.cache_write_tokens == 7


def test_run_claude_cache_tokens_default_zero_when_absent(monkeypatch, fake_exe):
    usage = {"input_tokens": 1, "output_tokens": 2}
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda argv, **kw: _proc(stdout=_envelope("hi", usage=usage)))
    res = claude_cli.run_claude("sys", "user", "m")
    assert res.cache_read_tokens == 0
    assert res.cache_write_tokens == 0


def test_run_claude_strips_result_whitespace(monkeypatch, fake_exe):
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda argv, **kw: _proc(stdout=_envelope("  padded  \n")))
    res = claude_cli.run_claude("sys", "user", "m")
    assert res.text == "padded"


# --------------------------------------------------------------------------
# run_claude: error kinds
# --------------------------------------------------------------------------

def test_run_claude_not_found_when_which_returns_none(monkeypatch):
    monkeypatch.setattr(claude_cli, "find_claude", lambda: None)
    with pytest.raises(claude_cli.ClaudeCLIError) as exc_info:
        claude_cli.run_claude("sys", "user", "m")
    assert exc_info.value.kind == "not_found"


def test_run_claude_timeout_expired_raises_timeout_kind(monkeypatch, fake_exe):
    import subprocess as real_subprocess

    def fake_run(argv, **kwargs):
        raise real_subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    with pytest.raises(claude_cli.ClaudeCLIError) as exc_info:
        claude_cli.run_claude("sys", "user", "m", timeout_s=5)
    assert exc_info.value.kind == "timeout"


def test_run_claude_nonzero_exit_with_rate_limit_stderr(monkeypatch, fake_exe):
    monkeypatch.setattr(
        claude_cli.subprocess, "run",
        lambda argv, **kw: _proc(returncode=1, stderr="Error: usage limit reached"))
    with pytest.raises(claude_cli.ClaudeCLIError) as exc_info:
        claude_cli.run_claude("sys", "user", "m")
    assert exc_info.value.kind == "rate_limit"


def test_run_claude_nonzero_exit_with_ordinary_stderr_is_error_kind(monkeypatch, fake_exe):
    monkeypatch.setattr(
        claude_cli.subprocess, "run",
        lambda argv, **kw: _proc(returncode=1, stderr="boom: segfault"))
    with pytest.raises(claude_cli.ClaudeCLIError) as exc_info:
        claude_cli.run_claude("sys", "user", "m")
    assert exc_info.value.kind == "error"


def test_run_claude_garbage_stdout_raises_bad_json(monkeypatch, fake_exe):
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda argv, **kw: _proc(stdout="not json at all {{{"))
    with pytest.raises(claude_cli.ClaudeCLIError) as exc_info:
        claude_cli.run_claude("sys", "user", "m")
    assert exc_info.value.kind == "bad_json"


def test_run_claude_is_error_envelope_rate_limit(monkeypatch, fake_exe):
    monkeypatch.setattr(
        claude_cli.subprocess, "run",
        lambda argv, **kw: _proc(stdout=_envelope("rate limit exceeded", is_error=True)))
    with pytest.raises(claude_cli.ClaudeCLIError) as exc_info:
        claude_cli.run_claude("sys", "user", "m")
    assert exc_info.value.kind == "rate_limit"


def test_run_claude_is_error_envelope_ordinary_error(monkeypatch, fake_exe):
    monkeypatch.setattr(
        claude_cli.subprocess, "run",
        lambda argv, **kw: _proc(stdout=_envelope("something broke", is_error=True)))
    with pytest.raises(claude_cli.ClaudeCLIError) as exc_info:
        claude_cli.run_claude("sys", "user", "m")
    assert exc_info.value.kind == "error"


def test_run_claude_empty_result_raises_error_kind(monkeypatch, fake_exe):
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda argv, **kw: _proc(stdout=_envelope("   ")))
    with pytest.raises(claude_cli.ClaudeCLIError) as exc_info:
        claude_cli.run_claude("sys", "user", "m")
    assert exc_info.value.kind == "error"


# --------------------------------------------------------------------------
# extract_json_text
# --------------------------------------------------------------------------

def test_extract_json_text_plain_json():
    assert json.loads(claude_cli.extract_json_text('{"a": 1}')) == {"a": 1}


def test_extract_json_text_strips_fences():
    text = "```json\n{\"a\": 1}\n```"
    assert json.loads(claude_cli.extract_json_text(text)) == {"a": 1}


def test_extract_json_text_strips_bare_fences():
    text = "```\n{\"a\": 1}\n```"
    assert json.loads(claude_cli.extract_json_text(text)) == {"a": 1}


def test_extract_json_text_prose_wrapped_object():
    text = 'Sure, here is the JSON:\n{"a": 1, "b": [1, 2]}\nHope that helps!'
    assert json.loads(claude_cli.extract_json_text(text)) == {"a": 1, "b": [1, 2]}


def test_extract_json_text_prose_wrapped_array():
    text = 'Here:\n[1, 2, 3]\nDone.'
    assert json.loads(claude_cli.extract_json_text(text)) == [1, 2, 3]


def test_extract_json_text_returns_canonical_json_string():
    out = claude_cli.extract_json_text('{"b": 2, "a": 1}')
    assert isinstance(out, str)
    assert json.loads(out) == {"b": 2, "a": 1}


def test_extract_json_text_invalid_raises_value_error():
    with pytest.raises(ValueError):
        claude_cli.extract_json_text("not json, no braces, nothing to salvage")


# --------------------------------------------------------------------------
# ClaudePool.generate: duck-typed response + json handling
# --------------------------------------------------------------------------

@pytest.fixture
def no_sleep(monkeypatch):
    """Record sleeps instead of actually sleeping (retry tests)."""
    recorded = []

    async def fake_sleep(s):
        recorded.append(s)

    monkeypatch.setattr(claude_cli.asyncio, "sleep", fake_sleep)
    return recorded


def test_pool_generate_returns_duck_typed_resp(monkeypatch):
    def fake_run_claude(system, user, model, *, json_mode=False, allow_websearch=False,
                        timeout_s=claude_cli.DEFAULT_TIMEOUT_S):
        return claude_cli.CLIResult("plain text", 3, 4, 0, 0)

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    pool = claude_cli.ClaudePool(max_procs=2)
    config = SimpleNamespace(system_instruction="sys", response_mime_type=None,
                             response_schema=None)
    resp = asyncio.run(pool.generate(model="claude-sonnet-5", contents="hi", config=config))
    assert resp.text == "plain text"
    assert resp.usage_metadata.prompt_token_count == 3
    assert resp.usage_metadata.candidates_token_count == 4


def test_pool_generate_json_mode_extracts_json(monkeypatch):
    def fake_run_claude(system, user, model, *, json_mode=False, allow_websearch=False,
                        timeout_s=claude_cli.DEFAULT_TIMEOUT_S):
        assert json_mode is True
        return claude_cli.CLIResult('```json\n{"x": 1}\n```', 1, 1, 0, 0)

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    pool = claude_cli.ClaudePool(max_procs=2)
    config = SimpleNamespace(system_instruction="sys", response_mime_type="application/json",
                             response_schema=None)
    resp = asyncio.run(pool.generate(model="m", contents="hi", config=config))
    assert json.loads(resp.text) == {"x": 1}


def test_pool_generate_schema_lands_in_system_prompt_after_instruction(monkeypatch):
    captured = {}

    def fake_run_claude(system, user, model, *, json_mode=False, allow_websearch=False,
                        timeout_s=claude_cli.DEFAULT_TIMEOUT_S):
        captured["system"] = system
        return claude_cli.CLIResult('{"ok": true}', 1, 1, 0, 0)

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    pool = claude_cli.ClaudePool(max_procs=2)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    config = SimpleNamespace(system_instruction="BASE INSTRUCTION",
                             response_mime_type="application/json", response_schema=schema)
    asyncio.run(pool.generate(model="m", contents="hi", config=config))
    system = captured["system"]
    assert system.index("BASE INSTRUCTION") < system.index(json.dumps(schema))


# --------------------------------------------------------------------------
# ClaudePool.generate: retry semantics
# --------------------------------------------------------------------------

def test_pool_generate_rate_limit_retries_then_succeeds(monkeypatch, no_sleep):
    calls = {"n": 0}

    def fake_run_claude(system, user, model, *, json_mode=False, allow_websearch=False,
                        timeout_s=claude_cli.DEFAULT_TIMEOUT_S):
        calls["n"] += 1
        if calls["n"] < 3:
            raise claude_cli.ClaudeCLIError("usage limit reached", kind="rate_limit")
        return claude_cli.CLIResult("ok", 1, 1, 0, 0)

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    pool = claude_cli.ClaudePool(max_procs=2)
    config = SimpleNamespace(system_instruction="sys", response_mime_type=None,
                             response_schema=None)
    resp = asyncio.run(pool.generate(model="m", contents="hi", config=config))
    assert resp.text == "ok"
    assert calls["n"] == 3
    assert len(no_sleep) == 2  # slept between attempt 1->2 and 2->3


def test_pool_generate_transient_exhaustion_raises(monkeypatch, no_sleep):
    def fake_run_claude(system, user, model, *, json_mode=False, allow_websearch=False,
                        timeout_s=claude_cli.DEFAULT_TIMEOUT_S):
        raise claude_cli.ClaudeCLIError("weird 500", kind="error")

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    pool = claude_cli.ClaudePool(max_procs=2)
    config = SimpleNamespace(system_instruction="sys", response_mime_type=None,
                             response_schema=None)
    with pytest.raises(claude_cli.ClaudeCLIError):
        asyncio.run(pool.generate(model="m", contents="hi", config=config))


def test_pool_generate_not_found_never_retries(monkeypatch, no_sleep):
    calls = {"n": 0}

    def fake_run_claude(system, user, model, *, json_mode=False, allow_websearch=False,
                        timeout_s=claude_cli.DEFAULT_TIMEOUT_S):
        calls["n"] += 1
        raise claude_cli.ClaudeCLIError("no cli", kind="not_found")

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    pool = claude_cli.ClaudePool(max_procs=2)
    config = SimpleNamespace(system_instruction="sys", response_mime_type=None,
                             response_schema=None)
    with pytest.raises(claude_cli.ClaudeCLIError) as exc_info:
        asyncio.run(pool.generate(model="m", contents="hi", config=config))
    assert exc_info.value.kind == "not_found"
    assert calls["n"] == 1
    assert no_sleep == []


def test_pool_generate_bad_json_retries_then_raises_claude_cli_error(monkeypatch, no_sleep):
    def fake_run_claude(system, user, model, *, json_mode=False, allow_websearch=False,
                        timeout_s=claude_cli.DEFAULT_TIMEOUT_S):
        return claude_cli.CLIResult("not json at all {{{", 1, 1, 0, 0)

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    pool = claude_cli.ClaudePool(max_procs=2)
    config = SimpleNamespace(system_instruction="sys", response_mime_type="application/json",
                             response_schema=None)
    with pytest.raises(claude_cli.ClaudeCLIError) as exc_info:
        asyncio.run(pool.generate(model="m", contents="hi", config=config))
    assert exc_info.value.kind == "bad_json"


# --------------------------------------------------------------------------
# ClaudePool.stats
# --------------------------------------------------------------------------

def test_pool_stats_keys_and_cache_token_accumulation(monkeypatch):
    def fake_run_claude(system, user, model, *, json_mode=False, allow_websearch=False,
                        timeout_s=claude_cli.DEFAULT_TIMEOUT_S):
        return claude_cli.CLIResult("ok", 1, 1, 5, 9)

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    pool = claude_cli.ClaudePool(max_procs=2)
    config = SimpleNamespace(system_instruction="sys", response_mime_type=None,
                             response_schema=None)
    asyncio.run(pool.generate(model="m", contents="hi", config=config))
    asyncio.run(pool.generate(model="m", contents="hi2", config=config))
    stats = pool.stats()
    assert stats == {
        "free_calls": 0, "vertex_calls": 0, "claude_calls": 2,
        "cache_read_tokens": 10, "cache_write_tokens": 18,
    }


# --------------------------------------------------------------------------
# ClaudePool: warm-up serialization gate
# --------------------------------------------------------------------------

def test_pool_warmup_gate_serializes_same_key_concurrent_calls(monkeypatch):
    """First call for a (model, system) key must complete before any concurrent
    same-key call invokes the CLI at all."""
    in_flight = []
    release_first = asyncio.Event()
    order = []

    async def fake_to_thread(fn, *args, **kwargs):
        # Identify which "call" this is by the user content (2nd positional arg).
        user = args[1]
        order.append(f"start:{user}")
        if user == "first":
            in_flight.append("first")
            await release_first.wait()
            order.append("end:first")
            return claude_cli.CLIResult("first-result", 1, 1, 0, 0)
        order.append(f"end:{user}")
        return claude_cli.CLIResult(f"{user}-result", 1, 1, 0, 0)

    monkeypatch.setattr(claude_cli.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(claude_cli, "run_claude", lambda *a, **k: None)  # unused directly

    pool = claude_cli.ClaudePool(max_procs=4)
    config = SimpleNamespace(system_instruction="SAME SYS", response_mime_type=None,
                             response_schema=None)

    async def scenario():
        first_task = asyncio.create_task(
            pool.generate(model="m", contents="first", config=config))
        await asyncio.sleep(0)  # let first_task start and hit the gate/CLI call
        await asyncio.sleep(0)
        second_task = asyncio.create_task(
            pool.generate(model="m", contents="second", config=config))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Second call must NOT have started the CLI invocation yet -- it is
        # gated behind the first call's completion.
        assert "start:second" not in order
        release_first.set()
        first_resp, second_resp = await asyncio.gather(first_task, second_task)
        return first_resp, second_resp

    first_resp, second_resp = asyncio.run(scenario())
    assert first_resp.text == "first-result"
    assert second_resp.text == "second-result"
    assert order.index("start:first") < order.index("start:second")


def test_pool_warmup_gate_distinct_keys_do_not_block_each_other(monkeypatch):
    release_first = asyncio.Event()
    order = []

    async def fake_to_thread(fn, *args, **kwargs):
        user = args[1]
        order.append(f"start:{user}")
        if user == "first":
            await release_first.wait()
        order.append(f"end:{user}")
        return claude_cli.CLIResult(f"{user}-result", 1, 1, 0, 0)

    monkeypatch.setattr(claude_cli.asyncio, "to_thread", fake_to_thread)
    pool = claude_cli.ClaudePool(max_procs=4)
    config_a = SimpleNamespace(system_instruction="SYSTEM A", response_mime_type=None,
                               response_schema=None)
    config_b = SimpleNamespace(system_instruction="SYSTEM B (different)",
                               response_mime_type=None, response_schema=None)

    async def scenario():
        first_task = asyncio.create_task(
            pool.generate(model="m", contents="first", config=config_a))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # A different (model, system) key must proceed immediately, not wait
        # on the first key's warm-up gate.
        other_resp = await pool.generate(model="m", contents="other", config=config_b)
        assert other_resp.text == "other-result"
        release_first.set()
        await first_task

    asyncio.run(scenario())
    assert order.index("start:other") < order.index("end:first")


def test_pool_warmup_gate_releases_after_first_call_failure(monkeypatch, no_sleep):
    """A failing first call must still release the gate so the second
    same-key call is not stuck forever."""
    calls = {"n": 0}

    def fake_run_claude(system, user, model, *, json_mode=False, allow_websearch=False,
                        timeout_s=claude_cli.DEFAULT_TIMEOUT_S):
        calls["n"] += 1
        if calls["n"] == 1:
            raise claude_cli.ClaudeCLIError("no cli", kind="not_found")
        return claude_cli.CLIResult("second-ok", 1, 1, 0, 0)

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    pool = claude_cli.ClaudePool(max_procs=4)
    config = SimpleNamespace(system_instruction="sys", response_mime_type=None,
                             response_schema=None)

    with pytest.raises(claude_cli.ClaudeCLIError):
        asyncio.run(pool.generate(model="m", contents="first", config=config))

    # Second call with the SAME key must proceed (not hang) after the first
    # call's failure released the gate.
    resp = asyncio.run(pool.generate(model="m", contents="second", config=config))
    assert resp.text == "second-ok"
    assert calls["n"] == 2
