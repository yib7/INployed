"""SP2: the Qt shell builds with seven tabs, a dark theme, and a single-instance lock."""
from PySide6 import QtGui

import app as qt_app
from jobsdata import _UILock
from qt import theme
from qt.main_window import TAB_TITLES, MainWindow


def test_seven_tabs_with_titles(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    assert w.tab_count() == 7
    assert w.tab_titles() == TAB_TITLES


def test_theme_is_dark(qapp):
    theme.apply_theme(qapp)
    win_color = qapp.palette().color(QtGui.QPalette.ColorRole.Window)
    assert win_color.lightnessF() < 0.3  # window background is near-black


def test_build_app_applies_theme(qapp):
    # build_app applies the theme: a non-empty stylesheet and the dark palette.
    app = qt_app.build_app([])
    assert app.styleSheet().strip()
    assert app.palette().color(QtGui.QPalette.ColorRole.Window).lightnessF() < 0.3


def test_single_instance_lock(tmp_path):
    p = tmp_path / "ui.lock"
    first, second = _UILock(p), _UILock(p)
    assert first.acquire() is True
    assert second.acquire() is False   # a second instance is blocked
    first.release()
    assert second.acquire() is True     # released -> the next instance can take it
    second.release()
