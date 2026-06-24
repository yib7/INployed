"""The right-side Apply panel for the dashboard.

When the user clicks Apply on a tailored job, this panel opens beside the job
tables (replacing the bottom score preview) and shows everything needed to fill
the application by hand or with Claude-in-Chrome: copyable résumé / cover-letter
PDF paths, an Open-folder button, and the full self-contained apply sheet
(apply.md) in a read-only viewer with a one-click "Copy apply sheet". A Close
button hides the panel and restores the score preview. Nothing here submits.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict

from PySide6 import QtWidgets


class ApplyPanel(QtWidgets.QWidget):
    def __init__(self, on_close: Callable[[], None] | None = None,
                 on_applied: Callable[[], None] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._on_close = on_close or (lambda: None)
        self._on_applied = on_applied or (lambda: None)
        self._folder: str = ""
        self.setMinimumWidth(320)
        self._build()

    # ---- construction --------------------------------------------------------

    def _build(self) -> None:
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

        top = QtWidgets.QHBoxLayout()
        self._title = QtWidgets.QLabel("Apply")
        self._title.setProperty("heading", True)
        self._title.setWordWrap(True)
        top.addWidget(self._title, 1)
        close = QtWidgets.QPushButton("✕")
        close.setFixedWidth(34)
        close.setToolTip("Close — back to the score preview")
        close.clicked.connect(lambda: self._on_close())
        top.addWidget(close)
        v.addLayout(top)

        hint = QtWidgets.QLabel(
            "Paste the apply sheet into Claude-in-Chrome to fill the form — it stops before "
            "the final Submit. Review every field and submit it yourself.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        v.addWidget(hint)

        # Document paths (copyable)
        self._resume_row, self._resume_edit = self._path_row("Résumé PDF")
        v.addLayout(self._resume_row)
        self._cover_row, self._cover_edit = self._path_row("Cover letter PDF")
        v.addLayout(self._cover_row)

        self._open_btn = QtWidgets.QPushButton("Open folder")
        self._open_btn.clicked.connect(self._open_folder)
        v.addWidget(self._open_btn)

        sheet_label = QtWidgets.QLabel("Apply sheet (apply.md)")
        sheet_label.setProperty("muted", True)
        v.addWidget(sheet_label)
        self._sheet = QtWidgets.QPlainTextEdit()
        self._sheet.setReadOnly(True)
        self._sheet.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        v.addWidget(self._sheet, 1)

        copy = QtWidgets.QPushButton("Copy apply sheet")
        copy.setProperty("accent", True)
        copy.clicked.connect(self.copy_sheet)
        v.addWidget(copy)

        # Completion action: confirm-then-record in the tracker, and close the panel
        # (so it doubles as the exit). Green to read as the "done with this one" step.
        self.applied_btn = QtWidgets.QPushButton("I applied to this job")
        self.applied_btn.setProperty("applyReady", True)
        self.applied_btn.setToolTip("Add this job to your application tracker (applied) and close")
        self.applied_btn.clicked.connect(lambda: self._on_applied())
        v.addWidget(self.applied_btn)

    def _path_row(self, label: str):
        row = QtWidgets.QHBoxLayout()
        lab = QtWidgets.QLabel(label)
        lab.setProperty("muted", True)
        lab.setFixedWidth(96)
        edit = QtWidgets.QLineEdit()
        edit.setReadOnly(True)
        copy = QtWidgets.QPushButton("Copy")
        copy.setFixedWidth(56)
        copy.clicked.connect(lambda: self._copy_text(edit.text()))
        row.addWidget(lab)
        row.addWidget(edit, 1)
        row.addWidget(copy)
        return row, edit

    # ---- population ----------------------------------------------------------

    def show_application(self, ctx: Dict[str, Any]) -> None:
        job = ctx.get("job") or {}
        title = job.get("title") or "Role"
        company = job.get("company") or "?"
        self._title.setText(f"Apply — {title} @ {company}")
        self._folder = ctx.get("generated_dir", "") or ""

        self._resume_edit.setText(ctx.get("resume_pdf", "") or "")
        cover = ctx.get("cover_letter_pdf", "") or ""
        self._cover_edit.setText(cover)
        self._set_row_visible(self._cover_row, bool(cover))
        self._open_btn.setEnabled(bool(self._folder))

        self._sheet.setPlainText(ctx.get("apply_md", "") or "")

    @staticmethod
    def _set_row_visible(row: QtWidgets.QHBoxLayout, visible: bool) -> None:
        for i in range(row.count()):
            w = row.itemAt(i).widget()
            if w is not None:
                w.setVisible(visible)

    # ---- actions -------------------------------------------------------------

    def current_sheet(self) -> str:
        return self._sheet.toPlainText()

    def copy_sheet(self) -> None:
        self._copy_text(self._sheet.toPlainText())

    @staticmethod
    def _copy_text(text: str) -> None:
        QtWidgets.QApplication.clipboard().setText(text or "")

    def _open_folder(self) -> None:
        folder = self._folder
        if folder and Path(folder).exists():
            try:
                os.startfile(folder)  # noqa: S606
            except OSError:
                pass
