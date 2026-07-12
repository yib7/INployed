"""Screen chrome for the restyled dashboard (cycle 40, Phase 3a).

Small custom-painted widgets that QSS can't express because their radii and
paddings track the live ui-scale (the stylesheet is static and never
re-applied):

- `Pill(QLabel)` — a semantic status pill (radius = height/2) mirroring the
  row delegate's `_paint_pill` metrics: h = fm.height() + 6*scale, bg =
  family `tint_base`/`base` at `pill_a`, fg = `pill_fg`. Also supports an
  outline variant (PANEL bg + hairline border, for meta chips) and a leading
  status dot.
- `Chip(QAbstractButton)` — a checkable filter chip with an optional count
  suffix and status dot. RAISED/hairline at rest, FLOATING/strong on hover,
  accent tint + accent border when checked.
- `ChipBar(QWidget)` — a row of chips; optionally exclusive (a QButtonGroup
  keeps exactly one checked) with an `on_change(key)` callback.
- `IdentityStrip(QFrame)` — the top app strip: INployed wordmark + mono
  tagline + freshness pill + jobs/unseen/tracked count badges.

Paint fonts derive from each widget's `typeRole` font (set via
`theme.set_type_role`), so a ui-scale change re-sizes them with no repolish.
"""
from __future__ import annotations

from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from qt import theme


def _pill_colors(family_name: str) -> tuple[QtGui.QColor, QtGui.QColor]:
    """(bg, fg) QColors for a semantic family's pill."""
    fam = theme.SEMANTICS.get(family_name, theme.SEMANTICS["neutral"])
    bg = theme.qcolor(fam.get("tint_base", fam["base"]), fam["pill_a"])
    return bg, theme.qcolor(fam["pill_fg"])


class Pill(QtWidgets.QLabel):
    """A rounded status pill. Modes:

    - `family="success"` … — semantic pill (tinted bg, family pill_fg text).
    - `outline=True` — PANEL bg + hairline border, muted text (meta chips).
    - `colors=(fg_hex, bg_alpha)` — custom fg with fg-tinted bg (score pill).
    A `dot` color (hex) draws a small leading dot inside the pill.
    """

    def __init__(self, text: str = "", family: str | None = None, *,
                 outline: bool = False, colors: tuple[str, float] | None = None,
                 dot: str | None = None, parent=None) -> None:
        super().__init__(text, parent)
        self._family = family
        self._outline = outline
        self._colors = colors
        self._dot = dot
        theme.set_type_role(self, "caption")
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed,
                           QtWidgets.QSizePolicy.Policy.Fixed)

    # -- state ------------------------------------------------------------------
    def set_family(self, family: str | None) -> None:
        self._family = family
        self.update()

    def set_dot(self, dot_hex: str | None) -> None:
        self._dot = dot_hex
        self.update()

    def setText(self, text: str) -> None:  # noqa: N802 (Qt naming)
        super().setText(text)
        self.updateGeometry()

    # -- geometry ---------------------------------------------------------------
    def _paint_font(self) -> QtGui.QFont:
        f = QtGui.QFont(self.font())
        f.setWeight(QtGui.QFont.Weight.DemiBold)
        return f

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        s = theme._current_scale
        fm = QtGui.QFontMetrics(self._paint_font())
        h = fm.height() + round(6 * s)
        w = fm.horizontalAdvance(self.text()) + round(16 * s)
        if self._dot:
            w += round(12 * s)
        return QtCore.QSize(w, h)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        return self.sizeHint()

    # -- paint ------------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        s = theme._current_scale
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        r = rect.height() / 2
        if self._colors is not None:
            fg_hex, bg_a = self._colors
            bg, fg = theme.qcolor(fg_hex, bg_a), theme.qcolor(fg_hex)
            border = None
        elif self._outline or not self._family:
            bg, fg = theme.qcolor(theme.PANEL), theme.qcolor(theme.MUTED)
            border = theme.qcolor(theme.BORDER)
        else:
            bg, fg = _pill_colors(self._family)
            border = None
        p.setPen(QtGui.QPen(border, 1) if border is not None
                 else QtCore.Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect, r, r)
        x = rect.left() + round(8 * s)
        if self._dot:
            d = round(8 * s) if self._outline else round(6 * s)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor(self._dot))
            p.drawEllipse(QtCore.QRectF(x, rect.center().y() - d / 2, d, d))
            x += d + round(6 * s)
        p.setPen(fg)
        p.setFont(self._paint_font())
        p.drawText(QtCore.QRectF(x, rect.top(), rect.right() - x, rect.height()),
                   QtCore.Qt.AlignmentFlag.AlignVCenter
                   | QtCore.Qt.AlignmentFlag.AlignLeft, self.text())
        p.end()


class Chip(QtWidgets.QAbstractButton):
    """A rounded filter chip: label + optional mono count + optional status dot.
    Checkable by default (filter chips); `checkable=False` renders a purely
    informational chip (auto-apply status counts)."""

    def __init__(self, label: str, *, dot: str | None = None,
                 checkable: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.setText(label)
        self._dot = dot
        self._count: int | None = None
        self.setCheckable(checkable)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor if checkable
                       else QtCore.Qt.CursorShape.ArrowCursor)
        theme.set_type_role(self, "control")
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed,
                           QtWidgets.QSizePolicy.Policy.Fixed)

    def set_count(self, n: int | None) -> None:
        self._count = n
        self.updateGeometry()
        self.update()

    def count(self) -> int | None:
        return self._count

    # -- geometry ---------------------------------------------------------------
    def _count_font(self) -> QtGui.QFont:
        f = QtGui.QFont(self.font())
        f.setFamilies(["Consolas", "Cascadia Mono"])
        f.setWeight(QtGui.QFont.Weight.Bold)
        return f

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        s = theme._current_scale
        fm = QtGui.QFontMetrics(self.font())
        h = round(theme.SIZES["control"] * s)
        w = fm.horizontalAdvance(self.text()) + round(24 * s)
        if self._dot:
            w += round(14 * s)
        if self._count is not None:
            w += QtGui.QFontMetrics(self._count_font()).horizontalAdvance(
                str(self._count)) + round(7 * s)
        return QtCore.QSize(w, h)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        return self.sizeHint()

    # -- paint ------------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        s = theme._current_scale
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        r = rect.height() / 2
        checked = self.isChecked()
        hovered = self.underMouse() and self.isEnabled()
        if checked:
            acc = theme.SEMANTICS["accent"]
            bg = theme.qcolor(acc["base"], acc["tint_a"])
            border = theme.qcolor(theme.ACCENT)
            fg = theme.qcolor(theme.TEXT)
        elif hovered and self.isCheckable():
            bg = theme.qcolor(theme.FLOATING)
            border = theme.qcolor(theme.BORDER_STRONG)
            fg = theme.qcolor(theme.TEXT_SECONDARY)
        else:
            bg = theme.qcolor(theme.RAISED)
            border = theme.qcolor(theme.BORDER)
            fg = theme.qcolor(theme.TEXT_SECONDARY)
        p.setPen(QtGui.QPen(border, 1))
        p.setBrush(bg)
        p.drawRoundedRect(rect, r, r)
        x = rect.left() + round(12 * s)
        if self._dot:
            d = round(8 * s)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor(self._dot))
            p.drawEllipse(QtCore.QRectF(x, rect.center().y() - d / 2, d, d))
            x += d + round(7 * s)
        font = QtGui.QFont(self.font())
        if checked:
            font.setWeight(QtGui.QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(fg)
        fm = QtGui.QFontMetrics(font)
        p.drawText(QtCore.QRectF(x, rect.top(), rect.width(), rect.height()),
                   QtCore.Qt.AlignmentFlag.AlignVCenter
                   | QtCore.Qt.AlignmentFlag.AlignLeft, self.text())
        if self._count is not None:
            x += fm.horizontalAdvance(self.text()) + round(7 * s)
            p.setFont(self._count_font())
            p.setPen(theme.qcolor(theme.TEXT))
            p.drawText(QtCore.QRectF(x, rect.top(), rect.right() - x, rect.height()),
                       QtCore.Qt.AlignmentFlag.AlignVCenter
                       | QtCore.Qt.AlignmentFlag.AlignLeft, str(self._count))
        p.end()

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().leaveEvent(event)
        self.update()


class ChipBar(QtWidgets.QWidget):
    """A horizontal row of `Chip`s keyed by string.

    `items` is `[(key, label, dot_hex_or_None), ...]`. With `exclusive=True`
    (the default) a QButtonGroup keeps exactly one chip checked and
    `on_change(key)` fires when the checked chip changes. `checkable=False`
    renders an informational (non-filtering) row."""

    def __init__(self, items, *, on_change: Callable[[str], None] | None = None,
                 exclusive: bool = True, checkable: bool = True,
                 parent=None) -> None:
        super().__init__(parent)
        self.setProperty("chipbar", True)
        self._on_change = on_change or (lambda _key: None)
        self._chips: dict[str, Chip] = {}
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(exclusive and checkable)
        for key, label, dot in items:
            chip = Chip(label, dot=dot, checkable=checkable)
            self._chips[key] = chip
            self._group.addButton(chip)
            lay.addWidget(chip)
            if checkable:
                chip.toggled.connect(
                    lambda on, k=key: self._on_change(k) if on else None)
        lay.addStretch(1)

    def chip(self, key: str) -> Chip | None:
        return self._chips.get(key)

    def set_counts(self, counts: dict[str, int]) -> None:
        for key, chip in self._chips.items():
            if key in counts:
                chip.set_count(counts[key])

    def checked_key(self) -> str | None:
        for key, chip in self._chips.items():
            if chip.isChecked():
                return key
        return None

    def set_checked(self, key: str) -> None:
        """Check `key`'s chip WITHOUT firing on_change (used to mirror external
        state, e.g. the tracker's Follow-up-due checkbox)."""
        chip = self._chips.get(key)
        if chip is None or chip.isChecked():
            return
        for c in self._chips.values():
            c.blockSignals(True)
        try:
            chip.setChecked(True)
        finally:
            for c in self._chips.values():
                c.blockSignals(False)


class _CountBadge(QtWidgets.QLabel):
    """A small bordered `N label` badge (mono-bold count + muted caption)."""

    def __init__(self, label: str, value_color: str = theme.TEXT, parent=None) -> None:
        super().__init__(parent)
        self._label = label
        self._value_color = value_color
        self.setProperty("countBadge", True)
        theme.set_type_role(self, "caption")
        self.set_value(0)

    def set_value(self, n: int) -> None:
        self._value = int(n)
        self.setText(
            f'<span style="font-family:Consolas,\'Cascadia Mono\',monospace;'
            f'font-weight:700;color:{self._value_color}">{self._value:,}</span> '
            f'<span style="color:{theme.MUTED}">{self._label}</span>')

    def value(self) -> int:
        return self._value


class IdentityStrip(QtWidgets.QFrame):
    """The persistent top strip: wordmark + tagline + freshness + counts."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("strip", True)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(16, 6, 16, 6)
        lay.setSpacing(12)

        self.wordmark = QtWidgets.QLabel(
            f'<span style="color:{theme.ACCENT};font-weight:700">IN</span>'
            f'<span style="color:{theme.TEXT};font-weight:700">ployed</span>')
        theme.set_type_role(self.wordmark, "section")
        lay.addWidget(self.wordmark)

        self.tagline = QtWidgets.QLabel(
            f'<span style="color:{theme.FAINT}">job-search console</span>')
        theme.set_type_role(self.tagline, "mono")
        lay.addWidget(self.tagline)

        self.freshness = Pill("last run unknown", outline=True, dot=theme.MUTED)
        lay.addWidget(self.freshness)
        lay.addStretch(1)

        self.jobs_badge = _CountBadge("jobs")
        self.unseen_badge = _CountBadge("unseen", value_color=theme.ACCENT)
        self.tracked_badge = _CountBadge("tracked")
        for b in (self.jobs_badge, self.unseen_badge, self.tracked_badge):
            lay.addWidget(b)

    def set_counts(self, jobs: int, unseen: int, tracked: int) -> None:
        self.jobs_badge.set_value(jobs)
        self.unseen_badge.set_value(unseen)
        self.tracked_badge.set_value(tracked)

    def set_freshness(self, state: str, label: str) -> None:
        """`state` in {"fresh","stale"} (jobsdata.run_staleness's states);
        anything else renders the neutral unknown dot."""
        dots = {"fresh": theme.GOOD, "stale": theme.AMBER}
        self.freshness.set_dot(dots.get(state, theme.MUTED))
        self.freshness.setText(label)
