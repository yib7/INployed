"""The Apply Answers editor (Qt): manage the master answer store from the dashboard.

A table over `apply_answers.json`: one row per screening-question answer (question,
answer, kind fixed/open-ended). Add / edit / delete. Save validates via
`apply_answers.validate` and backs up to `.bak`; "Revert to opening state" restores
the snapshot taken when the editor opened. Every row is saved active — the
needs-review status (and its filter) was retired in cycle 13.
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
        loader = apply_answers.load_with_defaults if self._merge_defaults else apply_answers.load
        for entry in loader(self.store_path):
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
        row = {"id": str(entry.get("id", "")), "question": question, "answer": answer,
               "kind": kind, "frame": frame}
        delete.clicked.connect(lambda _=False, r=row: self._delete_row(r))
        self._rows_box.insertWidget(self._rows_box.count() - 1, frame)  # before the stretch
        self.rows.append(row)
        return row

    def add_row(self, entry: dict | None = None) -> dict:
        entry = entry or {"id": "", "question": "", "answer": "",
                          "kind": "open-ended", "status": "active"}
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
            out.append({"id": rid, "question": question, "answer": answer,
                        "kind": row["kind"].currentText(), "status": "active"})
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
