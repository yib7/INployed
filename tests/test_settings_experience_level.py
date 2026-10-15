"""Settings rows for the experience level (now several levels) and the title words
every search leaves out (cycle 29).

The scraper reads both keys from search_config.json (pipeline/scraper.py
load_search_config / title_exclude_terms_for). Everything here runs against tmp
targets, so no real config file is read or written.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))
sys.path.insert(0, str(REPO / "pipeline"))

import scraper  # noqa: E402
import settings  # noqa: E402
import settings_archive  # noqa: E402

BY_KEY = {f.key: f for f in settings.SETTINGS_SCHEMA}
LEVELS = ("Internship", "Entry level", "Associate",
          "Mid-Senior level", "Director", "Executive")


def _targets(tmp_path: Path) -> dict[str, Path]:
    return {"config": tmp_path / "config.json",
            "search": tmp_path / "search_config.json",
            "scoring": tmp_path / "scoring_config.json"}


def _write_search(tmp_path: Path, data: dict) -> None:
    (tmp_path / "search_config.json").write_text(json.dumps(data), encoding="utf-8")


def _stored(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "search_config.json").read_text("utf-8"))


# --- the schema rows -------------------------------------------------------------

def test_experience_level_is_a_multichoice_with_the_six_levels():
    f = BY_KEY["experience_level"]
    assert f.type == "multichoice"
    assert f.default == ["Entry level"]
    assert f.choices == LEVELS
    assert f.target == "search" and f.section == "Scraper"
    assert f.file_form == settings.FORM_ONE_AS_TEXT


def test_title_row_follows_experience_level_with_the_label_the_run_log_names():
    keys = [f.key for f in settings.SETTINGS_SCHEMA]
    assert keys.index("title_exclude_terms") == keys.index("experience_level") + 1
    f = BY_KEY["title_exclude_terms"]
    # scraper.py's run-log line says "(change this in Settings > Leave out job
    # titles with)", so the label must match it exactly.
    assert f.label == "Leave out job titles with"
    assert f.type == "str" and f.default == ""
    assert f.target == "search" and f.section == "Scraper"
    assert f.file_form == settings.FORM_WORDS
    src = (REPO / "pipeline" / "scraper.py").read_text("utf-8")
    assert f"Settings > {f.label}" in src


def test_title_help_names_the_automatic_words_and_the_off_switch():
    help_ = BY_KEY["title_exclude_terms"].help
    for word in scraper.TITLE_EXCLUDE_BY_LEVEL["Entry level"]:
        assert word in help_, word
    assert "Internship" in help_ and "Entry level" in help_
    assert "none" in help_
    assert "titles only" in help_
    assert "Staff" in help_


def test_the_automatic_words_in_the_help_are_the_scrapers():
    """The help lists the words; the scraper's map is what runs. Pin them equal
    so a change to the map cannot leave the help naming the old words."""
    entry = scraper.TITLE_EXCLUDE_BY_LEVEL["Entry level"]
    assert scraper.TITLE_EXCLUDE_BY_LEVEL["Internship"] == entry
    listed = ", ".join(entry[:-1]) + " and " + entry[-1]
    assert listed in BY_KEY["title_exclude_terms"].help
    for level in ("Associate", "Mid-Senior level", "Director", "Executive"):
        assert scraper.TITLE_EXCLUDE_BY_LEVEL[level] == [], level


def test_experience_level_help_says_what_it_does_now():
    help_ = BY_KEY["experience_level"].help
    assert "LinkedIn ignores" in help_
    assert "Leave out job titles with" in help_
    assert "every ticked level" in help_


def test_file_form_is_declared_only_where_it_means_something():
    """FORM_ONE_AS_TEXT is a multichoice rule and FORM_WORDS a text rule; a
    stray flag on another type would be silently ignored."""
    for f in settings.SETTINGS_SCHEMA:
        if f.file_form == settings.FORM_ONE_AS_TEXT:
            assert f.type == "multichoice", f.key
        elif f.file_form == settings.FORM_WORDS:
            assert f.type == "str", f.key
        else:
            assert f.file_form == "", f.key


# --- load ------------------------------------------------------------------------

def test_absent_key_loads_the_default_list(tmp_path):
    assert settings.load(_targets(tmp_path))["experience_level"] == ["Entry level"]
    assert settings.load(_targets(tmp_path))["title_exclude_terms"] == ""


@pytest.mark.parametrize("stored, loaded", [
    ("Entry level", ["Entry level"]),          # every existing user's file
    ("  Director ", ["Director"]),
    ("", []),                                  # none ticked
    (["Entry level"], ["Entry level"]),
    (["Internship", "Entry level"], ["Internship", "Entry level"]),
    ([], []),
])
def test_stored_level_loads_as_a_list_without_a_problem(tmp_path, stored, loaded):
    _write_search(tmp_path, {"experience_level": stored})
    values = settings.load(_targets(tmp_path))
    assert values["experience_level"] == loaded
    assert settings.validate(values) == {}


def test_an_unknown_stored_level_is_named_not_hidden(tmp_path):
    _write_search(tmp_path, {"experience_level": "Wizard"})
    values = settings.load(_targets(tmp_path))
    assert values["experience_level"] == ["Wizard"]
    assert settings.validate(values) == {"experience_level": "Not allowed: Wizard."}


@pytest.mark.parametrize("stored, loaded", [
    ("", ""),
    ("none", "none"),
    ("Senior, Staff", "Senior, Staff"),
    (["Senior", "Staff"], "Senior, Staff"),    # the scraper takes a list too
    ([], "none"),                              # [] means leave nothing out
    (None, ""),                                # null means automatic
])
def test_stored_title_words_load_as_text(tmp_path, stored, loaded):
    _write_search(tmp_path, {"title_exclude_terms": stored})
    values = settings.load(_targets(tmp_path))
    assert values["title_exclude_terms"] == loaded
    assert settings.validate(values) == {}


# --- save ------------------------------------------------------------------------

def test_saving_one_level_writes_a_plain_string(tmp_path):
    """The paused VM runs an older scraper.py that hands the value to Bright Data
    as-is, so one level must stay the plain string every older file holds."""
    targets = _targets(tmp_path)
    values = settings.load(targets)
    values["experience_level"] = ["Director"]
    settings.save(values, targets)
    assert _stored(tmp_path)["experience_level"] == "Director"
    assert settings.load(targets)["experience_level"] == ["Director"]


def test_saving_several_levels_writes_a_list(tmp_path):
    targets = _targets(tmp_path)
    values = settings.load(targets)
    values["experience_level"] = ["Internship", "Entry level"]
    settings.save(values, targets)
    assert _stored(tmp_path)["experience_level"] == ["Internship", "Entry level"]
    assert settings.load(targets)["experience_level"] == ["Internship", "Entry level"]


def test_saving_no_level_writes_a_blank(tmp_path):
    targets = _targets(tmp_path)
    values = settings.load(targets)
    values["experience_level"] = []
    settings.save(values, targets)
    assert _stored(tmp_path)["experience_level"] == ""
    assert settings.load(targets)["experience_level"] == []


def test_save_takes_the_file_form_too(tmp_path):
    """A caller holding the file's own plain string is not refused."""
    targets = _targets(tmp_path)
    assert settings.validate({"experience_level": "Director"}) == {}
    assert "experience_level" in settings.validate({"experience_level": "Wizard"})
    settings.save({"experience_level": "Director"}, targets)
    assert _stored(tmp_path)["experience_level"] == "Director"


def test_other_multichoice_fields_keep_their_list_form(tmp_path):
    targets = _targets(tmp_path)
    values = settings.load(targets)
    values["remote_types"] = ["Remote"]
    settings.save(values, targets)
    assert _stored(tmp_path)["remote_types"] == ["Remote"]
    assert "remote_types" in settings.validate({"remote_types": "Remote"})


@pytest.mark.parametrize("typed", ["", "none", "Senior, Staff"])
def test_the_title_row_round_trips(tmp_path, typed):
    targets = _targets(tmp_path)
    values = settings.load(targets)
    values["title_exclude_terms"] = typed
    settings.save(values, targets)
    assert _stored(tmp_path)["title_exclude_terms"] == typed
    assert settings.load(targets)["title_exclude_terms"] == typed


def test_a_hand_edited_word_list_saves_back_as_the_same_words(tmp_path, monkeypatch):
    targets = _targets(tmp_path)
    _write_search(tmp_path, {"title_exclude_terms": ["Senior", "Lead"]})
    settings.save(settings.load(targets), targets)
    assert _stored(tmp_path)["title_exclude_terms"] == "Senior, Lead"
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    assert scraper.title_exclude_terms_for(scraper.load_search_config()) == ["Senior", "Lead"]


def test_save_of_every_field_keeps_a_blank_title_row_automatic(tmp_path, monkeypatch):
    """The form writes every field on Save, so a fresh profile's first Save pins
    the blank: it must stay "" (automatic), never "none" or a list."""
    targets = _targets(tmp_path)
    settings.save(settings.load(targets), targets)
    stored = _stored(tmp_path)
    assert stored["title_exclude_terms"] == ""
    assert stored["experience_level"] == "Entry level"
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    cfg = scraper.load_search_config()
    assert scraper.title_exclude_terms_for(cfg) == ["Senior", "Sr", "Lead", "Principal"]
    assert cfg["experience_level"] == "Entry level"


# --- what the scraper makes of a Settings save -------------------------------------

@pytest.mark.parametrize("levels, typed, terms, bright_data_level", [
    (["Entry level"], "", ["Senior", "Sr", "Lead", "Principal"], "Entry level"),
    (["Director"], "", [], "Director"),
    (["Entry level", "Mid-Senior level"], "", [], ""),
    (["Internship", "Entry level"], "", ["Senior", "Sr", "Lead", "Principal"], ""),
    ([], "", [], ""),
    (["Entry level"], "none", [], "Entry level"),
    (["Director"], "Senior, Staff", ["Senior", "Staff"], "Director"),
])
def test_the_scraper_reads_what_settings_saved(tmp_path, monkeypatch, levels, typed,
                                                terms, bright_data_level):
    targets = _targets(tmp_path)
    values = settings.load(targets)
    values["experience_level"] = levels
    values["title_exclude_terms"] = typed
    settings.save(values, targets)
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    cfg = scraper.load_search_config()
    assert cfg["experience_levels"] == levels
    assert cfg["experience_level"] == bright_data_level
    assert scraper.title_exclude_terms_for(cfg) == terms


# --- other readers ---------------------------------------------------------------

def test_a_snapshot_of_an_old_file_restores_as_a_list(tmp_path):
    """settings_archive reads a snapshot through settings.load, so an older
    snapshot's plain-string level comes back as the form's list."""
    targets = _targets(tmp_path)
    _write_search(tmp_path, {"experience_level": "Internship"})
    snap = settings_archive.snapshot(targets)
    assert snap is not None
    _write_search(tmp_path, {"experience_level": ["Director", "Executive"]})
    values = settings_archive.load_snapshot(snap, targets)
    assert values["experience_level"] == ["Internship"]
    assert values["title_exclude_terms"] == ""
