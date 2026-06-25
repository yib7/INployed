"""Wheel guard: scrolling over a combo/spin/slider must not edit it (focused or not)."""
from PySide6 import QtCore, QtGui, QtWidgets

from qt import wheelguard


def _wheel() -> QtGui.QWheelEvent:
    return QtGui.QWheelEvent(
        QtCore.QPointF(5, 5), QtCore.QPointF(5, 5),
        QtCore.QPoint(0, -120), QtCore.QPoint(0, -120),
        QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier,
        QtCore.Qt.ScrollPhase.NoScrollPhase, False)


def test_guard_swallows_wheel_on_unfocused_controls(qtbot):
    combo = QtWidgets.QComboBox()
    combo.addItems(["a", "b", "c"])
    spin = QtWidgets.QSpinBox()
    slider = QtWidgets.QSlider()
    label = QtWidgets.QLabel("x")
    for w in (combo, spin, slider, label):
        qtbot.addWidget(w)
    g = wheelguard.WheelGuard()
    # unfocused guarded controls: the guard eats the wheel (True)
    assert g.eventFilter(combo, _wheel()) is True
    assert g.eventFilter(spin, _wheel()) is True
    assert g.eventFilter(slider, _wheel()) is True
    # a non-guarded widget is untouched (False -> normal handling)
    assert g.eventFilter(label, _wheel()) is False


def test_guard_swallows_wheel_via_editable_combo_lineedit(qtbot):
    # Regression: an *editable* combo delivers the wheel to its inner QLineEdit,
    # not the combo. The guard must still neutralise it (the model selectors are
    # editable_choice, so this is the exact field the user saw changing on scroll).
    combo = QtWidgets.QComboBox()
    combo.setEditable(True)
    combo.addItems(["a", "b", "c"])
    combo.setCurrentIndex(0)
    qtbot.addWidget(combo)
    g = wheelguard.WheelGuard()
    assert g.eventFilter(combo.lineEdit(), _wheel()) is True
    assert combo.currentIndex() == 0


def test_guard_swallows_wheel_even_when_focused(qtbot, monkeypatch):
    # Cycle 16: focus must NOT re-enable scroll-editing. The editable model dropdowns
    # keep focus after a click, and a page-scroll over a focused one used to silently
    # change the Gemini model — so the guard now swallows regardless of focus.
    combo = QtWidgets.QComboBox()
    combo.addItems(["a", "b"])
    qtbot.addWidget(combo)
    monkeypatch.setattr(combo, "hasFocus", lambda: True)
    g = wheelguard.WheelGuard()
    assert g.eventFilter(combo, _wheel()) is True  # swallowed even when focused


def test_guard_lets_open_combo_popup_scroll(qtbot, monkeypatch):
    # When the dropdown is open, the wheel should scroll the popup list, not be eaten.
    combo = QtWidgets.QComboBox()
    combo.addItems(["a", "b", "c"])
    qtbot.addWidget(combo)
    monkeypatch.setattr(combo.view(), "isVisible", lambda: True)
    g = wheelguard.WheelGuard()
    assert g.eventFilter(combo, _wheel()) is False


def test_unfocused_combo_value_unchanged_by_wheel(qtbot):
    combo = QtWidgets.QComboBox()
    combo.addItems(["a", "b", "c"])
    combo.setCurrentIndex(0)
    qtbot.addWidget(combo)
    app = QtWidgets.QApplication.instance()
    g = wheelguard.install(app)
    try:
        app.sendEvent(combo, _wheel())
        assert combo.currentIndex() == 0  # would have advanced without the guard
    finally:
        app.removeEventFilter(g)


def test_install_registers_and_keeps_reference(qtbot):
    app = QtWidgets.QApplication.instance()
    g = wheelguard.install(app)
    try:
        assert isinstance(g, wheelguard.WheelGuard)
        assert app._wheel_guard is g
    finally:
        app.removeEventFilter(g)
