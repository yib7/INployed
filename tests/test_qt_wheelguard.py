"""Wheel guard: scrolling over an unfocused combo/spin/slider must not edit it."""
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


def test_guard_lets_focused_control_through(qtbot, monkeypatch):
    combo = QtWidgets.QComboBox()
    combo.addItems(["a", "b"])
    qtbot.addWidget(combo)
    monkeypatch.setattr(combo, "hasFocus", lambda: True)
    g = wheelguard.WheelGuard()
    assert g.eventFilter(combo, _wheel()) is False  # focused: user is deliberately scrolling it


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
