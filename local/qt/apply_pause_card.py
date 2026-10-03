"""The "Waiting for you" card at the top of the Auto-apply tab (cycle 19, SP7).

A run that meets a question it cannot answer pauses (`local/apply_pause.py`):
it writes a request file and waits for the answer file. The card shows the
oldest waiting request with each question in its real widget:

- the live form's options as a radio list (up to `RADIO_MAX`) or a dropdown;
- Yes / No for a tick box;
- a number box, or a text box;
- a sensitive field, a file or a password shows only "Type this one in the
  browser" (code never types those).

Each question the run may keep has a "Save for future runs" box, off by
default, absent for a sensitive field, and off with the reason when Add answer
would refuse the question (a built-in answers it, a custom answer already has
it). The three buttons write the answer file through `apply_pause.write_answer`:
"Fill and continue", "I filled it in the browser, continue" (hidden for a
headless run, which has no window to fill) and "Park it".

The panel (`qt.apply_queue_panel`) reads the pause folder on its 5 s poll and
hands the card the request; the card never waits on anything itself.
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

import apply_facts
import apply_pause
import errmsg
from resume_tailor import apply_answers

# A list with this many options or fewer shows as radio buttons; a longer one
# as a dropdown.
RADIO_MAX = 4

# The dropdowns' empty first entry: nothing chosen.
NOTHING = "Choose..."

BROWSER_ONLY = "Type this one in the browser"
BROWSER_ONLY_HEADLESS = ("This one needs the browser, and this run has no window: "
                         "Park it, then answer it on a run with the window shown.")
SAVE_LABEL = "Save for future runs"

# A number box takes digits with an optional decimal part.
_NUMBER_SHAPE = r"^\d{0,9}(\.\d{0,4})?$"


def _store_answers(path: Path | None) -> tuple[list[dict], str]:
    """The saved answers (for the save box's refusals), or [] and why they
    could not be read."""
    try:
        return list(apply_answers.load_store(path)["answers"]), ""
    except apply_answers.AnswerStoreError as e:
        return [], f"the answer store could not be read ({type(e).__name__})"



def _plain_tip(text: str) -> str:
    """`text` as a tooltip that shows exactly as written, markup and all."""
    return "<qt>" + html.escape(text, quote=False) + "</qt>"

class PauseCard(QtWidgets.QFrame):
    """The card for one pause request (`set_request`); hidden with none.
    `answered(job_id, mode)` fires once the answer file is written."""

    answered = QtCore.Signal(str, str)

    def __init__(self, parent: QtWidgets.QWidget | None = None, *,
                 answers_path: Path | None = None,
                 write_answer: Callable[..., Any] | None = None) -> None:
        super().__init__(parent)
        self.setProperty("callout", "warning")
        self._answers_path = answers_path
        self._write_answer = write_answer or apply_pause.write_answer
        self.request: Optional[Dict[str, Any]] = None
        self.rows: List[Dict[str, Any]] = []
        self._build()
        self.set_request(None)

    # -- construction ------------------------------------------------------------

    def _build(self) -> None:
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        self.title_label = QtWidgets.QLabel("")
        self.title_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.title_label.setWordWrap(True)
        v.addWidget(self.title_label)
        self.reason_label = QtWidgets.QLabel("")
        self.reason_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.reason_label.setWordWrap(True)
        self.reason_label.setProperty("muted", True)
        v.addWidget(self.reason_label)
        self._questions_box = QtWidgets.QWidget()
        self._grid = QtWidgets.QVBoxLayout(self._questions_box)
        self._grid.setContentsMargins(0, 4, 0, 4)
        self._grid.setSpacing(8)
        v.addWidget(self._questions_box)
        bar = QtWidgets.QHBoxLayout()
        self.fill_btn = QtWidgets.QPushButton("Fill and continue")
        self.fill_btn.setProperty("accent", True)
        self.fill_btn.setToolTip("Put your answers in their fields on the paused page; the "
                                 "run then goes on as usual.")
        self.fill_btn.clicked.connect(lambda: self._send("fill"))
        bar.addWidget(self.fill_btn)
        self.browser_btn = QtWidgets.QPushButton("I filled it in the browser, continue")
        self.browser_btn.setToolTip("You typed the answers in the run's browser window: the "
                                    "run reads the page again and goes on.")
        self.browser_btn.clicked.connect(lambda: self._send("browser"))
        bar.addWidget(self.browser_btn)
        self.park_btn = QtWidgets.QPushButton("Park it")
        self.park_btn.setProperty("tier", "tertiary")
        self.park_btn.setToolTip("Stop this job here: it parks as it would have with no "
                                 "pause, with the questions listed under Missing.")
        self.park_btn.clicked.connect(lambda: self._send("park"))
        bar.addWidget(self.park_btn)
        bar.addStretch(1)
        self.more_label = QtWidgets.QLabel("")
        self.more_label.setProperty("muted", True)
        bar.addWidget(self.more_label)
        v.addLayout(bar)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setProperty("muted", True)
        self.status_label.setWordWrap(True)
        v.addWidget(self.status_label)

    # -- the request ---------------------------------------------------------------

    def job_id(self) -> str:
        return str((self.request or {}).get("job") or "")

    def pause_id(self) -> str:
        return str((self.request or {}).get("pause_id") or "")

    def shows(self, request: Dict[str, Any] | None) -> bool:
        """Whether the card already shows `request` (the same job and pause):
        the poll leaves it be, so a half-typed answer stays."""
        return (self.request is not None and request is not None
                and str(request.get("job") or "") == self.job_id()
                and str(request.get("pause_id") or "") == self.pause_id())

    def set_waiting_count(self, n: int) -> None:
        """How many other runs wait behind this one."""
        self.more_label.setText(f"{n} more waiting" if n > 0 else "")

    def set_request(self, request: Dict[str, Any] | None) -> None:
        for row in self.rows:
            row["frame"].setParent(None)
            row["frame"].deleteLater()
        self.rows = []
        self.request = dict(request) if request else None
        self.status_label.setText("")
        if not self.request:
            self.setVisible(False)
            return
        req = self.request
        who = ": ".join(p for p in (str(req.get("company") or ""),
                                    str(req.get("title") or "")) if p)
        self.title_label.setText(
            "<b>Waiting for you</b>" + (f"&nbsp;&nbsp;{html.escape(who)}" if who else ""))
        self.reason_label.setText(str(req.get("reason") or ""))
        headless = bool(req.get("headless"))
        answers, unreadable = _store_answers(self._answers_path)
        for q in req.get("questions") or []:
            if isinstance(q, dict):
                self._add_row(q, headless, answers, unreadable)
        self.browser_btn.setVisible(not headless)
        self.fill_btn.setEnabled(any(r["widget"] is not None for r in self.rows)
                                 or not self.rows)
        self.setVisible(True)

    def _add_row(self, q: Dict[str, Any], headless: bool, answers: list[dict],
                 unreadable: str) -> None:
        frame = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        label = QtWidgets.QLabel(str(q.get("label") or "(a field with no label)")
                                 + (" *" if q.get("required") else ""))
        label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        lay.addWidget(label)
        if q.get("help"):
            hint = QtWidgets.QLabel(str(q["help"]))
            hint.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            hint.setWordWrap(True)
            hint.setProperty("muted", True)
            lay.addWidget(hint)
        row: Dict[str, Any] = {"question": q, "frame": frame, "widget": None, "kind": "",
                               "save": None}
        widget_kind = str(q.get("widget") or apply_pause.W_TEXT)
        if q.get("sensitive") or widget_kind == apply_pause.W_BROWSER:
            note = QtWidgets.QLabel(BROWSER_ONLY_HEADLESS if headless else BROWSER_ONLY)
            note.setWordWrap(True)
            lay.addWidget(note)
            row["kind"] = apply_pause.W_BROWSER
        else:
            row["kind"], row["widget"] = self._make_widget(widget_kind, q)
            lay.addWidget(row["widget"])
            save = QtWidgets.QCheckBox(SAVE_LABEL)
            save.setChecked(False)
            question = apply_facts.saved_question(str(q.get("label") or ""),
                                                  str(q.get("help") or ""))
            why = unreadable or apply_pause.save_refusal(question, answers)
            # The question is the page's words: Qt renders a tooltip that looks
            # like markup, so it goes in escaped inside an explicit <qt>.
            if why:
                save.setEnabled(False)
                save.setToolTip(_plain_tip(f"Not saved: {why}."))
            else:
                save.setToolTip(_plain_tip(
                    f"Keep this answer as a confirmed custom answer for "
                    f"\"{question}\", so a later form asking it fills on its own."))
            lay.addWidget(save)
            row["save"] = save
        self._grid.addWidget(frame)
        self.rows.append(row)

    def _make_widget(self, kind: str, q: Dict[str, Any]) -> tuple[str, QtWidgets.QWidget]:
        options = [str(o) for o in q.get("options") or [] if str(o).strip()]
        if kind == apply_pause.W_CHOICE and options and len(options) <= RADIO_MAX:
            box = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0)
            group = QtWidgets.QButtonGroup(box)
            group.setExclusive(True)
            for o in options:
                rb = QtWidgets.QRadioButton(o)
                group.addButton(rb)
                h.addWidget(rb)
            h.addStretch(1)
            box.group = group       # kept alive with the box
            return "radio", box
        if kind in (apply_pause.W_CHOICE, apply_pause.W_YES_NO):
            combo = QtWidgets.QComboBox()
            combo.addItem(NOTHING, "")
            for o in options if kind == apply_pause.W_CHOICE else ["Yes", "No"]:
                combo.addItem(o, o)
            return "combo", combo
        if kind == apply_pause.W_NUMBER:
            edit = QtWidgets.QLineEdit()
            edit.setValidator(QtGui.QRegularExpressionValidator(
                QtCore.QRegularExpression(_NUMBER_SHAPE), edit))
            edit.setPlaceholderText(str(q.get("placeholder") or "A number"))
            return "line", edit
        if str(q.get("field_type") or "") == "textarea":
            text = QtWidgets.QPlainTextEdit()
            text.setPlaceholderText(str(q.get("placeholder") or ""))
            text.setFixedHeight(72)
            return "text", text
        edit = QtWidgets.QLineEdit()
        edit.setPlaceholderText(str(q.get("placeholder") or ""))
        return "line", edit

    # -- reading the answers ----------------------------------------------------

    @staticmethod
    def row_value(row: Dict[str, Any]) -> str:
        w, kind = row["widget"], row["kind"]
        if w is None:
            return ""
        if kind == "radio":
            checked = w.group.checkedButton()
            return checked.text() if checked is not None else ""
        if kind == "combo":
            return str(w.currentData() or "")
        if kind == "text":
            return w.toPlainText().strip()
        return w.text().strip()

    def set_value(self, key: str, value: str) -> None:
        """Put `value` in the widget of question `key` (the tests and a
        keyboard user alike)."""
        row = next(r for r in self.rows if str(r["question"].get("key")) == str(key))
        w, kind = row["widget"], row["kind"]
        if kind == "radio":
            for b in w.group.buttons():
                b.setChecked(b.text() == value)
        elif kind == "combo":
            w.setCurrentIndex(max(0, w.findData(value)))
        elif kind == "text":
            w.setPlainText(value)
        elif kind == "line":
            w.setText(value)

    def values(self) -> Dict[str, str]:
        out = {}
        for row in self.rows:
            value = self.row_value(row)
            if value:
                out[str(row["question"].get("key"))] = value
        return out

    def save_flags(self) -> Dict[str, bool]:
        return {str(r["question"].get("key")): True for r in self.rows
                if r["save"] is not None and r["save"].isEnabled() and r["save"].isChecked()
                and self.row_value(r)}

    # -- the buttons ----------------------------------------------------------

    def _send(self, mode: str) -> None:
        job = self.job_id()
        if not job:
            return
        values = self.values() if mode == "fill" else {}
        save = self.save_flags() if mode == "fill" else {}
        try:
            self._write_answer(job, mode, values, save, pause_id=self.pause_id())
        except (OSError, ValueError) as e:
            self.status_label.setText(f"The answer was not sent: {errmsg.for_user(e)}")
            return
        self.set_request(None)
        self.answered.emit(job, mode)


__all__ = ["BROWSER_ONLY", "NOTHING", "PauseCard", "RADIO_MAX", "SAVE_LABEL"]
