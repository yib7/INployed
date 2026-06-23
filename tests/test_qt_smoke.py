"""SP0 smoke: PySide6 is installed and a widget constructs headless.

Proves the Qt toolkit works without a display so the rest of the port (and CI) can
rely on it. `conftest.py` sets QT_QPA_PLATFORM=offscreen; pytest-qt's `qapp` fixture
owns the single QApplication and `qtbot` manages widget lifetimes.
"""
from PySide6 import QtWidgets


def test_qapplication_exists(qapp):
    assert isinstance(qapp, QtWidgets.QApplication)


def test_qwidget_constructs(qtbot):
    w = QtWidgets.QWidget()
    qtbot.addWidget(w)
    w.setWindowTitle("smoke")
    assert w.windowTitle() == "smoke"
