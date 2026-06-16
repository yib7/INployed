"""Backend selection + tier->model resolution for the resume tailor LLM layer.

All Claude transport tests mock subprocess — no real `claude` calls, no network.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import config  # noqa: E402


def test_backend_default_is_vertex(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_BACKEND", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.backend() == "vertex"


def test_backend_from_config_json(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_BACKEND", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"backend": "claude"})
    assert config.backend() == "claude"


def test_backend_env_overrides_config(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "claude")
    monkeypatch.setattr(config, "_config_json", lambda: {"backend": "vertex"})
    assert config.backend() == "claude"


def test_backend_unknown_falls_back_to_vertex(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "gpt5")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.backend() == "vertex"


def test_backend_non_string_config_falls_back_to_vertex(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_BACKEND", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"backend": 123})
    assert config.backend() == "vertex"


def test_model_for_vertex(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "vertex")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.model_for(config.TIER_FLASH_LITE) == config.MODEL_FLASH_LITE
    assert config.model_for(config.TIER_FLASH) == config.MODEL_FLASH
    assert config.model_for(config.TIER_PRO) == config.MODEL_PRO


def test_model_for_claude(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "claude")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.model_for(config.TIER_FLASH_LITE) == config.CLAUDE_HAIKU
    assert config.model_for(config.TIER_FLASH) == config.CLAUDE_SONNET
    # pro maps to sonnet too (no opus tier)
    assert config.model_for(config.TIER_PRO) == config.CLAUDE_SONNET


from resume_tailor import llm  # noqa: E402


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _envelope(result, in_tok=11, out_tok=22, is_error=False):
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": is_error,
        "result": result,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    })


@pytest.fixture(autouse=True)
def _reset_usage():
    llm.reset_usage()
    yield
    llm.reset_usage()


def _use_claude(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "claude")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)


def test_claude_text_call(monkeypatch):
    _use_claude(monkeypatch)
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda *a, **k: _FakeProc(stdout=_envelope("hello world")))
    out = llm.call("sys", "user", config.TIER_FLASH)
    assert out == "hello world"
    assert llm.USAGE and llm.USAGE[0]["model"] == f"claude:{config.CLAUDE_SONNET}"
    assert llm.USAGE[0]["in"] == 11 and llm.USAGE[0]["out"] == 22


def test_claude_json_call_and_system_augment(monkeypatch):
    _use_claude(monkeypatch)
    seen = {}

    def fake_run(argv, **k):
        seen["argv"] = argv
        return _FakeProc(stdout=_envelope('{"score": 5}'))

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    out = llm.call("sys", "user", config.TIER_FLASH, json_out=True)
    assert out == {"score": 5}
    i = seen["argv"].index("--system-prompt")
    assert "ONLY valid JSON" in seen["argv"][i + 1]


def test_claude_websearch_flag_present_with_tools(monkeypatch):
    _use_claude(monkeypatch)
    seen = {}

    def fake_run(argv, **k):
        seen["argv"] = argv
        return _FakeProc(stdout=_envelope("ok"))

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    llm.call("sys", "user", config.TIER_FLASH, tools=[object()])
    assert "--allowedTools" in seen["argv"]
    assert "WebSearch" in seen["argv"]


def test_claude_no_websearch_without_tools(monkeypatch):
    _use_claude(monkeypatch)
    seen = {}

    def fake_run(argv, **k):
        seen["argv"] = argv
        return _FakeProc(stdout=_envelope("ok"))

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    llm.call("sys", "user", config.TIER_FLASH)
    assert "--allowedTools" not in seen["argv"]


def test_claude_missing_cli_raises(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "claude")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(llm.shutil, "which", lambda _: None)
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    with pytest.raises(llm.LLMError):
        llm.call("sys", "user", config.TIER_FLASH)


def test_claude_error_exit_retries_then_raises(monkeypatch):
    _use_claude(monkeypatch)
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda *a, **k: _FakeProc(returncode=1, stderr="boom"))
    with pytest.raises(llm.LLMError):
        llm.call("sys", "user", config.TIER_FLASH)


def test_dispatch_routes_to_vertex_with_resolved_model(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "vertex")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    captured = {}
    monkeypatch.setattr(llm, "_call_vertex",
                        lambda system, user, model, **k: captured.update(model=model) or "V")
    assert llm.call("sys", "user", config.TIER_PRO) == "V"
    assert captured["model"] == config.MODEL_PRO


def test_dispatch_routes_to_claude_with_resolved_model(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "claude")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    captured = {}
    monkeypatch.setattr(llm, "_call_claude",
                        lambda system, user, model, **k: captured.update(model=model) or "C")
    assert llm.call("sys", "user", config.TIER_FLASH_LITE) == "C"
    assert captured["model"] == config.CLAUDE_HAIKU
