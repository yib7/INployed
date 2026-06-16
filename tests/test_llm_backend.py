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
