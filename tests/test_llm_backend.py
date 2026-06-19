"""Backend selection + tier->model resolution + transport for the resume-tailor
LLM layer (gemini / anthropic / openai).

All transport tests fake the provider SDKs (google.genai.Client is monkeypatched;
anthropic / openai are injected into sys.modules) — no real API calls, no network.
"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import config  # noqa: E402
from resume_tailor import llm  # noqa: E402


# ── Backend resolution ───────────────────────────────────────────────────────
def test_backend_default_is_gemini(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_BACKEND", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.backend() == "gemini"


def test_backend_from_config_json(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_BACKEND", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"backend": "anthropic"})
    assert config.backend() == "anthropic"


def test_backend_env_overrides_config(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "openai")
    monkeypatch.setattr(config, "_config_json", lambda: {"backend": "gemini"})
    assert config.backend() == "openai"


def test_backend_legacy_vertex_maps_to_gemini(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "vertex")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.backend() == "gemini"


def test_backend_legacy_claude_maps_to_anthropic(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_BACKEND", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"backend": "claude"})
    assert config.backend() == "anthropic"


def test_backend_unknown_falls_back_to_gemini(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "gpt5")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.backend() == "gemini"


def test_backend_non_string_config_falls_back_to_gemini(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_BACKEND", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"backend": 123})
    assert config.backend() == "gemini"


# ── tier -> model resolution (robust to env-overridable model ids) ───────────
def test_model_for_gemini(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "gemini")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.model_for(config.TIER_FLASH_LITE) == config.MODEL_FLASH_LITE
    assert config.model_for(config.TIER_FLASH) == config.MODEL_FLASH
    assert config.model_for(config.TIER_PRO) == config.MODEL_PRO


def test_model_for_anthropic(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "anthropic")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    for tier in (config.TIER_FLASH_LITE, config.TIER_FLASH, config.TIER_PRO):
        assert config.model_for(tier) == config._ANTHROPIC_TIERS[tier]


def test_model_for_openai(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "openai")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    for tier in (config.TIER_FLASH_LITE, config.TIER_FLASH, config.TIER_PRO):
        assert config.model_for(tier) == config._OPENAI_TIERS[tier]


# ── _extract_json ────────────────────────────────────────────────────────────
def test_extract_json_plain():
    assert llm._extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert llm._extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_embedded_in_prose():
    assert llm._extract_json('Sure! {"a": 1} done') == {"a": 1}


def test_extract_json_invalid_raises():
    with pytest.raises(llm.LLMError):
        llm._extract_json("not json at all")


# ── usage_summary ────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_usage():
    llm.reset_usage()
    yield
    llm.reset_usage()


def test_usage_summary_empty():
    assert llm.usage_summary() == "no LLM calls recorded"


def test_usage_summary_aggregates():
    llm.USAGE.append({"model": "m", "in": 10, "out": 20})
    llm.USAGE.append({"model": "m", "in": 5, "out": 6})
    assert llm.usage_summary() == "m: 2 calls, 15+26 tok"


# ── Dispatch routing (call -> _call_X with the resolved model) ───────────────
def test_call_routes_to_gemini(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "gemini")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    captured = {}
    monkeypatch.setattr(llm, "_call_gemini",
                        lambda system, user, model, **k: captured.update(model=model) or "G")
    assert llm.call("s", "u", config.TIER_PRO) == "G"
    assert captured["model"] == config.MODEL_PRO


def test_call_routes_to_anthropic(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "anthropic")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    captured = {}
    monkeypatch.setattr(llm, "_call_anthropic",
                        lambda system, user, model, **k: captured.update(model=model) or "A")
    assert llm.call("s", "u", config.TIER_FLASH) == "A"
    assert captured["model"] == config._ANTHROPIC_TIERS[config.TIER_FLASH]


def test_call_routes_to_openai(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "openai")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    captured = {}
    monkeypatch.setattr(llm, "_call_openai",
                        lambda system, user, model, **k: captured.update(model=model) or "O")
    assert llm.call("s", "u", config.TIER_FLASH_LITE) == "O"
    assert captured["model"] == config._OPENAI_TIERS[config.TIER_FLASH_LITE]


# ── Transport: Gemini (real google.genai module, fake Client) ────────────────
def _fake_gemini_client(result_text):
    resp = types.SimpleNamespace(
        text=result_text,
        usage_metadata=types.SimpleNamespace(prompt_token_count=10, candidates_token_count=20),
    )
    models = types.SimpleNamespace(generate_content=lambda model, contents, config: resp)

    class _Client:
        def __init__(self, **kwargs):
            self.models = models

    return _Client


def test_gemini_text_call(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr("google.genai.Client", _fake_gemini_client("hello world"))
    out = llm.call("sys", "user", config.TIER_FLASH)
    assert out == "hello world"
    assert llm.USAGE and llm.USAGE[0] == {"model": config.MODEL_FLASH, "in": 10, "out": 20}


def test_gemini_json_call(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr("google.genai.Client", _fake_gemini_client('{"score": 5}'))
    assert llm.call("sys", "user", config.TIER_FLASH, json_out=True) == {"score": 5}


def test_gemini_missing_key_and_project_raises(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(config, "GCP_PROJECT", "")
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    with pytest.raises(llm.LLMError):
        llm.call("sys", "user", config.TIER_FLASH)


# ── Transport: Anthropic (fake 'anthropic' module in sys.modules) ────────────
def _install_fake_anthropic(monkeypatch, result_text):
    mod = types.ModuleType("anthropic")
    resp = types.SimpleNamespace(
        content=[types.SimpleNamespace(text=result_text)],
        usage=types.SimpleNamespace(input_tokens=11, output_tokens=22),
    )
    messages = types.SimpleNamespace(create=lambda **k: resp)

    class _Anthropic:
        def __init__(self, api_key=None):
            self.messages = messages

    mod.Anthropic = _Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", mod)


def test_anthropic_text_call_records_usage(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    _install_fake_anthropic(monkeypatch, "hi there")
    out = llm.call("sys", "user", config.TIER_FLASH)
    assert out == "hi there"
    assert llm.USAGE[0] == {"model": config._ANTHROPIC_TIERS[config.TIER_FLASH], "in": 11, "out": 22}


def test_anthropic_json_call(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    _install_fake_anthropic(monkeypatch, '{"ok": true}')
    assert llm.call("sys", "user", config.TIER_PRO, json_out=True) == {"ok": True}


def test_anthropic_missing_key_raises(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    _install_fake_anthropic(monkeypatch, "unused")
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm.call("sys", "user", config.TIER_FLASH)


# ── Transport: OpenAI (fake 'openai' module in sys.modules) ──────────────────
def _install_fake_openai(monkeypatch, result_text):
    mod = types.ModuleType("openai")
    resp = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=result_text))],
        usage=types.SimpleNamespace(prompt_tokens=7, completion_tokens=8),
    )
    completions = types.SimpleNamespace(create=lambda **k: resp)
    chat = types.SimpleNamespace(completions=completions)

    class _OpenAI:
        def __init__(self, api_key=None):
            self.chat = chat

    mod.OpenAI = _OpenAI
    monkeypatch.setitem(sys.modules, "openai", mod)


def test_openai_text_call_records_usage(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    _install_fake_openai(monkeypatch, "yo")
    out = llm.call("sys", "user", config.TIER_FLASH)
    assert out == "yo"
    assert llm.USAGE[0] == {"model": config._OPENAI_TIERS[config.TIER_FLASH], "in": 7, "out": 8}


def test_openai_missing_key_raises(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    _install_fake_openai(monkeypatch, "unused")
    with pytest.raises(llm.LLMError, match="OPENAI_API_KEY"):
        llm.call("sys", "user", config.TIER_FLASH)


def test_anthropic_retries_then_raises(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BACKEND", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    mod = types.ModuleType("anthropic")

    def _boom(**_k):
        raise RuntimeError("transient")

    class _Anthropic:
        def __init__(self, api_key=None):
            self.messages = types.SimpleNamespace(create=_boom)

    mod.Anthropic = _Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    with pytest.raises(llm.LLMError, match="Anthropic call failed"):
        llm.call("sys", "user", config.TIER_FLASH)
