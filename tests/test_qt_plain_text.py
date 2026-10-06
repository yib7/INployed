"""Scraped and page text shows as written in the dashboard, never as HTML.

Qt labels, message boxes and tooltips guess a string's format, so a parked
question, a form's help text or a company name that starts like a tag would
render as HTML, and an `<img src=...>` there would load a file with no click (a
probe saw sizeHint jump from 240x40 to 3852x2427). Every label is plain text
from birth, a label that shows composed markup sets RichText itself and
escapes what it inserts, and message box and tooltip text goes through
`plaintext.literal`.
"""
from __future__ import annotations

import ast
from pathlib import Path

import apply_queue
import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from qt import plaintext

REPO = Path(__file__).resolve().parent.parent
QT_DIR = REPO / "local" / "qt"
IMG = '<img src="file:///C:/Windows/Web/Screen/img100.jpg" width="4000" height="3000">'
AUTO = QtCore.Qt.TextFormat.AutoText


def _guessing(root: QtWidgets.QWidget) -> list[str]:
    return [f"{type(lab).__name__} {lab.objectName()!r} {lab.text()[:50]!r}"
            for lab in root.findChildren(QtWidgets.QLabel) if lab.textFormat() == AUTO]


# --- the helper ------------------------------------------------------------------

def test_literal_keeps_plain_text_as_it_is():
    for text in ("Saved.", "Line one\nLine two", ""):
        assert plaintext.literal(text) == text
    assert _rendered(plaintext.literal("a < b and c > d")) == "a < b and c > d"


def test_literal_shows_a_tag_as_text():
    doc = QtGui.QTextDocument()
    doc.setHtml(plaintext.literal(f"Delete '{IMG}'?\nSecond line"))
    assert doc.toPlainText() == f"Delete '{IMG}'?\nSecond line"
    assert "<img" not in doc.toHtml()


def test_a_label_shows_an_img_tag_as_text(qtbot):
    lab = plaintext.Label(IMG)
    qtbot.addWidget(lab)
    assert lab.textFormat() == QtCore.Qt.TextFormat.PlainText
    assert lab.text() == IMG
    assert lab.sizeHint().height() < 200            # the image did not load


# --- no label anywhere guesses -----------------------------------------------------

def test_no_label_in_the_main_window_guesses_its_format(qtbot):
    from qt.main_window import MainWindow
    w = MainWindow()
    qtbot.addWidget(w)
    assert _guessing(w) == []


def test_no_qt_module_builds_a_bare_qlabel():
    """Static sweep: every label is a `plaintext.Label` (or a subclass), so a
    label added later cannot fall back to Qt's guess."""
    offenders = []
    for path in sorted(QT_DIR.glob("*.py")):
        if path.name == "plaintext.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "QLabel":
                offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.ClassDef) and any(
                    isinstance(b, ast.Attribute) and b.attr == "QLabel" for b in node.bases):
                offenders.append(f"{path.name}:{node.lineno} class {node.name}")
    assert offenders == []


# --- the places page and scraped text reach ----------------------------------------

@pytest.fixture
def boxes(monkeypatch):
    shown = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, _n=name, **k: shown.append((_n, a))))

    def question(*a, **k):
        shown.append(("question", a))
        return QtWidgets.QMessageBox.StandardButton.No

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(question))
    return shown


def _rendered(text: str) -> str:
    doc = QtGui.QTextDocument()
    if QtGui.Qt.mightBeRichText(text):
        doc.setHtml(text)
    else:
        doc.setPlainText(text)
    return doc.toPlainText()


def test_the_add_answer_dialog_shows_the_forms_text_as_written(qtbot, tmp_path):
    from qt.answers_tab import AddAnswerDialog
    from resume_tailor import apply_answers
    prefill = {"question": f"Q {IMG}", "help": IMG, "type": "choice",
               "options": [IMG, "No"]}
    dlg = AddAnswerDialog(apply_answers.with_missing_builtins([]), prefill=prefill)
    qtbot.addWidget(dlg)
    assert _guessing(dlg) == []
    assert IMG in dlg.hint_label.text()
    assert dlg.hint_label.sizeHint().height() < 200


def test_the_answers_tab_delete_box_shows_the_question_as_written(
        qtbot, tmp_path, boxes):
    from qt.answers_tab import AnswersEditor
    from resume_tailor import apply_answers
    store = tmp_path / "apply_answers.json"
    apply_answers.save([{"id": "c1", "question": IMG, "type": "text", "answer": "x",
                         "note": "", "confirmed": True, "status": "active"}], store)
    ed = AnswersEditor(store_path=store)
    qtbot.addWidget(ed)
    assert _guessing(ed) == []
    row = next(r for r in ed.rows if r["id"] == "c1")
    ed._delete_clicked(row)
    [(kind, args)] = boxes
    assert kind == "question" and _rendered(args[2]) == f"Delete '{IMG}'?"


def test_the_queue_panel_shows_a_scraped_company_as_written(qtbot, tmp_path):
    from qt.apply_queue_panel import ApplyQueuePanel
    qfile = tmp_path / "apply_queue.json"
    apply_queue.enqueue(apply_queue.new_entry("1", company=IMG, title=IMG,
                                              apply_url="https://x/1"), path=qfile)
    p = ApplyQueuePanel(queue_path=qfile, jev_blocked=lambda: "")
    qtbot.addWidget(p)
    p.table.selectRow(0)
    assert _guessing(p) == []
    for lab in p.findChildren(QtWidgets.QLabel):
        if lab.textFormat() == QtCore.Qt.TextFormat.RichText:
            assert "<img" not in lab.text(), lab.objectName()


def test_an_elided_labels_tooltip_shows_the_full_text_as_written(qtbot):
    from qt.widgets import ElidedLabel
    lab = ElidedLabel(f"Analyst {IMG}")
    qtbot.addWidget(lab)
    assert lab.textFormat() == QtCore.Qt.TextFormat.PlainText
    assert _rendered(lab.toolTip()) == f"Analyst {IMG}"


def test_markup_we_compose_still_renders(qtbot):
    """The labels that show composed markup set RichText themselves; none of
    it shows up as literal tags in a plain label."""
    from qt.main_window import MainWindow
    w = MainWindow()
    qtbot.addWidget(w)
    shown_raw = [lab.text()[:60] for lab in w.findChildren(QtWidgets.QLabel)
                 if lab.textFormat() == QtCore.Qt.TextFormat.PlainText
                 and any(t in lab.text() for t in ("<span", "&nbsp;", "<b>", "<br>"))]
    assert shown_raw == []
