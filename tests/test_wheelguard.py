"""Cycle 16 SP2: the app-wide wheel guard must never let a stray scroll edit a
control — including the editable model dropdowns, whether or not they hold focus.
Only an OPEN dropdown keeps the wheel (so its list scrolls). Headless-safe: the
guard's decision is exercised directly so it doesn't depend on window activation.
"""
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from qt import wheelguard  # noqa: E402


def _wheel():
    return QtGui.QWheelEvent(
        QtCore.QPointF(5, 5), QtCore.QPointF(5, 5),
        QtCore.QPoint(0, 0), QtCore.QPoint(0, -120),
        QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier,
        QtCore.Qt.ScrollPhase.NoScrollPhase, False)


def _editable_combo(qtbot):
    combo = QtWidgets.QComboBox()
    combo.setEditable(True)
    combo.addItems(["alpha", "beta", "gamma"])
    combo.setCurrentIndex(1)
    qtbot.addWidget(combo)
    return combo


def test_guard_swallows_unfocused_editable_combo(qtbot):
    guard = wheelguard.WheelGuard(QtWidgets.QApplication.instance())
    combo = _editable_combo(qtbot)
    # the inner line edit is what actually receives the wheel on an editable combo
    assert guard.eventFilter(combo.lineEdit(), _wheel()) is True
    assert guard.eventFilter(combo, _wheel()) is True


def test_guard_swallows_plain_combo_and_spinbox(qtbot):
    guard = wheelguard.WheelGuard(QtWidgets.QApplication.instance())
    combo = QtWidgets.QComboBox()
    combo.addItems(["a", "b"])
    spin = QtWidgets.QSpinBox()
    qtbot.addWidget(combo)
    qtbot.addWidget(spin)
    assert guard.eventFilter(combo, _wheel()) is True
    assert guard.eventFilter(spin, _wheel()) is True


def test_guard_swallows_even_when_focused(qtbot, monkeypatch):
    # The reported regression: after clicking into the editable model dropdown it
    # keeps focus, and a subsequent page-scroll over it changed the model. Focus
    # must NOT re-enable scroll-editing — only the dropdown / typing / arrow keys do.
    guard = wheelguard.WheelGuard(QtWidgets.QApplication.instance())
    combo = _editable_combo(qtbot)
    if hasattr(wheelguard, "_has_focus"):
        monkeypatch.setattr(wheelguard, "_has_focus", lambda c: True)  # pretend focused
    assert guard.eventFilter(combo, _wheel()) is True


def test_guard_lets_open_dropdown_scroll(qtbot):
    guard = wheelguard.WheelGuard(QtWidgets.QApplication.instance())
    combo = QtWidgets.QComboBox()
    combo.addItems(["a", "b", "c"])
    qtbot.addWidget(combo)
    combo.showPopup()
    QtWidgets.QApplication.processEvents()
    try:
        assert guard.eventFilter(combo, _wheel()) is False  # popup open -> list scrolls
    finally:
        combo.hidePopup()


def test_guard_ignores_non_guarded_widget(qtbot):
    guard = wheelguard.WheelGuard(QtWidgets.QApplication.instance())
    edit = QtWidgets.QPlainTextEdit()
    qtbot.addWidget(edit)
    assert guard.eventFilter(edit, _wheel()) is False  # a text area scrolls itself


def test_installed_guard_keeps_unfocused_value(qtbot):
    # End-to-end through an installed application filter.
    app = QtWidgets.QApplication.instance()
    guard = wheelguard.WheelGuard(app)
    app.installEventFilter(guard)
    try:
        combo = _editable_combo(qtbot)
        before = combo.currentText()
        QtWidgets.QApplication.sendEvent(combo.lineEdit(), _wheel())
        assert combo.currentText() == before
    finally:
        app.removeEventFilter(guard)
