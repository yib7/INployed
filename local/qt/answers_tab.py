"""The Apply Answers editor (Qt): manage the master answer store from the dashboard.

A table over `apply_answers.json`: one row per screening-question answer (question,
answer, kind fixed/open-ended). Add / edit / delete. Save validates via
`apply_answers.validate` and backs up to `.bak`; "Revert to opening state" restores
the snapshot taken when the editor opened. Every row is saved active; the
needs-review status (and its filter) was retired.

Cycle 18 moved the store to version 2 (typed, confirmed answers). Until SP4
rebuilds this tab, the rows keep their plain widgets: `collect()` carries each
loaded entry's other fields (type, note, confirmed, unknown keys) through, a new
row is a text answer, and a changed answer is confirmed. A damaged store shows
its error and is never saved over.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6 import QtWidgets

import errmsg
from resume_tailor import apply_answers


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
        blurb = QtWidgets.QLabel("Reusable answers the apply helper fills into forms. Mark each "
                                 "fixed (never changed) or open-ended (adaptable per job).")
        blurb.setProperty("muted", True)
        blurb.setWordWrap(True)
        top.addWidget(blurb, 1)
        v.addLayout(top)

        header = QtWidgets.QHBoxLayout()
        for text, stretch in (("Question", 5), ("Answer", 4), ("Kind", 1), ("", 1)):
            lab = QtWidgets.QLabel(text)
            lab.setProperty("muted", True)
            header.addWidget(lab, stretch)
        v.addLayout(header)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        body = QtWidgets.QWidget()
        self._rows_box = QtWidgets.QVBoxLayout(body)
        self._rows_box.addStretch(1)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)

        bar = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save changes")
        save.setProperty("accent", True)
        save.clicked.connect(self.save)
        bar.addWidget(save)
        add = QtWidgets.QPushButton("Add answer")
        add.clicked.connect(lambda: self.add_row())
        bar.addWidget(add)
        val = QtWidgets.QPushButton("Validate")
        val.clicked.connect(self._validate_clicked)
        bar.addWidget(val)
        rev = QtWidgets.QPushButton("Revert to opening state")
        rev.clicked.connect(self._revert_clicked)
        bar.addWidget(rev)
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
        self.review: list[dict] = []
        try:
            store = apply_answers.load_store(self.store_path)
        except apply_answers.AnswerStoreError as exc:
            # A damaged file is shown, never replaced by defaults or saved over.
            self.load_error = (f"The Apply Answers file is damaged ({exc.path}): {exc.reason}. "
                               f"Restore {Path(exc.path).name}.bak or fix the file, then "
                               "reopen this tab.")
            self.status.setText(self.load_error)
            return
        entries = store["answers"]
        if self._merge_defaults:
            entries = apply_answers.with_missing_builtins(entries)
        # kept for the next save, so a migrated store's review list is not lost
        self.review = store["review"]
        for entry in entries:
            self._add_row_widgets(entry)

    def _add_row_widgets(self, entry: dict) -> dict:
        frame = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(frame)
        h.setContentsMargins(0, 0, 0, 0)
        question = QtWidgets.QLineEdit(str(entry.get("question", "")))
        answer = QtWidgets.QLineEdit(str(entry.get("answer", "")))
        # Rows sit under shared column headers, so the inputs carry no
        # per-widget label -- give assistive tech the column names.
        question.setAccessibleName("Question")
        answer.setAccessibleName("Answer")
        kind = QtWidgets.QComboBox()
        kind.setAccessibleName("Kind")
        kind.addItems(list(apply_answers.KINDS))
        kind.setCurrentText(str(entry.get("kind", "open-ended")))
        delete = QtWidgets.QPushButton("Delete")
        h.addWidget(question, 5)
        h.addWidget(answer, 4)
        h.addWidget(kind, 1)
        h.addWidget(delete, 1)
        # "entry" is the loaded record, so collect() carries its other fields
        # (type, note, confirmed and any key this table does not show) through.
        row = {"id": str(entry.get("id", "")), "question": question, "answer": answer,
               "kind": kind, "frame": frame, "entry": dict(entry),
               "loaded_answer": answer.text()}
        delete.clicked.connect(lambda _=False, r=row: self._delete_row(r))
        self._rows_box.insertWidget(self._rows_box.count() - 1, frame)  # before the stretch
        self.rows.append(row)
        return row

    def add_row(self, entry: dict | None = None) -> dict:
        # No "kind" here: a new row is a version 2 row and never had one, so
        # collect() below leaves it out of the saved entry.
        entry = entry or {"id": "", "question": "", "type": "text", "answer": "", "note": "",
                          "confirmed": False, "status": "active"}
        row = self._add_row_widgets(entry)
        return row

    def _delete_row(self, row: dict) -> None:
        row["frame"].setParent(None)
        if row in self.rows:
            self.rows.remove(row)

    # ---- actions -------------------------------------------------------------

    def collect(self) -> list[dict]:
        out: list[dict] = []
        taken: set = set()
        for row in self.rows:
            question = row["question"].text().strip()
            answer = row["answer"].text()
            if not question and not str(answer).strip():
                continue
            rid = row["id"] or apply_answers.new_id(question, taken)
            if rid in taken:
                rid = apply_answers.new_id(question or rid, taken)
            taken.add(rid)
            entry = dict(row["entry"])
            entry.update({"id": rid, "question": question, "answer": answer, "status": "active"})
            if "kind" in row["entry"]:            # only a v1-loaded row had one
                entry["kind"] = row["kind"].currentText()
            entry.setdefault("type", "text")
            entry.setdefault("note", "")
            entry["confirmed"] = entry.get("confirmed") is True
            if answer != row["loaded_answer"]:
                entry["confirmed"] = True       # the user changed it, so they confirmed it
            out.append(entry)
        return out

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
        self.reload()
        self.status.setText("Saved.")
        if self.on_saved:
            self.on_saved()
        return True

    def _validate_clicked(self) -> None:
        if self.load_error:
            QtWidgets.QMessageBox.critical(self, "Validate", self.load_error)
            self.status.setText(self.load_error)
            return
        errs = self.validate()
        if not errs:
            QtWidgets.QMessageBox.information(self, "Validate", "Looks good: no problems found.")
            self.status.setText("Valid.")
        else:
            QtWidgets.QMessageBox.critical(
                self, "Validate", "Problems found:\n\n- " + "\n- ".join(errs))
            self.status.setText(f"{len(errs)} problem(s); see the list.")

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
