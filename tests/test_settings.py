"""Tests for local/settings.py — the central user-settings layer.

These exercise load/validate/save against a temp config dir (via the `targets`
override), so nothing touches the real local/config.json.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import settings  # noqa: E402


def _targets(tmp_path: Path) -> dict[str, Path]:
    return {"config": tmp_path / "config.json"}


def test_load_returns_defaults_when_file_absent(tmp_path):
    """With no config.json, load() yields every schema Field's default."""
    values = settings.load(_targets(tmp_path))
    assert values["min_score"] == 4
    assert values["followup_days"] == 5
    assert values["gdrive_root"] == ""
    assert values["mtime_stable_seconds"] == 30


def test_save_then_load_roundtrips_changed_value(tmp_path):
    targets = _targets(tmp_path)
    values = settings.load(targets)
    values["min_score"] = 3
    settings.save(values, targets)
    assert settings.load(targets)["min_score"] == 3


def test_save_preserves_unrelated_preexisting_keys(tmp_path):
    targets = _targets(tmp_path)
    cfg = targets["config"]
    cfg.write_text(
        json.dumps({"resume_layout": {"Globex": {"line_targets": [2, 2]}}, "min_score": 4}),
        encoding="utf-8",
    )
    values = settings.load(targets)
    values["min_score"] = 2
    settings.save(values, targets)
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk["min_score"] == 2
    assert on_disk["resume_layout"] == {"Globex": {"line_targets": [2, 2]}}


def test_save_creates_bak_when_overwriting(tmp_path):
    targets = _targets(tmp_path)
    cfg = targets["config"]
    cfg.write_text(json.dumps({"min_score": 4}), encoding="utf-8")
    values = settings.load(targets)
    values["min_score"] = 5
    settings.save(values, targets)
    bak = cfg.with_name(cfg.name + ".bak")
    assert bak.exists()
    assert json.loads(bak.read_text(encoding="utf-8"))["min_score"] == 4


def test_validate_rejects_out_of_range_and_wrong_type(tmp_path):
    base = settings.load(_targets(tmp_path))

    bad_range = dict(base, min_score=9)
    errors = settings.validate(bad_range)
    assert "min_score" in errors

    bad_type = dict(base, min_score="not-an-int")
    errors = settings.validate(bad_type)
    assert "min_score" in errors

    assert settings.validate(base) == {}


def test_save_raises_on_invalid(tmp_path):
    targets = _targets(tmp_path)
    values = settings.load(targets)
    values["min_score"] = 99
    with pytest.raises(ValueError):
        settings.save(values, targets)


# --- new Scraper / Scoring targets and the "list" field type -------------------

def _all_targets(tmp_path: Path) -> dict[str, Path]:
    return {
        "config": tmp_path / "config.json",
        "search": tmp_path / "search_config.json",
        "scoring": tmp_path / "scoring_config.json",
    }


def test_scraper_and_scoring_targets_registered():
    assert "search" in settings.TARGET_FILES
    assert "scoring" in settings.TARGET_FILES
    # they point at the repo ROOT (settings.py lives in local/)
    root = settings.HERE.parent
    assert settings.TARGET_FILES["search"] == root / "search_config.json"
    assert settings.TARGET_FILES["scoring"] == root / "scoring_config.json"


def test_apply_section_removed_from_schema():
    # Apply-form answers now live solely in the Apply Answers tab (apply_answers.json),
    # so the dead "Apply" section / "apply" target must not appear in the schema.
    assert not any(f.section == "Apply" for f in settings.SETTINGS_SCHEMA)
    assert not any(f.target == "apply" for f in settings.SETTINGS_SCHEMA)


def test_list_type_validate_accepts_list_of_str():
    by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
    assert "keywords" in by_key and by_key["keywords"].type == "list"
    assert settings.validate({"keywords": ['"Data Scientist"', '"AI Engineer"']}) == {}


def test_list_type_validate_rejects_non_list_and_non_str_items():
    assert "keywords" in settings.validate({"keywords": "not a list"})
    assert "keywords" in settings.validate({"keywords": ["ok", 5]})


def test_list_field_save_roundtrips_to_search_target(tmp_path):
    targets = _all_targets(tmp_path)
    values = settings.load(targets)
    values["keywords"] = ['"Foo"', '"Bär"']  # non-ASCII keyword
    settings.save(values, targets)
    on_disk = json.loads(targets["search"].read_text(encoding="utf-8"))
    assert on_disk["keywords"] == ['"Foo"', '"Bär"']
    # ensure_ascii=False keeps the non-ASCII char literal (not \uXXXX-escaped)
    assert "Bär" in targets["search"].read_text(encoding="utf-8")
    assert settings.load(targets)["keywords"] == ['"Foo"', '"Bär"']


# --- env target (secrets / paths in .env), choice + multichoice ---------------

def _all_targets_env(tmp_path: Path) -> dict[str, Path]:
    return {
        "config": tmp_path / "config.json",
        "search": tmp_path / "search_config.json",
        "scoring": tmp_path / "scoring_config.json",
        "apply": tmp_path / "apply_config.json",
        "env": tmp_path / ".env",
    }


def test_env_target_registered_points_at_repo_dotenv():
    assert "env" in settings.TARGET_FILES
    assert settings.TARGET_FILES["env"] == settings.HERE.parent / ".env"


def test_env_field_load_save_roundtrips_to_dotenv(tmp_path):
    targets = _all_targets_env(tmp_path)
    assert settings.load(targets)["RESUME_TAILOR_CANDIDATE"] == "Your_Name"  # default
    settings.save({"RESUME_TAILOR_CANDIDATE": "Ada_Lovelace"}, targets)
    assert "RESUME_TAILOR_CANDIDATE=Ada_Lovelace" in (tmp_path / ".env").read_text("utf-8")
    assert settings.load(targets)["RESUME_TAILOR_CANDIDATE"] == "Ada_Lovelace"


def test_env_secret_save_writes_only_to_dotenv(tmp_path):
    targets = _all_targets_env(tmp_path)
    settings.save({"GEMINI_API_KEYS": "k1,k2"}, targets)
    assert "GEMINI_API_KEYS=k1,k2" in (tmp_path / ".env").read_text("utf-8")
    assert settings.secret_status(targets)["GEMINI_API_KEYS"] is True


def test_secret_status_reports_set_and_unset(tmp_path):
    targets = _all_targets_env(tmp_path)
    (tmp_path / ".env").write_text("BRIGHT_DATA_API_TOKEN=tok\n", encoding="utf-8")
    status = settings.secret_status(targets)
    assert status["BRIGHT_DATA_API_TOKEN"] is True
    assert status["GEMINI_API_KEYS"] is False  # absent from .env


def test_omitting_secret_key_preserves_existing_value(tmp_path):
    """The form omits a blank secret box; saving other keys must not wipe it."""
    targets = _all_targets_env(tmp_path)
    (tmp_path / ".env").write_text("BRIGHT_DATA_API_TOKEN=existing\n", encoding="utf-8")
    settings.save({"RESUME_TAILOR_CANDIDATE": "Ada"}, targets)
    assert "existing" in (tmp_path / ".env").read_text("utf-8")
    assert settings.secret_status(targets)["BRIGHT_DATA_API_TOKEN"] is True


def test_multichoice_validate_accepts_subset_rejects_unknown_and_non_list():
    assert settings.validate({"remote_types": ["Remote", "Hybrid"]}) == {}
    assert "remote_types" in settings.validate({"remote_types": ["Telepathic"]})
    assert "remote_types" in settings.validate({"remote_types": "Remote"})


def test_choice_validate_gemini_auth():
    assert settings.validate({"gemini_auth": "api_key"}) == {}
    assert "gemini_auth" in settings.validate({"gemini_auth": "nope"})


def test_gemini_auth_saves_to_config_target(tmp_path):
    targets = _all_targets_env(tmp_path)
    settings.save({"gemini_auth": "api_key"}, targets)
    on_disk = json.loads((tmp_path / "config.json").read_text("utf-8"))
    assert on_disk["gemini_auth"] == "api_key"


def test_path_field_with_spaces_roundtrips_through_dotenv(tmp_path):
    targets = _all_targets_env(tmp_path)
    out = "C:\\Generated Resumes\\out"
    settings.save({"RESUME_TAILOR_OUTPUT": out}, targets)
    assert settings.load(targets)["RESUME_TAILOR_OUTPUT"] == out
