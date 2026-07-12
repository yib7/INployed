"""Claude provider config — tier->model resolution + timeout schedule (SP2).

Mirror of test_llm_backend.py: tailor_provider(), claude_model_for(), and
claude_timeout_schedule() resolve live from env > config.json > defaults.
All tests hermetic: no real Claude CLI invocation, no network.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import config  # noqa: E402


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
