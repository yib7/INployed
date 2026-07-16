"""Gemini auth-mode selection + tier->model resolution + transport for the
resume-tailor LLM layer. All transport tests fake google.genai.Client -- no
real API calls, no network.
"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import config  # noqa: E402
from resume_tailor import llm  # noqa: E402


# -- gemini_auth resolution ---------------------------------------------------
def test_gemini_auth_default_is_vertex(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_GEMINI_AUTH", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.gemini_auth() == "vertex"


def test_gemini_auth_from_config_json(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_GEMINI_AUTH", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"gemini_auth": "api_key"})
    assert config.gemini_auth() == "api_key"


def test_gemini_auth_env_overrides_config(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_GEMINI_AUTH", "api_key")
    monkeypatch.setattr(config, "_config_json", lambda: {"gemini_auth": "vertex"})
    assert config.gemini_auth() == "api_key"


def test_gemini_auth_unknown_falls_back_to_vertex(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_GEMINI_AUTH", "weird")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.gemini_auth() == "vertex"


# -- tier -> model resolution -------------------------------------------------
def test_model_for_returns_gemini_tier(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.model_for(config.TIER_FLASH_LITE) == config.MODEL_FLASH_LITE
    assert config.model_for(config.TIER_FLASH) == config.MODEL_FLASH
    assert config.model_for(config.TIER_PRO) == config.MODEL_PRO


def test_model_defaults_are_upgraded():
    assert config.MODEL_FLASH_LITE == "gemini-3.1-flash-lite"
    assert config.MODEL_FLASH == "gemini-3.5-flash"
    assert config.MODEL_PRO == "gemini-3.5-flash"


# P2-13: RESUME_TAILOR_MODEL_* used to be read only at import (module-level
# os.getenv), so a Settings-written .env change was invisible to a running
# dashboard until restart. model_for() now resolves the env fresh on every
# call, like gemini_auth() already does -- so a mid-process env change is
# picked up immediately, with no code change/ripple into llm.py or Settings.
def test_model_for_flash_lite_picks_up_env_change_live(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_MODEL_FLASH_LITE", "gemini-9.9-custom")
    assert config.model_for(config.TIER_FLASH_LITE) == "gemini-9.9-custom"


def test_model_for_flash_picks_up_env_change_live(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_MODEL_FLASH", "gemini-9.9-custom")
    assert config.model_for(config.TIER_FLASH) == "gemini-9.9-custom"


def test_model_for_pro_picks_up_env_change_live(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_MODEL_PRO", "gemini-9.9-custom")
    assert config.model_for(config.TIER_PRO) == "gemini-9.9-custom"


# -- _extract_json ------------------------------------------------------------
def test_extract_json_plain():
    assert llm._extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert llm._extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_embedded_in_prose():
    assert llm._extract_json('Sure! {"a": 1} done') == {"a": 1}


def test_extract_json_invalid_raises():
    with pytest.raises(llm.LLMError) as ei:
        llm._extract_json("not json at all")
    # Deliberately raised `from None`: the LLMError message already embeds the
    # offending payload, so the internal JSONDecodeError chain is suppressed to
    # keep the user-facing error clean.
    assert ei.value.__suppress_context__ is True
    assert ei.value.__cause__ is None


# -- usage_summary ------------------------------------------------------------
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


# -- Dispatch routing ---------------------------------------------------------
def test_call_routes_to_gemini(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})
    captured = {}
    monkeypatch.setattr(llm, "_call_gemini",
                        lambda system, user, model, **k: captured.update(model=model) or "G")
    assert llm.call("s", "u", config.TIER_PRO) == "G"
    assert captured["model"] == config.MODEL_PRO


# -- Transport: Gemini (real google.genai module, fake Client) ----------------
def _fake_gemini_client(result_text, record=None):
    resp = types.SimpleNamespace(
        text=result_text,
        usage_metadata=types.SimpleNamespace(prompt_token_count=10, candidates_token_count=20),
    )
    models = types.SimpleNamespace(generate_content=lambda model, contents, config: resp)

    class _Client:
        def __init__(self, **kwargs):
            if record is not None:
                record.update(kwargs)
            self.models = models

    return _Client


def test_gemini_vertex_mode_uses_project(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_GEMINI_AUTH", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(config, "GCP_PROJECT", "proj")
    rec = {}
    monkeypatch.setattr("google.genai.Client", _fake_gemini_client("hello", rec))
    out = llm.call("sys", "user", config.TIER_FLASH)
    assert out == "hello"
    assert rec.get("vertexai") is True and rec.get("project") == "proj"
    assert llm.USAGE and llm.USAGE[0] == {"model": config.MODEL_FLASH, "in": 10, "out": 20}


def test_gemini_api_key_mode_uses_dedicated_key(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"gemini_auth": "api_key"})
    monkeypatch.setenv("RESUME_TAILOR_GEMINI_API_KEY", "tailor-key")
    monkeypatch.setenv("GEMINI_API_KEY", "pool-key-ignored")
    rec = {}
    monkeypatch.setattr("google.genai.Client", _fake_gemini_client("ok", rec))
    assert llm.call("sys", "user", config.TIER_FLASH) == "ok"
    assert rec.get("api_key") == "tailor-key"
    assert "vertexai" not in rec


def test_vertex_mode_missing_project_raises(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_GEMINI_AUTH", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(config, "GCP_PROJECT", "")
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    with pytest.raises(llm.LLMError):
        llm.call("sys", "user", config.TIER_FLASH)


def test_api_key_mode_missing_key_raises(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"gemini_auth": "api_key"})
    monkeypatch.delenv("RESUME_TAILOR_GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    with pytest.raises(llm.LLMError):
        llm.call("sys", "user", config.TIER_FLASH)
