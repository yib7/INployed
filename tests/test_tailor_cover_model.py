"""The cover letter's own model and Claude effort (cycle 22).

TIER_COVER drafts the letter and TIER_COVER_EDIT runs its repair, humanizer and
style-fix passes. In 'tiers' mode both read one cover-letter model per provider
and, left blank, resolve as before the setting existed (draft on the deep tier,
edits on the standard one). The Claude effort for both can be set on its own;
'same' keeps the general effort. Hermetic: no real CLI or API call.
"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "local"))
sys.path.insert(0, str(REPO / "pipeline"))          # claude_cli.py lives in pipeline/

import claude_cli  # noqa: E402
from resume_tailor import compose, config, coverletter, llm  # noqa: E402

BULLETS = {"a1": "Built the parser"}


@pytest.fixture(autouse=True)
def _no_config_json(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})


# -- models --------------------------------------------------------------------
def test_blank_cover_model_keeps_the_deep_draft_and_standard_edits():
    assert config.claude_model_for(config.TIER_COVER) == config.claude_model_for(config.TIER_PRO)
    assert (config.claude_model_for(config.TIER_COVER_EDIT)
            == config.claude_model_for(config.TIER_FLASH))
    assert config.model_for(config.TIER_COVER) == config.model_for(config.TIER_PRO)
    assert config.model_for(config.TIER_COVER_EDIT) == config.model_for(config.TIER_FLASH)


def test_whitespace_cover_model_reads_as_blank(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_MODEL_COVER", "   ")
    assert config.claude_model_for(config.TIER_COVER) == config.claude_model_for(config.TIER_PRO)


def test_a_named_claude_cover_model_runs_every_cover_pass_and_nothing_else(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_MODEL_COVER", "claude-opus-5-5")
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_MODEL_PRO", "claude-sonnet-5")
    assert config.claude_model_for(config.TIER_COVER) == "claude-opus-5-5"
    assert config.claude_model_for(config.TIER_COVER_EDIT) == "claude-opus-5-5"
    assert config.claude_model_for(config.TIER_PRO) == "claude-sonnet-5"
    assert config.claude_model_for(config.TIER_FLASH) == config.CLAUDE_MODEL_FLASH


def test_a_named_gemini_cover_model_runs_every_cover_pass(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_MODEL_COVER", "gemini-3.1-pro-preview")
    assert config.model_for(config.TIER_COVER) == "gemini-3.1-pro-preview"
    assert config.model_for(config.TIER_COVER_EDIT) == "gemini-3.1-pro-preview"
    assert config.model_for(config.TIER_PRO) == config.MODEL_PRO


def test_simple_mode_one_model_beats_the_cover_model(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_MODEL_MODE", "simple")
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_MODEL_ALL", "claude-haiku-4-5")
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_MODEL_COVER", "claude-opus-5-5")
    assert config.claude_model_for(config.TIER_COVER) == "claude-haiku-4-5"
    assert config.claude_model_for(config.TIER_COVER_EDIT) == "claude-haiku-4-5"


# -- effort --------------------------------------------------------------------
@pytest.mark.parametrize("value", ["", "same", "nonsense"])
def test_cover_effort_same_or_unknown_uses_the_general_effort(monkeypatch, value):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_EFFORT", "medium")
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_EFFORT_COVER", value)
    assert config.claude_effort(config.TIER_COVER) == "medium"
    assert config.claude_effort(config.TIER_COVER_EDIT) == "medium"


def test_cover_effort_applies_only_to_the_cover_tiers(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_EFFORT_COVER", "high")
    assert config.claude_effort(config.TIER_COVER) == "high"
    assert config.claude_effort(config.TIER_COVER_EDIT) == "high"
    assert config.claude_effort(config.TIER_PRO) == config.CLAUDE_EFFORT_DEFAULT
    assert config.claude_effort() == config.CLAUDE_EFFORT_DEFAULT


def test_cover_effort_default_sends_no_level(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_EFFORT_COVER", "default")
    assert config.claude_effort(config.TIER_COVER) == ""


def test_cover_effort_sets_the_cover_time_limits(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_EFFORT_COVER", "high")
    assert config.claude_timeout_schedule(config.TIER_COVER) == \
        config.CLAUDE_TIMEOUTS_BY_EFFORT["high"]
    assert config.claude_timeout_schedule(config.TIER_PRO) == \
        config.CLAUDE_TIMEOUTS_BY_EFFORT["low"]


def test_the_settings_effort_choices_are_same_plus_the_general_ones():
    from settings import SETTINGS_SCHEMA
    by_key = {f.key: f for f in SETTINGS_SCHEMA}
    cover = by_key["RESUME_TAILOR_CLAUDE_EFFORT_COVER"]
    assert cover.default == "same"
    assert cover.choices == ("same",) + by_key["RESUME_TAILOR_CLAUDE_EFFORT"].choices


# -- the Claude call carries the cover tier's model, effort and time limits ------
def test_call_sends_the_cover_model_effort_and_schedule_to_the_cli(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_PROVIDER", "claude")
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_MODEL_COVER", "claude-opus-5-5")
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE_EFFORT_COVER", "high")
    seen: list[dict] = []

    def run_claude(system, user, model, **kwargs):
        seen.append(dict(kwargs, model=model))
        return claude_cli.CLIResult("letter", 1, 2)

    monkeypatch.setattr(llm, "_claude_cli", lambda: types.SimpleNamespace(
        run_claude=run_claude, find_claude=lambda: "claude"))
    assert llm.call("s", "u", config.TIER_COVER) == "letter"
    assert llm.call("s", "u", config.TIER_FLASH) == "letter"
    cover, other = seen
    assert cover["model"] == "claude-opus-5-5"
    assert cover["effort"] == "high"
    assert cover["timeout_s"] == config.CLAUDE_TIMEOUTS_BY_EFFORT["high"][0]
    assert other["model"] == config.CLAUDE_MODEL_FLASH
    assert other["effort"] == config.CLAUDE_EFFORT_DEFAULT
    assert other["timeout_s"] == config.CLAUDE_TIMEOUTS_BY_EFFORT["low"][0]


# -- Gemini pool fallbacks follow the class the pass runs in --------------------
def test_cover_fallbacks_follow_the_resolved_tier(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_FALLBACK_PRO", "deep-a")
    monkeypatch.setenv("RESUME_TAILOR_FALLBACK_FLASH", "std-a")
    assert config.gemini_fallback_models(config.TIER_COVER) == ["deep-a"]
    assert config.gemini_fallback_models(config.TIER_COVER_EDIT) == ["std-a"]
    monkeypatch.setenv("RESUME_TAILOR_MODEL_COVER", "gemini-3.1-pro-preview")
    assert config.gemini_fallback_models(config.TIER_COVER_EDIT) == ["deep-a"]


# -- the cover letter asks for the cover tiers ---------------------------------
def test_generate_body_drafts_on_the_cover_tier(monkeypatch):
    tiers = []
    monkeypatch.setattr(compose, "call",
                        lambda system, user, tier, **k: tiers.append(tier) or "Gen")
    monkeypatch.setattr(coverletter, "refine_body", lambda jt, co, body, b, **k: body)
    monkeypatch.setattr(coverletter, "enforce_body_style", lambda jt, co, body, b, **k: body)
    monkeypatch.setattr(coverletter.assets, "load_master",
                        lambda: {"basics": {"name": "Test User", "location": "NYC"}})
    coverletter.generate_body("jd", "Engineer", "Acme", BULLETS)
    assert tiers == [config.TIER_COVER]


def test_refine_body_edits_on_the_cover_edit_tier(monkeypatch):
    tiers = []
    monkeypatch.setattr(compose, "call",
                        lambda system, user, tier, **k: tiers.append(tier) or "polished")
    coverletter.refine_body("Engineer", "Acme", "rough draft", BULLETS)
    assert tiers == [config.TIER_COVER_EDIT]
