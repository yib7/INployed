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
