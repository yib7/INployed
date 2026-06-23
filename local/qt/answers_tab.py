"""The Apply Answers editor (Qt): manage the master answer store from the dashboard.

A table over `apply_answers.json`: one row per screening-question answer (question,
answer, kind fixed/open-ended, status active/needs-review). Add / edit / delete,
filter to the needs-review items the apply skill flagged. Save validates via
`apply_answers.validate` and backs up to `.bak`; "Revert to opening state" restores
the snapshot taken when the editor opened. The needs-review filter only hides rows
— it never drops them on save.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6 import QtWidgets

from resume_tailor import apply_answers


class AnswersEditor(QtWidgets.QWidget):
    def __init__(self, on_saved: Callable[[], None] | None = None,
                 store_path: Path | None = None, parent=None):
        super().__init__(parent)
        self.on_saved = on_saved
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
        self.filter_check = QtWidgets.QCheckBox("Show needs-review only")
        self.filter_check.stateChanged.connect(self._apply_filter)
        top.addWidget(self.filter_check)
        v.addLayout(top)

        header = QtWidgets.QHBoxLayout()
        for text, stretch in (("Question", 5), ("Answer", 4), ("Kind", 1),
                              ("Status", 1), ("", 1)):
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
        for entry in apply_answers.load(self.store_path):
            self._add_row_widgets(entry)
        self._apply_filter()

    def _add_row_widgets(self, entry: dict) -> dict:
        frame = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(frame)
        h.setContentsMargins(0, 0, 0, 0)
        question = QtWidgets.QLineEdit(str(entry.get("question", "")))
        answer = QtWidgets.QLineEdit(str(entry.get("answer", "")))
        kind = QtWidgets.QComboBox()
        kind.addItems(list(apply_answers.KINDS))
        kind.setCurrentText(str(entry.get("kind", "open-ended")))
        status = QtWidgets.QComboBox()
        status.addItems(list(apply_answers.STATUSES))
        status.setCurrentText(str(entry.get("status", "active")))
        delete = QtWidgets.QPushButton("Delete")
        h.addWidget(question, 5)
        h.addWidget(answer, 4)
        h.addWidget(kind, 1)
        h.addWidget(status, 1)
        h.addWidget(delete, 1)
        row = {"id": str(entry.get("id", "")), "question": question, "answer": answer,
               "kind": kind, "status": status, "frame": frame}
        delete.clicked.connect(lambda _=False, r=row: self._delete_row(r))
        self._rows_box.insertWidget(self._rows_box.count() - 1, frame)  # before the stretch
        self.rows.append(row)
        return row

    def add_row(self, entry: dict | None = None) -> dict:
        entry = entry or {"id": "", "question": "", "answer": "",
                          "kind": "open-ended", "status": "active"}
        row = self._add_row_widgets(entry)
        # A new row defaults to "active", so the needs-review filter would hide it
        # the instant it's added — drop the filter so the user sees what they added.
        self.filter_check.setChecked(False)
        self._apply_filter()
        return row

    def _delete_row(self, row: dict) -> None:
        row["frame"].setParent(None)
        if row in self.rows:
            self.rows.remove(row)

    # ---- actions -------------------------------------------------------------

    def _apply_filter(self, *_):
        only_nr = self.filter_check.isChecked()
        for row in self.rows:
            show = (not only_nr) or row["status"].currentText() == "needs-review"
            row["frame"].setVisible(show)

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
            out.append({"id": rid, "question": question, "answer": answer,
                        "kind": row["kind"].currentText(), "status": row["status"].currentText()})
        return out

    def validate(self) -> list[str]:
        return apply_answers.validate(self.collect())

    def save(self) -> bool:
        answers = self.collect()
        errs = apply_answers.validate(answers)
        if errs:
            self.status.setText("Not saved — see error.")
            QtWidgets.QMessageBox.critical(self, "Apply answers",
                                           "Problems found:\n\n- " + "\n- ".join(errs))
            return False
        try:
            apply_answers.save(answers, self.store_path)
        except (ValueError, OSError) as exc:
            self.status.setText("Save failed.")
            QtWidgets.QMessageBox.critical(self, "Apply answers", str(exc))
            return False
        self.reload()
        self.status.setText("Saved.")
        if self.on_saved:
            self.on_saved()
        return True

    def _validate_clicked(self) -> None:
        errs = self.validate()
        if not errs:
            QtWidgets.QMessageBox.information(self, "Validate", "Looks good — no problems found.")
            self.status.setText("Valid.")
        else:
            QtWidgets.QMessageBox.critical(
                self, "Validate", "Problems found:\n\n- " + "\n- ".join(errs))
            self.status.setText(f"{len(errs)} problem(s) — see the list.")

    def revert(self) -> None:
        if self.snapshot:
            apply_answers.restore_bytes(self.snapshot, self.store_path)
        elif self.store_path.exists():
            self.store_path.unlink()
        self.reload()

    def _revert_clicked(self) -> None:
        if QtWidgets.QMessageBox.question(
                self, "Revert", "Undo every change since you opened this tab?"
        ) == QtWidgets.QMessageBox.StandardButton.Yes:
            self.revert()
            self.status.setText("Reverted to opening state.")
