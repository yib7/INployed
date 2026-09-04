"""Tests for the Résumé/tailor Settings section.

The three "Resume" Fields are artifact toggles + a cover-letter tone knob, all
backed by local/config.json (target "config"). Defaults must reproduce today's
behaviour: ATS on, prep on-demand, professional tone. These exercise the schema
+ load/save round-trip against a temp config dir so nothing touches the real
config.json.

Note: `tailor_cover_letter` was removed from the schema in the settings-page
audit — no consumer ever read it; every tailor call site prompts live via
QMessageBox or hardcodes the value instead. The key may still linger
harmlessly in an existing config.json (merge semantics).
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import settings  # noqa: E402


def _targets(tmp_path: Path) -> dict[str, Path]:
    return {"config": tmp_path / "config.json"}


def _by_key() -> dict:
    return {f.key: f for f in settings.SETTINGS_SCHEMA}


def test_resume_fields_exist_with_exact_defaults_and_types():
    by_key = _by_key()
    for key in ("tailor_ats_report", "tailor_prep_sheet", "resume_tone"):
        assert key in by_key, f"missing Resume field {key}"
        assert by_key[key].section == "Resume"
        assert by_key[key].target == "config"

    assert "tailor_cover_letter" not in by_key, (
        "tailor_cover_letter was removed — no consumer read it, the "
        "cover-letter choice is always a live QMessageBox prompt")

    assert by_key["tailor_ats_report"].type == "bool"
    assert by_key["tailor_ats_report"].default is True

    assert by_key["tailor_prep_sheet"].type == "bool"
    assert by_key["tailor_prep_sheet"].default is False


def test_resume_tone_is_choice_with_expected_options():
    tone = _by_key()["resume_tone"]
    assert tone.type == "choice"
    assert tone.default == "professional"
    assert tone.choices == ("professional", "concise", "enthusiastic", "impactful")


def test_load_returns_resume_defaults_on_fresh_config(tmp_path):
    values = settings.load(_targets(tmp_path))
    assert values["tailor_ats_report"] is True
    assert values["tailor_prep_sheet"] is False
    assert values["resume_tone"] == "professional"


def test_save_roundtrips_resume_toggles_and_tone(tmp_path):
    targets = _targets(tmp_path)
    values = settings.load(targets)
    values["resume_tone"] = "concise"
    settings.save(values, targets)

    reloaded = settings.load(targets)
    assert reloaded["resume_tone"] == "concise"
    # untouched toggles keep their defaults
    assert reloaded["tailor_ats_report"] is True
    assert reloaded["tailor_prep_sheet"] is False


def test_validate_rejects_unknown_tone(tmp_path):
    base = settings.load(_targets(tmp_path))
    errors = settings.validate(dict(base, resume_tone="snarky"))
    assert "resume_tone" in errors
    assert settings.validate(base) == {}


# --- SP5: claude provider dropdowns + Claude model fields -----------------------

def _scoring_targets(tmp_path: Path) -> dict[str, Path]:
    return {"config": tmp_path / "config.json", "scoring": tmp_path / "scoring_config.json"}


def test_tailor_provider_field_exists_engine_config_gemini_claude():
    f = _by_key()["tailor_provider"]
    assert f.section == "Engine"
    assert f.target == "config"
    assert f.type == "choice"
    assert f.default == "gemini"
    assert f.choices == ("gemini", "claude")


def test_scoring_provider_field_key_is_literally_provider():
    f = _by_key()["provider"]
    assert f.section == "Scoring"
    assert f.target == "scoring"
    assert f.type == "choice"
    assert f.default == "gemini"
    assert f.choices == ("gemini", "claude")


def test_resume_tailor_claude_model_fields_exist():
    by_key = _by_key()
    for key, default in (
        ("RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", "claude-haiku-4-5"),
        ("RESUME_TAILOR_CLAUDE_MODEL_FLASH", "claude-sonnet-5"),
        ("RESUME_TAILOR_CLAUDE_MODEL_PRO", "claude-opus-5"),
    ):
        assert key in by_key, f"missing Claude tailor model field {key}"
        f = by_key[key]
        assert f.section == "Engine"
        assert f.target == "env"
        assert f.type == "editable_choice"
        assert f.default == default
        assert f.choices == settings.CLAUDE_MODELS


# --- one model for every step: the mode switch and its two "all" rows ------------

def test_the_two_model_mode_fields_are_bounded_dropdowns_defaulting_to_tiers():
    """`choice`, not `editable_choice`, and that is the load-bearing part.

    The four sibling model rows are editable so a model id newer than this repo
    is never blocked. A MODE is the opposite kind of value: there are exactly two
    and `config._model_mode` reads anything else as `tiers`, so a free-text box
    would let the user type a third that looks saved and does nothing. It is also
    what makes the row usable as a GATE — `settings.is_visible` compares against
    `Field.choices`, and `test_every_gate_names_a_real_field_and_real_choices`
    rejects a gate whose allowed values are not in them.
    """
    by_key = _by_key()
    for key in ("RESUME_TAILOR_MODEL_MODE", "RESUME_TAILOR_CLAUDE_MODEL_MODE"):
        assert key in by_key, f"missing tailor model-mode field {key}"
        f = by_key[key]
        assert f.section == "Engine"
        assert f.target == "env"
        assert f.type == "choice"
        assert f.choices == settings.MODEL_MODES == ("tiers", "simple")
        assert f.default == "tiers"      # today's behaviour, unchanged
        assert f.restart is True         # .env, like every other row in this block
        # NOT advanced: the whole point is a user who does not want to think about
        # tiers, and a disclosure toggle is exactly the thing they will not find.
        assert f.advanced is False
        assert "one model" in f.help.lower() and "per stage" in f.help.lower()


def test_the_two_one_model_fields_are_editable_and_gated_on_simple():
    """The single id, one per provider. Editable like every other model picker
    (a new model id must never be blocked), non-advanced so that flipping the
    mode reveals something — a `simple` mode whose only box is folded away behind
    the advanced toggle would be worse than no feature."""
    by_key = _by_key()
    for key, default, choices in (
        ("RESUME_TAILOR_MODEL_ALL", "gemini-3.5-flash", settings.GEMINI_MODELS),
        ("RESUME_TAILOR_CLAUDE_MODEL_ALL", "claude-sonnet-5", settings.CLAUDE_MODELS),
    ):
        assert key in by_key, f"missing one-model field {key}"
        f = by_key[key]
        assert f.section == "Engine"
        assert f.target == "env"
        assert f.type == "editable_choice"
        assert f.default == default
        assert f.default in choices      # restore-defaults lands on a real id
        assert f.choices == choices
        assert f.restart is True
        assert f.advanced is False
        assert f.show_if[1] == ("simple",)


def test_the_mode_gates_the_tier_rows_and_the_provider_gates_the_mode():
    """The gate CHAIN, which is why the mode field exists as a field at all.

    A `Field` has exactly one `show_if`, so a tier row cannot say both "the
    provider is gemini" AND "the mode is tiers". It says the second; the mode row
    says the first; `is_visible` walks the chain. Assert the wiring here and the
    behaviour it buys through `is_visible` — a tier row must be hidden by EITHER
    condition alone."""
    by_key = _by_key()
    for key in ("RESUME_TAILOR_MODEL_FLASH_LITE", "RESUME_TAILOR_MODEL_FLASH",
                "RESUME_TAILOR_MODEL_PRO"):
        assert by_key[key].show_if == ("RESUME_TAILOR_MODEL_MODE", ("tiers",))
    for key in ("RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", "RESUME_TAILOR_CLAUDE_MODEL_FLASH",
                "RESUME_TAILOR_CLAUDE_MODEL_PRO"):
        assert by_key[key].show_if == ("RESUME_TAILOR_CLAUDE_MODEL_MODE", ("tiers",))
    assert by_key["RESUME_TAILOR_MODEL_MODE"].show_if == ("tailor_provider", ("gemini",))
    assert by_key["RESUME_TAILOR_CLAUDE_MODEL_MODE"].show_if == ("tailor_provider", ("claude",))

    pro = by_key["RESUME_TAILOR_MODEL_PRO"]
    one = by_key["RESUME_TAILOR_MODEL_ALL"]
    on_tiers = {"tailor_provider": "gemini", "RESUME_TAILOR_MODEL_MODE": "tiers"}
    on_simple = dict(on_tiers, RESUME_TAILOR_MODEL_MODE="simple")
    wrong_provider = dict(on_tiers, tailor_provider="claude")

    assert settings.is_visible(pro, on_tiers) is True
    assert settings.is_visible(one, on_tiers) is False
    assert settings.is_visible(pro, on_simple) is False       # hidden by the mode
    assert settings.is_visible(one, on_simple) is True
    assert settings.is_visible(pro, wrong_provider) is False  # hidden transitively


def test_the_model_mode_rows_round_trip_to_dotenv(tmp_path):
    """A `target="env"` Field is keyed by the literal env-var name, so the value
    the dropdown writes is the one `config._model_mode` reads back."""
    import envfile

    targets = {"config": tmp_path / "config.json", "env": tmp_path / ".env"}
    values = settings.load(targets)
    assert values["RESUME_TAILOR_MODEL_MODE"] == "tiers"
    values["RESUME_TAILOR_MODEL_MODE"] = "simple"
    values["RESUME_TAILOR_MODEL_ALL"] = "gemini-3.7-flash"
    settings.save(values, targets)

    on_disk = envfile.read(targets["env"])
    assert on_disk["RESUME_TAILOR_MODEL_MODE"] == "simple"
    assert on_disk["RESUME_TAILOR_MODEL_ALL"] == "gemini-3.7-flash"
    assert settings.load(targets)["RESUME_TAILOR_MODEL_ALL"] == "gemini-3.7-flash"


def test_a_mode_outside_the_pair_is_rejected_by_validate():
    """`choice` validation is what stops a hand-edited third mode reaching .env
    through a Save — where `config._model_mode` would read it as `tiers` and the
    row would look saved while doing nothing."""
    assert settings.validate({"RESUME_TAILOR_MODEL_MODE": "simple"}) == {}
    assert "RESUME_TAILOR_MODEL_MODE" in settings.validate(
        {"RESUME_TAILOR_MODEL_MODE": "one"})
    # ...while the one-model box stays open to an id this repo has never heard of.
    assert settings.validate({"RESUME_TAILOR_MODEL_ALL": "gemini-4.2-whatever"}) == {}


def test_scoring_claude_model_fields_exist():
    by_key = _by_key()
    for key, default in (
        ("stage1_model_claude", "claude-haiku-4-5"),
        ("stage2_model_claude", "claude-sonnet-5"),
    ):
        assert key in by_key, f"missing Claude scoring model field {key}"
        f = by_key[key]
        assert f.section == "Scoring"
        assert f.target == "scoring"
        assert f.type == "editable_choice"
        assert f.default == default
        assert f.choices == settings.CLAUDE_MODELS


def test_validate_rejects_unknown_scoring_provider(tmp_path):
    base = settings.load(_scoring_targets(tmp_path))
    errors = settings.validate(dict(base, provider="chatgpt"))
    assert "provider" in errors
    assert settings.validate(base) == {}


def test_validate_rejects_unknown_tailor_provider(tmp_path):
    base = settings.load(_targets(tmp_path))
    errors = settings.validate(dict(base, tailor_provider="chatgpt"))
    assert "tailor_provider" in errors


def test_save_roundtrips_scoring_provider_without_clobbering_other_keys(tmp_path):
    targets = _scoring_targets(tmp_path)
    # simulate a pre-existing scoring_config.json with unrelated keys.
    targets["scoring"].write_text('{"stage1_model": "gemini-3.1-flash-lite", '
                                   '"custom_unknown_key": "keep-me"}', encoding="utf-8")
    values = settings.load(targets)
    values["provider"] = "claude"
    settings.save(values, targets)

    import json
    on_disk = json.loads(targets["scoring"].read_text(encoding="utf-8"))
    assert on_disk["provider"] == "claude"
    assert on_disk["custom_unknown_key"] == "keep-me"
    assert on_disk["stage1_model"] == "gemini-3.1-flash-lite"

    reloaded = settings.load(targets)
    assert reloaded["provider"] == "claude"


def test_save_roundtrips_tailor_provider(tmp_path):
    targets = _targets(tmp_path)
    values = settings.load(targets)
    values["tailor_provider"] = "claude"
    settings.save(values, targets)
    reloaded = settings.load(targets)
    assert reloaded["tailor_provider"] == "claude"
