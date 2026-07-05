"""SP2: the Qt shell builds with its full tab set, a dark theme, and a
single-instance lock. (Cycle 33 SP3 grew the set to eight — Auto-apply.)"""
from PySide6 import QtGui, QtWidgets

import app as qt_app
from jobsdata import _UILock
from qt import theme
from qt.main_window import TAB_TITLES, MainWindow


def test_eight_tabs_with_titles(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    assert w.tab_count() == 8      # cycle 33 SP3 added the Auto-apply tab
    assert w.tab_titles() == TAB_TITLES
    assert "Auto-apply" in TAB_TITLES


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


def test_main_exits_silently_when_lock_already_held(monkeypatch):
    # P2-2: a second instance must exit(0) quietly -- the live instance's own
    # FS-watcher/poll already picks up new files, so a modal here only interrupts
    # the user for no reason.
    monkeypatch.setattr(qt_app._UILock, "acquire", lambda self: False)

    def _boom(*a, **k):
        raise AssertionError("second instance must not show a modal dialog")

    monkeypatch.setattr(QtWidgets.QMessageBox, "information", _boom)
    assert qt_app.main([]) == 0
