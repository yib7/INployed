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
dismisses by saving (`ED-8`).
"""
from __future__ import annotations

import json

from PySide6 import QtGui, QtWidgets

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
    _seed_v2(store, [_entry("work_authorized", "yes_no", "Yes", confirmed=False),
                     _entry("willing_to_relocate", "yes_no", "", confirmed=False)])
    ed = _editor(qtbot, store)
    ed._confirm_all_clicked()
    assert _row(ed, "work_authorized")["confirmed_cb"].isChecked() is True
    assert _row(ed, "willing_to_relocate")["confirmed_cb"].isChecked() is False


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


# --- ED-4: Add ------------------------------------------------------------------------

def test_add_dialog_ok_disabled_until_a_question_is_entered(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, [])
    ed = _editor(qtbot, store)
    dlg = AddAnswerDialog(ed.collect())
    qtbot.addWidget(dlg)
    assert dlg.ok_button.isEnabled() is False


def test_add_dialog_refuses_a_question_a_builtin_already_covers(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed_v2(store, apply_answers.with_missing_builtins([]))
    ed = _editor(qtbot, store)
    dlg = AddAnswerDialog(ed.collect())
    qtbot.addWidget(dlg)
    dlg.question_edit.setText("Are you authorized to work in the US?")
    assert dlg.ok_button.isEnabled() is False
    assert apply_answers.BUILTINS["work_authorized"].question in dlg.message_label.text()


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


def test_save_and_revert_disabled_on_a_damaged_store(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    store.write_text("not json{", encoding="utf-8")
    ed = _editor(qtbot, store)
    assert ed.save_btn.isEnabled() is False
    assert ed.revert_btn.isEnabled() is False


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
