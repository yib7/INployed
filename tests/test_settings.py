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
import settings_archive  # noqa: E402
import watcher  # noqa: E402


def _targets(tmp_path: Path) -> dict[str, Path]:
    return {"config": tmp_path / "config.json"}


def test_load_returns_defaults_when_file_absent(tmp_path):
    """With no config.json, load() yields every schema Field's default."""
    values = settings.load(_targets(tmp_path))
    assert values["min_score"] == 4
    assert values["followup_days"] == 5
    assert values["gdrive_root"] == ""


def test_projects_max_is_not_a_settings_field():
    # Cycle 19: the project count + at-most/exactly-N mode moved out of Settings
    # into the Resume Data tab's Resume Layout section (jobsdata.save_projects_count).
    assert not any(f.key == "projects_max" for f in settings.SETTINGS_SCHEMA)


def test_mtime_stable_seconds_is_not_a_settings_field():
    # Settings declutter: the sync debounce is a WATCHER-only key. Its help even
    # claimed the dashboard used it, but the only reader is local/watcher.py.
    # It must stay out of the schema AND keep its watcher-side default: that
    # default is what makes the removal invisible, because watcher.load_config()
    # reads config.json directly, so an absent key falls back to 30 and a value a
    # user already saved is still honoured.
    assert "mtime_stable_seconds" not in {f.key for f in settings.SETTINGS_SCHEMA}
    assert watcher.DEFAULT_CONFIG["mtime_stable_seconds"] == 30


def test_watcher_still_honours_a_saved_mtime_stable_seconds(tmp_path, monkeypatch):
    """The other half of the deletion contract, and the half that can rot.

    Pinning DEFAULT_CONFIG alone isn't enough: it would still pass if
    load_config() were refactored to return the parsed file WITHOUT merging the
    defaults underneath, and that is exactly the change that breaks a fresh user
    (KeyError at watcher.py's two cfg["mtime_stable_seconds"] reads). Assert the
    merge itself, from both sides.
    """
    monkeypatch.setattr(watcher, "CONFIG_PATH", tmp_path / "missing.json")
    assert watcher.load_config()["mtime_stable_seconds"] == 30      # absent -> default

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"mtime_stable_seconds": 90}), encoding="utf-8")
    monkeypatch.setattr(watcher, "CONFIG_PATH", cfg)
    assert watcher.load_config()["mtime_stable_seconds"] == 90      # saved value wins


def test_ui_scale_pct_is_not_a_settings_field():
    # Cycle 17: scaling moved to the bottom bar; ui_scale_pct is persisted via
    # jobsdata (config.json), not the settings schema.
    assert "ui_scale_pct" not in {f.key for f in settings.SETTINGS_SCHEMA}


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
    # ...nor in the registries that hang off a target id. With zero fields
    # pointing at it, a registered "apply" target only made every snapshot walk a
    # file that isn't there. Legacy apply_config.json is still READ by
    # resume_tailor.apply_config.load_apply_config(), which opens the repo-root
    # path itself — it never went through the settings layer.
    assert "apply" not in settings.TARGET_FILES
    assert "apply" not in settings.STORAGE_LABELS
    assert "apply" not in settings_archive._SNAPSHOT_TARGETS


def test_every_field_targets_a_registered_backing_file():
    """No Field may point at a target id TARGET_FILES doesn't know.

    save() skips an unregistered target silently (`path = targets.get(id)` then
    `if path is None: continue`) and storage_location() falls back to printing
    the raw id, so such a field renders a plausible "stored in" chip while every
    Save quietly discards its value. That is the deletion-safety failure mode in
    reverse, and later phases move fields between targets — pin it now.
    """
    assert {f.target for f in settings.SETTINGS_SCHEMA} <= set(settings.TARGET_FILES)


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


# --- résumé-tailor model dropdowns + editable_choice + slider hint -------------

def test_gemini_models_constant_lists_3x():
    assert "gemini-3.1-flash-lite" in settings.GEMINI_MODELS
    assert "gemini-3.5-flash" in settings.GEMINI_MODELS
    assert "gemini-3.1-pro-preview" in settings.GEMINI_MODELS


def test_resume_tailor_model_fields_present_and_editable():
    by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
    for k in ("RESUME_TAILOR_MODEL_FLASH_LITE", "RESUME_TAILOR_MODEL_FLASH",
              "RESUME_TAILOR_MODEL_PRO"):
        assert k in by_key, k
        f = by_key[k]
        assert f.target == "env"
        assert f.type == "editable_choice"
        assert "gemini-3.1-pro-preview" in f.choices


def test_scorer_models_are_editable_choice():
    by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
    assert by_key["stage1_model"].type == "editable_choice"
    assert by_key["stage2_model"].type == "editable_choice"


def test_editable_choice_accepts_a_custom_id_not_in_choices():
    # editable: a custom model id that isn't in the dropdown still validates.
    assert settings.validate({"stage1_model": "gemini-9-custom"}) == {}


def test_slider_flag_on_bounded_ints_but_not_min_score():
    by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
    assert by_key["followup_days"].slider is True
    assert by_key["stage2_threshold"].slider is True
    assert by_key["min_score"].slider is False  # stays a plain entry


def test_resume_tailor_model_roundtrips_to_dotenv(tmp_path):
    targets = _all_targets_env(tmp_path)
    settings.save({"RESUME_TAILOR_MODEL_PRO": "gemini-3.1-pro-preview"}, targets)
    assert settings.load(targets)["RESUME_TAILOR_MODEL_PRO"] == "gemini-3.1-pro-preview"
    assert "RESUME_TAILOR_MODEL_PRO=gemini-3.1-pro-preview" in (tmp_path / ".env").read_text("utf-8")


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


def test_storage_location_maps_each_target():
    """Every field can report the friendly filename its value is saved to, so the
    config GUI can show a 'stored in X' tag next to it."""
    by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
    assert settings.storage_location(by_key["BRIGHT_DATA_API_TOKEN"]) == ".env"
    assert settings.storage_location(by_key["keywords"]) == "search_config.json"
    assert settings.storage_location(by_key["stage1_model"]) == "scoring_config.json"
    assert settings.storage_location(by_key["min_score"]) == "config.json"


def test_tailor_open_folder_is_a_resume_bool_defaulting_off(tmp_path):
    f = {x.key: x for x in settings.SETTINGS_SCHEMA}["tailor_open_folder"]
    assert f.type == "bool" and f.target == "config"
    assert f.section == "Resume"  # résumé-tailor UX, not dashboard surfacing/tracking
    assert f.default is False
    assert settings.load(_targets(tmp_path))["tailor_open_folder"] is False
    assert settings.validate({"tailor_open_folder": True}) == {}


def test_stale_after_hours_is_a_dashboard_int_default_36(tmp_path):
    f = {x.key: x for x in settings.SETTINGS_SCHEMA}["stale_after_hours"]
    assert f.type == "int" and f.target == "config"
    assert f.section == "Dashboard"
    assert f.default == 36
    assert settings.load(_targets(tmp_path))["stale_after_hours"] == 36
    assert settings.validate({"stale_after_hours": 48}) == {}


def test_vm_enabled_defaults_false(tmp_path):
    assert settings.load(_targets(tmp_path))["vm_enabled"] is False


def test_vm_enabled_is_a_config_bool_in_vm_section():
    f = {x.key: x for x in settings.SETTINGS_SCHEMA}["vm_enabled"]
    assert f.type == "bool" and f.target == "config"
    assert f.section == "VM (cloud scraper)"
    assert settings.validate({"vm_enabled": True}) == {}


def test_local_task_autosync_is_a_config_bool_defaulting_off(tmp_path):
    f = {x.key: x for x in settings.SETTINGS_SCHEMA}["local_task_autosync"]
    assert f.type == "bool" and f.target == "config"
    assert f.section == "VM (cloud scraper)"
    assert settings.load(_targets(tmp_path))["local_task_autosync"] is False


def test_local_task_offsets_is_an_editable_config_str(tmp_path):
    f = {x.key: x for x in settings.SETTINGS_SCHEMA}["local_task_offsets"]
    assert f.type == "str" and f.target == "config"
    assert f.section == "VM (cloud scraper)"
    assert settings.load(_targets(tmp_path))["local_task_offsets"] == "30,50,70"


def test_inbox_map_default_matches_apply_queue():
    import apply_queue
    f = next(f for f in settings.SETTINGS_SCHEMA if f.key == "auto_apply_inbox_map")
    schema_map = dict(line.split(None, 1) for line in f.default)
    assert schema_map == apply_queue.DEFAULT_INBOX_MAP


def test_drop_easy_apply_is_a_scraper_section_scoring_target_bool(tmp_path):
    # Deliberate split: section "Scraper" (so it renders under Job discovery) but
    # target "scoring" (stored in scoring_config.json, which the VM push ships).
    f = {x.key: x for x in settings.SETTINGS_SCHEMA}["drop_easy_apply"]
    assert f.type == "bool"
    assert f.default is False
    assert f.section == "Scraper"
    assert f.target == "scoring"
    assert settings.load(_all_targets(tmp_path))["drop_easy_apply"] is False
    assert settings.validate({"drop_easy_apply": True}) == {}


def test_drop_easy_apply_save_roundtrips_to_scoring_config(tmp_path):
    targets = _all_targets(tmp_path)
    values = settings.load(targets)
    values["drop_easy_apply"] = True
    settings.save(values, targets)
    on_disk = json.loads(targets["scoring"].read_text(encoding="utf-8"))
    assert on_disk["drop_easy_apply"] is True
    assert settings.load(targets)["drop_easy_apply"] is True
    # flipping back to False persists as False, not a lingering True
    values = settings.load(targets)
    values["drop_easy_apply"] = False
    settings.save(values, targets)
    assert settings.load(targets)["drop_easy_apply"] is False


# --- the merged auto-apply inbox fallback --------------------------------------

def test_auto_apply_inbox_url_is_not_a_settings_field(tmp_path, monkeypatch):
    """Settings declutter: the single fallback URL was redundant with the map.

    It only ever fired when the signup email's domain missed `auto_apply_inbox_map`,
    and the shipped DEFAULT_INBOX_MAP already covers the common providers — an
    unmapped domain is one line in the map, exactly as easy as a fallback URL.

    The deletion is safe ONLY because of the second half asserted here:
    `apply_queue.build_context` reads config.json DIRECTLY and carries its own
    DEFAULT_INBOX_URL, so a value an existing public-repo user already saved keeps
    resolving exactly as before. Anything routed through `settings.load()` would
    not qualify — that returns schema fields only, so deleting the Field would
    silently revert the user's real value to a code default with no error.
    """
    import apply_queue
    from resume_tailor import assets, config as rt_config

    assert "auto_apply_inbox_url" not in {f.key for f in settings.SETTINGS_SCHEMA}

    monkeypatch.setattr(assets, "load_master",
                        lambda: {"basics": {"email": "grad@acme.io"}})  # unmapped domain
    monkeypatch.setattr(rt_config, "OUTPUT_ROOT", tmp_path / "Generated_Resumes")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"auto_apply_inbox_url": "https://legacy.inbox"}),
                   encoding="utf-8")
    monkeypatch.setattr(apply_queue, "CONFIG_JSON", cfg)
    ctx = apply_queue.build_context(path=tmp_path / "apply_queue.json")
    assert ctx["inbox_url"] == "https://legacy.inbox"      # legacy key still honoured


def test_inbox_map_help_names_the_gmail_fallback():
    """With the fallback Field gone, the map's help is the only place a user learns
    what an unlisted domain does — so it must say it."""
    f = next(f for f in settings.SETTINGS_SCHEMA if f.key == "auto_apply_inbox_map")
    assert "Gmail" in f.help and "not listed" in f.help


# --- archive_mode: the merged snapshot-retention setting -----------------------

def test_archive_mode_replaces_the_four_legacy_keys():
    keys = {f.key for f in settings.SETTINGS_SCHEMA}
    assert "archive_mode" in keys
    for gone in ("archive_enabled", "archive_prune_mode",
                 "archive_prune_keep", "archive_prune_days"):
        assert gone not in keys, gone
    f = next(f for f in settings.SETTINGS_SCHEMA if f.key == "archive_mode")
    assert (f.type, f.default, f.section, f.target) == (
        "choice", "Keep everything", "Settings history", "config")
    assert f.choices == ("Keep everything", "Keep newest 20", "Keep newest 100", "Off")
    # Index 0 is the default on purpose: QComboBox falls back to it when a stored
    # value matches no item, so a hand-edited typo lands on "keep everything"
    # rather than silently switching snapshots off.
    assert f.choices[0] == f.default


def test_settings_history_section_is_now_a_single_field():
    fields = [f for f in settings.SETTINGS_SCHEMA if f.section == "Settings history"]
    assert [f.key for f in fields] == ["archive_mode"]


def test_keep_everything_is_the_same_string_the_pruner_no_ops_on():
    """`prune()` deletes nothing for PRUNE_OFF, and that is exactly how the new
    default reproduces today's effective behaviour. Renaming either constant
    without the other would silently start deleting snapshots."""
    assert settings.ARCHIVE_KEEP_ALL == settings_archive.PRUNE_OFF


def test_the_migration_recognises_the_pruners_legacy_count_mode():
    """The other cross-module string coupling, and the one that decides whether a
    legacy config migrates at all. Rename `PRUNE_COUNT` without this copy and
    `_legacy_archive_mode` stops recognising every saved `archive_prune_keep`,
    silently reading "Keep everything" for all of them — fail-safe, but it defeats
    the entire migration."""
    assert settings._LEGACY_PRUNE_COUNT == settings_archive.PRUNE_COUNT


# One case per legacy combination, against the PURE derivation function.
# The invariant: never prune more aggressively than the old policy — which is why
# every count that is not one of the two offered rounds UP, and why anything
# unrecognised reads "Keep everything".
LEGACY_ARCHIVE_CASES = [
    ({}, "Keep everything", "absent-entirely"),
    ({"archive_enabled": False}, "Off", "disabled"),
    ({"archive_enabled": False, "archive_prune_mode": "Keep newest N",
      "archive_prune_keep": 5}, "Off", "disabled-beats-a-prune-policy"),
    ({"archive_enabled": True, "archive_prune_mode": "Keep everything"},
     "Keep everything", "keep-everything"),
    # The repo owner's real local/config.json, verbatim: 10 is not one of the new
    # options, so it must round UP to 20 rather than down to nothing.
    ({"archive_enabled": True, "archive_prune_mode": "Keep newest N",
      "archive_prune_keep": 10, "archive_prune_days": 30},
     "Keep newest 20", "owners-live-config-keep-10"),
    ({"archive_prune_mode": "Keep newest N", "archive_prune_keep": 20},
     "Keep newest 20", "keep-20-exactly"),
    ({"archive_prune_mode": "Keep newest N", "archive_prune_keep": 21},
     "Keep newest 100", "keep-21-rounds-up"),
    ({"archive_prune_mode": "Keep newest N", "archive_prune_keep": 100},
     "Keep newest 100", "keep-100-exactly"),
    ({"archive_prune_mode": "Keep newest N", "archive_prune_keep": 500},
     "Keep everything", "keep-500-exceeds-every-option"),
    ({"archive_prune_mode": "Delete older than N days", "archive_prune_days": 30},
     "Keep everything", "age-based-mode-is-dropped"),
    ({"archive_prune_mode": "Sell them on eBay"}, "Keep everything", "garbage-mode"),
    ({"archive_prune_mode": "Keep newest N", "archive_prune_keep": "ten"},
     "Keep everything", "garbage-count"),
]


@pytest.mark.parametrize("stored,expected",
                         [(c[0], c[1]) for c in LEGACY_ARCHIVE_CASES],
                         ids=[c[2] for c in LEGACY_ARCHIVE_CASES])
def test_legacy_archive_mode_derivation(stored, expected):
    assert settings._legacy_archive_mode(stored) == expected


def test_legacy_archive_mode_is_pure():
    """No I/O, no mutation — that is what makes the migration table testable
    without a filesystem, and what lets load() call it per backing store."""
    stored = {"archive_prune_mode": "Keep newest N", "archive_prune_keep": 10}
    before = dict(stored)
    settings._legacy_archive_mode(stored)
    assert stored == before


def test_every_derived_key_is_a_schema_key():
    keys = {f.key for f in settings.SETTINGS_SCHEMA}
    assert set(settings.DERIVED_WHEN_ABSENT) <= keys
    assert settings.DERIVED_WHEN_ABSENT["archive_mode"] is settings._legacy_archive_mode


def test_load_derives_archive_mode_from_a_legacy_config(tmp_path):
    targets = _targets(tmp_path)
    targets["config"].write_text(
        json.dumps({"archive_enabled": True, "archive_prune_mode": "Keep newest N",
                    "archive_prune_keep": 10}), encoding="utf-8")
    assert settings.load(targets)["archive_mode"] == "Keep newest 20"


def test_load_derives_off_from_a_disabled_legacy_config(tmp_path):
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({"archive_enabled": False}), encoding="utf-8")
    assert settings.load(targets)["archive_mode"] == "Off"


def test_a_stored_archive_mode_wins_over_the_derivation(tmp_path):
    """The hook sits between the stored value and the DEFAULT, so it only ever
    runs for a config written before the key existed. Once the user has saved,
    their choice is final however the old keys read."""
    targets = _targets(tmp_path)
    targets["config"].write_text(
        json.dumps({"archive_mode": "Keep newest 100", "archive_enabled": False,
                    "archive_prune_mode": "Keep newest N", "archive_prune_keep": 10}),
        encoding="utf-8")
    assert settings.load(targets)["archive_mode"] == "Keep newest 100"


def test_load_falls_back_to_the_default_for_a_fresh_config(tmp_path):
    """No file at all: the derivation sees {} and yields the default, so a brand
    new user gets today's effective behaviour (nothing is ever deleted)."""
    assert settings.load(_targets(tmp_path))["archive_mode"] == "Keep everything"


def test_legacy_config_saves_archive_mode_and_keeps_the_old_keys(tmp_path):
    """The downgrade path: save() MERGES, so the four legacy keys stay on disk.

    A user who checks out an older commit after this change gets their old
    retention behaviour back, because nothing reaped the keys that behaviour reads.
    """
    targets = _targets(tmp_path)
    legacy = {"archive_enabled": True, "archive_prune_mode": "Keep newest N",
              "archive_prune_keep": 10, "archive_prune_days": 30, "min_score": 4}
    targets["config"].write_text(json.dumps(legacy), encoding="utf-8")

    values = settings.load(targets)
    assert values["archive_mode"] == "Keep newest 20"        # derived for the form
    settings.save(values, targets)

    on_disk = json.loads(targets["config"].read_text(encoding="utf-8"))
    assert on_disk["archive_mode"] == "Keep newest 20"       # new key written
    for key, was in legacy.items():
        assert on_disk[key] == was, key                      # ...old keys untouched
    assert settings.load(targets)["archive_mode"] == "Keep newest 20"


def test_archive_mode_validates_only_its_four_choices():
    assert settings.validate({"archive_mode": "Off"}) == {}
    assert settings.validate({"archive_mode": "Keep newest 20"}) == {}
    assert "archive_mode" in settings.validate({"archive_mode": "Keep newest 7"})


# --- the pruner side: one signature, a new mode shape it parses ----------------

@pytest.mark.parametrize("mode,expected", [
    (settings.ARCHIVE_KEEP_20, 20),
    (settings.ARCHIVE_KEEP_100, 100),
    (settings.ARCHIVE_KEEP_ALL, None),
    (settings.ARCHIVE_OFF, None),
    # The two LEGACY modes, referenced through the constants rather than copied:
    # the counted arm runs FIRST, so if either is ever renamed to something ending
    # in a digit the arms silently reorder — which is the regression this pins.
    (settings_archive.PRUNE_COUNT, None),   # counts live in a separate key
    (settings_archive.PRUNE_AGE, None),
    ("", None),
])
def test_keep_from_mode_parses_only_a_trailing_count(mode, expected):
    """Every legacy mode string is None-safe by construction (their counts live in
    separate keys), so the new arm can be tried first without shadowing them."""
    assert settings_archive._keep_from_mode(mode) == expected


def test_keep_from_mode_ignores_a_zero_count(tmp_path):
    """A zero count is treated as "no counted policy" so the PRIMARY prune arm can
    never wipe the archive — including the snapshot the same Save just wrote."""
    targets = _targets(tmp_path)
    _snap_n(targets, 3)
    assert settings_archive.prune("Keep newest 0", targets=targets) == []
    assert len(settings_archive.list_snapshots(targets)) == 3


def _snap_n(targets, n):
    from datetime import datetime as _dt
    settings.save({"min_score": 4}, targets)
    for sec in range(n):
        settings_archive.snapshot(targets, when=_dt(2026, 6, 23, 10, 0, sec))


def test_prune_honours_a_counted_archive_mode_without_the_keep_kwarg(tmp_path):
    """`prune()` keeps its exact signature — the count rides in the mode string, so
    all eleven pre-existing archive tests still pass untouched."""
    targets = _targets(tmp_path)
    _snap_n(targets, 5)
    deleted = settings_archive.prune("Keep newest 2", targets=targets)
    remaining = settings_archive.list_snapshots(targets)
    assert len(deleted) == 3 and len(remaining) == 2
    assert {s.timestamp.second for s in remaining} == {4, 3}   # the two newest survive


def test_prune_deletes_nothing_for_keep_everything_or_off(tmp_path):
    targets = _targets(tmp_path)
    _snap_n(targets, 3)
    assert settings_archive.prune("Keep everything", targets=targets) == []
    assert settings_archive.prune("Off", targets=targets) == []
    assert len(settings_archive.list_snapshots(targets)) == 3
