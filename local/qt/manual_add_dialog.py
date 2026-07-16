"""The "Add a job by hand" form (PySide6 / Qt).

A thin shell over `manual_add` — it collects input, validates the required fields for
the chosen action, then hands the values back to the caller (MainWindow) which runs
the parse -> score -> (tailor) -> append pipeline on a worker thread. All the real
logic lives in the toolkit-agnostic `manual_add` module (project convention:
keep Qt-agnostic logic out of widgets).

Two actions on add:
  * "Just score"     — score the résumé against the job and add it to the dataset
                       (no tailoring, no cover-letter prompt). Requires title,
                       company, and a pasted job description (URL optional).
  * "Score + tailor" — also runs the résumé engine. Requires all four fields
                       (URL, title, company, description) so tailoring never breaks.

`edit_mode=True` reuses the same form to fix an existing job's fields (all four
required); it does NOT re-score or re-tailor — those stay on the table actions.
"""
from __future__ import annotations

from PySide6 import QtWidgets


class ManualAddDialog(QtWidgets.QDialog):
    """Collects manual job input. `values()` returns the entered fields + chosen mode."""

    def __init__(self, parent=None, *, edit_mode: bool = False,
                 initial: dict | None = None) -> None:
        super().__init__(parent)
        self._edit_mode = edit_mode
        self._initial = initial or {}
        self._do_tailor = False
        self.setWindowTitle("Edit job" if edit_mode else "Add a job by hand")
        self.setMinimumWidth(520)
        self._build()

    def _build(self) -> None:
        v = QtWidgets.QVBoxLayout(self)

        if self._edit_mode:
            intro_text = (
                "Fix this job's details so scoring and résumé tailoring work cleanly. "
                "This updates the saved fields only — it does not re-score or re-tailor "
                "(use the table actions for that).")
        else:
            intro_text = (
                "Add a job by hand (for a posting the automatic search didn't surface). "
                "Paste the job description — sites usually block fetching — or give a URL "
                "and we'll try a free fetch. It's then scored, and optionally tailored, the "
                "same way discovered jobs are.")
        intro = QtWidgets.QLabel(intro_text)
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        v.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self.url = QtWidgets.QLineEdit(str(self._initial.get("url", "")))
        self.url.setPlaceholderText("https://…")
        form.addRow("Job URL:", self.url)
        self.title = QtWidgets.QLineEdit(str(self._initial.get("title", "")))
        self.title.setPlaceholderText("e.g. Data Analyst")
        form.addRow("Job title:", self.title)
        self.company = QtWidgets.QLineEdit(str(self._initial.get("company", "")))
        self.company.setPlaceholderText("e.g. Acme Corp")
        form.addRow("Company:", self.company)
        v.addLayout(form)

        v.addWidget(QtWidgets.QLabel("Job description (paste the posting text):"))
        self.jd = QtWidgets.QPlainTextEdit(str(self._initial.get("jd_text", "")))
        self.jd.setPlaceholderText("Paste the full job description here.")
        self.jd.setMinimumHeight(220)
        v.addWidget(self.jd, 1)

        btns = QtWidgets.QDialogButtonBox()
        btns.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        if self._edit_mode:
            save = btns.addButton("Save changes",
                                  QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
            save.setProperty("accent", True)
            save.setDefault(True)
            save.clicked.connect(lambda: self._on_accept(do_tailor=False))
        else:
            self._score_btn = btns.addButton(
                "Just score", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
            self._score_btn.clicked.connect(lambda: self._on_accept(do_tailor=False))
            self._tailor_btn = btns.addButton(
                "Score + tailor", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
            self._tailor_btn.setProperty("accent", True)
            self._tailor_btn.setDefault(True)
            self._tailor_btn.clicked.connect(lambda: self._on_accept(do_tailor=True))
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def _on_accept(self, *, do_tailor: bool) -> None:
        """Validate the fields required for this action, then accept. URL is required
        for tailoring (and for an edit) so the tailored résumé/links are complete; for
        a plain score it's optional."""
        missing: list[str] = []
        if not self.title.text().strip():
            missing.append("job title")
        if not self.company.text().strip():
            missing.append("company")
        if not self.jd.toPlainText().strip():
            missing.append("job description")
        if (do_tailor or self._edit_mode) and not self.url.text().strip():
            missing.append("job URL")
        if missing:
            QtWidgets.QMessageBox.warning(
                self, self.windowTitle(),
                "Please fill in: " + ", ".join(missing) + ".")
            return
        self._do_tailor = do_tailor
        self.accept()

    def values(self) -> dict:
        """The entered fields + chosen mode, ready for `manual_add.add_manual_job`."""
        return {
            "jd_text": self.jd.toPlainText().strip(),
            "url": self.url.text().strip(),
            "title": self.title.text().strip(),
            "company": self.company.text().strip(),
            "do_tailor": self._do_tailor,
        }
