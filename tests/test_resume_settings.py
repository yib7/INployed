"""Tests for the Résumé/tailor Settings section (SP3 T3.4).

The four "Resume" Fields are artifact toggles + a cover-letter tone knob, all
backed by local/config.json (target "config"). Defaults must reproduce today's
behaviour: cover letter off, ATS on, prep on-demand, professional tone. These
exercise the schema + load/save round-trip against a temp config dir so nothing
touches the real config.json.
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
    for key in ("tailor_cover_letter", "tailor_ats_report",
                "tailor_prep_sheet", "resume_tone"):
        assert key in by_key, f"missing Resume field {key}"
        assert by_key[key].section == "Resume"
        assert by_key[key].target == "config"

    assert by_key["tailor_cover_letter"].type == "bool"
    assert by_key["tailor_cover_letter"].default is False

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
    assert values["tailor_cover_letter"] is False
    assert values["tailor_ats_report"] is True
    assert values["tailor_prep_sheet"] is False
    assert values["resume_tone"] == "professional"


def test_save_roundtrips_resume_toggles_and_tone(tmp_path):
    targets = _targets(tmp_path)
    values = settings.load(targets)
    values["tailor_cover_letter"] = True
    values["resume_tone"] = "concise"
    settings.save(values, targets)

    reloaded = settings.load(targets)
    assert reloaded["tailor_cover_letter"] is True
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
        ("RESUME_TAILOR_CLAUDE_MODEL_PRO", "claude-opus-4-8"),
    ):
        assert key in by_key, f"missing Claude tailor model field {key}"
        f = by_key[key]
        assert f.section == "Engine"
        assert f.target == "env"
        assert f.type == "editable_choice"
        assert f.default == default
        assert f.choices == settings.CLAUDE_MODELS


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
