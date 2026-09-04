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

import local_task  # noqa: E402
import settings  # noqa: E402
import settings_archive  # noqa: E402
import vm_sync  # noqa: E402
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


# --- Field.pattern: the one free-text format rule the schema declares ------------

@pytest.mark.parametrize("text", ["30,50,70", "0", "30, 50, 70", " 30 ,50 ", "5,5",
                                  "", "  ", "30,", ",30", "30,,50"])
def test_local_task_offsets_accepts_comma_separated_minutes(text):
    """The five extra cases are the ones the first cut of this rule rejected.

    `parse_offsets` honours every one of them in full — blank and whitespace mean
    "use the built-in 30,50,70", and a stray or trailing comma is simply an empty
    entry it skips, so `30,,50` really is (30, 50). See
    `test_the_offsets_pattern_is_never_stricter_than_its_consumer` for why that
    matters more than tidiness.
    """
    assert settings.validate({"local_task_offsets": text}) == {}


@pytest.mark.parametrize("text", ["abc", "30,abc", "30;50", "-5", "1.5"])
def test_local_task_offsets_rejects_anything_that_is_not_minutes(text):
    """The one field whose value is free text a consumer must PARSE.

    `local_task.parse_offsets` is deliberately junk-safe — it skips unreadable
    entries and falls back to its own default — so junk here has always been
    silently ignored rather than reported. That tolerance is right for the
    consumer (a mangled value must never leave the watcher task trigger-less) and
    wrong for the editor: the Settings tab is where someone finds out they typed
    something the pipeline will throw away.
    """
    errors = settings.validate({"local_task_offsets": text})
    assert "local_task_offsets" in errors
    assert "30,50,70" in errors["local_task_offsets"]      # the message shows the shape


def _consumer_loses_part_of(text) -> bool:
    """Did `parse_offsets` throw away part of what the user actually wrote?

    Written entries = the non-empty comma-separated pieces. Nothing written at all
    (blank, spaces, a lone comma) is not a loss: falling back to the built-in
    offsets IS what an empty setting means, exactly as it does for every other
    optional field in the schema.
    """
    written = [p.strip() for p in str(text).split(",") if p.strip()]
    sentinel = (-99999,)
    parsed = local_task.parse_offsets(text, default=sentinel)
    if not written:
        return False
    return parsed == sentinel or len(parsed) != len(written)


@pytest.mark.parametrize("text", [
    "30,50,70", "0", "30, 50, 70", " 30 ,50 ", "5,5", "", "  ", "30,", ",30",
    "30,,50", " , , ", "abc", "30,abc", "30;50", "-5", "1.5", "30 50", "1e3",
    "30,50,70 plus whatever", "every half hour", "0030",
])
def test_the_offsets_pattern_is_never_stricter_than_its_consumer(text):
    """A pattern may reject only what its consumer would silently DISCARD.

    This repo is public and other people run it. `validate()` runs over EVERY
    collected field, so a rule stricter than the runtime does not nag — it locks
    someone whose config already holds such a value out of saving any OTHER
    setting, from a row (`local_task_offsets` is `advanced` AND inside the VM
    section) that a configuration gate can keep off screen entirely. The first cut
    rejected "", "30," and ",30", all of which `parse_offsets` honours exactly.

    Asserted against the real consumer rather than against a second regex, so the
    two cannot drift apart in the same commit.
    """
    rejected = "local_task_offsets" in settings.validate({"local_task_offsets": text})
    assert rejected == _consumer_loses_part_of(text), text


def test_pattern_is_declared_on_the_field_and_off_by_default():
    """A format rule is schema DATA, like `choices` — not a branch in validate()."""
    assert settings.Field("k", "L", "str", "", "S", "config").pattern is None
    f = {x.key: x for x in settings.SETTINGS_SCHEMA}["local_task_offsets"]
    assert f.pattern and f.pattern_help
    # ...and it is the only one today, so nothing else silently gained a rule.
    assert [x.key for x in settings.SETTINGS_SCHEMA if x.pattern] == ["local_task_offsets"]


def test_pattern_must_match_the_WHOLE_value():
    """A `re.search`-style check would pass '30,50,70 and some junk'."""
    assert "local_task_offsets" in settings.validate(
        {"local_task_offsets": "30,50,70 plus whatever"})


def test_a_field_defaults_pass_their_own_pattern():
    """Restore-defaults must never land on a value the same schema rejects."""
    for f in settings.SETTINGS_SCHEMA:
        if f.pattern is not None:
            assert settings.validate({f.key: f.default}) == {}, f.key


def test_a_pattern_is_only_declared_on_a_text_field():
    """`re.fullmatch` needs a string. A pattern on a list/int/bool field would
    raise TypeError out of validate() the first time someone saved — catch it in
    the schema lint instead.

    `is not None`, not truthiness, in both schema lints: `pattern=""` is falsy but
    validate() still enforces it — and an empty pattern rejects every non-empty
    value, so it is the one spelling that most needs linting."""
    for f in settings.SETTINGS_SCHEMA:
        if f.pattern is not None:
            assert f.type in settings.TEXT_TYPES, f.key


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


# --- show_if: a field is on screen only when it can actually do something ------

# The sixteen gates, spelled out so the schema can't drift without a failure here.
#
# The six tailor-model tier rows do NOT gate on `tailor_provider` directly, even
# though the provider is exactly what should hide them: a Field has one `show_if`,
# and they need a second condition (the simple/tiers mode). They gate on their
# provider's MODE field, which gates on `tailor_provider` — `is_visible` walks the
# chain, so the wrong provider still hides them, transitively.
SHOW_IF_GATES = {
    "stage1_model": ("provider", ("gemini",)),
    "stage2_model": ("provider", ("gemini",)),
    "stage1_model_claude": ("provider", ("claude",)),
    "stage2_model_claude": ("provider", ("claude",)),
    "RESUME_TAILOR_MODEL_MODE": ("tailor_provider", ("gemini",)),
    "RESUME_TAILOR_MODEL_ALL": ("RESUME_TAILOR_MODEL_MODE", ("simple",)),
    "RESUME_TAILOR_MODEL_FLASH_LITE": ("RESUME_TAILOR_MODEL_MODE", ("tiers",)),
    "RESUME_TAILOR_MODEL_FLASH": ("RESUME_TAILOR_MODEL_MODE", ("tiers",)),
    "RESUME_TAILOR_MODEL_PRO": ("RESUME_TAILOR_MODEL_MODE", ("tiers",)),
    "RESUME_TAILOR_CLAUDE_MODEL_MODE": ("tailor_provider", ("claude",)),
    "RESUME_TAILOR_CLAUDE_MODEL_ALL": ("RESUME_TAILOR_CLAUDE_MODEL_MODE", ("simple",)),
    "RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE": ("RESUME_TAILOR_CLAUDE_MODEL_MODE", ("tiers",)),
    "RESUME_TAILOR_CLAUDE_MODEL_FLASH": ("RESUME_TAILOR_CLAUDE_MODEL_MODE", ("tiers",)),
    "RESUME_TAILOR_CLAUDE_MODEL_PRO": ("RESUME_TAILOR_CLAUDE_MODEL_MODE", ("tiers",)),
    "gemini_auth": ("tailor_provider", ("gemini",)),
    "RESUME_TAILOR_GEMINI_API_KEY": ("gemini_auth", ("api_key",)),
}


def test_the_sixteen_gates_are_declared_on_the_schema():
    gated = {f.key: f.show_if for f in settings.SETTINGS_SCHEMA if f.show_if is not None}
    assert gated == SHOW_IF_GATES
    assert len(SHOW_IF_GATES) == 16


def test_show_if_is_a_declarative_tuple_not_a_callable():
    """`Field` is frozen and pure-schema tests print/compare gates, so a gate is
    data. A callable would also make the acyclic check below impossible."""
    for f in settings.SETTINGS_SCHEMA:
        if f.show_if is None:
            continue
        gate_key, allowed = f.show_if
        assert isinstance(gate_key, str) and isinstance(allowed, tuple), f.key
        assert all(isinstance(v, str) for v in allowed), f.key


def test_every_gate_names_a_real_field_and_real_choices():
    """The failure mode this catches is invisible at runtime: a typo in either
    half ("api-key" for "api_key", "Provider" for "provider") hides the gated
    field FOREVER, with no error anywhere — the value keeps round-tripping to
    disk, so nothing else notices either."""
    by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
    for f in settings.SETTINGS_SCHEMA:
        if f.show_if is None:
            continue
        gate_key, allowed = f.show_if
        assert gate_key in by_key, f"{f.key} gates on unknown field {gate_key!r}"
        gate = by_key[gate_key]
        assert allowed, f"{f.key} has an empty allowed set — it could never show"
        assert set(allowed) <= set(gate.choices), (
            f"{f.key} gates on {gate_key} values {allowed} that aren't in {gate.choices}")


def test_show_if_graph_is_acyclic():
    """`is_visible` walks the gate chain, so a cycle is a hang (or, with the
    guard, an exception) in front of a real user. Prove the shipped graph
    terminates from every starting field."""
    edges = {f.key: f.show_if[0] for f in settings.SETTINGS_SCHEMA if f.show_if}
    for start in edges:
        chain = [start]
        node = edges[start]
        while node in edges:
            assert node not in chain, f"show_if cycle: {' -> '.join(chain + [node])}"
            chain.append(node)
            node = edges[node]


def test_is_visible_raises_on_a_cyclic_gate_graph(monkeypatch):
    """The other half of the acyclic contract: the walk must TERMINATE on a bad
    graph rather than spin. Only the schema test above ships, but this proves the
    guard is real, so a future cycle surfaces as an error instead of a frozen UI."""
    a = settings.Field("a", "A", "choice", "x", "S", "config",
                       choices=("x",), show_if=("b", ("x",)))
    b = settings.Field("b", "B", "choice", "x", "S", "config",
                       choices=("x",), show_if=("a", ("x",)))
    monkeypatch.setattr(settings, "SETTINGS_SCHEMA", [a, b])
    with pytest.raises(ValueError):
        settings.is_visible(a, {"a": "x", "b": "x"})


def test_show_if_gates_resolve_transitively():
    """A field is visible iff its OWN predicate holds AND its gate is visible.

    Without the second half, tailor_provider="claude" hides `gemini_auth` but a
    stored `gemini_auth="api_key"` leaves the Gemini API-key box on screen with
    nothing on the form governing it — the exact orphan this phase removes.
    """
    by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
    auth = by_key["gemini_auth"]
    api_key = by_key["RESUME_TAILOR_GEMINI_API_KEY"]

    both_open = {"tailor_provider": "gemini", "gemini_auth": "api_key"}
    assert settings.is_visible(auth, both_open) is True
    assert settings.is_visible(api_key, both_open) is True

    own_predicate_fails = {"tailor_provider": "gemini", "gemini_auth": "vertex"}
    assert settings.is_visible(auth, own_predicate_fails) is True
    assert settings.is_visible(api_key, own_predicate_fails) is False

    # The transitive case: the API key's OWN predicate holds, but its gate is
    # itself hidden, so it must go too.
    orphaned = {"tailor_provider": "claude", "gemini_auth": "api_key"}
    assert settings.is_visible(auth, orphaned) is False
    assert settings.is_visible(api_key, orphaned) is False
    assert "RESUME_TAILOR_GEMINI_API_KEY" not in settings.visible_keys(orphaned)


def test_is_visible_falls_back_to_the_gates_default_when_it_is_absent():
    """`values` may be partial (the form only reads the gate widgets). An absent
    gate must read as its default, not as "hidden"."""
    by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
    assert settings.is_visible(by_key["stage1_model"], {}) is True        # provider=gemini
    assert settings.is_visible(by_key["stage1_model_claude"], {}) is False
    assert settings.is_visible(by_key["min_score"], {}) is True           # ungated


def test_visible_keys_at_the_shipped_defaults_hides_the_nine_inapplicable_fields(tmp_path):
    """The audit's headline finding, pinned. At the shipped defaults
    (provider=gemini, tailor_provider=gemini, gemini_auth=vertex,
    RESUME_TAILOR_MODEL_MODE=tiers) these nine describe machinery that cannot run
    — two Claude scorer pickers, the Claude tailor block (its mode field, its
    one-model box and three tier pickers, the last four hidden TRANSITIVELY
    through the mode field), the Gemini one-model box that only 'simple' mode
    reads, and the Gemini API key that only 'api_key' billing reads."""
    values = settings.load(_targets(tmp_path))
    hidden = {f.key for f in settings.SETTINGS_SCHEMA} - set(settings.visible_keys(values))
    assert hidden == {
        "stage1_model_claude", "stage2_model_claude",
        "RESUME_TAILOR_CLAUDE_MODEL_MODE", "RESUME_TAILOR_CLAUDE_MODEL_ALL",
        "RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", "RESUME_TAILOR_CLAUDE_MODEL_FLASH",
        "RESUME_TAILOR_CLAUDE_MODEL_PRO",
        "RESUME_TAILOR_MODEL_ALL", "RESUME_TAILOR_GEMINI_API_KEY",
    }


def test_a_hidden_field_still_loads_and_saves_its_value(tmp_path):
    """`show_if` is a UI concern only: load()/save()/validate() never consult it.
    Hiding a field must not touch what is on disk — that is what makes the whole
    feature reversible and what keeps a Claude user's Gemini ids intact."""
    targets = _targets(tmp_path)
    targets["scoring"] = tmp_path / "scoring_config.json"
    values = settings.load(targets)
    values["provider"] = "claude"                 # hides stage1_model
    values["stage1_model"] = "gemini-9-custom"
    settings.save(values, targets)
    assert settings.load(targets)["stage1_model"] == "gemini-9-custom"
    assert json.loads(targets["scoring"].read_text("utf-8"))["stage1_model"] == "gemini-9-custom"


# --- advanced: progressive disclosure -------------------------------------------

# The eighteen advanced fields, spelled out so the set can't drift silently. Every
# one is a knob whose default is already right for the person who just installed
# this, and whose wrong value is either invisible (a model id nobody's account can
# serve) or irrelevant until something else is set up (the VM block).
ADVANCED_KEYS = {
    # the four scoring model pickers — a wrong id breaks scoring silently
    "stage1_model", "stage2_model", "stage1_model_claude", "stage2_model_claude",
    # scorer throughput / retry plumbing
    "stage1_concurrency", "stage2_concurrency", "rescore_cap",
    # a Stats-tab warning threshold
    "stale_after_hours",
    # the Vertex region — 'global' works for most users
    "GOOGLE_CLOUD_LOCATION",
    # the six résumé-tailor model pickers (three per provider)
    "RESUME_TAILOR_MODEL_FLASH_LITE", "RESUME_TAILOR_MODEL_FLASH", "RESUME_TAILOR_MODEL_PRO",
    "RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", "RESUME_TAILOR_CLAUDE_MODEL_FLASH",
    "RESUME_TAILOR_CLAUDE_MODEL_PRO",
    # VM plumbing, inert unless you run the cloud job-discovery VM
    "VM_GCLOUD_PATH", "VM_REMOTE_DIR", "local_task_offsets",
}


def test_the_advanced_set_is_declared_on_the_schema():
    """PLAN.md's P4 calls this list "17 fields"; enumerating it gives 18 (4 + 5
    singles + 6 + 3). The enumeration names every key explicitly, so it is the
    authoritative half — see DECISIONS.md. Nothing in the UI hardcodes either
    number: the checkbox counts at runtime."""
    declared = {f.key for f in settings.SETTINGS_SCHEMA if f.advanced}
    assert declared == ADVANCED_KEYS
    assert len(ADVANCED_KEYS) == 18


def test_advanced_set_excludes_country_pdflatex_and_max_scored():
    """Three fields that LOOK advanced and are deliberately kept in plain sight.

    `country` sits beside `location` and is what a non-US user must change:
    `location="United Kingdom"` with `country="US"` silently mis-searches, and a
    mis-search returns plausible results rather than an error, so nothing else
    tells them.

    `PDFLATEX_PATH` is the fix for "no PDF came out" — what someone hunts for
    when the tool is ALREADY broken. Hiding a repair knob behind a disclosure
    toggle is backwards: a user in that state has no reason to suspect the
    setting exists at all.

    `max_scored_per_run` is the only ceiling on an LLM bill, so it must stay
    where someone worried about spend can find it. (`rescore_cap` reads like its
    twin but is retry-of-failures plumbing — genuinely advanced, and in the set
    above.)

    This test exists to stop a future tidy-up pass from folding them in.
    """
    by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
    for key in ("country", "PDFLATEX_PATH", "max_scored_per_run"):
        assert by_key[key].advanced is False, f"{key} must stay visible by default"
    assert by_key["rescore_cap"].advanced is True     # the contrast that makes the point


def test_advanced_defaults_to_false_and_is_a_bool_everywhere():
    """A new Field is plain unless it opts in — the safe direction: a forgotten
    flag leaves a setting visible, never invisible."""
    assert settings.Field("k", "L", "str", "", "S", "config").advanced is False
    assert all(isinstance(f.advanced, bool) for f in settings.SETTINGS_SCHEMA)


def test_advanced_is_a_rendering_flag_only(tmp_path):
    """Same contract as `show_if`: load()/save()/validate() never consult it, so
    an advanced field keeps round-tripping its stored value whether or not the
    disclosure toggle has ever been ticked."""
    targets = _targets(tmp_path)
    targets["env"] = tmp_path / ".env"
    values = settings.load(targets)
    assert values["GOOGLE_CLOUD_LOCATION"] == "global"      # advanced, still loaded
    values["GOOGLE_CLOUD_LOCATION"] = "us-east1"
    settings.save(values, targets)
    assert settings.load(targets)["GOOGLE_CLOUD_LOCATION"] == "us-east1"
    assert settings.validate(values) == {}


# --- restart: the .env values a running dashboard has already frozen ------------

# Every `.env`-target field, minus the six the VM tab reads back out of the FILE.
# That carve-out is the whole rule: `local/app.py` calls `load_dotenv()` at
# startup, so the dashboard's `os.environ` is a snapshot of `.env` taken once per
# launch, and `python-dotenv` defaults to `override=False` — a later
# `load_dotenv()` anywhere, in this process or in a child that inherited its
# environment, cannot beat a value that is already set.
RESTART_KEYS = {
    # frozen as module constants at import (local/resume_tailor/config.py)
    "RESUME_TAILOR_OUTPUT", "RESUME_TAILOR_CANDIDATE",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "PDFLATEX_PATH",
    "RESUME_TAILOR_MODEL_FLASH_LITE", "RESUME_TAILOR_MODEL_FLASH", "RESUME_TAILOR_MODEL_PRO",
    "RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", "RESUME_TAILOR_CLAUDE_MODEL_FLASH",
    "RESUME_TAILOR_CLAUDE_MODEL_PRO",
    # the simple/tiers switch and its one-model id. Read live from os.environ by
    # config._model_mode / _one_model — which puts them in the same bucket as the
    # API key row under it, not the frozen-constant bucket above: live off a
    # snapshot that a .env write does not reach.
    "RESUME_TAILOR_MODEL_MODE", "RESUME_TAILOR_MODEL_ALL",
    "RESUME_TAILOR_CLAUDE_MODEL_MODE", "RESUME_TAILOR_CLAUDE_MODEL_ALL",
    # read live from os.environ — but os.environ is the stale startup snapshot
    "RESUME_TAILOR_GEMINI_API_KEY",     # llm.py, per call
    "GEMINI_API_KEYS",                  # keypool.KeyPool.from_env, per run
    "LINKEDIN_CHROME_ACCOUNT",          # chrome.CHROME_ACCOUNT, at import
    # module constants inside a SUBPROCESS that inherits the stale snapshot
    "BRIGHT_DATA_API_TOKEN", "BRIGHT_DATA_DATASET_ID",
}


def test_the_restart_set_is_declared_on_the_schema():
    declared = {f.key for f in settings.SETTINGS_SCHEMA if f.restart}
    assert declared == RESTART_KEYS
    assert len(RESTART_KEYS) == 20


def test_every_env_field_needs_a_restart_except_the_six_the_vm_tab_re_reads():
    """The rule behind the list, so a NEW .env field cannot quietly opt out.

    `local/app.py` loads `.env` into `os.environ` once, at launch. Nothing writes
    it back: `settings.save()` and `envfile` touch the FILE only (pinned below).
    So an edited `.env` value is invisible to this process — and to every child it
    spawns, because `subprocess` hands the child a copy of the parent's
    environment and `load_dotenv(override=False)` will not overwrite what is
    already there. The scraper and the scorer run as subprocesses and are covered
    by exactly that.

    The six VM keys are the one real exception: `vm_sync.VMTarget.from_env` calls
    `settings.load(targets)`, which reads the file, and says so at
    `local/vm_sync.py:101`.
    """
    env_keys = {f.key for f in settings.SETTINGS_SCHEMA if f.target == "env"}
    vm_keys = {f.key for f in settings.SETTINGS_SCHEMA
               if f.target == "env" and f.section == "VM (cloud scraper)"}
    assert vm_keys == set(vm_sync.VM_KEYS)
    assert env_keys - vm_keys == RESTART_KEYS
    # ...and nothing OUTSIDE .env carries the badge. Two different reasons, and
    # only one of them is "re-read live":
    #   * `config`  — genuinely live (config.py:_config_json, vm_sync, settings.load).
    #   * `search` / `scoring` — NOT live: score_jobs.py freezes all eleven scoring
    #     settings as module constants at import. They go unbadged because the path
    #     that reads them is a fresh subprocess per run, which re-imports. The one
    #     seam is the in-process manual-add (manual_add imports score_jobs), which
    #     holds whatever the session's first manual add froze — too narrow to badge
    #     every scoring row over, but noted here rather than left to look checked.
    assert all(f.target == "env" for f in settings.SETTINGS_SCHEMA if f.restart)


def test_saving_a_secret_never_writes_os_environ(tmp_path):
    """The mechanism the badge exists for, asserted rather than assumed. If a
    future save() ever DID export into os.environ, `RESUME_TAILOR_GEMINI_API_KEY`
    and `GEMINI_API_KEYS` would stop needing the badge and this test is where
    that gets noticed."""
    import os
    before = dict(os.environ)
    targets = _targets(tmp_path)
    targets["env"] = tmp_path / ".env"
    settings.save({"GEMINI_API_KEYS": "a-brand-new-key",
                   "RESUME_TAILOR_CANDIDATE": "Someone_Else"}, targets)
    assert dict(os.environ) == before
    assert "a-brand-new-key" in targets["env"].read_text(encoding="utf-8")


def test_restart_defaults_to_false_and_is_a_bool_everywhere():
    """Same safe direction as `advanced`: a forgotten flag under-promises (no
    badge on a field that turns out to need one) rather than nagging every user
    to restart for a setting that takes effect immediately."""
    assert settings.Field("k", "L", "str", "", "S", "config").restart is False
    assert all(isinstance(f.restart, bool) for f in settings.SETTINGS_SCHEMA)


def test_restart_is_a_rendering_flag_only(tmp_path):
    """Third member of the `show_if` / `advanced` family: load/save/validate never
    consult it."""
    targets = _targets(tmp_path)
    targets["env"] = tmp_path / ".env"
    values = settings.load(targets)
    values["RESUME_TAILOR_CANDIDATE"] = "Ada_Lovelace"
    settings.save(values, targets)
    assert settings.load(targets)["RESUME_TAILOR_CANDIDATE"] == "Ada_Lovelace"
    assert settings.validate(values) == {}


def test_the_fields_that_take_effect_without_a_restart_carry_no_badge():
    """`gemini_auth` and `tailor_provider` look like the model pickers beside them
    and are the opposite case: `config.py`'s `gemini_auth()` / `tailor_provider()`
    re-read `local/config.json` on EVERY call, so a change lands on the next
    tailor run. Badging them would train the user to ignore the badge."""
    by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
    for key in ("gemini_auth", "tailor_provider", "VM_REMOTE_DIR", "VM_GCLOUD_PATH"):
        assert by_key[key].restart is False, f"{key} takes effect without a restart"


def test_no_restart_field_repeats_the_word_in_its_help():
    """The badge says it once, in a place every restart-required row shows it the
    same way. Leaving the sentence in three of the sixteen help strings is how
    the count got to be 3-of-16 in the first place."""
    for f in settings.SETTINGS_SCHEMA:
        if f.restart:
            assert "restart" not in f.help.lower(), f.key


# --- schema lint: a hard cap on help length --------------------------------------

HELP_MAX_CHARS = 450


def test_no_help_string_is_longer_than_the_cap():
    """A hard cap is the only thing that stops the next twelve-line paragraph.

    `cover_letter_avoid_ai_writing` reached 702 characters — a wall of prose in a
    form whose whole problem is that there is too much to read. Two sentences in
    the field, the rest in `docs/USER_GUIDE.md`.

    The number is a BUDGET, and it is set with headroom on purpose. The longest
    surviving help is `archive_mode` at 347, which earned its length in P2
    (four legacy keys collapsed into one dropdown, plus the secrets-on-disk
    note) — a cap three characters above it would fire on the next word anyone
    adds there and tell them to move it into the guide, which is the wrong advice
    for a 348-character explanation. 450 is comfortably under the 702 that
    motivated this, so tripping it still means someone wrote an essay.
    """
    too_long = {f.key: len(f.help) for f in settings.SETTINGS_SCHEMA
                if len(f.help) > HELP_MAX_CHARS}
    assert too_long == {}, f"move the detail to docs/USER_GUIDE.md: {too_long}"
    # The cap is worth nothing if it drifts up to meet whatever was just written.
    assert max(len(f.help) for f in settings.SETTINGS_SCHEMA) <= 400


def test_no_help_string_points_at_a_row_by_position():
    """From the P3 review. "the Google engine above" was written when every field
    was on screen; `show_if` now hides rows, so a positional word can point at
    nothing — and does so in exactly the state where the sentence is being read.
    `tailor_provider`'s help said "'gemini' uses the Google engine above" while
    `gemini_auth`, the row it means, is gated on `tailor_provider == "gemini"` and
    is therefore ABSENT whenever someone reads that sentence to decide whether to
    switch back. Name the setting instead of its position.

    `min_score`'s "at/above this score" is the numeric sense, not the positional
    one, so it is the single spelling this lint lets through.
    """
    import re
    positional = re.compile(r"(?<!at/)\b(above|below)\b", re.IGNORECASE)
    # Positive control: `assert offenders == set()` passes just as well against a
    # gutted regex, and no help string currently contains "below" at all, so both
    # arms and the lookbehind are pinned here rather than by the schema happening
    # to be clean.
    assert positional.search("'gemini' uses the Google engine above")
    assert positional.search("the API-key box below")
    assert not positional.search("Jobs at/above this score are surfaced")

    offenders = {f.key for f in settings.SETTINGS_SCHEMA if positional.search(f.help)}
    assert offenders == set(), "name the setting instead of its position"


def test_validate_rejects_a_line_break_in_an_env_field(tmp_path):
    """A .env value is one physical line, so a newline cannot round-trip.

    envfile.update refuses it too, but that raise fires mid-save with earlier
    targets already written and carries no field name. Catching it in validate()
    keeps the refusal atomic and points at the box that is wrong.
    """
    errs = settings.validate({"GEMINI_API_KEYS": "key1\nkey2"})
    assert "GEMINI_API_KEYS" in errs
    assert "line break" in errs["GEMINI_API_KEYS"].lower()


def test_validate_rejects_control_characters_across_the_env_field_types(tmp_path):
    """Every .env-backed field is string-shaped, and all of them go through the
    same one-line writer, so the check cannot be scoped to the secret boxes."""
    for key, bad in (("BRIGHT_DATA_API_TOKEN", "tok\rEVIL=1"),   # str
                     ("PDFLATEX_PATH", "C:\\tex\\pdflatex.exe\n"),  # path
                     ("RESUME_TAILOR_MODEL_FLASH", "gemini\x00-flash")):  # editable_choice
        errs = settings.validate({key: bad})
        assert key in errs, key


def test_validate_leaves_json_backed_fields_and_ordinary_values_alone(tmp_path):
    """The rule belongs to the .env writer, not to every setting.

    A config.json value is JSON-encoded, where a newline is representable, and
    the awkward-but-legal .env values (Windows paths, apostrophes, comma-joined
    key pools) must keep passing.
    """
    assert settings.validate({"cover_letter_tone": "warm\nand direct"}) == {}
    assert settings.validate({
        "PDFLATEX_PATH": r"C:\Program Files\MiKTeX\pdflatex.exe",
        "GEMINI_API_KEYS": "key1,key2,key3",
        "RESUME_TAILOR_CANDIDATE": "Jane_Doe",
    }) == {}
