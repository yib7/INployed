"""Small shared widgets for the Qt dashboard.

`CollapsibleSection` is a titled card whose body folds away on a header click —
used to tame the long Settings form. `ColorLegend` is the thin row-color key
shown under the tinted job tables.
"""
from __future__ import annotations

from typing import Callable

from PySide6 import QtCore, QtWidgets

from qt import theme


class CollapsibleSection(QtWidgets.QFrame):
    """A titled CARD whose body collapses/expands on a header click (restyle 3f).

    The card is a `QFrame[card="true"]` (panel surface, hairline border, r8).
    The header row holds the ▾/▸ tool button (`_header` — its identity and the
    `is_collapsed()`/`set_collapsed()` API are test-coupled), the always-visible
    muted tagline, and a right-aligned tertiary Collapse/Expand button. A 1px
    soft divider separates the header from the body. Add the section's content
    with `add_widget` / `add_layout`. `on_toggled(collapsed)` fires on each user
    toggle so the caller can persist the state. The header carries the section
    type role (scales with the app font); weight comes from the QSS
    `sectionHeader` property.
    """

    def __init__(self, title: str, *, subtitle: str = "", collapsed: bool = False,
                 on_toggled: Callable[[bool], None] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.title = title
        self._on_toggled = on_toggled or (lambda _c: None)
        self.setProperty("card", True)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header row: toggle button + an always-visible muted tagline (so a
        # collapsed section still tells you what it's for) + Collapse/Expand.
        header_row = QtWidgets.QHBoxLayout()
        header_row.setContentsMargins(14, 8, 10, 8)
        header_row.setSpacing(10)
        self._header = QtWidgets.QToolButton()
        # && — a literal ampersand ("Connection & paths"), not a mnemonic.
        self._header.setText(title.replace("&", "&&"))
        self._header.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.setAutoRaise(True)
        self._header.setProperty("sectionHeader", True)
        theme.set_type_role(self._header, "section")
        self._header.clicked.connect(self._on_header_clicked)
        header_row.addWidget(self._header)
        self._subtitle = QtWidgets.QLabel(subtitle)
        self._subtitle.setProperty("muted", True)
        self._subtitle.setVisible(bool(subtitle))
        header_row.addWidget(self._subtitle)
        header_row.addStretch(1)
        self._toggle_btn = QtWidgets.QPushButton("Collapse")
        self._toggle_btn.setProperty("tier", "tertiary")
        self._toggle_btn.clicked.connect(self._on_header_clicked)
        header_row.addWidget(self._toggle_btn)
        outer.addLayout(header_row)

        self._divider = QtWidgets.QFrame()
        self._divider.setProperty("divider", True)
        self._divider.setFixedHeight(1)
        outer.addWidget(self._divider)

        self._body = QtWidgets.QWidget()
        self._body_layout = QtWidgets.QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(16, 10, 16, 12)
        outer.addWidget(self._body)

        self.set_collapsed(collapsed)

    def content_layout(self) -> QtWidgets.QVBoxLayout:
        return self._body_layout

    def add_widget(self, w: QtWidgets.QWidget) -> None:
        self._body_layout.addWidget(w)

    def add_layout(self, lay) -> None:
        self._body_layout.addLayout(lay)

    def is_collapsed(self) -> bool:
        # isHidden() is the explicit fold flag — unlike isVisible() it does not also
        # report False just because the form hasn't been shown yet (e.g. in tests).
        return self._body.isHidden()

    def set_collapsed(self, collapsed: bool) -> None:
        self._body.setVisible(not collapsed)
        self._divider.setVisible(not collapsed)
        self._toggle_btn.setText("Expand" if collapsed else "Collapse")
        self._header.setArrowType(
            QtCore.Qt.ArrowType.RightArrow if collapsed else QtCore.Qt.ArrowType.DownArrow)

    def _on_header_clicked(self) -> None:
        self.set_collapsed(not self.is_collapsed())
        self._on_toggled(self.is_collapsed())

class ColorLegend(QtWidgets.QWidget):
    """A thin horizontal key: a small color swatch + muted label per `(color, text)`.

    Used under the job tables to explain the row tints. `items` is a list of
    `(hex_color, label)` (the caller passes the `theme.ROW_*` tints) so the swatches
    match exactly what users see in the rows.
    """

    def __init__(self, items, parent=None) -> None:
        super().__init__(parent)
        self.items = list(items)
        self._labels: list[str] = []
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(2, 2, 2, 2)
        h.setSpacing(14)
        for color, text in self.items:
            swatch = QtWidgets.QLabel()
            swatch.setFixedSize(13, 13)
            swatch.setStyleSheet(
                f"background: {color}; border: 1px solid {theme.BORDER}; border-radius: 3px;")
            label = QtWidgets.QLabel(text)
            label.setProperty("muted", True)
            self._labels.append(text)
            h.addWidget(swatch)
            h.addWidget(label)
        h.addStretch(1)

    def labels(self) -> list[str]:
        return list(self._labels)
