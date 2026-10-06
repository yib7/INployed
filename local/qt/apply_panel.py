"""The right-side Apply panel for the dashboard.

When the user clicks Apply on a tailored job, this panel opens beside the job
tables (replacing the bottom score preview) and shows everything needed to fill
the application by hand or with Claude-in-Chrome: copyable LinkedIn / GitHub
links, résumé / cover-letter PDF paths, an Open-folder button, and the full
self-contained apply sheet (apply.md) in a read-only viewer with a one-click
"Copy apply sheet". A Close button hides the panel and restores the score
preview. Nothing here submits.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict

from PySide6 import QtCore, QtGui, QtWidgets
from qt.plaintext import Label

import osopen


class SheetViewer(QtWidgets.QTextBrowser):
    """The rendered apply sheet: a read-only markdown viewer that copies as text.

    A rendered list copies with no `- ` markers, so pasted bullets run together.
    Here a copy is built block by block: a block inside a list gets a `- ` prefix,
    and blocks are separated by a blank line. Only the selected part of the first
    and last block is taken. The `- ` goes on only when the selection reaches back
    to the start of the block; a selection that starts mid-bullet is a quote of
    the words, so it gets no marker. Links are not followed.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)

    def selected_text(self) -> str:
        """The selection as plain text with `- ` bullets and blank-line breaks."""
        sel = self.textCursor()
        start, end = sel.selectionStart(), sel.selectionEnd()
        doc = self.document()
        parts = []
        block = doc.findBlock(start)
        while block.isValid() and block.position() < end:
            lo = max(start, block.position())
            hi = min(end, block.position() + block.length() - 1)
            if hi > lo:
                cur = QtGui.QTextCursor(doc)
                cur.setPosition(lo)
                cur.setPosition(hi, QtGui.QTextCursor.MoveMode.KeepAnchor)
                piece = cur.selectedText().replace("\u2028", "\n").replace("\u00a0", " ")
                if piece.strip():
                    if lo == block.position() and block.textList() is not None:
                        piece = "- " + piece
                    parts.append(piece)
            block = block.next()
        return "\n\n".join(parts)

    def createMimeDataFromSelection(self) -> QtCore.QMimeData:
        mime = QtCore.QMimeData()
        mime.setText(self.selected_text())
        return mime


class _ColumnLabel(Label):
    """A row label as wide as the widest label in its column, so the boxes
    beside them start at one x. It measures at the current type size: a width
    fixed in pixels when the panel was built clipped "Cover letter PDF" at 150%."""

    def __init__(self, text: str, column: list) -> None:
        super().__init__(text)
        self._column = column
        column.append(self)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed,
                           QtWidgets.QSizePolicy.Policy.Preferred)

    def _own_width(self) -> int:
        return super().sizeHint().width()

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        width = max(lab._own_width() for lab in self._column) + 6
        return QtCore.QSize(width, super().sizeHint().height())

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        return self.sizeHint()


class ApplyPanel(QtWidgets.QWidget):
    def __init__(self, on_close: Callable[[], None] | None = None,
                 on_applied: Callable[[], None] | None = None,
                 on_ask_ai: Callable[[], None] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._on_close = on_close or (lambda: None)
        self._on_applied = on_applied or (lambda: None)
        # "Ask AI" opens the per-job chat for whichever job the panel is showing;
        # the owner knows the identity, so this fires with no arguments.
        self._on_ask_ai = on_ask_ai or (lambda: None)
        self._folder: str = ""
        self._raw_md: str = ""   # the apply.md source — rendered in the viewer, copied verbatim
        self._popout: QtWidgets.QDialog | None = None  # the Expand reader, kept alive
        self.setMinimumWidth(320)
        self._build()

    # ---- construction --------------------------------------------------------

    def _build(self) -> None:
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        self._label_column: list = []   # the four path rows' labels

        top = QtWidgets.QHBoxLayout()
        self._title = Label("Apply")
        self._title.setProperty("heading", True)
        self._title.setWordWrap(True)
        top.addWidget(self._title, 1)
        close = QtWidgets.QPushButton("✕")
        close.setToolTip("Close (back to the score preview)")
        close.clicked.connect(lambda: self._on_close())
        top.addWidget(close)
        v.addLayout(top)

        hint = Label(
            "For portals that don't auto-fill from your résumé upload: paste the apply sheet into "
            "Claude-in-Chrome to fill the fields by hand; it stops before the final Submit. Review "
            "every field and submit it yourself.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        v.addWidget(hint)

        # Profile links + document paths (copyable). LinkedIn and GitHub sit above
        # the résumé so they're the first thing pasted into the application form.
        link_tip = "Copy to paste into the application form"
        self._linkedin_row, self._linkedin_edit, _ = self._path_row(
            "LinkedIn", tooltip=link_tip)
        v.addLayout(self._linkedin_row)
        self._github_row, self._github_edit, _ = self._path_row(
            "GitHub", tooltip=link_tip)
        v.addLayout(self._github_row)
        self._resume_row, self._resume_edit, _ = self._path_row("Résumé PDF")
        v.addLayout(self._resume_row)
        self._cover_row, self._cover_edit, _ = self._path_row("Cover letter PDF")
        v.addLayout(self._cover_row)

        tools = QtWidgets.QHBoxLayout()
        self._open_btn = QtWidgets.QPushButton("Open folder")
        self._open_btn.clicked.connect(self._open_folder)
        tools.addWidget(self._open_btn)
        self.ask_ai_btn = QtWidgets.QPushButton("Ask AI")
        self.ask_ai_btn.setToolTip(
            "Chat about this job: its apply sheet, bullets and cover letter")
        self.ask_ai_btn.clicked.connect(lambda: self._on_ask_ai())
        tools.addWidget(self.ask_ai_btn)
        v.addLayout(tools)

        sheet_row = QtWidgets.QHBoxLayout()
        sheet_label = Label("Apply sheet (apply.md)")
        sheet_label.setProperty("muted", True)
        sheet_row.addWidget(sheet_label)
        sheet_row.addStretch(1)
        self._expand_btn = QtWidgets.QPushButton("Expand ⤢")
        self._expand_btn.setToolTip("Open the apply sheet in a larger, resizable window")
        self._expand_btn.clicked.connect(self._pop_out)
        sheet_row.addWidget(self._expand_btn)
        v.addLayout(sheet_row)
        # Rendered markdown viewer (nice to read). The clipboard still gets the raw
        # markdown source via copy_sheet() — see self._raw_md. Selecting text and copying
        # it gives `- ` bullets spaced by blank lines (SheetViewer). Read-only; no links.
        self._sheet = SheetViewer()
        self._sheet.setAccessibleName("Apply sheet")
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

    def _path_row(self, label: str, tooltip: str | None = None):
        row = QtWidgets.QHBoxLayout()
        lab = _ColumnLabel(label, self._label_column)
        lab.setProperty("muted", True)
        edit = QtWidgets.QLineEdit()
        edit.setAccessibleName(label)
        edit.setReadOnly(True)
        copy = QtWidgets.QPushButton("Copy")
        copy.clicked.connect(lambda: self._copy_text(edit.text()))
        if tooltip:
            lab.setToolTip(tooltip)
            edit.setToolTip(tooltip)
            copy.setToolTip(tooltip)
        row.addWidget(lab)
        row.addWidget(edit, 1)
        row.addWidget(copy)
        return row, edit, lab

    # ---- population ----------------------------------------------------------

    def show_application(self, ctx: Dict[str, Any]) -> None:
        job = ctx.get("job") or {}
        title = job.get("title") or "Role"
        company = job.get("company") or "?"
        self._title.setText(f"Apply: {title} @ {company}")
        self._folder = ctx.get("generated_dir", "") or ""

        links = ctx.get("links") or {}
        linkedin = links.get("LinkedIn", "") or ""
        self._linkedin_edit.setText(linkedin)
        self._set_row_visible(self._linkedin_row, bool(linkedin))
        github = links.get("GitHub", "") or ""
        self._github_edit.setText(github)
        self._set_row_visible(self._github_row, bool(github))

        self._resume_edit.setText(ctx.get("resume_pdf", "") or "")
        cover = ctx.get("cover_letter_pdf", "") or ""
        self._cover_edit.setText(cover)
        self._set_row_visible(self._cover_row, bool(cover))
        self._open_btn.setEnabled(bool(self._folder))

        self._raw_md = ctx.get("apply_md", "") or ""
        self._sheet.setMarkdown(self._raw_md)

    @staticmethod
    def _set_row_visible(row: QtWidgets.QHBoxLayout, visible: bool) -> None:
        for i in range(row.count()):
            w = row.itemAt(i).widget()
            if w is not None:
                w.setVisible(visible)

    # ---- actions -------------------------------------------------------------

    def current_sheet(self) -> str:
        return self._raw_md

    def copy_sheet(self) -> None:
        self._copy_text(self._raw_md)

    @staticmethod
    def _copy_text(text: str) -> None:
        QtWidgets.QApplication.clipboard().setText(text or "")

    def _open_folder(self) -> None:
        folder = self._folder
        if folder and Path(folder).exists():
            try:
                osopen.open_path(folder)
            except OSError:
                pass

    def _pop_out(self) -> QtWidgets.QDialog:
        """Open the apply sheet in a large, resizable, non-modal reader — the same
        rendered markdown as the side panel, with its own Copy. The compact panel
        is unchanged; this just gives more room to read without scrunching."""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(self._title.text() or "Apply sheet")
        dlg.resize(720, 800)
        lay = QtWidgets.QVBoxLayout(dlg)
        viewer = SheetViewer()
        viewer.setMarkdown(self._raw_md)
        lay.addWidget(viewer, 1)
        bar = QtWidgets.QHBoxLayout()
        bar.addStretch(1)
        copy = QtWidgets.QPushButton("Copy apply sheet")
        copy.setProperty("accent", True)
        copy.clicked.connect(self.copy_sheet)   # clipboard gets the RAW md, as elsewhere
        bar.addWidget(copy)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(dlg.close)
        bar.addWidget(close)
        lay.addLayout(bar)
        self._popout = dlg   # keep a reference so the window isn't garbage-collected
        dlg.show()
        return dlg
