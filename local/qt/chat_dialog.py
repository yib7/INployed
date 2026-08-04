"""The per-job "Ask AI" window: a chat about ONE job, non-modal beside the dashboard.

Everything the pipeline knows about a job — the tailored bullets, the cover
letter, the standard answers, the JD — already sits in one folder, but asking
"how should I word the 'why this role' box?" used to mean copying files into a
separate chat by hand. This window puts that conversation next to the job.

The view is thin on purpose: `resume_tailor.chat` owns the prompt, the caps and
the transport (and knows nothing about Qt), while this file owns the widgets, the
busy state and the object lifetime. Every turn runs through `workers.run_async` —
a chat turn is a network round-trip and would freeze the window inline. Building
the context is part of that same worker call: it scans the output root for the
job's tailored folder, which on a Drive-backed mount is exactly the kind of I/O
that must never touch the UI thread.

**Lifetime.** The dialog is non-modal and the main window keeps a reference to it,
which is the widget-leak shape `tests/conftest.py::_drain_qt_widgets` exists to
catch: closed-but-alive widgets pile up, and `theme.apply_theme` re-polishing
`app.allWidgets()` then grows without bound until a test effectively hangs. So the
dialog is parented to the main window, calls `deleteLater()` on close (via close
OR reject — Esc never raises a close event), and tells the owner to drop its
reference on the way out. Do not "simplify" any of those three.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from PySide6 import QtGui, QtWidgets

from qt import workers


class JobChatDialog(QtWidgets.QDialog):
    """A chat window scoped to one job. `job` is a dashboard row payload (or the
    apply panel's marker dict); `on_closed` lets the owner drop its reference."""

    def __init__(self, job: dict, parent: Optional[QtWidgets.QWidget] = None,
                 on_closed: Optional[Callable[[], None]] = None) -> None:
        super().__init__(parent)
        self._job: Dict = dict(job or {})
        self._on_closed = on_closed or (lambda: None)
        self._closed = False
        self._busy = False
        self._context: str = ""                    # built once, reused every turn
        self._history: List[Tuple[str, str]] = []  # answered exchanges only
        self._blocks: List[str] = []               # rendered transcript, markdown
        self._last_answer = ""

        title = (self._job.get("job_title") or self._job.get("title") or "this job")
        company = (self._job.get("company_name") or self._job.get("company") or "?")
        self.setWindowTitle(f"Ask AI — {title} @ {company}")
        self.setModal(False)      # the user keeps working in the dashboard
        self.resize(720, 800)
        self._build()

    # ---- construction --------------------------------------------------------

    def _build(self) -> None:
        v = QtWidgets.QVBoxLayout(self)

        hint = QtWidgets.QLabel(
            "Ask about this job's apply sheet, tailored bullets, cover letter or "
            "posting. Answers come only from those — it says so when something "
            "isn't there rather than making it up.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.transcript = QtWidgets.QTextBrowser()
        self.transcript.setAccessibleName("Chat transcript")
        self.transcript.setOpenLinks(False)          # a reader, not a browser
        self.transcript.setOpenExternalLinks(False)
        v.addWidget(self.transcript, 1)

        self.input = QtWidgets.QPlainTextEdit()
        self.input.setAccessibleName("Your question")
        self.input.setPlaceholderText("Ask something about this job… (Ctrl+Enter to send)")
        self.input.setMaximumHeight(120)
        v.addWidget(self.input)

        self.status = QtWidgets.QLabel("")
        self.status.setProperty("muted", True)
        v.addWidget(self.status)

        bar = QtWidgets.QHBoxLayout()
        self.copy_btn = QtWidgets.QPushButton("Copy answer")
        self.copy_btn.setToolTip("Copy the latest answer to the clipboard")
        self.copy_btn.clicked.connect(self.copy_answer)
        bar.addWidget(self.copy_btn)
        bar.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.setProperty("accent", True)
        self.send_btn.setDefault(True)
        self.send_btn.clicked.connect(self.send)
        bar.addWidget(self.send_btn)
        v.addLayout(bar)

        # Ctrl+Enter sends, so plain Enter can still add a line to the question.
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self, activated=self.send)
        # Esc / accept() also tear the dialog down (they never raise a close event).
        self.finished.connect(self._teardown)

    # ---- the conversation ----------------------------------------------------

    def history(self) -> List[Tuple[str, str]]:
        """The answered exchanges, oldest first. A failed turn is not in here."""
        return list(self._history)

    def send(self) -> None:
        """Send the input box as the next turn. No-op while one is in flight."""
        question = self.input.toPlainText().strip()
        if not question or self._busy:
            return
        self.input.clear()
        self._append("You", question)
        self._set_busy(True)
        history = self.history()
        workers.run_async(self, lambda: self._work(question, history),
                          on_done=self._finish_turn, on_error=self._finish_turn_error)

    def _work(self, question: str, history: List[Tuple[str, str]]):
        """Worker thread: build the context once, then take one turn.

        Returns the context alongside the answer so the UI thread — not this one —
        is what stores it on the dialog.
        """
        from resume_tailor import chat  # local import: keeps the engine off the UI import path

        context = self._context or chat.context_for_job(self._job)
        return context, chat.ask(context, history, question), question

    def _finish_turn(self, result) -> None:
        context, answer, question = result
        self._context = context
        self._history.append((question, answer))
        self._last_answer = answer
        self._append("AI", answer)
        self._set_busy(False)

    def _finish_turn_error(self, exc: BaseException) -> None:
        self._append("Couldn't answer", str(exc) or exc.__class__.__name__)
        self._set_busy(False)

    def copy_answer(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self._last_answer or "")

    # ---- presentation --------------------------------------------------------

    def _append(self, speaker: str, text: str) -> None:
        self._blocks.append(f"**{speaker}**\n\n{text}")
        self.transcript.setMarkdown("\n\n---\n\n".join(self._blocks))
        self.transcript.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.send_btn.setEnabled(not busy)
        self.input.setReadOnly(busy)
        self.status.setText("Thinking…" if busy else "")

    # ---- lifetime ------------------------------------------------------------

    def closeEvent(self, event) -> None:   # noqa: N802 - Qt naming
        super().closeEvent(event)
        self._teardown()

    def _teardown(self, *_args) -> None:
        """Drop the owner's reference and schedule destruction — exactly once.

        Reached from closeEvent (the window's ✕ / Close) and from `finished`
        (Esc -> reject). See the module docstring for why this is not optional.
        """
        if self._closed:
            return
        self._closed = True
        self._on_closed()
        self.deleteLater()
