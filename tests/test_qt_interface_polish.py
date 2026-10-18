"""Interface pass pins: contrast of the text/ground pairs that measured under
their floor, the arrow and check-mark SVGs, header label fonts after a
rescale, the queue's Updated cell, the sign-in dialog's primary button and the
spin box text inset."""
import re

import pytest
from PySide6 import QtGui, QtWidgets

from qt import apply_queue_panel, claude_signin, theme
from qt.widgets import SpinBox


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _over(fg, a, bg):
    return "#%02x%02x%02x" % tuple(round(x * a + y * (1 - a))
                                   for x, y in zip(_rgb(fg), _rgb(bg)))


def _lum(h):
    def c(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * c(r) + 0.7152 * c(g) + 0.0722 * c(b)


def _ratio(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _row_tints():
    """Every row ground the delegate paints: each family's tint, plain and
    selected, over PANEL."""
    for fam, s in theme.SEMANTICS.items():
        for key in ("tint_a", "sel_a"):
            yield f"{fam}/{key}", _over(s.get("tint_base", s["base"]), s[key], theme.PANEL)


def test_disabled_text_keeps_a_legible_floor():
    # WCAG exempts disabled controls; 3:1 is the floor kept so a greyed label
    # still reads. It was 2.09:1 on PANEL.
    for ground in (theme.PANEL, theme.WINDOW):
        assert _ratio(theme.TEXT_DISABLED, ground) >= 3.0


def test_selected_input_text_is_readable_on_the_selection():
    assert _ratio(theme.SEL_TEXT, theme.SEL) >= 4.5
    pal = theme._dark_palette()
    role = QtGui.QPalette.ColorRole
    assert _ratio(pal.color(role.HighlightedText).name(), pal.color(role.Highlight).name()) >= 4.5


@pytest.mark.parametrize("score", sorted(theme.SCORE_BADGES))
def test_score_badges_read_on_their_panel_ground(score):
    # The delegate lays PANEL under the badge on every row, so the ground is
    # the badge's own tint over PANEL whatever the row's colour.
    color, alpha = theme.SCORE_BADGES[score]
    assert _ratio(color, _over(color, alpha, theme.PANEL)) >= 4.5


def test_the_open_link_reads_on_every_row_tint():
    link = theme.SEMANTICS["accent"]["pill_fg"]
    for name, tint in _row_tints():
        assert _ratio(link, tint) >= 4.5, name


def test_each_arrow_svg_is_drawn_in_its_token():
    for name, (fname, token) in theme.ARROW_ICONS.items():
        svg = (theme.ASSETS_DIR / fname).read_text(encoding="utf-8")
        colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{6})"', svg))
        assert colours == {token.lower()}, name


def test_the_stylesheet_points_at_svgs_that_exist(qtbot):
    qss = theme._qss()
    urls = re.findall(r'url\("([^"]+)"\)', qss)
    assert urls
    for url in urls:
        assert (theme.ASSETS_DIR / url.rsplit("/", 1)[-1]).is_file(), url


def test_header_labels_keep_the_header_font_after_a_rescale(qtbot):
    # A header paints its labels on its viewport; set_scale once gave that
    # viewport the body font, so labels outgrew sections sized for the
    # caption font and lost their first and last letters.
    table = QtWidgets.QTableView()
    qtbot.addWidget(table)
    header = table.horizontalHeader()
    app = QtWidgets.QApplication.instance()
    try:
        theme.set_scale(app, 1.5)
        assert header.viewport().font().pointSizeF() == header.font().pointSizeF()
        assert table.viewport().font().pointSizeF() == table.font().pointSizeF()
    finally:
        theme.set_scale(app, 1.0)


@pytest.mark.parametrize("stamp, shown", [
    ("2026-10-18T10:11:42", "2026-10-18 10:11"),
    ("2026-10-18T10:11:42.123456+00:00", "2026-10-18 10:11"),
    ("2026-10-18", "2026-10-18"),
    ("", ""),
    (None, ""),
])
def test_the_queue_updated_cell_drops_the_t_and_seconds(stamp, shown):
    assert apply_queue_panel._updated_text(stamp) == shown


def test_sign_in_is_the_primary_button_of_its_dialog(qtbot, monkeypatch):
    seen = {}

    def fake_exec(box):
        [b] = [b for b in box.buttons() if b.text() == claude_signin.SIGNIN_BUTTON]
        seen.update(default=b.isDefault(), accent=b.property("accent"))
        return 0

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", fake_exec)
    claude_signin.warn_with_signin(None, "Tailor resume", "signed out")
    assert seen == {"default": True, "accent": True}
    assert "`" not in claude_signin.SIGNIN_HINT


def test_spin_box_digits_are_inset_like_other_inputs(qtbot):
    spin = SpinBox()
    qtbot.addWidget(spin)
    assert spin.lineEdit().textMargins().left() > 0
