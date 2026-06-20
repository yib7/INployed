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
