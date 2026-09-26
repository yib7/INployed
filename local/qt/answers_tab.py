"""The Apply Answers editor (Qt): manage the master answer store from the dashboard.

Cycle 18 (SP4) rebuilds this tab around the version 2 typed store (SP1): one row
per answer, widgeted by its type so the run can never read a saved answer more
than one way. A yes/no row is a combo of Not set/Yes/No; a number row is a line
edit shaped to the store's number pattern; a choice row is a combo of Not set
plus its options (`address_state` swaps between the US state list and free text
as the address_country row changes); a text row is a multi-line box with a live
counter. A built-in's question is a read-only label; a custom question is
editable, and only a custom row deletes (after a confirmation). Under each row
a preview line reads `apply_answers.fact_value` on the row's current, unsaved
state. Changing an answer confirms its row; the top line counts unset and
unconfirmed answers, and those rows carry the theme's warning highlight.
"Add answer" opens `AddAnswerDialog`, whose OK stays disabled while the
candidate collides with a built-in's topic or another custom question, or
fails `validate`. Save runs `validate` (blocking on problems) and shows
`warnings` after a clean write. A damaged store shows its error, never renders
or saves defaults over it, and offers "Restore backup" only when a good
`.bak` sits next to it. A migration's review list shows as a banner; "I've
checked these" clears it and saves.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

import errmsg
from qt import theme
from resume_tailor import apply_answers

# The built-in's own text is authoritative for the address rules; hard-coded
# here (rather than importing answer_tables) because BUILTINS["address_country"]
# already documents it as one of its own options.
_US_COUNTRY = "United States"

# The custom-answer type picker's labels, in `apply_answers.CUSTOM_TYPES` order.
_TYPE_LABELS = {"text": "Text", "yes_no": "Yes/No", "number": "Number"}
_LABEL_TYPES = {v: k for k, v in _TYPE_LABELS.items()}

# ST-1's number shape (`\d{1,2}(\.5)?`), written so every valid prefix a user
# types is already a complete match -- there is no "not yet, but keep typing"
# state to model. The 0-60 range is a separate check `validate()` makes at save.
_NUMBER_SHAPE = r"^\d{0,2}(\.5?)?$"


def _is_us(answer: str) -> bool:
    return (answer or "").strip() in ("", _US_COUNTRY)


def _number_validator(parent=None) -> QtGui.QRegularExpressionValidator:
    return QtGui.QRegularExpressionValidator(QtCore.QRegularExpression(_NUMBER_SHAPE), parent)


class AddAnswerDialog(QtWidgets.QDialog):
    """"Add answer" (ED-4): question, type, answer, and a note where the type
    has one. OK stays disabled while the question collides with a built-in's
    topic or another custom question (`apply_answers.find_collision`), or the
    candidate entry fails `apply_answers.validate` run against the existing
    answers -- either way the reason shows under the fields.
    """

    def __init__(self, existing_answers: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add answer")
        self._existing = list(existing_answers)
        self.answer_widget: QtWidgets.QWidget | None = None
        self._build()
        self._rebuild_answer_widget()
        self._recompute()

    def _build(self) -> None:
        v = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.question_edit = QtWidgets.QLineEdit()
        self.question_edit.textChanged.connect(self._recompute)
        form.addRow("Question:", self.question_edit)

        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.addItems([_TYPE_LABELS[t] for t in apply_answers.CUSTOM_TYPES])
        self.type_combo.currentTextChanged.connect(self._on_type_changed)
        form.addRow("Type:", self.type_combo)

        self._answer_row = QtWidgets.QHBoxLayout()
        form.addRow("Answer:", self._answer_row)

        self.note_edit = QtWidgets.QLineEdit()
        self.note_edit.setMaxLength(apply_answers.NOTE_MAX)
        self.note_edit.textChanged.connect(self._recompute)
        form.addRow("Note:", self.note_edit)
        v.addLayout(form)

        self.message_label = QtWidgets.QLabel("")
        self.message_label.setWordWrap(True)
        self.message_label.setProperty("muted", True)
        v.addWidget(self.message_label)

        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = box.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        v.addWidget(box)

    def _on_type_changed(self, _label: str) -> None:
        self._rebuild_answer_widget()
        self._recompute()

    def _selected_type(self) -> str:
        return _LABEL_TYPES[self.type_combo.currentText()]

    def _rebuild_answer_widget(self) -> None:
        while self._answer_row.count():
            item = self._answer_row.takeAt(0)
            old = item.widget()
            if old is not None:
                old.setParent(None)
                old.deleteLater()
        etype = self._selected_type()
        if etype == "yes_no":
            widget: QtWidgets.QWidget = QtWidgets.QComboBox()
            widget.addItems(["Not set", *apply_answers.YES_NO])
            widget.currentTextChanged.connect(self._recompute)
        elif etype == "number":
            widget = QtWidgets.QLineEdit()
            widget.setValidator(_number_validator(widget))
            widget.textChanged.connect(self._recompute)
        else:
            widget = QtWidgets.QLineEdit()
            widget.textChanged.connect(self._recompute)
        self._answer_row.addWidget(widget)
        self.answer_widget = widget
        self.note_edit.setVisible(etype in ("yes_no", "number"))

    def _answer_text(self) -> str:
        if isinstance(self.answer_widget, QtWidgets.QComboBox):
            text = self.answer_widget.currentText()
            return "" if text == "Not set" else text
        return self.answer_widget.text().strip()

    def result_entry(self) -> dict:
        """The candidate entry OK would add; a fresh id (`apply_answers.new_id`),
        confirmed when it carries an answer (the user just gave it one)."""
        question = self.question_edit.text().strip()
        etype = self._selected_type()
        answer = self._answer_text()
        note = self.note_edit.text().strip() if etype in ("yes_no", "number") else ""
        taken = {str(e.get("id", "")).strip() for e in self._existing}
        eid = apply_answers.new_id(question or "answer", taken)
        return {"id": eid, "question": question, "type": etype, "answer": answer,
                "note": note, "confirmed": bool(answer), "status": "active"}

    def _recompute(self, *_args) -> None:
        question = self.question_edit.text().strip()
        if not question:
            self.message_label.setText("")
            self.ok_button.setEnabled(False)
            return
        hit = apply_answers.find_collision(question, self._existing)
        if hit:
            self.message_label.setText(
                "Already covered by '%s'. Edit that answer instead." % hit)
            self.ok_button.setEnabled(False)
            return
        errs = apply_answers.validate(self._existing + [self.result_entry()])
        if errs:
            self.message_label.setText(errs[0])
            self.ok_button.setEnabled(False)
            return
        self.message_label.setText("")
        self.ok_button.setEnabled(True)


class AnswersEditor(QtWidgets.QWidget):
    def __init__(self, on_saved: Callable[[], None] | None = None,
                 store_path: Path | None = None, parent=None):
        super().__init__(parent)
        self.on_saved = on_saved
        # The live tab (no explicit path) always offers the complete standard set
        # (so newly-added defaults like the address fields appear). Tests/tools that
        # pass an explicit store_path get an exact read of that file.
        self._merge_defaults = store_path is None
        self.store_path = Path(store_path) if store_path is not None else apply_answers.STORE_PATH
        self.snapshot = self.store_path.read_bytes() if self.store_path.exists() else b""
        self.rows: list[dict] = []
        self.load_error = ""
        self.review: list[dict] = []

        self._build_shell()
        self.reload()

    # ---- construction --------------------------------------------------------

    def _build_shell(self) -> None:
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

        top = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Apply Answers")
        title.setProperty("heading", True)
        top.addWidget(title)
        blurb = QtWidgets.QLabel(
            "Reusable answers the apply helper fills into forms. Every answer is "
            "typed, so a form gets exactly what you mean or nothing at all.")
        blurb.setProperty("muted", True)
        blurb.setWordWrap(True)
        top.addWidget(blurb, 1)
        v.addLayout(top)

        self.counts_label = QtWidgets.QLabel("")
        self.counts_label.setProperty("muted", True)
        v.addWidget(self.counts_label)

        self.review_banner = QtWidgets.QFrame()
        self.review_banner.setProperty("callout", "warning")
        rb = QtWidgets.QHBoxLayout(self.review_banner)
        self.review_label = QtWidgets.QLabel("")
        self.review_label.setWordWrap(True)
        rb.addWidget(self.review_label, 1)
        self.review_confirm_btn = QtWidgets.QPushButton("I've checked these")
        self.review_confirm_btn.clicked.connect(self._review_confirmed_clicked)
        rb.addWidget(self.review_confirm_btn, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        v.addWidget(self.review_banner)
        self.review_banner.setVisible(False)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        body = QtWidgets.QWidget()
        self._rows_box = QtWidgets.QVBoxLayout(body)
        self._rows_box.addStretch(1)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)

        bar = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("Save changes")
        self.save_btn.setProperty("accent", True)
        self.save_btn.clicked.connect(self.save)
        bar.addWidget(self.save_btn)
        self.add_btn = QtWidgets.QPushButton("Add answer")
        self.add_btn.clicked.connect(self._add_answer_clicked)
        bar.addWidget(self.add_btn)
        self.confirm_all_btn = QtWidgets.QPushButton("Confirm all")
        self.confirm_all_btn.clicked.connect(self._confirm_all_clicked)
        bar.addWidget(self.confirm_all_btn)
        self.revert_btn = QtWidgets.QPushButton("Revert to opening state")
        self.revert_btn.clicked.connect(self._revert_clicked)
        bar.addWidget(self.revert_btn)
        self.restore_backup_btn = QtWidgets.QPushButton("Restore backup")
        self.restore_backup_btn.clicked.connect(self._restore_backup_clicked)
        bar.addWidget(self.restore_backup_btn)
        self.status = QtWidgets.QLabel("")
        self.status.setProperty("muted", True)
        bar.addWidget(self.status)
        bar.addStretch(1)
        # Room left in this bar for SP6's "Test my answers" (ED-9); not built here.
        v.addLayout(bar)

    def reload(self) -> None:
        for row in self.rows:
            row["frame"].setParent(None)
        self.rows.clear()
        self.load_error = ""
        self.review = []
        try:
            store = apply_answers.load_store(self.store_path)
        except apply_answers.AnswerStoreError as exc:
            # A damaged file is shown, never replaced by defaults or saved over.
            self.load_error = (f"The Apply Answers file is damaged ({exc.path}): {exc.reason}. "
                               f"Restore {Path(exc.path).name}.bak or fix the file, then "
                               "reopen this tab.")
            self.status.setText(self.load_error)
            self._update_damaged_controls()
            self._refresh_counts()
            self._update_review_banner()
            return
        entries = store["answers"]
        if self._merge_defaults:
            entries = apply_answers.with_missing_builtins(entries)
        self.review = store["review"]          # kept for the next save
        country_answer = next(
            (str(e.get("answer", "") or "").strip() for e in entries
             if str(e.get("id", "")).strip() == "address_country"), "")
        us_hint = _is_us(country_answer)
        for entry in entries:
            self._append_row(entry, us_hint=us_hint)
        self.status.setText("")
        self._update_damaged_controls()
        self._refresh_counts()
        self._update_review_banner()

    # ---- rows ------------------------------------------------------------------

    def _row_by_id(self, eid: str) -> dict | None:
        return next((r for r in self.rows if r["id"] == eid), None)

    def _make_state_answer_widget(self, us: bool, value: str) -> QtWidgets.QWidget:
        if us:
            widget = QtWidgets.QComboBox()
            options = apply_answers.BUILTINS["address_state"].options
            widget.addItems(["Not set", *options])
            widget.setCurrentText(value if value in options else "Not set")
            return widget
        widget = QtWidgets.QLineEdit(value[:apply_answers.STATE_TEXT_MAX])
        widget.setMaxLength(apply_answers.STATE_TEXT_MAX)
        return widget

    def _build_row(self, entry: dict, *, us_hint: bool | None = None) -> dict:
        eid = str(entry.get("id", "")).strip()
        builtin = apply_answers.BUILTINS.get(eid)
        is_builtin = builtin is not None
        etype = entry.get("type") or (builtin.type if builtin is not None else "text")
        question_text = builtin.question if is_builtin else str(entry.get("question", "") or "")
        answer = str(entry.get("answer", "") or "")

        frame = QtWidgets.QFrame()
        frame.setProperty("card", True)
        outer = QtWidgets.QVBoxLayout(frame)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        head = QtWidgets.QHBoxLayout()
        question_widget: QtWidgets.QWidget
        if is_builtin:
            question_widget = QtWidgets.QLabel(question_text)
            question_widget.setWordWrap(True)
        else:
            question_widget = QtWidgets.QLineEdit(question_text)
        head.addWidget(question_widget, 1)
        confirmed_cb = QtWidgets.QCheckBox("Confirmed")
        confirmed_cb.setChecked(entry.get("confirmed") is True)
        head.addWidget(confirmed_cb)
        delete_btn = None
        if not is_builtin:
            delete_btn = QtWidgets.QPushButton("Delete")
            head.addWidget(delete_btn)
        outer.addLayout(head)

        answer_line = QtWidgets.QHBoxLayout()
        note_edit = None
        note_counter = None
        counter_label = None
        if etype == "yes_no":
            answer_widget: QtWidgets.QWidget = QtWidgets.QComboBox()
            answer_widget.addItems(["Not set", *apply_answers.YES_NO])
            answer_widget.setCurrentText(answer if answer in apply_answers.YES_NO else "Not set")
            answer_line.addWidget(answer_widget, 1)
            note_edit = QtWidgets.QLineEdit(str(entry.get("note", "") or ""))
            note_edit.setMaxLength(apply_answers.NOTE_MAX)
            note_edit.setPlaceholderText("Note (optional)")
            answer_line.addWidget(note_edit, 1)
            note_counter = QtWidgets.QLabel("")
            note_counter.setProperty("muted", True)
            answer_line.addWidget(note_counter)
        elif etype == "number":
            answer_widget = QtWidgets.QLineEdit(answer)
            answer_widget.setValidator(_number_validator(answer_widget))
            answer_line.addWidget(answer_widget, 1)
            note_edit = QtWidgets.QLineEdit(str(entry.get("note", "") or ""))
            note_edit.setMaxLength(apply_answers.NOTE_MAX)
            note_edit.setPlaceholderText("Note (optional)")
            answer_line.addWidget(note_edit, 1)
            note_counter = QtWidgets.QLabel("")
            note_counter.setProperty("muted", True)
            answer_line.addWidget(note_counter)
        elif etype == "choice":
            if eid == "address_state":
                us = True if us_hint is None else us_hint
                answer_widget = self._make_state_answer_widget(us, answer)
            else:
                options = builtin.options if builtin is not None else ()
                answer_widget = QtWidgets.QComboBox()
                answer_widget.addItems(["Not set", *options])
                answer_widget.setCurrentText(answer if answer in options else "Not set")
            answer_line.addWidget(answer_widget, 1)
        else:  # text
            answer_widget = QtWidgets.QPlainTextEdit(answer)
            answer_widget.setMaximumHeight(70)
            answer_line.addWidget(answer_widget, 1)
            counter_label = QtWidgets.QLabel("")
            counter_label.setProperty("muted", True)
            answer_line.addWidget(counter_label, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        outer.addLayout(answer_line)

        preview_label = QtWidgets.QLabel("")
        preview_label.setProperty("muted", True)
        outer.addWidget(preview_label)

        row = {"id": eid, "entry": dict(entry), "type": etype, "is_builtin": is_builtin,
               "frame": frame, "question_widget": question_widget,
               "answer_widget": answer_widget, "answer_line": answer_line,
               "note_edit": note_edit, "note_counter": note_counter,
               "counter_label": counter_label, "confirmed_cb": confirmed_cb,
               "delete_btn": delete_btn, "preview_label": preview_label}

        self._wire_answer_widget(row)
        self._wire_note_edit(row)
        confirmed_cb.toggled.connect(lambda checked, r=row: self._on_confirmed_toggled(r, checked))
        if delete_btn is not None:
            delete_btn.clicked.connect(lambda _=False, r=row: self._delete_clicked(r))

        if note_counter is not None:
            self._update_note_counter(row)
        if counter_label is not None:
            self._update_text_counter(row)
        self._update_row_preview(row)
        return row

    def _append_row(self, entry: dict, *, us_hint: bool | None = None) -> dict:
        row = self._build_row(entry, us_hint=us_hint)
        self._rows_box.insertWidget(self._rows_box.count() - 1, row["frame"])
        self.rows.append(row)
        return row

    def _wire_answer_widget(self, row: dict) -> None:
        widget = row["answer_widget"]
        if isinstance(widget, QtWidgets.QComboBox):
            widget.currentIndexChanged.connect(lambda _i, r=row: self._on_answer_changed(r))
        elif isinstance(widget, QtWidgets.QPlainTextEdit):
            widget.textChanged.connect(lambda r=row: self._on_answer_changed(r))
        else:
            widget.textChanged.connect(lambda _t, r=row: self._on_answer_changed(r))

    def _wire_note_edit(self, row: dict) -> None:
        if row["note_edit"] is None:
            return
        row["note_edit"].textChanged.connect(lambda _t, r=row: self._update_note_counter(r))

    def _replace_answer_widget(self, row: dict, new_widget: QtWidgets.QWidget) -> None:
        layout = row["answer_line"]
        old_widget = row["answer_widget"]
        idx = layout.indexOf(old_widget)
        layout.removeWidget(old_widget)
        old_widget.setParent(None)
        old_widget.deleteLater()
        layout.insertWidget(idx if idx >= 0 else 0, new_widget, 1)
        row["answer_widget"] = new_widget

    # ---- live updates ------------------------------------------------------------

    def _row_answer_text(self, row: dict) -> str:
        widget = row["answer_widget"]
        if isinstance(widget, QtWidgets.QComboBox):
            text = widget.currentText()
            return "" if text == "Not set" else text
        if isinstance(widget, QtWidgets.QPlainTextEdit):
            return widget.toPlainText().strip()
        return widget.text().strip()

    def _entry_snapshot(self, row: dict) -> dict:
        """The entry this row would save right now -- unsaved, live widget state."""
        entry = dict(row["entry"])
        entry["id"] = row["id"]
        entry["type"] = row["type"]
        if row["is_builtin"]:
            entry["question"] = apply_answers.BUILTINS[row["id"]].question
        elif isinstance(row["question_widget"], QtWidgets.QLineEdit):
            entry["question"] = row["question_widget"].text().strip()
        entry["answer"] = self._row_answer_text(row)
        entry["confirmed"] = row["confirmed_cb"].isChecked()
        entry["status"] = "active"
        if row["note_edit"] is not None:
            entry["note"] = row["note_edit"].text().strip()
        return entry

    def _preview_text(self, row: dict) -> str:
        answer = self._row_answer_text(row)
        if not answer:
            return "Forms will get nothing (not set)"
        value = apply_answers.fact_value(self._entry_snapshot(row))
        if not value:
            return "Forms will get nothing until you confirm"
        return "Forms will get: %s" % value

    def _update_row_preview(self, row: dict) -> None:
        row["preview_label"].setText(self._preview_text(row))

    def _update_note_counter(self, row: dict) -> None:
        if row["note_counter"] is not None:
            row["note_counter"].setText(
                "%d/%d" % (len(row["note_edit"].text()), apply_answers.NOTE_MAX))

    def _update_text_counter(self, row: dict) -> None:
        if row["counter_label"] is not None:
            row["counter_label"].setText(
                "%d/%d" % (len(row["answer_widget"].toPlainText()), apply_answers.TEXT_MAX))

    def _refresh_row_highlight(self, row: dict) -> None:
        answer = self._row_answer_text(row)
        warn = (not answer) or (not row["confirmed_cb"].isChecked())
        frame = row["frame"]
        frame.setProperty("card", not warn)
        frame.setProperty("callout", "warning" if warn else None)
        theme.repolish(frame)

    def _refresh_counts(self) -> None:
        not_set = 0
        not_confirmed = 0
        for row in self.rows:
            answer = self._row_answer_text(row)
            if not answer:
                not_set += 1
            elif not row["confirmed_cb"].isChecked():
                not_confirmed += 1
            self._refresh_row_highlight(row)
        noun = "answer" if not_set == 1 else "answers"
        self.counts_label.setText(
            "%d %s not set, %d not confirmed" % (not_set, noun, not_confirmed))

    def _on_answer_changed(self, row: dict, *_args) -> None:
        row["confirmed_cb"].setChecked(True)
        if row["id"] == "address_country":
            self._sync_state_widget()
        if row["counter_label"] is not None:
            self._update_text_counter(row)
        self._update_row_preview(row)
        self._refresh_counts()

    def _on_confirmed_toggled(self, row: dict, _checked: bool) -> None:
        self._update_row_preview(row)
        self._refresh_counts()

    def _sync_state_widget(self) -> None:
        state_row = self._row_by_id("address_state")
        country_row = self._row_by_id("address_country")
        if state_row is None or country_row is None:
            return
        us = _is_us(self._row_answer_text(country_row))
        is_combo = isinstance(state_row["answer_widget"], QtWidgets.QComboBox)
        if us == is_combo:
            return
        old_value = self._row_answer_text(state_row)
        if us:
            keep = (old_value
                    if old_value in apply_answers.BUILTINS["address_state"].options else "")
        else:
            keep = old_value if len(old_value) <= apply_answers.STATE_TEXT_MAX else ""
        new_widget = self._make_state_answer_widget(us, keep)
        self._replace_answer_widget(state_row, new_widget)
        self._wire_answer_widget(state_row)
        self._update_row_preview(state_row)
        self._refresh_row_highlight(state_row)

    # ---- toolbar actions -----------------------------------------------------------

    def collect(self) -> list[dict]:
        return [self._entry_snapshot(row) for row in self.rows]

    def _confirm_all_clicked(self) -> None:
        for row in self.rows:
            if self._row_answer_text(row):
                row["confirmed_cb"].setChecked(True)
        self._refresh_counts()

    def _add_answer_clicked(self) -> None:
        dialog = AddAnswerDialog(self.collect(), parent=self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._append_row(dialog.result_entry())
            self._refresh_counts()

    def _delete_clicked(self, row: dict) -> None:
        question = row["entry"].get("question", "") or row["id"]
        if isinstance(row["question_widget"], QtWidgets.QLineEdit):
            question = row["question_widget"].text().strip() or question
        if QtWidgets.QMessageBox.question(
                self, "Delete answer", "Delete '%s'?" % question
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        row["frame"].setParent(None)
        if row in self.rows:
            self.rows.remove(row)
        self._refresh_counts()

    def validate(self) -> list[str]:
        return apply_answers.validate(self.collect())

    def save(self) -> bool:
        if self.load_error:
            self.status.setText("Not saved: the answers file is damaged.")
            QtWidgets.QMessageBox.critical(self, "Apply answers", self.load_error)
            return False
        answers = self.collect()
        errs = apply_answers.validate(answers)
        if errs:
            self.status.setText("Not saved; see the error.")
            QtWidgets.QMessageBox.critical(self, "Apply answers",
                                           "Problems found:\n\n- " + "\n- ".join(errs))
            return False
        try:
            apply_answers.save(answers, self.store_path, review=self.review)
        except (ValueError, OSError, apply_answers.AnswerStoreError) as exc:
            self.status.setText("Save failed.")
            QtWidgets.QMessageBox.critical(self, "Apply answers", errmsg.for_user(exc))
            return False
        warn = apply_answers.warnings(answers)
        self.reload()
        if warn:
            QtWidgets.QMessageBox.warning(self, "Apply answers", "\n\n".join(warn))
        self.status.setText("Saved.")
        if self.on_saved:
            self.on_saved()
        return True

    def revert(self) -> None:
        # A damaged store has no good snapshot to go back to (self.snapshot is
        # the damaged bytes); reverting would overwrite the file's own .bak with
        # them. Do nothing and say so; the user restores the .bak by hand.
        if self.load_error:
            self.status.setText("Not reverted: the answers file is damaged.")
            return
        if self.snapshot:
            apply_answers.restore_bytes(self.snapshot, self.store_path)
        elif self.store_path.exists():
            self.store_path.unlink()
        self.reload()
        self.status.setText("Reverted to opening state.")

    def _revert_clicked(self) -> None:
        if QtWidgets.QMessageBox.question(
                self, "Revert", "Undo every change since you opened this tab?"
        ) == QtWidgets.QMessageBox.StandardButton.Yes:
            self.revert()

    # ---- damaged store / backup / review -------------------------------------------

    def _update_damaged_controls(self) -> None:
        damaged = bool(self.load_error)
        self.save_btn.setEnabled(not damaged)
        self.revert_btn.setEnabled(not damaged)
        self.add_btn.setEnabled(not damaged)
        self.confirm_all_btn.setEnabled(not damaged)
        self.restore_backup_btn.setEnabled(damaged and self._backup_is_usable())

    def _backup_path(self) -> Path:
        return self.store_path.with_name(self.store_path.name + ".bak")

    def _backup_is_usable(self) -> bool:
        bak = self._backup_path()
        if not bak.exists():
            return False
        try:
            apply_answers.load_store(bak)
        except apply_answers.AnswerStoreError:
            return False
        return True

    def _restore_backup_clicked(self) -> None:
        bak = self._backup_path()
        if QtWidgets.QMessageBox.question(
                self, "Restore backup", "Replace the damaged file with %s?" % bak.name
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        shutil.copy2(str(bak), str(self.store_path))
        self.reload()

    def _update_review_banner(self) -> None:
        if self.review:
            lines = ["We read '%s' as %s" % (r["before"], r["after"]) for r in self.review]
            self.review_label.setText("\n".join(lines))
            self.review_banner.setVisible(True)
        else:
            self.review_label.setText("")
            self.review_banner.setVisible(False)

    def _review_confirmed_clicked(self) -> None:
        self.review = []
        self._update_review_banner()
        self.save()
