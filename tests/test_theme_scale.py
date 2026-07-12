"""Cycle 16/17 SP1: interface scaling.

`theme.set_scale(app, s)` sizes the whole UI off one factor — the app font's point
size — and (cycle 17) does so WITHOUT re-applying the global stylesheet, which was
the source of the scaling lag. The stylesheet is static (headings scale via the app
font, not a pinned px). Headless: the session QApplication (via qtbot) is enough.
"""
import sys
from pathlib import Path

import pytest
from PySide6 import QtGui, QtWidgets

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
    # The stylesheet is scale-INDEPENDENT (headings scale via the app font, not a
    # pinned px) so re-applying it costs nothing extra. _qss() takes no scale arg.
    qss = theme._qss()
    assert "font-size" not in qss            # headings scale via the app font instead
    assert "QLabel[heading" in qss           # the heading rule still exists (weight only)


def test_set_scale_overrides_pinned_widget_fonts_live(qtbot):
    # Live-scaling fix (fast path): a global stylesheet pins each widget's font, so a
    # bare app.setFont() can't override an explicitly-set widget font — which is why
    # the size used to change only after a restart. set_scale pushes the new font onto
    # the live widgets so it updates at once, WITHOUT the global stylesheet re-polish
    # that caused the lag (so the stylesheet is left untouched).
    app = _app()
    try:
        before = app.styleSheet()
        w = QtWidgets.QLabel("x")
        qtbot.addWidget(w)
        w.setFont(QtGui.QFont("Segoe UI", 10))    # simulate the stylesheet's font pin
        theme.set_scale(app, 1.4)                 # within [MIN_SCALE, MAX_SCALE]
        assert w.font().pointSizeF() == pytest.approx(theme.BASE_FONT_PT * 1.4)
        assert app.styleSheet() == before         # not re-applied -> no re-polish lag
    finally:
        theme.set_scale(app, 1.0)


def test_set_scale_clamps_extremes(qtbot):
    app = _app()
    try:
        assert theme.MIN_SCALE == 0.75       # interface size floor = 75%
        assert theme.MAX_SCALE == 1.5        # interface size ceiling = 150%
        theme.set_scale(app, 99.0)
        assert app.font().pointSizeF() == pytest.approx(theme.BASE_FONT_PT * theme.MAX_SCALE)
        theme.set_scale(app, 0.01)
        assert app.font().pointSizeF() == pytest.approx(theme.BASE_FONT_PT * theme.MIN_SCALE)
    finally:
        theme.set_scale(app, 1.0)


def test_widget_created_after_rescale_gets_control_font(qtbot):
    # Per-class app fonts (restyle cycle): a QPushButton created AFTER set_scale
    # must resolve the "control" type role (0.93 x live base) with no explicit
    # setFont — this is what keeps dialogs/popups built post-rescale on-scale.
    app = _app()
    try:
        theme.set_scale(app, 1.2)
        btn = QtWidgets.QPushButton("x")
        qtbot.addWidget(btn)
        btn.ensurePolished()  # font resolution happens at polish (as before display)
        expected = theme.BASE_FONT_PT * 1.2 * theme.TYPE_SCALE["control"]
        assert btn.font().pointSizeF() == pytest.approx(expected, rel=1e-3)
    finally:
        theme.set_scale(app, 1.0)


def test_apply_theme_accepts_scale(qtbot):
    app = _app()
    try:
        theme.apply_theme(app, scale=1.2)
        assert app.font().pointSizeF() == pytest.approx(theme.BASE_FONT_PT * 1.2)
    finally:
        theme.apply_theme(app)  # back to 1.0
