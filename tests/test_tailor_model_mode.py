"""One model for every step: RESUME_TAILOR_MODEL_MODE / _CLAUDE_MODEL_MODE.

`config.model_for(tier)` and `config.claude_model_for(tier)` map flash_lite /
flash / pro onto three env vars each. That split is a cost-tuning knob and a
leaky abstraction for someone who just wants ONE model everywhere -- today that
means setting three vars consistently, per provider, and knowing what "pro"
buys. `simple` mode collapses all three onto a single id.

Both providers live here rather than in test_llm_backend.py (Gemini) and
test_llm_claude_backend.py (Claude), because the whole design claim is that the
two sides behave IDENTICALLY and INDEPENDENTLY -- a claim that is only visible
with both in front of you.

Nothing here calls an API: every assertion is a pure env-var -> model-id
resolution, which is all `model_for` does.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import settings  # noqa: E402
from resume_tailor import config  # noqa: E402

_TIERS = (config.TIER_FLASH_LITE, config.TIER_FLASH, config.TIER_PRO)

# The shipped tier maps, spelled out rather than read back out of `config._TIER_ENV`:
# comparing the resolver against the table it reads would pass with the table empty.
_GEMINI_TIER_DEFAULTS = {
    config.TIER_FLASH_LITE: "gemini-3.1-flash-lite",
    config.TIER_FLASH: "gemini-3.5-flash",
    config.TIER_PRO: "gemini-3.5-flash",
}
_CLAUDE_TIER_DEFAULTS = {
    config.TIER_FLASH_LITE: "claude-haiku-4-5",
    config.TIER_FLASH: "claude-sonnet-5",
    config.TIER_PRO: "claude-opus-5",
}

# (mode env var, "all" env var, resolver, shipped tier map) -- every test that has
# nothing provider-specific to say is parametrized over this pair, so neither side
# can grow behaviour the other lacks without a failure here.
_GEMINI = pytest.param(
    config.MODEL_MODE_ENV, config.MODEL_ALL_ENV, config.model_for,
    config.model_mode, _GEMINI_TIER_DEFAULTS, "gemini-9.9-one-model", id="gemini")
_CLAUDE = pytest.param(
    config.CLAUDE_MODEL_MODE_ENV, config.CLAUDE_MODEL_ALL_ENV, config.claude_model_for,
    config.claude_model_mode, _CLAUDE_TIER_DEFAULTS, "claude-99-one-model", id="claude")
_BOTH = pytest.mark.parametrize(
    "mode_env,all_env,resolve,mode_of,tier_defaults,one_model", [_GEMINI, _CLAUDE])


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    """No model var set at all, so every test starts from the shipped defaults.

    conftest scrubs the four new vars process-wide already; this also clears the
    six TIER vars, which it deliberately does not, so a developer's exported
    `RESUME_TAILOR_MODEL_PRO` cannot make a fallback assertion here pass or fail
    for the wrong reason. The module CONSTANTS are import-time and out of reach
    either way -- `test_model_defaults_are_upgraded` in test_llm_backend.py is
    what pins those.
    """
    for var in (config.MODEL_MODE_ENV, config.MODEL_ALL_ENV,
                config.CLAUDE_MODEL_MODE_ENV, config.CLAUDE_MODEL_ALL_ENV,
                "RESUME_TAILOR_MODEL_FLASH_LITE", "RESUME_TAILOR_MODEL_FLASH",
                "RESUME_TAILOR_MODEL_PRO",
                "RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", "RESUME_TAILOR_CLAUDE_MODEL_FLASH",
                "RESUME_TAILOR_CLAUDE_MODEL_PRO"):
        monkeypatch.delenv(var, raising=False)


# --- the compatibility guarantee ------------------------------------------------

@_BOTH
def test_the_default_is_tiers_and_every_tier_resolves_as_it_always_did(
        mode_env, all_env, resolve, mode_of, tier_defaults, one_model):
    """THE contract of this feature: an install that never touches the new
    setting resolves exactly the model it resolved before the setting existed.

    Both halves matter. `mode_of() == 'tiers'` with nothing set is the default;
    the three tier assertions are what makes "default" mean "unchanged" rather
    than merely "named tiers"."""
    assert mode_of() == config.MODEL_MODE_TIERS
    for tier in _TIERS:
        assert resolve(tier) == tier_defaults[tier]


@_BOTH
def test_tiers_mode_still_honours_a_per_tier_override(
        mode_env, all_env, resolve, mode_of, tier_defaults, one_model, monkeypatch):
    """...and the per-stage env vars keep working, with the mode set explicitly.
    An "all" value sitting in .env from an earlier experiment must not leak into
    `tiers` mode either."""
    monkeypatch.setenv(mode_env, config.MODEL_MODE_TIERS)
    monkeypatch.setenv(all_env, one_model)
    prefix = "RESUME_TAILOR_MODEL" if mode_env == config.MODEL_MODE_ENV \
        else "RESUME_TAILOR_CLAUDE_MODEL"
    monkeypatch.setenv(f"{prefix}_PRO", "per-tier-custom")
    assert resolve(config.TIER_PRO) == "per-tier-custom"
    assert resolve(config.TIER_FLASH) == tier_defaults[config.TIER_FLASH]


# --- simple mode ----------------------------------------------------------------

@_BOTH
def test_simple_mode_uses_the_one_model_for_every_tier(
        mode_env, all_env, resolve, mode_of, tier_defaults, one_model, monkeypatch):
    """The feature in one assertion: three tiers, one answer."""
    monkeypatch.setenv(mode_env, config.MODEL_MODE_SIMPLE)
    monkeypatch.setenv(all_env, one_model)
    assert mode_of() == config.MODEL_MODE_SIMPLE
    assert {resolve(tier) for tier in _TIERS} == {one_model}


@_BOTH
def test_simple_mode_beats_a_per_tier_override(
        mode_env, all_env, resolve, mode_of, tier_defaults, one_model, monkeypatch):
    """The tier vars are not merged with the one model, they are bypassed. A user
    who switched to `simple` and left three old per-stage ids in .env must get the
    one model, not a silent mix."""
    monkeypatch.setenv(mode_env, config.MODEL_MODE_SIMPLE)
    monkeypatch.setenv(all_env, one_model)
    prefix = "RESUME_TAILOR_MODEL" if mode_env == config.MODEL_MODE_ENV \
        else "RESUME_TAILOR_CLAUDE_MODEL"
    monkeypatch.setenv(f"{prefix}_PRO", "per-tier-custom")
    assert resolve(config.TIER_PRO) == one_model


@_BOTH
def test_simple_mode_answers_an_unknown_tier_too(
        mode_env, all_env, resolve, mode_of, tier_defaults, one_model, monkeypatch):
    """The unknown-tier arm resolves to the flash model in `tiers` mode. In
    `simple` mode there is only one model, so it answers there as well -- the
    short-circuit sits in front of the whole tier table, including its fallback."""
    assert resolve("not_a_tier") == tier_defaults[config.TIER_FLASH]
    monkeypatch.setenv(mode_env, config.MODEL_MODE_SIMPLE)
    monkeypatch.setenv(all_env, one_model)
    assert resolve("not_a_tier") == one_model


@_BOTH
@pytest.mark.parametrize("mode", ["simple", "SIMPLE", "  Simple  "])
def test_the_mode_string_is_normalised_like_every_other_resolver(
        mode_env, all_env, resolve, mode_of, tier_defaults, one_model, monkeypatch, mode):
    """strip + lower, matching `tailor_provider()` / `projects_mode()`. A value
    typed into .env by hand carries whatever case and spacing the user typed."""
    monkeypatch.setenv(mode_env, mode)
    monkeypatch.setenv(all_env, one_model)
    assert mode_of() == config.MODEL_MODE_SIMPLE
    assert resolve(config.TIER_PRO) == one_model


@_BOTH
def test_the_one_model_id_is_stripped(
        mode_env, all_env, resolve, mode_of, tier_defaults, one_model, monkeypatch):
    """A trailing space after a hand-typed .env value must not reach the API as
    part of the model id."""
    monkeypatch.setenv(mode_env, config.MODEL_MODE_SIMPLE)
    monkeypatch.setenv(all_env, f"  {one_model}  ")
    assert resolve(config.TIER_FLASH) == one_model


# --- the failure arms: never return "" ------------------------------------------

@_BOTH
@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_one_model_falls_back_to_the_tier_map(
        mode_env, all_env, resolve, mode_of, tier_defaults, one_model, monkeypatch, blank):
    """`simple` with nothing to be simple ABOUT. Returning the blank would push
    an empty model id into the API call -- an opaque error two layers from the
    setting that caused it. Doing what the install already did is the safe
    failure, and it is silent on purpose: there is nothing for the user to fix
    that the Settings row does not already say."""
    monkeypatch.setenv(mode_env, config.MODEL_MODE_SIMPLE)
    monkeypatch.setenv(all_env, blank)
    for tier in _TIERS:
        assert resolve(tier) == tier_defaults[tier]


@_BOTH
def test_an_unset_one_model_falls_back_to_the_tier_map(
        mode_env, all_env, resolve, mode_of, tier_defaults, one_model, monkeypatch):
    """The same arm reached the other way: mode flipped, the id never written."""
    monkeypatch.setenv(mode_env, config.MODEL_MODE_SIMPLE)
    monkeypatch.delenv(all_env, raising=False)
    for tier in _TIERS:
        assert resolve(tier) == tier_defaults[tier]


@_BOTH
@pytest.mark.parametrize("bogus", ["tier", "Tiers ", "one", "SIMPLE_MODE", "1", "  "])
def test_an_unrecognised_mode_reads_as_tiers(
        mode_env, all_env, resolve, mode_of, tier_defaults, one_model, monkeypatch, bogus):
    """A typo must not silently route every stage through the one-model id, and
    must not blow up either. It reads as `tiers` -- the direction that changes
    nothing. ("Tiers " is here because normalisation makes it VALID: the case
    that would fail a naive `== 'tiers'` check.)"""
    monkeypatch.setenv(mode_env, bogus)
    monkeypatch.setenv(all_env, one_model)
    assert mode_of() == config.MODEL_MODE_TIERS
    for tier in _TIERS:
        assert resolve(tier) == tier_defaults[tier]


# --- live resolution ------------------------------------------------------------

@_BOTH
def test_the_mode_is_read_live_not_frozen_at_import(
        mode_env, all_env, resolve, mode_of, tier_defaults, one_model, monkeypatch):
    """Two calls in ONE process, no reload between them, different answers.

    The tier vars gained this in P2-13 and the mode has to match: `llm.call`
    resolves the model per call, and the dashboard tailors in-process, so a mode
    frozen at import would need a relaunch that the tier vars no longer do.
    (What a restart IS still needed for is a .env EDIT -- os.environ is a startup
    snapshot. Different problem, same as every other .env key.)"""
    assert resolve(config.TIER_PRO) == tier_defaults[config.TIER_PRO]
    monkeypatch.setenv(mode_env, config.MODEL_MODE_SIMPLE)
    monkeypatch.setenv(all_env, one_model)
    assert resolve(config.TIER_PRO) == one_model
    monkeypatch.setenv(all_env, "second-model")
    assert resolve(config.TIER_PRO) == "second-model"
    monkeypatch.setenv(mode_env, config.MODEL_MODE_TIERS)
    assert resolve(config.TIER_PRO) == tier_defaults[config.TIER_PRO]


# --- the two providers are independent ------------------------------------------

def test_the_gemini_mode_does_not_touch_the_claude_models(monkeypatch):
    """Two mode vars, not one. A user running Claude on one model and Gemini on
    the tuned split is the reason -- and the Settings tab only ever shows the
    mode row for the provider in use, so a shared key would be edited blind."""
    monkeypatch.setenv(config.MODEL_MODE_ENV, config.MODEL_MODE_SIMPLE)
    monkeypatch.setenv(config.MODEL_ALL_ENV, "gemini-9.9-one-model")
    assert config.model_for(config.TIER_PRO) == "gemini-9.9-one-model"
    assert config.claude_model_mode() == config.MODEL_MODE_TIERS
    for tier in _TIERS:
        assert config.claude_model_for(tier) == _CLAUDE_TIER_DEFAULTS[tier]


def test_the_claude_mode_does_not_touch_the_gemini_models(monkeypatch):
    monkeypatch.setenv(config.CLAUDE_MODEL_MODE_ENV, config.MODEL_MODE_SIMPLE)
    monkeypatch.setenv(config.CLAUDE_MODEL_ALL_ENV, "claude-99-one-model")
    assert config.claude_model_for(config.TIER_PRO) == "claude-99-one-model"
    assert config.model_mode() == config.MODEL_MODE_TIERS
    for tier in _TIERS:
        assert config.model_for(tier) == _GEMINI_TIER_DEFAULTS[tier]


# --- the cross-module string coupling -------------------------------------------

def test_the_settings_dropdown_offers_exactly_the_two_modes_config_understands():
    """`settings.MODEL_MODES` is what the dropdown writes into .env and
    `config.MODEL_MODES` is what reads it back. A drift between them is invisible
    at runtime: config's unknown-mode arm would read the new string as `tiers`,
    so the row would look saved and do nothing -- the same failure shape the
    ARCHIVE_KEEP_ALL / PRUNE_OFF pin exists to stop."""
    assert settings.MODEL_MODES == config.MODEL_MODES == ("tiers", "simple")
    assert settings.MODEL_MODE_TIERS == config.MODEL_MODE_TIERS
    assert settings.MODEL_MODE_SIMPLE == config.MODEL_MODE_SIMPLE


def test_the_four_env_names_are_the_keys_the_settings_schema_writes():
    """The other half of the coupling: the Settings rows are keyed by the literal
    env-var name (that is what a `target="env"` Field means), so a rename in
    either file that is not made in both leaves a dropdown writing a key nothing
    reads."""
    keys = {f.key for f in settings.SETTINGS_SCHEMA if f.target == "env"}
    assert {config.MODEL_MODE_ENV, config.MODEL_ALL_ENV,
            config.CLAUDE_MODEL_MODE_ENV, config.CLAUDE_MODEL_ALL_ENV} <= keys
