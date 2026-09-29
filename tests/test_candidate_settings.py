"""Cycle 21, Task 4: the "About you" settings section.

Four rows in the Settings tab (school status, graduation month, clearance held,
open to employer sponsorship) write the four scoring_config.json keys Task 3 taught
score_jobs to read. The rows are schema DATA, so the pins here are about that data
staying in step with the consumer:

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


# Task 3's month table (tests/test_candidate_profile.py), accepted then rejected.
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
    assert "score_jobs.py and jev_score.py" in blurb


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
    """Null means no date to the scorer: the row opens blank, not dirty, and Save is allowed."""
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
    asked = []

    def question(parent, title, text, *a, **k):
        asked.append(title)
        return QtWidgets.QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(question))
    form._setters["vm_enabled"](True)
    form._setters["clearance_level"]("Top Secret")
    assert form.save() is True
    assert asked == ["Push config to VM?"]
    assert pushed == [True]
