"""The Qt Apply Answers editor (cycle 18, SP4): typed rows, so a saved answer
reaches a form exactly one way.

Every row is typed (`ED-1`): a yes/no combo, a number line edit with a shape
validator, a choice combo, or a multi-line text box with a live counter. A
built-in's question is a read-only label; a custom question is editable.
Under each row a preview line (`ED-2`) reads `apply_answers.fact_value` on the
row's current, unsaved state. Changing an answer confirms its row (`ED-3`);
the top counts line and each row's warning highlight track unset/unconfirmed
rows live. `AddAnswerDialog` (`ED-4`) keeps OK disabled while the candidate
collides with a built-in or fails `validate`. Only custom rows delete, after a
confirmation (`ED-5`). Save blocks on `validate` errors and shows `warnings`
after a clean save (`ED-6`). A damaged store shows its error, offers "Restore
backup" only when a good `.bak` exists (`ED-7`), and never renders or saves
defaults over it. A migration's review list shows as a banner the user
dismisses by saving (`ED-8`). "Test my answers" (`ED-9`) runs the shipped
screening set against the saved, confirmed answers with the live judge, off
the UI thread; disabled with no TypeSafe key.
"""
from __future__ import annotations

import json
import sys
import types
from collections import namedtuple

import jev
import pytest
from PySide6 import QtGui, QtWidgets

from qt import answers_tab as at
from qt.answers_tab import AddAnswerDialog, AnswersEditor
from resume_tailor import apply_answers


def _seed_v1(path, entries):
    """A version 1 store on disk (no "version" key); load migrates it."""
    path.write_text(json.dumps({"answers": entries}), encoding="utf-8")


def _seed_v2(path, entries):
    apply_answers.save(entries, path)


def _editor(qtbot, path):
    ed = AnswersEditor(store_path=path)
    qtbot.addWidget(ed)
    return ed


def _row(ed, eid):
    return next(r for r in ed.rows if r["id"] == eid)


def _entry(eid, etype, answer="", note="", confirmed=False, question=None):
    question = question if question is not None else apply_answers.BUILTINS[eid].question
    return {"id": eid, "question": question, "type": etype, "answer": answer,
            "note": note, "confirmed": confirmed, "status": "active"}


def _custom(eid, question, etype="text", answer="", note="", confirmed=False):
    return {"id": eid, "question": question, "type": etype, "answer": answer,
            "note": note, "confirmed": confirmed, "status": "active"}


# --- ED-1: row widgets by type ---------------------------------------------------------

def test_yes_no_row_is_a_combo_box_with_only_three_options(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    ed = _editor(qtbot, store)
    combo = _row(ed, "work_authorized")["answer_widget"]
    assert isinstance(combo, QtWidgets.QComboBox)
    assert [combo.itemText(i) for i in range(combo.count())] == ["Not set", "Yes", "No"]
    assert combo.currentText() == "Yes"


def test_number_row_is_a_line_edit_with_a_shape_validator(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("years_experience", "number", "3")])
    ed = _editor(qtbot, store)
    edit = _row(ed, "years_experience")["answer_widget"]
    assert isinstance(edit, QtWidgets.QLineEdit)
    validator = edit.validator()
    assert validator is not None
    assert validator.validate("abc", 0)[0] != QtGui.QValidator.State.Acceptable
    assert validator.validate("3.2", 0)[0] != QtGui.QValidator.State.Acceptable
    assert validator.validate("3.5", 0)[0] == QtGui.QValidator.State.Acceptable
    assert validator.validate("12", 0)[0] == QtGui.QValidator.State.Acceptable


def test_choice_row_is_a_combo_listing_not_set_then_its_options(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("gender", "choice", "Female", confirmed=True)])
    ed = _editor(qtbot, store)
    combo = _row(ed, "gender")["answer_widget"]
    assert isinstance(combo, QtWidgets.QComboBox)
    assert combo.itemText(0) == "Not set"
    assert list(apply_answers.BUILTINS["gender"].options) == \
        [combo.itemText(i) for i in range(1, combo.count())]
    assert combo.currentText() == "Female"


def test_text_row_is_a_multiline_box_with_a_live_counter(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("how_did_you_hear", "text", "LinkedIn", confirmed=True)])
    ed = _editor(qtbot, store)
    row = _row(ed, "how_did_you_hear")
    box = row["answer_widget"]
    assert isinstance(box, QtWidgets.QPlainTextEdit)
    assert "8" in row["counter_label"].text()
    box.setPlainText("x" * 50)
    assert "50" in row["counter_label"].text()


def test_builtin_question_is_a_read_only_label(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    ed = _editor(qtbot, store)
    widget = _row(ed, "work_authorized")["question_widget"]
    assert isinstance(widget, QtWidgets.QLabel)
    assert widget.text() == apply_answers.BUILTINS["work_authorized"].question


def test_custom_question_is_editable_and_round_trips(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_custom("github", "What is your GitHub?")])
    ed = _editor(qtbot, store)
    widget = _row(ed, "github")["question_widget"]
    assert isinstance(widget, QtWidgets.QLineEdit)
    widget.setText("What's your GitHub handle?")
    (out,) = ed.collect()
    assert out["question"] == "What's your GitHub handle?"


def test_note_field_only_on_yes_no_and_number_rows(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", note="via portal",
                            confirmed=True),
                     _entry("years_experience", "number", "3", note="in Python",
                            confirmed=True),
                     _entry("gender", "choice", "Male", confirmed=True),
                     _entry("how_did_you_hear", "text", "LinkedIn", confirmed=True)])
    ed = _editor(qtbot, store)
    assert _row(ed, "work_authorized")["note_edit"].text() == "via portal"
    assert _row(ed, "years_experience")["note_edit"].text() == "in Python"
    assert _row(ed, "gender")["note_edit"] is None
    assert _row(ed, "how_did_you_hear")["note_edit"] is None


def test_typing_into_the_note_field_moves_the_counter_toward_note_max(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    ed = _editor(qtbot, store)
    row = _row(ed, "work_authorized")
    assert row["note_counter"].text() == "0/%d" % apply_answers.NOTE_MAX
    qtbot.keyClicks(row["note_edit"], "via portal")
    assert row["note_counter"].text() == "10/%d" % apply_answers.NOTE_MAX


def _write_v2(path, entries):
    """A version 2 file written by hand, with no validate pass."""
    path.write_text(json.dumps({"version": 2, "answers": entries, "review": []}),
                    encoding="utf-8")


def test_a_spaced_or_cased_option_shows_and_saves_as_its_option(qtbot, tmp_path, monkeypatch):
    # final review UI I2: the run reads "Yes " as Yes, so the editor does too,
    # and the next save writes the option in place of an empty answer
    store = tmp_path / "apply_answers.json"
    _write_v2(store, [_entry("work_authorized", "yes_no", "Yes ", confirmed=True),
                      _entry("gender", "choice", "female ", confirmed=True),
                      _entry("years_experience", "number", "2", confirmed=True)])
    ed = _editor(qtbot, store)
    assert _row(ed, "work_authorized")["answer_widget"].currentText() == "Yes"
    assert _row(ed, "gender")["answer_widget"].currentText() == "Female"
    assert _row(ed, "work_authorized")["confirmed_cb"].isChecked() is True
    _row(ed, "years_experience")["answer_widget"].setText("4")
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    assert ed.save() is True
    saved = {e["id"]: e for e in apply_answers.load(store)}
    assert (saved["work_authorized"]["answer"], saved["work_authorized"]["confirmed"]) == \
        ("Yes", True)
    assert (saved["gender"]["answer"], saved["gender"]["confirmed"]) == ("Female", True)


def test_a_stored_value_that_names_no_option_is_kept_and_flagged(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _write_v2(store, [_entry("work_authorized", "yes_no", "maybe", confirmed=True),
                      _entry("years_experience", "number", "2", confirmed=True)])
    before = store.read_bytes()
    ed = _editor(qtbot, store)
    row = _row(ed, "work_authorized")
    assert row["answer_widget"].currentText() == "maybe"
    assert row["preview_label"].text() == \
        "Forms will get nothing: 'maybe' is not one of the options, so pick one"
    assert row["frame"].property("callout") == "warning"
    assert ed.collect()[0]["answer"] == "maybe"
    _row(ed, "years_experience")["answer_widget"].setText("4")
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a, **k: None)
    assert ed.save() is False
    assert store.read_bytes() == before                  # the file keeps "maybe"
    row["answer_widget"].setCurrentText("Yes")
    assert row["preview_label"].text() == "Forms will get: Yes"


def test_a_long_migrated_note_loads_whole_and_blocks_save_until_shortened(
        qtbot, tmp_path, monkeypatch):
    # final review UI I3 / S2: the note is the user's own version 1 text
    store = tmp_path / "apply_answers.json"
    _seed_v1(store, [{"id": "work_authorized", "question": "x",
                      "answer": "Yes, " + "a" * 467}])
    before = store.read_bytes()
    ed = _editor(qtbot, store)
    row = _row(ed, "work_authorized")
    assert row["note_edit"].text() == "a" * 467
    assert row["note_counter"].text() == "467/300"
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a: shown.append(a[2]))
    assert ed.save() is False
    assert "the note is 467 of 300 characters" in shown[0]
    assert store.read_bytes() == before
    row["note_edit"].setText("a" * 300)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    assert ed.save() is True


def test_address_state_is_a_combo_when_the_country_is_the_us(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("address_country", "choice", "United States", confirmed=True),
                     _entry("address_state", "choice", "Texas", confirmed=True)])
    ed = _editor(qtbot, store)
    widget = _row(ed, "address_state")["answer_widget"]
    assert isinstance(widget, QtWidgets.QComboBox)
    assert widget.currentText() == "Texas"


def test_address_state_is_free_text_when_the_country_is_not_the_us(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("address_country", "choice", "Canada", confirmed=True),
                     _entry("address_state", "choice", "Ontario", confirmed=True)])
    ed = _editor(qtbot, store)
    widget = _row(ed, "address_state")["answer_widget"]
    assert isinstance(widget, QtWidgets.QLineEdit)
    assert widget.text() == "Ontario"
    assert widget.maxLength() == apply_answers.STATE_TEXT_MAX


def test_changing_country_to_non_us_switches_address_state_to_free_text(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("address_country", "choice", "United States", confirmed=True),
                     _entry("address_state", "choice", "Texas", confirmed=True)])
    ed = _editor(qtbot, store)
    _row(ed, "address_country")["answer_widget"].setCurrentText("Canada")
    widget = _row(ed, "address_state")["answer_widget"]
    assert isinstance(widget, QtWidgets.QLineEdit)
    assert widget.text() == "Texas"        # fits free text too; kept across the switch


def test_changing_country_back_to_us_switches_address_state_to_a_combo(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("address_country", "choice", "Canada", confirmed=True),
                     _entry("address_state", "choice", "Ontario", confirmed=True)])
    ed = _editor(qtbot, store)
    _row(ed, "address_country")["answer_widget"].setCurrentText("United States")
    widget = _row(ed, "address_state")["answer_widget"]
    assert isinstance(widget, QtWidgets.QComboBox)
    assert widget.currentText() == "Not set"   # "Ontario" is not a US state


# --- ED-2: preview -----------------------------------------------------------------------

def test_preview_says_not_set_when_the_answer_is_empty(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no")])
    ed = _editor(qtbot, store)
    assert _row(ed, "work_authorized")["preview_label"].text() == \
        "Forms will get nothing (not set)"


def test_preview_says_until_you_confirm_when_set_but_not_confirmed(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=False)])
    ed = _editor(qtbot, store)
    assert _row(ed, "work_authorized")["preview_label"].text() == \
        "Forms will get nothing until you confirm"


def test_preview_shows_the_fact_value_when_set_and_confirmed(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    ed = _editor(qtbot, store)
    assert _row(ed, "work_authorized")["preview_label"].text() == "Forms will get: Yes"


def test_preview_updates_live_as_the_answer_changes(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no")])
    ed = _editor(qtbot, store)
    row = _row(ed, "work_authorized")
    row["answer_widget"].setCurrentText("Yes")
    assert row["preview_label"].text() == "Forms will get: Yes"


def test_preview_for_a_number_row(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("years_experience", "number", "3", confirmed=True)])
    ed = _editor(qtbot, store)
    assert _row(ed, "years_experience")["preview_label"].text() == "Forms will get: 3"


def test_preview_for_a_text_row(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("how_did_you_hear", "text", "LinkedIn", confirmed=True)])
    ed = _editor(qtbot, store)
    assert _row(ed, "how_did_you_hear")["preview_label"].text() == \
        "Forms will get: LinkedIn"


# --- ED-3: confirmed, "Confirm all", counts and highlight ---------------------------------

def test_changing_the_answer_confirms_the_row(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no")])
    ed = _editor(qtbot, store)
    row = _row(ed, "work_authorized")
    assert row["confirmed_cb"].isChecked() is False
    row["answer_widget"].setCurrentText("Yes")
    assert row["confirmed_cb"].isChecked() is True


def test_the_user_can_untick_confirmed(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    ed = _editor(qtbot, store)
    _row(ed, "work_authorized")["confirmed_cb"].setChecked(False)
    (out,) = ed.collect()
    assert out["confirmed"] is False


def test_confirm_all_ticks_every_set_row_but_not_unset_rows(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("onsite_ok", "yes_no", "Yes", confirmed=False),
                     _entry("willing_to_relocate", "yes_no", "", confirmed=False)])
    ed = _editor(qtbot, store)
    ed._confirm_all_clicked()
    assert _row(ed, "onsite_ok")["confirmed_cb"].isChecked() is True
    assert _row(ed, "willing_to_relocate")["confirmed_cb"].isChecked() is False


def test_confirm_all_leaves_the_untouched_legal_seeds_for_their_own_tick(qtbot, tmp_path):
    # final review UI I1: the seeds claim authorization, no sponsorship and
    # zero years, so each needs the user's own tick
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, apply_answers.seed_defaults())
    ed = _editor(qtbot, store)
    ed._confirm_all_clicked()
    for eid in ("work_authorized", "requires_sponsorship", "years_experience"):
        assert _row(ed, eid)["confirmed_cb"].isChecked() is False, eid
    for eid in ("willing_to_relocate", "gender", "how_did_you_hear", "address_country"):
        assert _row(ed, eid)["confirmed_cb"].isChecked() is True, eid
    assert ed.status.text() == (
        "Confirmed the rest. These still hold the starting value, so tick each one "
        "yourself: work authorization, sponsorship, years of experience.")


def test_confirm_all_confirms_a_legal_answer_the_user_changed(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("requires_sponsorship", "yes_no", "Yes"),
                     _entry("years_experience", "number", "4"),
                     _entry("work_authorized", "yes_no", "Yes")])
    ed = _editor(qtbot, store)
    ed._confirm_all_clicked()
    assert _row(ed, "requires_sponsorship")["confirmed_cb"].isChecked() is True
    assert _row(ed, "years_experience")["confirmed_cb"].isChecked() is True
    assert _row(ed, "work_authorized")["confirmed_cb"].isChecked() is False
    assert ed.status.text().endswith(": work authorization.")


def test_counts_line_reports_unset_and_unconfirmed(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "", confirmed=False),
                     _entry("willing_to_relocate", "yes_no", "", confirmed=False),
                     _entry("onsite_ok", "yes_no", "Yes", confirmed=False)])
    ed = _editor(qtbot, store)
    assert ed.counts_label.text() == "2 answers not set, 1 not confirmed"


def test_counts_line_singular_for_one_unset_answer(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "", confirmed=False),
                     _entry("onsite_ok", "yes_no", "Yes", confirmed=True)])
    ed = _editor(qtbot, store)
    assert ed.counts_label.text() == "1 answer not set, 0 not confirmed"


def test_unset_and_unconfirmed_rows_are_highlighted(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "", confirmed=False),
                     _entry("willing_to_relocate", "yes_no", "Yes", confirmed=False),
                     _entry("onsite_ok", "yes_no", "Yes", confirmed=True)])
    ed = _editor(qtbot, store)
    assert _row(ed, "work_authorized")["frame"].property("callout") == "warning"
    assert _row(ed, "willing_to_relocate")["frame"].property("callout") == "warning"
    assert not _row(ed, "onsite_ok")["frame"].property("callout")


def test_typing_into_a_number_box_confirms_it_and_updates_the_preview_and_counts(qtbot, tmp_path):
    # A number row's answer widget is a plain QLineEdit (not a combo or a plain-text
    # box); this pins that its textChanged is wired to the same live updates as the
    # other row types, not just seeded on load.
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("years_experience", "number", "", confirmed=False)])
    ed = _editor(qtbot, store)
    row = _row(ed, "years_experience")
    assert row["confirmed_cb"].isChecked() is False
    assert ed.counts_label.text() == "1 answer not set, 0 not confirmed"
    assert row["frame"].property("callout") == "warning"
    qtbot.keyClicks(row["answer_widget"], "5")
    assert row["confirmed_cb"].isChecked() is True
    assert row["preview_label"].text() == "Forms will get: 5"
    assert ed.counts_label.text() == "0 answers not set, 0 not confirmed"
    assert not row["frame"].property("callout")


# --- ED-4: Add ------------------------------------------------------------------------

def test_add_dialog_ok_disabled_until_a_question_is_entered(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    ed = _editor(qtbot, store)
    dlg = AddAnswerDialog(ed.collect())
    qtbot.addWidget(dlg)
    assert dlg.ok_button.isEnabled() is False


@pytest.mark.parametrize("question", [
    "Are you legally authorized to work in the US?",       # the built-in's own words
    "are you legally authorized to work in the us",
    "Are you authorized to work in the US?",               # the run answers it the same way
])
def test_add_dialog_refuses_a_question_the_run_fills_from_a_builtin(qtbot, tmp_path, question):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, apply_answers.with_missing_builtins([]))
    ed = _editor(qtbot, store)
    dlg = AddAnswerDialog(ed.collect())
    qtbot.addWidget(dlg)
    dlg.question_edit.setText(question)
    assert dlg.ok_button.isEnabled() is False
    text = dlg.message_label.text()
    assert text == ("The built-in answer 'Are you legally authorized to work in the US?' "
                    "already answers this question, and the run fills it from that answer.")
    assert "Edit that answer" not in text


def test_add_dialog_refuses_a_years_heading_the_run_fills_from_the_number(qtbot, tmp_path):
    # a heading with no verb heads a status list only for a yes / no built-in;
    # the run fills a years box from the years_experience number
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, apply_answers.with_missing_builtins([]))
    ed = _editor(qtbot, store)
    dlg = AddAnswerDialog(ed.collect())
    qtbot.addWidget(dlg)
    dlg.question_edit.setText("Years of experience")
    dlg.answer_widget.setText("an answer")
    assert dlg.ok_button.isEnabled() is False
    assert dlg.message_label.text().startswith(
        "The built-in answer '%s'" % apply_answers.BUILTINS["years_experience"].question)


@pytest.mark.parametrize("question", [
    "Are you legally authorized to work in Canada?",       # another country
    "Are you willing to relocate to Austin, TX?",          # a city
    "Will you require H-1B visa sponsorship?",             # a visa type
    "How many years of experience do you have with Python?",   # years of a skill
    "How many years of experience do you have in your current role?",   # the current job
    "Work authorization",                                  # a status list heading
    "State your desired salary",
    "Country of citizenship",
])
def test_add_dialog_accepts_a_question_the_run_hands_to_a_custom_answer(qtbot, tmp_path,
                                                                       question):
    # final review UI C1: the guide tells the user to add these word for word
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, apply_answers.with_missing_builtins([]))
    ed = _editor(qtbot, store)
    dlg = AddAnswerDialog(ed.collect())
    qtbot.addWidget(dlg)
    dlg.question_edit.setText(question)
    dlg.answer_widget.setText("an answer")
    assert dlg.message_label.text() == ""
    assert dlg.ok_button.isEnabled() is True


def test_add_dialog_names_a_custom_answer_that_already_has_the_question(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_custom("github", "What is your GitHub?", answer="x", confirmed=True)])
    ed = _editor(qtbot, store)
    dlg = AddAnswerDialog(ed.collect())
    qtbot.addWidget(dlg)
    dlg.question_edit.setText("what is your github")
    assert dlg.ok_button.isEnabled() is False
    assert dlg.message_label.text() == \
        "Your custom answer 'What is your GitHub?' already has this question."


def test_a_version_1_store_holding_a_narrower_custom_question_saves(qtbot, tmp_path,
                                                                   monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v1(store, [{"id": "work_authorized", "question": "x", "answer": "Yes"},
                     {"id": "canada", "question": "Are you authorized to work in Canada?",
                      "answer": "No"}])
    ed = _editor(qtbot, store)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    assert ed.save() is True
    saved = {e["id"]: e for e in apply_answers.load(store)}
    assert saved["canada"]["answer"] == "No"


def test_save_refuses_a_custom_question_edited_into_a_builtins_question(qtbot, tmp_path,
                                                                        monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True),
                     _custom("q", "Are you over 18?", "yes_no", "Yes", confirmed=True)])
    before = store.read_bytes()
    ed = _editor(qtbot, store)
    _row(ed, "q")["question_widget"].setText("Are you authorized to work in the US?")
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a: shown.append(a[2]))
    assert ed.save() is False
    assert store.read_bytes() == before
    assert ("answer 'q': the built-in answer 'Are you legally authorized to work in the US?' "
            "already answers this question, and the run fills it from that answer; delete "
            "this custom answer or change its question") in shown[0]


def test_add_dialog_ok_enabled_for_a_clean_new_question(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    ed = _editor(qtbot, store)
    dlg = AddAnswerDialog(ed.collect())
    qtbot.addWidget(dlg)
    dlg.question_edit.setText("What is your GitHub?")
    dlg.answer_widget.setText("https://github.com/x")
    assert dlg.ok_button.isEnabled() is True
    entry = dlg.result_entry()
    assert entry["question"] == "What is your GitHub?"
    assert entry["type"] == "text"
    assert entry["answer"] == "https://github.com/x"
    assert entry["confirmed"] is True


def test_add_dialog_number_out_of_range_blocks_ok_until_fixed(qtbot, tmp_path):
    # The line edit's shape validator only rejects a bad shape (letters, three
    # digits, a non-half fraction); it still lets through "99", which is a valid
    # shape but out of the store's 0-60 range. This pins that `_recompute` also
    # runs `validate`, not just `find_collision`, so the range is caught too.
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    ed = _editor(qtbot, store)
    dlg = AddAnswerDialog(ed.collect())
    qtbot.addWidget(dlg)
    dlg.question_edit.setText("How many pets do you have?")
    dlg.type_combo.setCurrentText("Number")
    dlg.answer_widget.setText("99")
    assert dlg.ok_button.isEnabled() is False
    assert "0 to 60" in dlg.message_label.text()
    dlg.answer_widget.setText("5")
    assert dlg.ok_button.isEnabled() is True
    assert dlg.message_label.text() == ""


def test_add_dialog_note_field_only_for_yes_no_and_number(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    ed = _editor(qtbot, store)
    dlg = AddAnswerDialog(ed.collect())
    qtbot.addWidget(dlg)
    assert dlg.note_edit.isHidden() is True
    dlg.type_combo.setCurrentText("Yes/No")
    assert dlg.note_edit.isHidden() is False
    assert isinstance(dlg.answer_widget, QtWidgets.QComboBox)


def test_adding_an_answer_appends_a_confirmed_row(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    ed = _editor(qtbot, store)
    entry = {"id": "github", "question": "What is your GitHub?", "type": "text",
             "answer": "https://github.com/x", "note": "", "confirmed": True,
             "status": "active"}
    ed._append_row(entry)
    row = _row(ed, "github")
    assert row["answer_widget"].toPlainText() == "https://github.com/x"
    assert row["delete_btn"] is not None
    assert row["confirmed_cb"].isChecked() is True


# --- ED-5: Delete ----------------------------------------------------------------------

def test_builtin_rows_have_no_delete_button(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no")])
    ed = _editor(qtbot, store)
    assert _row(ed, "work_authorized")["delete_btn"] is None


def test_deleting_a_custom_row_asks_first_and_names_the_question(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_custom("github", "What is your GitHub?", answer="x", confirmed=True)])
    ed = _editor(qtbot, store)
    row = _row(ed, "github")
    asked = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        lambda *a, **k: asked.append(a) or QtWidgets.QMessageBox.StandardButton.No)
    row["delete_btn"].click()
    assert asked and "What is your GitHub?" in str(asked[0])
    assert any(r["id"] == "github" for r in ed.rows)     # No -> kept

    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    row["delete_btn"].click()
    assert not any(r["id"] == "github" for r in ed.rows)


# --- ED-6: Save ------------------------------------------------------------------------

def test_save_blocks_on_a_validate_error_and_writes_nothing(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_custom("github", "What is your GitHub?", answer="short", confirmed=True)])
    original = store.read_text(encoding="utf-8")
    ed = _editor(qtbot, store)
    _row(ed, "github")["answer_widget"].setPlainText("x" * 1001)
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        lambda *a, **k: shown.append(a[2]))
    assert ed.save() is False
    assert shown and "1000" in shown[0]
    assert store.read_text(encoding="utf-8") == original


def test_save_shows_warnings_after_a_successful_save(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "No", confirmed=True),
                     _entry("requires_sponsorship", "yes_no", "No", confirmed=True)])
    ed = _editor(qtbot, store)
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: shown.append(a[2]))
    assert ed.save() is True
    assert shown and "disagree" in shown[0]


def test_save_with_no_warnings_shows_none(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    # A fully-set, fully-confirmed, non-contradictory store: every built-in
    # answered, and work_authorized/requires_sponsorship do not disagree.
    entries = []
    for eid, b in apply_answers.BUILTINS.items():
        if b.type == "yes_no":
            answer = "No" if eid == "requires_sponsorship" else "Yes"
        elif b.type == "number":
            answer = "3"
        elif b.type == "choice":
            answer = b.options[0]
        else:
            answer = "n/a"
        entries.append(_entry(eid, b.type, answer, confirmed=True))
    _seed_v2(store, entries)
    ed = _editor(qtbot, store)
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: shown.append(a[2]))
    assert ed.save() is True
    assert not shown


# --- ED-7: damaged store ----------------------------------------------------------------

def test_damaged_store_shows_error_and_never_renders_defaults(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    store.write_text("not json{", encoding="utf-8")
    ed = _editor(qtbot, store)
    assert ed.rows == []
    assert ed.load_error
    assert "damaged" in ed.status.text()


def test_save_and_revert_disabled_on_a_damaged_store(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    store.write_text("not json{", encoding="utf-8")
    original = store.read_bytes()
    ed = _editor(qtbot, store)
    assert ed.save_btn.isEnabled() is False
    assert ed.revert_btn.isEnabled() is False
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a, **k: None)
    assert ed.save() is False
    assert store.read_bytes() == original


def test_save_and_revert_enabled_on_a_healthy_store(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no")])
    ed = _editor(qtbot, store)
    assert ed.save_btn.isEnabled() is True
    assert ed.revert_btn.isEnabled() is True


def test_restore_backup_disabled_with_no_bak(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    store.write_text("not json{", encoding="utf-8")
    ed = _editor(qtbot, store)
    assert ed.restore_backup_btn.isEnabled() is False


def test_restore_backup_disabled_when_the_bak_is_also_damaged(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    bak = store.with_name(store.name + ".bak")
    bak.write_text("also not json{", encoding="utf-8")
    store.write_text("not json{", encoding="utf-8")
    ed = _editor(qtbot, store)
    assert ed.restore_backup_btn.isEnabled() is False


def test_restore_backup_replaces_the_damaged_file_after_confirmation(qtbot, tmp_path,
                                                                     monkeypatch):
    store = tmp_path / "apply_answers.json"
    bak = store.with_name(store.name + ".bak")
    apply_answers.save([_entry("work_authorized", "yes_no", "Yes", confirmed=True)], bak)
    store.write_text("not json{", encoding="utf-8")
    ed = _editor(qtbot, store)
    assert ed.restore_backup_btn.isEnabled() is True
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    ed._restore_backup_clicked()
    assert not ed.load_error
    assert any(r["id"] == "work_authorized" for r in ed.rows)


def test_restore_backup_declined_leaves_the_damaged_file(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    bak = store.with_name(store.name + ".bak")
    apply_answers.save([_entry("work_authorized", "yes_no", "Yes", confirmed=True)], bak)
    store.write_text("not json{", encoding="utf-8")
    ed = _editor(qtbot, store)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No)
    ed._restore_backup_clicked()
    assert ed.load_error
    assert store.read_text(encoding="utf-8") == "not json{"


# --- ED-8: review banner -----------------------------------------------------------------

def test_review_banner_lists_each_review_item(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v1(store, [{"id": "work_authorized", "question": "Work auth?",
                      "answer": "Yes, I am a US citizen", "kind": "fixed",
                      "status": "active"}])
    ed = _editor(qtbot, store)
    assert ed.review
    assert ed.review_banner.isHidden() is False
    assert ("We read 'Yes, I am a US citizen' as Yes, note 'I am a US citizen'"
            in ed.review_label.text())


def test_no_review_banner_when_there_is_nothing_to_review(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    ed = _editor(qtbot, store)
    assert ed.review_banner.isHidden() is True


def test_checking_the_review_clears_it_and_saves(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v1(store, [{"id": "work_authorized", "question": "Work auth?",
                      "answer": "Yes, I am a US citizen", "kind": "fixed",
                      "status": "active"}])
    ed = _editor(qtbot, store)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    ed._review_confirmed_clicked()
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data["review"] == []
    assert ed.review_banner.isHidden() is True


# --- kept public surface / bridge behavior -------------------------------------------------

def test_default_store_editor_shows_full_standard_set(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("how_did_you_hear", "text", "LinkedIn", confirmed=True)])
    monkeypatch.setattr(apply_answers, "STORE_PATH", store)
    ed = AnswersEditor()               # no store_path -> live default store, merges defaults
    qtbot.addWidget(ed)
    ids = {r["id"] for r in ed.rows}
    assert "address_street" in ids and "address_country" in ids


def test_explicit_store_editor_is_exact(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_custom("only", "Q", answer="a", confirmed=True)])
    ed = AnswersEditor(store_path=store)
    qtbot.addWidget(ed)
    assert len(ed.rows) == 1


def test_save_persists(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    ed = _editor(qtbot, store)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    _row(ed, "work_authorized")["answer_widget"].setCurrentText("No")
    assert ed.save() is True
    reloaded = apply_answers.load(store)
    assert any(e["answer"] == "No" for e in reloaded)


def test_revert_restores_snapshot(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    ed = _editor(qtbot, store)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    _row(ed, "work_authorized")["answer_widget"].setCurrentText("No")
    ed.save()
    ed.revert()
    assert _row(ed, "work_authorized")["answer_widget"].currentText() == "Yes"


def test_revert_does_nothing_on_a_damaged_store_and_keeps_the_bak(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    bak = store.with_name(store.name + ".bak")
    bak.write_text("the good copy", encoding="utf-8")
    store.write_text("not json{", encoding="utf-8")
    ed = _editor(qtbot, store)
    assert ed.load_error
    ed.revert()
    assert "damaged" in ed.status.text()
    assert bak.read_text(encoding="utf-8") == "the good copy"
    assert store.read_text(encoding="utf-8") == "not json{"


def test_collect_preserves_an_unknown_key_on_a_loaded_entry(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    entry = _custom("github", "What is your GitHub?", answer="x", confirmed=True)
    entry["extra_key"] = [1, 2]
    _seed_v2(store, [entry])
    ed = _editor(qtbot, store)
    (out,) = ed.collect()
    assert out["extra_key"] == [1, 2]


# --- ED-9: "Test my answers" -------------------------------------------------------
#
# `local/apply_screening.py` (Agent A's module, built in parallel) is not
# imported at module scope by `answers_tab.py`: it lands lazily inside the
# worker closure, so these tests install a stand-in under its exact import
# name (`import apply_screening`, the same top-level-sibling style every
# other `local/` module uses) via `sys.modules`. A FakeJev (or a plain stub)
# is injected through `AnswersEditor`'s `judge_factory` constructor parameter;
# the real (paid) judge factory is never exercised here. `_current_jev_mode`
# is monkeypatched in every test below (rather than left to read the real,
# absent-in-this-worktree `local/config.json`) so the mode-dependent gating is
# never left to an ambient file.

_Row = namedtuple("Row", "qid question answer")


def _stub_apply_screening(monkeypatch, *, rows=None, fn=None):
    mod = types.ModuleType("apply_screening")
    mod.run_screening = fn if fn is not None else (lambda answers, judge: list(rows or []))
    monkeypatch.setitem(sys.modules, "apply_screening", mod)
    return mod


def test_test_answers_button_disabled_without_a_key(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: False)
    ed = _editor(qtbot, store)
    assert ed.test_answers_btn.isEnabled() is False
    assert "TypeSafe API key" in ed.test_answers_btn.toolTip()


def test_test_answers_button_enabled_with_a_key(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    ed = _editor(qtbot, store)
    assert ed.test_answers_btn.isEnabled() is True


def test_test_answers_tooltip_names_the_saved_confirmed_answers(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    ed = _editor(qtbot, store)
    tip = ed.test_answers_btn.toolTip().lower()
    assert "saved" in tip and "confirmed" in tip


def test_test_answers_tooltip_names_the_auto_apply_judge_setting(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    ed = _editor(qtbot, store)
    assert "Auto-apply judge setting" in ed.test_answers_btn.toolTip()


def test_test_answers_disabled_on_a_damaged_store_even_with_a_key(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    store.write_text("not json{", encoding="utf-8")
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    ed = _editor(qtbot, store)
    assert ed.test_answers_btn.isEnabled() is False


def test_test_answers_click_hands_the_work_to_run_async_not_the_ui_thread(
        qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    calls = []
    _stub_apply_screening(monkeypatch, fn=lambda answers, judge: (calls.append(1), [])[1])
    captured = {}
    monkeypatch.setattr(
        at.workers, "run_async",
        lambda owner, fn, on_done=None, on_error=None:
            captured.update(fn=fn, on_done=on_done, on_error=on_error))
    ed = AnswersEditor(store_path=store, judge_factory=lambda: jev.FakeJev())
    qtbot.addWidget(ed)
    ed.test_answers_btn.click()
    # the click handler must hand the work to run_async, never run it inline
    assert calls == []
    assert "fn" in captured
    captured["fn"]()
    assert calls == [1]


def test_test_answers_button_disables_itself_while_a_run_is_in_flight(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    _stub_apply_screening(monkeypatch, rows=[])
    captured = {}
    monkeypatch.setattr(
        at.workers, "run_async",
        lambda owner, fn, on_done=None, on_error=None: captured.update(fn=fn))
    ed = AnswersEditor(store_path=store, judge_factory=lambda: jev.FakeJev())
    qtbot.addWidget(ed)
    ed.test_answers_btn.click()
    assert ed.test_answers_btn.isEnabled() is False


def test_test_answers_runs_against_the_saved_store_not_unsaved_edits(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    seen = {}
    _stub_apply_screening(
        monkeypatch,
        fn=lambda answers, judge: (seen.setdefault("answers", answers), [])[1])
    captured = {}
    monkeypatch.setattr(
        at.workers, "run_async",
        lambda owner, fn, on_done=None, on_error=None: captured.update(fn=fn))
    ed = AnswersEditor(store_path=store, judge_factory=lambda: jev.FakeJev())
    qtbot.addWidget(ed)
    _row(ed, "work_authorized")["answer_widget"].setCurrentText("No")  # unsaved edit
    ed.test_answers_btn.click()
    captured["fn"]()
    assert seen["answers"][0]["answer"] == "Yes"   # the saved value, not the live edit


def test_test_answers_shows_a_table_of_the_run_picks_or_stops_here(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    rows = [_Row("q1", "Are you authorized to work in the US?", "Yes"),
            _Row("q2", "Do you require sponsorship?", None)]
    _stub_apply_screening(monkeypatch, rows=rows)
    captured = {}
    monkeypatch.setattr(
        at.workers, "run_async",
        lambda owner, fn, on_done=None, on_error=None: captured.update(fn=fn, on_done=on_done))
    seen = {}
    monkeypatch.setattr(at.QtWidgets.QDialog, "exec",
                        lambda self: seen.setdefault("dialog", self))
    ed = AnswersEditor(store_path=store, judge_factory=lambda: jev.FakeJev())
    qtbot.addWidget(ed)
    ed.test_answers_btn.click()
    result = captured["fn"]()
    captured["on_done"](result)
    dlg = seen["dialog"]
    assert dlg.table.rowCount() == 2
    assert dlg.table.item(0, 0).text() == "Are you authorized to work in the US?"
    assert dlg.table.item(0, 1).text() == "Yes"
    assert dlg.table.item(1, 1).text() == "stops here"


def test_test_answers_spend_falls_back_to_a_request_count_with_no_reported_cost(
        qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    # a counter of this test's own: the process-global one belongs to the session
    monkeypatch.setattr(jev, "_USAGE", {"requests": 0, "input_tokens": 0})
    _stub_apply_screening(monkeypatch, rows=[])
    captured = {}
    monkeypatch.setattr(
        at.workers, "run_async",
        lambda owner, fn, on_done=None, on_error=None: captured.update(fn=fn, on_done=on_done))
    seen = {}
    monkeypatch.setattr(at.QtWidgets.QDialog, "exec",
                        lambda self: seen.setdefault("dialog", self))
    ed = AnswersEditor(store_path=store, judge_factory=lambda: jev.FakeJev())
    qtbot.addWidget(ed)
    ed.test_answers_btn.click()
    result = captured["fn"]()
    captured["on_done"](result)
    text = seen["dialog"].spend_label.text()
    assert "$" not in text
    assert "0" in text


def test_test_answers_spend_shows_the_cost_the_judge_reports(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    # final review S5: a counter of this test's own, so the simulated request
    # stays out of the session's usage summary
    session = jev._USAGE
    session_before = dict(session)
    monkeypatch.setattr(jev, "_USAGE", {"requests": 0, "input_tokens": 0})

    def fn(answers, judge):
        jev.count_usage(1000)   # simulate one live request the run just made
        return []

    _stub_apply_screening(monkeypatch, fn=fn)
    captured = {}
    monkeypatch.setattr(
        at.workers, "run_async",
        lambda owner, fn, on_done=None, on_error=None: captured.update(fn=fn, on_done=on_done))
    seen = {}
    monkeypatch.setattr(at.QtWidgets.QDialog, "exec",
                        lambda self: seen.setdefault("dialog", self))
    ed = AnswersEditor(store_path=store, judge_factory=lambda: jev.FakeJev())
    qtbot.addWidget(ed)
    ed.test_answers_btn.click()
    result = captured["fn"]()
    captured["on_done"](result)
    assert "$" in seen["dialog"].spend_label.text()
    assert session == session_before


def test_test_answers_failure_shows_a_message_and_re_enables_the_button(
        qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a, **k: None)
    captured = {}
    monkeypatch.setattr(
        at.workers, "run_async",
        lambda owner, fn, on_done=None, on_error=None: captured.update(on_error=on_error))
    ed = AnswersEditor(store_path=store, judge_factory=lambda: jev.FakeJev())
    qtbot.addWidget(ed)
    ed.test_answers_btn.click()
    assert ed.test_answers_btn.isEnabled() is False
    captured["on_error"](RuntimeError("boom"))
    assert ed.test_answers_btn.isEnabled() is True
    assert "failed" in ed.status.text().lower()


# --- Fix round 1: the judge the Auto-apply judge setting names, not a hard-coded
# "typesafe" ---------------------------------------------------------------------

def test_test_answers_fake_mode_builds_the_fake_judge_and_skips_the_key_check(
        qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "fake")
    key_checks = []
    monkeypatch.setattr(at, "_typesafe_key_present",
                        lambda: key_checks.append(1) or False)
    seen = {}
    _stub_apply_screening(
        monkeypatch,
        fn=lambda answers, judge: (seen.setdefault("judge", judge), [])[1])
    captured = {}
    monkeypatch.setattr(
        at.workers, "run_async",
        lambda owner, fn, on_done=None, on_error=None: captured.update(fn=fn))
    # No judge_factory override: the DEFAULT factory must itself build the
    # fake judge from the configured mode -- `jev.get("fake")` is safe to call
    # for real (no key, no live request), unlike the typesafe path.
    ed = AnswersEditor(store_path=store)
    qtbot.addWidget(ed)
    assert ed.test_answers_btn.isEnabled() is True   # fake mode: enabled with no key
    ed.test_answers_btn.click()
    captured["fn"]()
    assert isinstance(seen["judge"], jev.FakeJev)
    assert key_checks == []   # the key is never checked for a non-live mode


def test_test_answers_typesafe_mode_without_a_key_stays_disabled(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: False)
    ed = _editor(qtbot, store)
    assert ed.test_answers_btn.isEnabled() is False


def test_test_answers_result_line_names_the_judge_mode(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "fake")
    _stub_apply_screening(monkeypatch, rows=[])
    captured = {}
    monkeypatch.setattr(
        at.workers, "run_async",
        lambda owner, fn, on_done=None, on_error=None: captured.update(fn=fn, on_done=on_done))
    seen = {}
    monkeypatch.setattr(at.QtWidgets.QDialog, "exec",
                        lambda self: seen.setdefault("dialog", self))
    ed = AnswersEditor(store_path=store, judge_factory=lambda: jev.FakeJev())
    qtbot.addWidget(ed)
    ed.test_answers_btn.click()
    result = captured["fn"]()
    captured["on_done"](result)
    assert "fake" in seen["dialog"].spend_label.text().lower()


# --- final review fixes: the banner, Restore backup, Test my answers ------------------

def test_review_banner_tells_the_user_to_tick_confirmed_and_its_button_confirms_nothing(
        qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v1(store, [{"id": "requires_sponsorship", "question": "x",
                      "answer": "No, but I will need H-1B sponsorship after my OPT ends"}])
    ed = _editor(qtbot, store)
    assert ed.review_label.text().startswith(
        "The update read these answers from your old file. Tick Confirmed on each one "
        "that is right: the run leaves an answer out until you confirm it.\n")
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    ed._review_confirmed_clicked()
    assert ed.review_banner.isHidden() is True
    (saved,) = apply_answers.load(store)
    assert (saved["answer"], saved["confirmed"]) == ("No", False)


def test_the_review_banner_stays_when_the_save_behind_its_button_fails(
        qtbot, tmp_path, monkeypatch):
    # final review UI M9
    store = tmp_path / "apply_answers.json"
    _seed_v1(store, [{"id": "work_authorized", "question": "x",
                      "answer": "Yes, " + "a" * 467}])
    ed = _editor(qtbot, store)
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a, **k: None)
    ed._review_confirmed_clicked()
    assert ed.review
    assert ed.review_banner.isHidden() is False


def _damaged_with_bak(tmp_path):
    store = tmp_path / "apply_answers.json"
    bak = store.with_name(store.name + ".bak")
    apply_answers.save([_entry("work_authorized", "yes_no", "Yes", confirmed=True)], bak)
    store.write_text("my newest answers, damaged{", encoding="utf-8")
    return store, bak


def test_restore_backup_keeps_the_damaged_file_and_names_it(qtbot, tmp_path, monkeypatch):
    # final review UI I4
    store, bak = _damaged_with_bak(tmp_path)
    damaged = store.with_name(store.name + ".damaged")
    damaged.write_text("an older damaged copy", encoding="utf-8")
    ed = _editor(qtbot, store)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    ed._restore_backup_clicked()
    assert damaged.read_text(encoding="utf-8") == "my newest answers, damaged{"
    assert store.read_bytes() == bak.read_bytes()
    assert ed.status.text() == ("Restored apply_answers.json.bak. The damaged file is kept "
                                "as apply_answers.json.damaged.")


def test_revert_after_a_restore_goes_back_to_the_restored_answers(qtbot, tmp_path,
                                                                  monkeypatch):
    store, bak = _damaged_with_bak(tmp_path)
    restored = bak.read_bytes()
    ed = _editor(qtbot, store)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    ed._restore_backup_clicked()
    assert ed.snapshot == restored
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    _row(ed, "work_authorized")["answer_widget"].setCurrentText("No")
    assert ed.save() is True
    ed.revert()
    assert store.read_bytes() == restored
    assert not ed.load_error


def test_a_save_during_a_test_run_leaves_the_button_off_until_the_run_ends(
        qtbot, tmp_path, monkeypatch):
    # final review UI I7: one run at a time
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=True)])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    _stub_apply_screening(monkeypatch, rows=[])
    captured = {}
    monkeypatch.setattr(
        at.workers, "run_async",
        lambda owner, fn, on_done=None, on_error=None: captured.update(fn=fn, on_done=on_done))
    monkeypatch.setattr(at.QtWidgets.QDialog, "exec", lambda self: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    ed = AnswersEditor(store_path=store, judge_factory=lambda: jev.FakeJev())
    qtbot.addWidget(ed)
    ed.test_answers_btn.click()
    _row(ed, "work_authorized")["answer_widget"].setCurrentText("No")
    assert ed.save() is True
    assert ed.test_answers_btn.isEnabled() is False
    captured["on_done"](captured["fn"]())
    assert ed.test_answers_btn.isEnabled() is True


def test_a_settings_save_refreshes_the_test_answers_button(qtbot, tmp_path, monkeypatch):
    # final review UI I8: a key set in Settings turns the button on at once
    from types import SimpleNamespace

    from qt import main_window as mw
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    monkeypatch.setattr(at, "_current_jev_mode", lambda: "typesafe")
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: False)
    ed = _editor(qtbot, store)
    assert ed.test_answers_btn.isEnabled() is False
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    for name in ("load_min_score", "load_repost_window_days", "load_followup_days"):
        monkeypatch.setattr(mw, name, lambda: 0)
    window = SimpleNamespace(answers_tab=ed, reload_data_async=lambda: None,
                             resume_data_tab=SimpleNamespace(refresh_push_state=lambda: None))
    mw.MainWindow._on_settings_saved(window)
    assert ed.test_answers_btn.isEnabled() is True


def test_the_fake_judge_result_line_says_it_is_free_and_makes_no_requests():
    # final review U3
    text = at._spend_text("fake", {"requests": 0, "input_tokens": 0, "usd": 0.0})
    assert text == "Judge: fake. The fake judge is free and makes no requests."
    assert "live" not in at._spend_text("replay", {"requests": 0, "usd": 0.0})
    assert at._spend_text("typesafe", {"requests": 2, "usd": 0.0}) == \
        "Judge: typesafe. 2 live requests made (no cost reported)."


def test_test_answers_wording_names_saved_answers_in_plain_words(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    monkeypatch.setattr(at, "_typesafe_key_present", lambda: True)
    for mode in ("typesafe", "fake"):
        monkeypatch.setattr(at, "_current_jev_mode", lambda m=mode: m)
        ed = _editor(qtbot, store)
        tip = ed.test_answers_btn.toolTip()
        assert "Uses your saved, confirmed answers. Save first to include new edits." in tip
        assert "not any unsaved edits" not in tip
    blurbs = [w.text() for w in ed.findChildren(QtWidgets.QLabel)]
    assert any(t.endswith("so a form gets the answer you picked, or a blank.") for t in blurbs)
    dlg = at.TestAnswersDialog([], "Judge: fake.")
    qtbot.addWidget(dlg)
    assert not any("live judge" in w.text() for w in dlg.findChildren(QtWidgets.QLabel))
