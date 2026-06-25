"""Cycle 16 SP1: interface scaling.

`theme.set_scale(app, s)` drives the whole UI off one factor — the app font's
point size and the QSS heading size — so the dashboard can be tuned to any
display. Headless: the session QApplication (via qtbot) is enough.
"""
import sys
from pathlib import Path

import pytest
from PySide6 import QtWidgets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from qt import theme  # noqa: E402


def _app():
    return QtWidgets.QApplication.instance()


def test_set_scale_sets_app_font_pointsize(qtbot):
    app = _app()
    try:
        theme.set_scale(app, 1.0)
        assert app.font().pointSizeF() == pytest.approx(theme.BASE_FONT_PT)
        theme.set_scale(app, 1.5)
        assert app.font().pointSizeF() == pytest.approx(theme.BASE_FONT_PT * 1.5)
    finally:
        theme.set_scale(app, 1.0)  # don't leave the session app scaled for later tests


def test_qss_heading_scales_with_factor(qtbot):
    base = theme._qss(1.0)
    big = theme._qss(2.0)
    assert "font-size: 16px" in base
    assert "font-size: 32px" in big


def test_set_scale_clamps_extremes(qtbot):
    app = _app()
    try:
        theme.set_scale(app, 99.0)
        assert app.font().pointSizeF() == pytest.approx(theme.BASE_FONT_PT * theme.MAX_SCALE)
        theme.set_scale(app, 0.01)
        assert app.font().pointSizeF() == pytest.approx(theme.BASE_FONT_PT * theme.MIN_SCALE)
    finally:
        theme.set_scale(app, 1.0)


def test_apply_theme_accepts_scale(qtbot):
    app = _app()
    try:
        theme.apply_theme(app, scale=1.2)
        assert app.font().pointSizeF() == pytest.approx(theme.BASE_FONT_PT * 1.2)
    finally:
        theme.apply_theme(app)  # back to 1.0
