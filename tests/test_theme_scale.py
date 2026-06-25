"""Cycle 16/17 SP1: interface scaling.

`theme.set_scale(app, s)` sizes the whole UI off one factor — the app font's point
size — and (cycle 17) does so WITHOUT re-applying the global stylesheet, which was
the source of the scaling lag. The stylesheet is static (headings scale via the app
font, not a pinned px). Headless: the session QApplication (via qtbot) is enough.
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


def test_qss_is_static_with_no_pinned_font_size(qtbot):
    # Cycle 17 lag fix: nothing in the stylesheet depends on scale, so a scale change
    # never re-applies it (a full re-polish was the lag). _qss() takes no scale arg.
    qss = theme._qss()
    assert "font-size" not in qss            # headings scale via the app font instead
    assert "QLabel[heading" in qss           # the heading rule still exists (weight only)


def test_set_scale_clamps_extremes(qtbot):
    app = _app()
    try:
        assert theme.MIN_SCALE == 0.5        # cycle 17 lowered the floor to 50%
        assert theme.MAX_SCALE == 2.0
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
