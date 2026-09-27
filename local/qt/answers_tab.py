"""The Apply Answers editor (Qt): manage the master answer store from the dashboard.

Cycle 18 (SP4) rebuilds this tab around the version 2 typed store (SP1): one row
per answer, widgeted by its type so the run reads a saved answer one way only. A yes/no row is a combo of Not set/Yes/No; a number row is a line
edit shaped to the store's number pattern; a choice row is a combo of Not set
plus its options (`address_state` swaps between the US state list and free text
as the address_country row changes); a text row is a multi-line box with a live
counter. A built-in's question is a read-only label; a custom question is
editable, and only a custom row deletes (after a confirmation). Under each row
a preview line reads `apply_answers.fact_value` on the row's current, unsaved
state. Changing an answer confirms its row; the top line counts unset and
unconfirmed answers, and those rows carry the theme's warning highlight.
"Add answer" opens `AddAnswerDialog`, whose OK stays disabled while the
candidate is a question the run fills from a built-in (`builtin_answering`)
or another custom answer's question, or fails `validate`; `add_answer(prefill)`
opens it prefilled with a form's question (PR-9), for the Auto-apply tab's
Pre-answer and a parked question's Answer now. Save runs the same
checks (blocking on problems) and shows `warnings` after a clean write. A
damaged store shows its error, keeps its file as it is (no defaults drawn or
saved over it), and offers "Restore backup" only when a good `.bak` sits next to it; the restore
keeps the damaged file as `<name>.damaged`. A migration's review list shows as
a banner that asks the user to tick Confirmed on each answer; "I've checked
these" dismisses it and saves.

"Test my answers" (`ED-9`, SP6) runs the shipped screening set
(`apply_screening.run_screening`) over the saved, confirmed answers on disk
with the judge the Auto-apply judge setting names (`_current_jev_mode`, read by
`jev_switch.apply_mode`, the reader `local/apply_run.py` uses), on a worker
thread (`qt.workers.run_async`), and shows the picks in
`TestAnswersDialog` with the mode named in the result line. The test is a Jev
use, so the button is off while `jev_switch.apply_blocked` (the Jev gate
`local/apply_run.py` asks) names a reason, with that sentence: Jev switched off
stops every mode, and the key and SDK checks apply to the live "typesafe" mode
only. The test is a probe, so the fake and replay judges run here; the
Auto-apply panel's Start refuses them as the drain does
(`jev_switch.start_blocked`).
The button stays off while a run is going (`_test_running`), and
`refresh_test_answers_state` re-reads the mode, the switch and the key after a
Settings save and at each click.
`apply_screening` is imported inside the worker closure; the judge factory is
a constructor parameter so tests inject `jev.FakeJev`.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

import apply_facts
import errmsg
import jev
import jev_switch
from qt import theme, workers
from apply_pause import builtin_answering  # noqa: F401  (re-exported for the tests)
from resume_tailor import apply_answers

# The built-in's own text is authoritative for the address rules. It is
# hard-coded here: BUILTINS["address_country"] already lists it as one of its
# own options.
_US_COUNTRY = "United States"

# The legal and experience seeds "Confirm all" leaves for the user's own tick
# while they still hold the seed value, with the name the status line gives each.
_OWN_TICK = {"work_authorized": "work authorization",
             "requires_sponsorship": "sponsorship",
             "years_experience": "years of experience",
             "authorization_statement": "work-authorization statement"}


# `builtin_answering` lives in `apply_pause` (the run's "save for future runs"
# refuses the same questions, PR-6); the dialog and Save read it from there.


# "The <this>." in the Add answer dialog, "the <this>; ..." in a Save message.
_ANSWERED_BY = ("built-in answer '%s' already answers this question, and the run "
                "fills it from that answer")


def _own_question_problems(answers: list[dict]) -> list[str]:
    """Save's check that no custom answer holds a question the run fills from a
    built-in in other words (the store's `validate` checks the exact words)."""
    problems = []
    for e in answers:
        eid = str(e.get("id", "")).strip()
        question = str(e.get("question", "") or "").strip()
        if eid in apply_answers.BUILTINS or not question:
            continue
        if apply_answers.find_collision(question, []):
            continue      # `validate` names it
        hit = builtin_answering(question)
        if hit:
            problems.append("answer '%s': the %s; delete this custom answer or change "
                            "its question" % (eid, _ANSWERED_BY % hit))
    return problems


# The review banner's first line, above one "We read ..." line per answer.
_REVIEW_HEAD = ("The update read these answers from your old file. Tick Confirmed on "
                "each one that is right: the run leaves an answer out until you confirm it.")

# The custom-answer type picker's labels, in `apply_answers.CUSTOM_TYPES` order.
_TYPE_LABELS = {"text": "Text", "yes_no": "Yes/No", "number": "Number"}
_LABEL_TYPES = {v: k for k, v in _TYPE_LABELS.items()}

# ST-1's number shape (`\d{1,2}(\.5)?`), written so every valid prefix a user
# types is already a complete match, with no "keep typing" state to model. The
# 0-60 range is a separate check `validate()` makes at save.
_NUMBER_SHAPE = r"^\d{0,2}(\.5?)?$"


def _is_us(answer: str) -> bool:
    return (answer or "").strip() in ("", _US_COUNTRY)


def _number_validator(parent=None) -> QtGui.QRegularExpressionValidator:
    return QtGui.QRegularExpressionValidator(QtCore.QRegularExpression(_NUMBER_SHAPE), parent)


# --- ED-9: "Test my answers" -------------------------------------------------------

def _typesafe_key_present() -> bool:
    """Whether `jev.KEY_ENV` (TYPESAFE_API_KEY) is saved in Settings
    (`jev_switch.key_saved`, the probe the Auto-apply panel's Start uses) or
    set in the environment: presence only, the value itself is never read,
    printed or logged here."""
    return jev_switch.key_saved() or bool(os.environ.get(jev.KEY_ENV, "").strip())


def _current_jev_mode() -> str:
    """The Auto-apply judge mode, read by `jev_switch.apply_mode`, the one
    reader the Auto-apply panel's Start and `local/apply_run.py` use: the
    setting (`auto_apply_jev_mode`), else "typesafe". Importing `apply_run.py`
    here would pull in the whole auto-apply module graph (`apply_form`,
    `apply_queue`, `apply_trace`, `ats_accounts`, Playwright-adjacent code)."""
    return jev_switch.apply_mode()


def _default_judge_factory():
    """The judge the Auto-apply judge setting names, via the same `jev.get`
    factory `local/apply_run.py` calls for a real run. "fake" (and "replay")
    build a key-free judge that makes no live request, safe to construct for
    real; "typesafe" is the only mode whose key and SDK the Jev gate in
    `refresh_test_answers_state` checks. A click gets here only once that
    gate passes, and it names a mode `jev.get` does not build first."""
    return jev.get(_current_jev_mode())


def _usage_delta(before: dict, after: dict) -> dict:
    return {"requests": after["requests"] - before["requests"],
            "input_tokens": after["input_tokens"] - before["input_tokens"],
            "usd": after["usd"] - before["usd"]}


def _spend_text(mode: str, delta: dict) -> str:
    """The run's result line: the configured judge mode, then, for the live
    judge, its reported cost or its request count. A fake or replay judge is
    free and makes no requests, and the line says so."""
    if mode != "typesafe":
        return "Judge: %s. The %s judge is free and makes no requests." % (mode, mode)
    usd = delta.get("usd") or 0.0
    requests = int(delta.get("requests") or 0)
    noun = "request" if requests == 1 else "requests"
    if usd:
        body = "This run cost about $%.4f (%d %s)." % (usd, requests, noun)
    else:
        body = "%d live %s made (no cost reported)." % (requests, noun)
    return "Judge: %s. %s" % (mode, body)


class TestAnswersDialog(QtWidgets.QDialog):
    """ED-9's result: one row per shipped screening question, the run's pick
    for it or "stops here", and the run's spend line."""

    def __init__(self, rows: list, spend_text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Test my answers")
        v = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "Ran the shipped screening questions against your saved, confirmed "
            "answers.")
        note.setWordWrap(True)
        note.setProperty("muted", True)
        v.addWidget(note)

        self.table = QtWidgets.QTableWidget(len(rows), 2)
        self.table.setHorizontalHeaderLabels(["Question", "The run would answer"])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        for r, row in enumerate(rows):
            self.table.setItem(r, 0, QtWidgets.QTableWidgetItem(str(row.question)))
            text = row.answer if row.answer is not None else "stops here"
            self.table.setItem(r, 1, QtWidgets.QTableWidgetItem(str(text)))
        self.table.resizeColumnsToContents()
        theme.register_table(self.table)
        v.addWidget(self.table, 1)

        self.spend_label = QtWidgets.QLabel(spend_text)
        self.spend_label.setProperty("muted", True)
        v.addWidget(self.spend_label)

        box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        v.addWidget(box)


class AddAnswerDialog(QtWidgets.QDialog):
    """"Add answer" (ED-4): question, type, answer, and a note where the type
    has one. OK stays disabled while the run would fill the question from a
    built-in (`builtin_answering`), another custom answer already has it
    (`apply_answers.find_collision`), or the candidate entry fails
    `apply_answers.validate` run against the existing answers; the reason
    shows under the fields. A narrower question the run hands to a custom
    answer (another country, a city, a visa type) is accepted.

    `prefill` (PR-9: the difficulty check's Pre-answer and a parked question's
    Answer now) holds a form's question: "question" (its label), "help",
    "type" and "options". The question box gets `apply_facts.saved_question`
    (the label plus its help, so the run's own-question gate matches the same
    field next time) and the type its widget's (`apply_facts.answer_type` when
    none is given). A custom answer holds text, yes / no or a number, so a
    "choice" is saved as text picked from the form's options in an editable
    box. A muted hint shows the form's help and options. The refusals above
    apply to a prefilled question as they do to a typed one.
    """

    def __init__(self, existing_answers: list[dict], parent=None, *,
                 prefill: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add answer")
        self._existing = list(existing_answers)
        self.answer_widget: QtWidgets.QWidget | None = None
        prefill = dict(prefill or {})
        self._options = [str(o) for o in prefill.get("options") or [] if str(o).strip()]
        self._build()
        self._rebuild_answer_widget()
        if prefill:
            self._apply_prefill(prefill)
        self._recompute()

    def _apply_prefill(self, prefill: dict) -> None:
        help_text = str(prefill.get("help") or "").strip()
        wanted = str(prefill.get("type") or "").strip() or \
            apply_facts.answer_type("text", self._options)
        etype = wanted if wanted in _TYPE_LABELS else "text"
        self.type_combo.setCurrentText(_TYPE_LABELS[etype])
        self.question_edit.setText(
            apply_facts.saved_question(str(prefill.get("question") or ""), help_text))
        lines = []
        if help_text:
            lines.append("The form's help text: %s" % help_text)
        if self._options:
            lines.append("The form's options: %s" % "; ".join(self._options))
        if wanted == "choice":
            lines.append("Saved as text: pick the option the run should choose.")
        self.hint_label.setText("\n".join(lines))
        self.hint_label.setVisible(bool(lines))

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

        self.hint_label = QtWidgets.QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setProperty("muted", True)
        self.hint_label.setVisible(False)
        v.addWidget(self.hint_label)

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
        elif self._options:
            # a prefilled choice (PR-9): the form's options to pick from, or type
            widget = QtWidgets.QComboBox()
            widget.setEditable(True)
            widget.addItems(self._options)
            widget.setCurrentIndex(-1)
            widget.setEditText("")
            widget.currentTextChanged.connect(self._recompute)
        else:
            widget = QtWidgets.QLineEdit()
            widget.textChanged.connect(self._recompute)
        self._answer_row.addWidget(widget)
        self.answer_widget = widget
        self.note_edit.setVisible(etype in ("yes_no", "number"))

    def _answer_text(self) -> str:
        if isinstance(self.answer_widget, QtWidgets.QComboBox):
            text = self.answer_widget.currentText().strip()
            return "" if text == "Not set" and not self.answer_widget.isEditable() else text
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
        owner = builtin_answering(question)
        custom = "" if owner else apply_answers.find_collision(question, self._existing)
        if owner or custom:
            self.message_label.setText(
                "The %s." % (_ANSWERED_BY % owner) if owner else
                "Your custom answer '%s' already has this question." % custom)
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
                 store_path: Path | None = None, parent=None,
                 judge_factory: Callable[[], object] | None = None):
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
        # ED-9: the live judge by default; tests pass a fake/stub factory.
        self._judge_factory = judge_factory or _default_judge_factory
        self._test_running = False      # a Test my answers run is going

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
            "typed, so a form gets the answer you picked, or a blank.")
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
        self.test_answers_btn = QtWidgets.QPushButton("Test my answers")
        self.test_answers_btn.clicked.connect(self._test_answers_clicked)
        bar.addWidget(self.test_answers_btn)
        self.status = QtWidgets.QLabel("")
        self.status.setProperty("muted", True)
        bar.addWidget(self.status)
        bar.addStretch(1)
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
            self.refresh_test_answers_state()
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
        self.refresh_test_answers_state()

    # ---- rows ------------------------------------------------------------------

    def _row_by_id(self, eid: str) -> dict | None:
        return next((r for r in self.rows if r["id"] == eid), None)

    @staticmethod
    def _option_combo(options, value: str) -> tuple[QtWidgets.QComboBox, str]:
        """A combo of Not set plus `options`, on the option `value` names
        (`apply_answers.match_option`: "Yes " and "yes" are Yes). A stored value
        that names no option is added as its own item and selected, so a save
        keeps it and `validate` names it; the second item of the pair is that
        value, else ""."""
        widget = QtWidgets.QComboBox()
        widget.addItems(["Not set", *options])
        raw = (value or "").strip()
        option = apply_answers.match_option(raw, tuple(options))
        if option:
            widget.setCurrentText(option)
            return widget, ""
        if not raw:
            return widget, ""
        widget.addItem(raw)
        widget.setCurrentIndex(widget.count() - 1)
        return widget, raw

    def _make_state_answer_widget(self, us: bool,
                                  value: str) -> tuple[QtWidgets.QWidget, str]:
        """The address_state answer widget and its unmatched stored value."""
        if us:
            return self._option_combo(apply_answers.BUILTINS["address_state"].options, value)
        widget = QtWidgets.QLineEdit(value[:apply_answers.STATE_TEXT_MAX])
        widget.setMaxLength(apply_answers.STATE_TEXT_MAX)
        return widget, ""

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
        unmatched = ""
        if etype in ("yes_no", "number"):
            if etype == "yes_no":
                answer_widget, unmatched = self._option_combo(apply_answers.YES_NO, answer)
            else:
                answer_widget = QtWidgets.QLineEdit(answer)
                answer_widget.setValidator(_number_validator(answer_widget))
            answer_line.addWidget(answer_widget, 1)
            # No max length: a loaded note keeps every character (a migrated
            # note is the user's own old text); the counter and Save's
            # `validate` hold it to NOTE_MAX.
            note_edit = QtWidgets.QLineEdit(str(entry.get("note", "") or ""))
            note_edit.setPlaceholderText("Note (optional)")
            answer_line.addWidget(note_edit, 1)
            note_counter = QtWidgets.QLabel("")
            note_counter.setProperty("muted", True)
            answer_line.addWidget(note_counter)
        elif etype == "choice":
            if eid == "address_state":
                us = True if us_hint is None else us_hint
                answer_widget, unmatched = self._make_state_answer_widget(us, answer)
            else:
                options = builtin.options if builtin is not None else ()
                answer_widget, unmatched = self._option_combo(options, answer)
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
               "delete_btn": delete_btn, "preview_label": preview_label,
               "unmatched": unmatched}

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
        """The entry this row would save right now, read from the live widgets."""
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
        if self._is_unmatched(row, answer):
            return ("Forms will get nothing: '%s' is not one of the options, so pick one"
                    % answer)
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

    @staticmethod
    def _is_unmatched(row: dict, answer: str) -> bool:
        """True while a combo shows the stored value that names none of its options."""
        return bool(row.get("unmatched")) and answer == row["unmatched"]

    def _refresh_row_highlight(self, row: dict) -> None:
        answer = self._row_answer_text(row)
        warn = ((not answer) or (not row["confirmed_cb"].isChecked())
                or self._is_unmatched(row, answer))
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
            keep = apply_answers.match_option(
                old_value, apply_answers.BUILTINS["address_state"].options) or ""
        else:
            keep = old_value if len(old_value) <= apply_answers.STATE_TEXT_MAX else ""
        new_widget, state_row["unmatched"] = self._make_state_answer_widget(us, keep)
        self._replace_answer_widget(state_row, new_widget)
        self._wire_answer_widget(state_row)
        self._update_row_preview(state_row)
        self._refresh_row_highlight(state_row)

    # ---- toolbar actions -----------------------------------------------------------

    def collect(self) -> list[dict]:
        return [self._entry_snapshot(row) for row in self.rows]

    def _confirm_all_clicked(self) -> None:
        # The legal answers still holding their starting value wait for their
        # own tick: a seed the user has not looked at is a guess about them.
        seeds = {e["id"]: e["answer"] for e in apply_answers.seed_defaults()}
        left = []
        for row in self.rows:
            answer = self._row_answer_text(row)
            if not answer or row["confirmed_cb"].isChecked():
                continue
            if row["id"] in _OWN_TICK and answer == seeds.get(row["id"]):
                left.append(_OWN_TICK[row["id"]])
                continue
            row["confirmed_cb"].setChecked(True)
        self._refresh_counts()
        if left:
            self.status.setText(
                "Confirmed the rest. These still hold the starting value, so tick "
                "each one yourself: %s." % ", ".join(left))
        else:
            self.status.setText("Confirmed every answer that is set.")

    def _add_answer_clicked(self) -> None:
        self.add_answer()

    def add_answer(self, prefill: dict | None = None) -> bool:
        """Open Add answer, prefilled from a form's question when `prefill` is
        given (PR-9, `AddAnswerDialog`), and append the new row unsaved: the
        status line asks for Save changes. True when a row was added; False
        when the dialog was cancelled or the store is damaged (nothing opens
        then, and the status line shows the damage)."""
        if self.load_error:
            self.status.setText(self.load_error)
            return False
        dialog = AddAnswerDialog(self.collect(), parent=self, prefill=prefill)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return False
        entry = dialog.result_entry()
        self._append_row(entry)
        self._refresh_counts()
        self.status.setText("Added '%s'. Click Save changes to keep it." % entry["question"])
        return True

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
        errs = apply_answers.validate(answers) + _own_question_problems(answers)
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

    def _damaged_copy_path(self) -> Path:
        return self.store_path.with_name(self.store_path.name + ".damaged")

    def _restore_backup_clicked(self) -> None:
        bak = self._backup_path()
        damaged = self._damaged_copy_path()
        if QtWidgets.QMessageBox.question(
                self, "Restore backup",
                "Replace the damaged file with %s? The damaged file is kept as %s."
                % (bak.name, damaged.name)
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            # The damaged bytes may hold the newest answers; keep them first.
            shutil.copyfile(str(self.store_path), str(damaged))
            shutil.copy2(str(bak), str(self.store_path))
            restored = self.store_path.read_bytes()
        except OSError as exc:
            self.status.setText("Restore failed.")
            QtWidgets.QMessageBox.critical(self, "Restore backup", errmsg.for_user(exc))
            return
        self.reload()
        # Revert now goes back to the restored answers.
        self.snapshot = restored
        self.status.setText("Restored %s. The damaged file is kept as %s."
                            % (bak.name, damaged.name))

    def _update_review_banner(self) -> None:
        if self.review:
            lines = ["We read '%s' as %s" % (r["before"], r["after"]) for r in self.review]
            self.review_label.setText("\n".join([_REVIEW_HEAD, *lines]))
            self.review_banner.setVisible(True)
        else:
            self.review_label.setText("")
            self.review_banner.setVisible(False)

    def _review_confirmed_clicked(self) -> None:
        """Dismiss the review list. It confirms no answer; each keeps its own tick."""
        keep = self.review
        self.review = []
        if not self.save():
            self.review = keep
        self._update_review_banner()

    # ---- ED-9: "Test my answers" ----------------------------------------------------

    def refresh_test_answers_state(self) -> str:
        """Re-read the judge mode, the Jev switch and the key, and set the
        button. The main window calls it after a Settings save, so a switch
        flipped or a key set there counts at once.

        The test is a Jev use (SP1 review A): while `jev_switch.apply_blocked`
        names a reason, the button is off with that sentence, the one the
        Auto-apply panel's Start shows for the live judge. Jev switched off
        stops every mode; the key and SDK checks skip the fake and replay
        judges, which run here (a probe) while Start refuses them as the drain
        does. A mode `jev.get` does not build is named first
        (`jev_switch.unknown_mode`, SP1 follow-up 3), in the drain's sentence.
        Returns the sentence, "" when the gate is open."""
        mode = _current_jev_mode()
        live = mode not in jev_switch.TEST_MODES
        # The saved-key probe reads the settings files; a test judge skips it.
        blocked = jev_switch.unknown_mode(mode) or jev_switch.apply_blocked(
            mode=mode, saved_key=live and _typesafe_key_present())
        self.test_answers_btn.setEnabled(
            not blocked and not self.load_error and not self._test_running)
        if self.load_error:
            self.test_answers_btn.setToolTip("Fix the damaged answers file first.")
        elif blocked:
            self.test_answers_btn.setToolTip(blocked)
        elif live:
            self.test_answers_btn.setToolTip(
                "Uses the Auto-apply judge setting (currently: %s). Runs the "
                "shipped screening questions with the live judge. Uses your saved, "
                "confirmed answers. Save first to include new edits. Costs a small "
                "live-request fee per click." % mode)
        else:
            self.test_answers_btn.setToolTip(
                "Uses the Auto-apply judge setting (currently: %s). Runs the "
                "shipped screening questions. Uses your saved, confirmed answers. "
                "Save first to include new edits. This mode makes no live request "
                "and costs nothing." % mode)
        return blocked

    def _test_answers_clicked(self) -> None:
        blocked = self.refresh_test_answers_state()   # the switch may have moved since
        if blocked:
            self.status.setText(blocked)
            return
        store_path = self.store_path
        judge_factory = self._judge_factory
        mode = _current_jev_mode()
        self._test_running = True
        self.test_answers_btn.setEnabled(False)
        self.status.setText("Testing your answers (%s judge)..." % mode)

        def work():
            import apply_screening   # imported inside the worker
            answers = apply_answers.load(store_path)   # the saved, confirmed store
            judge = judge_factory()
            before = jev.usage()
            rows = apply_screening.run_screening(answers, judge)
            after = jev.usage()
            return rows, _usage_delta(before, after)

        workers.run_async(self, work,
                          on_done=lambda result: self._test_answers_done(result, mode),
                          on_error=self._test_answers_failed)

    def _test_answers_done(self, result, mode: str) -> None:
        rows, spend = result
        self._test_running = False
        self.refresh_test_answers_state()
        self.status.setText("Tested your answers.")
        TestAnswersDialog(rows, _spend_text(mode, spend), parent=self).exec()

    def _test_answers_failed(self, exc) -> None:
        self._test_running = False
        self.refresh_test_answers_state()
        self.status.setText("Test my answers failed.")
        QtWidgets.QMessageBox.critical(self, "Test my answers", errmsg.for_user(exc))
