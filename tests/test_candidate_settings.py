"""The "About you" settings section.

Four rows in the Settings tab (school status, graduation month, clearance held,
open to employer sponsorship) write the four scoring_config.json keys score_jobs
reads. The rows are schema DATA, so the pins here are about that data staying in
step with the consumer:

  * the two dropdowns list exactly the labels score_jobs resolves,
  * every default equals the scorer's own default (a fresh install and the VM
    behave as today),
  * the graduation month's format rule is never stricter than
    score_jobs.parse_graduation_month (the rule in settings.Field.pattern), and a
    blank stays valid because the scorer honours a blank as "no date".
"""
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))
sys.path.insert(0, str(REPO / "pipeline"))

import score_jobs as sj  # noqa: E402
import settings  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402
from qt import settings_tab as st  # noqa: E402
from qt.settings_tab import SettingsForm  # noqa: E402

KEYS = ("education_status", "graduation_month", "clearance_level", "clearance_sponsorship")
BY_KEY = {f.key: f for f in settings.SETTINGS_SCHEMA}


def _targets(tmp_path):
    return {
        "config": tmp_path / "config.json",
        "search": tmp_path / "search_config.json",
        "scoring": tmp_path / "scoring_config.json",
        "env": tmp_path / ".env",
    }


# --- the schema rows ------------------------------------------------------------

def test_the_four_rows_exist_in_about_you_and_write_to_the_scoring_file():
    for key in KEYS:
        f = BY_KEY[key]
        assert f.section == "About you", key
        assert f.target == "scoring", key
        assert f.advanced is False, key
        assert f.show_if is None, key
        assert f.help.strip(), key


def test_the_rows_carry_the_documented_types_and_labels():
    assert (BY_KEY["education_status"].type, BY_KEY["education_status"].label) == (
        "choice", "School status")
    assert (BY_KEY["graduation_month"].type, BY_KEY["graduation_month"].label) == (
        "str", "Graduation month")
    assert (BY_KEY["clearance_level"].type, BY_KEY["clearance_level"].label) == (
        "choice", "Security clearance held")
    assert (BY_KEY["clearance_sponsorship"].type, BY_KEY["clearance_sponsorship"].label) == (
        "bool", "Open to getting a clearance through the employer")
    assert BY_KEY["graduation_month"].optional is True


def test_the_four_rows_sit_together_right_before_the_scraper_block():
    keys = [f.key for f in settings.SETTINGS_SCHEMA]
    at = keys.index("education_status")
    assert keys[at:at + 4] == list(KEYS)
    first_scraper = next(i for i, f in enumerate(settings.SETTINGS_SCHEMA)
                         if f.section == "Scraper")
    assert first_scraper == at + 4


def test_education_status_choices_are_the_labels_the_scorer_resolves():
    assert BY_KEY["education_status"].choices == sj.EDUCATION_STATUSES


def test_clearance_level_choices_are_the_levels_the_scorer_ranks():
    assert BY_KEY["clearance_level"].choices == sj.CLEARANCE_LEVELS


@pytest.mark.parametrize("key", KEYS)
def test_each_default_equals_the_scorers_default(key):
    _env_var, default, _kind = sj._SCORING_DEFAULTS[key]
    assert BY_KEY[key].default == default


@pytest.mark.parametrize("key", ("education_status", "clearance_level"))
def test_a_choice_default_is_one_of_its_choices(key):
    f = BY_KEY[key]
    assert f.default in f.choices


def test_a_fresh_load_yields_the_four_defaults(tmp_path):
    values = settings.load(_targets(tmp_path))
    assert values["education_status"] == "Finished school"
    assert values["graduation_month"] == "May 2026"
    assert values["clearance_level"] == "None"
    assert values["clearance_sponsorship"] is False


# --- the graduation month rule vs the consumer ----------------------------------

def _accepts(value: str) -> bool:
    """Would the Settings tab let `value` be saved?"""
    return settings.field_problem(BY_KEY["graduation_month"], value) is None


def _consumer_ok(value: str) -> bool:
    """Does the scorer keep the value: a parsed month, or a blank meaning no date?"""
    return sj.parse_graduation_month(value) is not None or not value.strip()


# The scorer's month table (tests/test_candidate_profile.py), accepted then rejected.
_ACCEPTED = [
    "May 2026", "January 2027", "Jan 2027", "February 2028", "Feb 2028", "March 2026",
    "mar 2026", "April 2026", "Apr 2026", "June 2026", "Jun 2026", "July 2026",
    "Jul 2026", "August 2026", "Aug 2026", "September 2026", "Sept 2026", "Sept. 2026",
    "Sep 2026", "October 2026", "Oct 2026", "November 2026", "Nov 2026",
    "December 2026", "Dec 2026", "dec. 2026", "MAY 2026", "may 2026", "  May   2026  ",
    "\tDec.\t2030\n", "May 1999", chr(0x17F) + "ep 2026",
]
_REJECTED = [
    "garbage", "May", "2026", "May 26", "May 3026", "May 1899", "Maybe 2026",
    "Smarch 2026", "5/2026", "2026-05", "May, 2026", "Mayy 2026", "May 2026 and more",
    "May2026",
]
_BLANK = ["", " ", "   ", "\t", "\n", " \t\n "]


@pytest.mark.parametrize("value", _ACCEPTED)
def test_the_pattern_accepts_every_month_the_scorer_parses(value):
    assert sj.parse_graduation_month(value) is not None
    assert _accepts(value), value


@pytest.mark.parametrize("value", _REJECTED)
def test_the_pattern_rejects_what_the_scorer_would_discard(value):
    assert sj.parse_graduation_month(value) is None
    assert not _accepts(value), value


@pytest.mark.parametrize("value", _BLANK)
def test_a_blank_month_is_valid_because_the_scorer_reads_it_as_no_date(value):
    """The scorer's resolver treats a blank as "no date", so the row must let it save."""
    assert _accepts(value)
    assert sj.parse_graduation_month(value) is None
    profile = sj.candidate_profile(status="Finished school", graduation=value,
                                   clearance="None", sponsorship=False)
    assert profile.graduation is None and profile.notes == ()


def test_a_blank_month_passes_validate_and_a_junk_one_is_named():
    assert settings.validate({"graduation_month": ""}) == {}
    assert settings.validate({"graduation_month": "   "}) == {}
    errors = settings.validate({"graduation_month": "May 26"})
    assert errors == {"graduation_month": "A month and year, such as May 2026."}


def test_the_pattern_is_never_stricter_than_the_scorer_across_a_generated_grid():
    """Differential: the editor accepts a value exactly when the scorer keeps it.

    "Keeps it" is a parsed month or a blank; the grid crosses every month name and
    its abbreviations (plus the non-months around them) with separators, periods,
    cases, spacing and years, so a regex that drifts from GRADUATION_MONTH_RE in
    either direction fails on a named value.
    """
    names = list(sj._MONTH_NAMES) + [n[:3] for n in sj._MONTH_NAMES] + [
        "Sept", "Smarch", "Mayy", "Maybe", "Ju", "Marc", "Decem", "", "May May"]
    forms = (lambda s: s, str.upper, str.lower, str.title)
    seps = (" ", "  ", "\t", "", "-", ",", ". ", ".  ", "\n")
    years = ("2026", "1999", "2099", "1899", "3026", "26", "202", "20260", "2O26",
             "".join(chr(c) for c in (0x662, 0x660, 0x662, 0x666)))             # the last is 2026 in Arabic-Indic digits
    mismatches = []
    for name in names:
        for form in forms:
            for sep in seps:
                for year in years:
                    for pad in ("", " ", "\t\n"):
                        value = f"{pad}{form(name)}{sep}{year}{pad}"
                        if _accepts(value) != _consumer_ok(value):
                            mismatches.append(value)
    assert mismatches == [], mismatches[:10]


def test_the_pattern_carries_its_own_case_flag_because_validate_passes_none():
    """settings.field_problem calls re.fullmatch(pattern, value) with no flags."""
    pattern = BY_KEY["graduation_month"].pattern
    assert pattern.startswith("(?i)")
    assert re.fullmatch(pattern, "MAY 2026") is not None


def test_the_pattern_help_names_the_shape_and_echoes_no_regex():
    f = BY_KEY["graduation_month"]
    assert f.pattern_help == "A month and year, such as May 2026."
    assert "(?" not in f.pattern_help and "\\" not in f.pattern_help


# --- the values reach the scorer ------------------------------------------------

def _clear_env(monkeypatch):
    for var in ("SCORE_EDUCATION_STATUS", "SCORE_GRADUATION_MONTH",
                "SCORE_CLEARANCE_LEVEL", "SCORE_CLEARANCE_SPONSORSHIP"):
        monkeypatch.delenv(var, raising=False)


def test_a_saved_profile_round_trips_into_the_scorers_config(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    targets = _targets(tmp_path)
    settings.save({"education_status": "In school: graduate",
                   "graduation_month": "Dec. 2099",
                   "clearance_level": "Secret",
                   "clearance_sponsorship": True}, targets)
    on_disk = json.loads(targets["scoring"].read_text(encoding="utf-8"))
    assert on_disk["education_status"] == "In school: graduate"
    assert on_disk["clearance_sponsorship"] is True

    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    cfg = sj.load_scoring_config()
    profile = sj.candidate_profile(
        status=cfg["education_status"], graduation=cfg["graduation_month"],
        clearance=cfg["clearance_level"], sponsorship=cfg["clearance_sponsorship"])
    assert profile.status == "grad"
    assert profile.graduation == (2099, 12)
    assert profile.clearance_label == "Secret"
    assert profile.sponsorship is True
    assert profile.notes == ()


def test_a_blank_month_saved_from_the_tab_means_no_date_to_the_scorer(tmp_path, monkeypatch):
    """Blank is written as "" (not dropped), so the scorer does not fall back to May 2026."""
    _clear_env(monkeypatch)
    targets = _targets(tmp_path)
    settings.save({"graduation_month": ""}, targets)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    cfg = sj.load_scoring_config()
    assert cfg["graduation_month"] == ""
    profile = sj.candidate_profile(
        status=cfg["education_status"], graduation=cfg["graduation_month"],
        clearance=cfg["clearance_level"], sponsorship=cfg["clearance_sponsorship"])
    assert profile.graduation is None and profile.notes == ()


def test_a_null_month_opens_blank_and_saves(tmp_path):
    """A hand-edited null means no date to the scorer; the row shows blank and saves."""
    targets = _targets(tmp_path)
    targets["scoring"].write_text(json.dumps({"graduation_month": None}), encoding="utf-8")
    assert settings.load(targets)["graduation_month"] is None
    assert settings.validate({"graduation_month": ""}) == {}


# --- the Settings tab -----------------------------------------------------------

def test_about_you_is_a_section_placed_right_before_scraper():
    order = st.SECTION_ORDER
    assert order.index("About you") + 1 == order.index("Scraper")
    names = [name for name, _fields in st._ordered_sections()]
    assert names.index("About you") + 1 == names.index("Scraper")


def test_about_you_copy_is_the_documented_text():
    assert st.SECTION_TAGLINE["About you"] == (
        "School status and security clearance, read by the scorer")
    blurb = st.SECTION_HELP["About you"]
    assert blurb.startswith("What the scorer knows about you.")
    assert "push config to the VM" in blurb
    # The VM reads these rows only from score_jobs.py; jev_score.py is optional there.
    assert "score_jobs.py" in blurb and "jev_score.py" not in blurb
    # The reason is that the new script is the one that understands the rows. The old
    # "the VM reads these rows only from that script" could be read as "only the VM".
    assert blurb.endswith("upload the new score_jobs.py once, because only the new "
                          "score_jobs.py knows how to use these settings.")
    assert "reads these rows only" not in blurb


_BANNED = re.compile(
    r"\u2014|\s--\s|, not\b|\brather than\b|\binstead of\b|\bnot just\b"
    r"|\b(?:very|successfully|robust|above|below)\b", re.I)


def test_the_new_copy_follows_the_writing_rules():
    """Whole words only: "every" must not trip the "very" ban."""
    texts = [BY_KEY[k].help for k in KEYS] + [BY_KEY[k].label for k in KEYS]
    texts += [BY_KEY["graduation_month"].pattern_help,
              st.SECTION_TAGLINE["About you"], st.SECTION_HELP["About you"]]
    for text in texts:
        assert _BANNED.search(text) is None, text
    assert _BANNED.search("a very short line") and _BANNED.search("the list above")
    assert _BANNED.search("this, not that") and _BANNED.search("a -- b")
    assert _BANNED.search("every day") is None


def test_the_form_renders_a_control_for_each_row(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    for key in KEYS:
        assert key in form._widgets, key
        assert key in form._getters, key
        assert key in form._setters, key


def test_a_stored_month_opens_and_a_blank_one_saves_from_the_form(qtbot, tmp_path, monkeypatch):
    targets = _targets(tmp_path)
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    assert form._getters["graduation_month"]() == "May 2026"
    form._setters["graduation_month"]("")
    values, coercion = form.collect()
    assert coercion == {} and settings.validate(values) == {}
    assert values["graduation_month"] == ""
    assert form.save() is True
    assert json.loads(targets["scoring"].read_text(encoding="utf-8"))["graduation_month"] == ""


def test_a_null_month_in_the_file_opens_blank_and_clean_and_saves(qtbot, tmp_path, monkeypatch):
    """Null means no date to the scorer: the row opens blank and clean, and Save is allowed."""
    targets = _targets(tmp_path)
    targets["scoring"].write_text(json.dumps({"graduation_month": None}), encoding="utf-8")
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    assert form._getters["graduation_month"]() == ""
    assert "graduation_month" not in form._dirty
    assert form.save() is True


def test_a_two_digit_year_is_named_and_blocks_save(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    form._setters["graduation_month"]("May 26")
    values, _coercion = form.collect()
    assert settings.validate(values) == {
        "graduation_month": "A month and year, such as May 2026."}
    assert form.save() is False
    assert not (tmp_path / "scoring_config.json").exists()


def test_an_about_you_change_prompts_the_vm_push(qtbot, tmp_path, monkeypatch):
    """The four rows target scoring_config.json, which the VM reads from its own copy."""
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    pushed = []
    form._vm_panel = type("Panel", (), {"push_config":
                                        lambda self, skip_confirm=False: pushed.append(skip_confirm)})()
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    asked, texts = [], []

    def question(parent, title, text, *a, **k):
        asked.append(title)
        texts.append(text)
        return QtWidgets.QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(question))
    form._setters["vm_enabled"](True)
    form._setters["clearance_level"]("Top Secret")
    assert form.save() is True
    assert asked == ["Push config to VM?"]
    assert pushed == [True]
    # The VM reads the new keys only from its copy of score_jobs.py (jev_score.py is
    # optional there), so the prompt names that one file for the upload.
    assert "score_jobs.py" in texts[0] and "jev_score.py" not in texts[0]
    assert "gcloud compute scp" in texts[0]
    assert ("(there is no automated code push), because only the new score_jobs.py knows "
            "how to use these settings; run:") in texts[0]
    assert "reads these settings only from it" not in texts[0]


@pytest.mark.parametrize("key", ["education_status", "graduation_month", "clearance_level",
                                 "clearance_sponsorship"])
def test_each_about_you_change_puts_the_code_upload_note_in_the_vm_prompt(
        qtbot, tmp_path, monkeypatch, key):
    """Any of the four keys needs the new score_jobs.py on the VM."""
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    form._vm_panel = type("Panel", (), {"push_config": lambda self, skip_confirm=False: None})()
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    texts = []

    def question(parent, title, text, *a, **k):
        texts.append(text)
        return QtWidgets.QMessageBox.StandardButton.No

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(question))
    form._setters["vm_enabled"](True)
    new = {"education_status": sj.EDUCATION_STATUSES[1], "graduation_month": "June 2027",
           "clearance_level": "Secret", "clearance_sponsorship": True}[key]
    form._setters[key](new)
    assert form.save() is True
    assert len(texts) == 1
    assert "score_jobs.py" in texts[0] and "jev_score.py" not in texts[0], key


def test_the_code_note_covers_exactly_the_four_about_you_keys():
    assert st.ABOUT_YOU_KEYS == frozenset(KEYS)


def test_a_vm_config_change_outside_about_you_keeps_the_code_note_off(qtbot, tmp_path, monkeypatch):
    """The note names code files only for the keys that need new code on the VM."""
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    form._vm_panel = type("Panel", (), {"push_config": lambda self, skip_confirm=False: None})()
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    texts = []

    def question(parent, title, text, *a, **k):
        texts.append(text)
        return QtWidgets.QMessageBox.StandardButton.No

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(question))
    form._setters["vm_enabled"](True)
    form._setters["location"]("Canada")
    assert form.save() is True
    assert len(texts) == 1
    assert "score_jobs.py" not in texts[0] and "jev_score.py" not in texts[0]


# --- the checkbox reads a hand-edited string bool the way the scorer does --------

_SCORING_BOOLS = [f.key for f in settings.SETTINGS_SCHEMA
                  if f.type == "bool" and f.target == "scoring"]
_STRING_BOOLS = ["false", "False", " FALSE ", "0", "no", "off", "", "maybe",
                 "true", "True", " TRUE ", "1", "yes", "on"]


def test_the_scoring_file_bools_include_the_sponsorship_box():
    assert "clearance_sponsorship" in _SCORING_BOOLS


@pytest.mark.parametrize("key", _SCORING_BOOLS)
@pytest.mark.parametrize("stored", _STRING_BOOLS)
def test_a_stored_string_bool_opens_as_the_scorer_reads_it(qtbot, tmp_path, key, stored):
    """score_jobs._as_bool reads every scoring_config.json bool; the box must agree."""
    targets = _targets(tmp_path)
    targets["scoring"].write_text(json.dumps({key: stored}), encoding="utf-8")
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    assert form._getters[key]() is sj._as_bool(stored)
    assert form._field_value(BY_KEY[key]) == (sj._as_bool(stored), None)


def test_a_stored_false_string_opens_unchecked_and_a_true_one_checked(qtbot, tmp_path, monkeypatch):
    targets = _targets(tmp_path)
    targets["scoring"].write_text(json.dumps({"clearance_sponsorship": "false"}), encoding="utf-8")
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    assert form._getters["clearance_sponsorship"]() is False
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    # the next Save keeps it off as a real False (bool("false") gave True)
    assert form.save() is True
    assert json.loads(targets["scoring"].read_text(encoding="utf-8"))[
        "clearance_sponsorship"] is False

    targets["scoring"].write_text(json.dumps({"clearance_sponsorship": "true"}), encoding="utf-8")
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    assert form._getters["clearance_sponsorship"]() is True


def test_a_real_bool_and_a_missing_key_read_as_before(qtbot, tmp_path):
    targets = _targets(tmp_path)
    targets["scoring"].write_text(json.dumps({"clearance_sponsorship": True,
                                              "drop_easy_apply": False}), encoding="utf-8")
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    assert form._getters["clearance_sponsorship"]() is True
    assert form._getters["drop_easy_apply"]() is False
    assert form._getters["jev_writer"]() is True        # missing key: the default


# --- the default clearance "None" is a string, and stays one --------------------

def test_the_default_clearance_none_round_trips_the_form_as_the_string(qtbot, tmp_path, monkeypatch):
    """The scorer's default clearance is the word "None" (no clearance held). It stays
    that string from the schema default through the form to the saved file."""
    targets = _targets(tmp_path)
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    assert form._field_value(BY_KEY["clearance_level"]) == ("None", None)
    assert "clearance_level" not in form._dirty
    assert form.save() is True
    saved = json.loads(targets["scoring"].read_text(encoding="utf-8"))
    assert saved["clearance_level"] == "None"
    assert isinstance(saved["clearance_level"], str)
    reopened = SettingsForm(targets=targets)
    qtbot.addWidget(reopened)
    assert reopened._field_value(BY_KEY["clearance_level"]) == ("None", None)
    assert "clearance_level" not in reopened._dirty


def test_a_stored_null_on_a_text_field_opens_clean(qtbot, tmp_path):
    """The form reads a stored null as blank for every str field, so a null
    in search_config.json shows an empty box and marks nothing changed."""
    targets = _targets(tmp_path)
    targets["search"].write_text(json.dumps({"location": None}), encoding="utf-8")
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    assert form._getters["location"]() == ""
    assert form._field_value(BY_KEY["location"]) == ("", None)
    assert "location" not in form._dirty


# --- the month pattern cannot backtrack quadratically ---------------------------

def test_the_month_pattern_fails_fast_on_a_long_run_of_spaces():
    """A pasted run of spaces ending in a stray character must reject in linear time."""
    pattern = BY_KEY["graduation_month"].pattern
    value = " " * 50_000 + "x"
    started = time.perf_counter()
    assert re.fullmatch(pattern, value) is None
    assert settings.field_problem(BY_KEY["graduation_month"], value) is not None
    assert time.perf_counter() - started < 0.5


def test_the_month_pattern_still_accepts_padding_around_a_month_and_a_blank_run():
    pattern = BY_KEY["graduation_month"].pattern
    assert re.fullmatch(pattern, " " * 50_000) is not None
    assert re.fullmatch(pattern, " " * 5_000 + "May 2026" + " " * 5_000) is not None
    assert re.fullmatch(pattern, " " * 5_000 + "May 2026" + " " * 5_000 + "x") is None


# --- a graduation month that has passed is named at Save ----------------------------
#
# The scorer rolls an in-school status whose graduation month is before this month
# over to Finished school (score_jobs.candidate_profile). The shipped default month is
# already in the past, so a user who picks "In school" and leaves the month gets the
# opposite of what they chose, with nothing said. Save now says it. settings.py holds
# a copy of the month grammar and the rollover rule (it never imports score_jobs), and
# these pins hold the copy to the scorer's own answer.

_MONTH_GRID = _ACCEPTED + _REJECTED + _BLANK + [
    f"{name} {year}" for name in ("January", "May", "September", "December")
    for year in (1999, 2025, 2026, 2027, 2099)]
_TODAYS = (date(2026, 9, 29), date(2026, 9, 1), date(2026, 1, 31), date(2027, 5, 15),
           date(2026, 12, 31))
_STATUS_GRID = list(sj.EDUCATION_STATUSES) + list(sj.EDUCATION_STATUS_CODES) + [
    "IN SCHOOL: GRADUATE", "  In   school: undergraduate ", "GRAD", "Unknown status", ""]


@pytest.mark.parametrize("value", _MONTH_GRID)
def test_the_settings_month_parser_agrees_with_the_scorers(value):
    assert settings.parse_graduation_month(value) == sj.parse_graduation_month(value), value


@pytest.mark.parametrize("value", [None, 5, 2026.0, ["May 2026"], b"May 2026"])
def test_the_settings_month_parser_reads_a_non_string_as_no_month(value):
    assert settings.parse_graduation_month(value) is None
    assert sj.parse_graduation_month(value) is None


@pytest.mark.parametrize("today", _TODAYS)
def test_the_rollover_helper_agrees_with_the_scorers_profile(today):
    """One rule in two modules: the helper names a month exactly when the scorer rolls over."""
    disagreements = []
    for status in _STATUS_GRID:
        for month in _MONTH_GRID:
            profile = sj.candidate_profile(status=status, graduation=month, today=today)
            named = settings.graduation_month_passed(status, month, today)
            if profile.rolled_over != (named is not None):
                disagreements.append((status, month, named))
            elif profile.rolled_over and named != profile.graduation_text:
                disagreements.append((status, month, named))
    assert disagreements == [], disagreements[:10]


def test_the_rollover_helper_names_the_canonical_month():
    today = date(2026, 9, 29)
    assert settings.graduation_month_passed("In school: graduate", "sept. 2025", today) == "September 2025"
    assert settings.graduation_month_passed("In school: undergraduate", "May 2026", today) == "May 2026"


@pytest.mark.parametrize("status,month,today", [
    ("Finished school", "May 2026", date(2026, 9, 29)),
    ("In school: graduate", "September 2026", date(2026, 9, 29)),   # this month has not passed
    ("In school: graduate", "October 2026", date(2026, 9, 29)),
    ("In school: undergraduate", "", date(2026, 9, 29)),
    ("In school: undergraduate", "May 26", date(2026, 9, 29)),
    ("Something else", "May 2026", date(2026, 9, 29)),
    (None, "May 2026", date(2026, 9, 29)),
    ("In school: graduate", None, date(2026, 9, 29)),
])
def test_the_rollover_helper_stays_quiet(status, month, today):
    assert settings.graduation_month_passed(status, month, today) is None


def test_the_rollover_helper_reads_the_clock_when_no_date_is_given():
    assert settings.graduation_month_passed("In school: graduate", "January 2000") == "January 2000"
    assert settings.graduation_month_passed("In school: graduate", "May 2099") is None


def _save_boxes(qtbot, tmp_path, monkeypatch, status, month, targets=None):
    """Set the two rows, Save, and return (form, the texts of every message box shown)."""
    form = SettingsForm(targets=targets or _targets(tmp_path))
    qtbot.addWidget(form)
    boxes = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda parent, title, text, *a, **k: boxes.append(text)))
    form._setters["education_status"](status)
    form._setters["graduation_month"](month)
    assert form.save() is True
    return form, boxes


_PASSED_WARNING = ("Your graduation month ({month}) has passed, so the scorer counts you as "
                   "Finished school. Set the month you expect to graduate.")


@pytest.mark.parametrize("status", ["In school: undergraduate", "In school: graduate"])
def test_save_warns_when_an_in_school_status_carries_a_month_that_has_passed(
        qtbot, tmp_path, monkeypatch, status):
    targets = _targets(tmp_path)
    _form, boxes = _save_boxes(qtbot, tmp_path, monkeypatch, status, "January 2000", targets)
    assert len(boxes) == 1
    assert _PASSED_WARNING.format(month="January 2000") in boxes[0]
    assert boxes[0].startswith("Settings saved.")
    # Non-blocking: the save itself went through.
    saved = json.loads(targets["scoring"].read_text(encoding="utf-8"))
    assert saved["education_status"] == status and saved["graduation_month"] == "January 2000"


def test_save_names_the_month_the_way_the_scorer_writes_it(qtbot, tmp_path, monkeypatch):
    _form, boxes = _save_boxes(qtbot, tmp_path, monkeypatch, "In school: graduate", "dec. 1999")
    assert _PASSED_WARNING.format(month="December 1999") in boxes[0]


@pytest.mark.parametrize("status,month", [
    ("Finished school", "January 2000"),            # finished with a past month is the normal case
    ("In school: graduate", "May 2099"),
    ("In school: undergraduate", "May 2099"),
    ("In school: undergraduate", ""),
])
def test_save_stays_quiet_when_nothing_rolled_over(qtbot, tmp_path, monkeypatch, status, month):
    _form, boxes = _save_boxes(qtbot, tmp_path, monkeypatch, status, month)
    assert len(boxes) == 1
    assert "has passed" not in boxes[0] and "Finished school." not in boxes[0]


def test_a_save_with_no_changes_still_warns_about_a_stored_passed_month(
        qtbot, tmp_path, monkeypatch):
    targets = _targets(tmp_path)
    targets["scoring"].write_text(json.dumps({"education_status": "In school: graduate",
                                              "graduation_month": "May 2001"}), encoding="utf-8")
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    boxes = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda parent, title, text, *a, **k: boxes.append(text)))
    assert form.save() is True
    assert len(boxes) == 1
    assert boxes[0].startswith("No changes to save")
    assert _PASSED_WARNING.format(month="May 2001") in boxes[0]


def test_the_warning_is_one_line_of_the_saved_box_and_never_another_dialog(
        qtbot, tmp_path, monkeypatch):
    """Headless tests stub QMessageBox.information; an extra dialog kind would block them."""
    def refuse(*_a, **_k):
        raise AssertionError("the warning must not open another dialog")

    for name in ("warning", "question", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, staticmethod(refuse))
    _form, boxes = _save_boxes(qtbot, tmp_path, monkeypatch, "In school: graduate", "January 2000")
    assert len(boxes) == 1
    assert _PASSED_WARNING.format(month="January 2000") in boxes[0]


def test_the_warning_follows_the_writing_rules():
    text = _PASSED_WARNING.format(month="May 2026")
    assert _BANNED.search(text) is None
