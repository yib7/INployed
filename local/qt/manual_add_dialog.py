"""The "Add a job by hand" form (PySide6 / Qt).

A thin shell over `manual_add` — it only collects input and validates that there's
something to work with, then hands the values back to the caller (MainWindow) which
runs the parse -> score -> tailor -> append pipeline on a worker thread. All the
real logic lives in the toolkit-agnostic `manual_add` module (per CLAUDE.md: keep
Qt-agnostic logic out of widgets).

Two input modes, either of which is enough to submit:
  (a) a pasted job description (plus optional link / company / title), or
  (b) a job URL (a free, optional page fetch is attempted; if it's blocked the
      user is told to paste the description instead).
"""
from __future__ import annotations

from PySide6 import QtWidgets


class ManualAddDialog(QtWidgets.QDialog):
    """Collects manual job input. `values()` returns the entered fields."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add a job by hand")
        self.setMinimumWidth(520)
        self._build()

    def _build(self) -> None:
        v = QtWidgets.QVBoxLayout(self)

        intro = QtWidgets.QLabel(
            "Add a job without the scraper. Paste the job description (best — sites "
            "usually block fetching), or give just a URL and we'll try a free fetch. "
            "It's then scored and tailored the same way scraped jobs are.")
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        v.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self.url = QtWidgets.QLineEdit()
        self.url.setPlaceholderText("https://… (optional if you paste the description)")
        form.addRow("Job URL:", self.url)
        self.title = QtWidgets.QLineEdit()
        self.title.setPlaceholderText("optional — guessed from the pasted text if blank")
        form.addRow("Job title:", self.title)
        self.company = QtWidgets.QLineEdit()
        self.company.setPlaceholderText("optional — guessed from the pasted text if blank")
        form.addRow("Company:", self.company)
        v.addLayout(form)

        v.addWidget(QtWidgets.QLabel("Job description (paste the posting text):"))
        self.jd = QtWidgets.QPlainTextEdit()
        self.jd.setPlaceholderText(
            "Paste the full job description here. Required unless a URL fetch "
            "succeeds (most job sites block it).")
        self.jd.setMinimumHeight(220)
        v.addWidget(self.jd, 1)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self._ok_btn = btns.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setText("Add + tailor")
        self._ok_btn.setProperty("accent", True)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def _on_accept(self) -> None:
        if not self.jd.toPlainText().strip() and not self.url.text().strip():
            QtWidgets.QMessageBox.warning(
                self, "Add a job by hand",
                "Paste the job description, or enter a job URL to fetch.")
            return
        self.accept()

    def values(self) -> dict:
        """The entered fields, ready for `manual_add.add_manual_job`."""
        return {
            "jd_text": self.jd.toPlainText().strip(),
            "url": self.url.text().strip(),
            "title": self.title.text().strip(),
            "company": self.company.text().strip(),
        }
