"""Small shared widgets for the Qt dashboard.

`ScorePreview` renders the per-job analysis (reason / strengths / gaps / JD
snippet) built by `jobsdata.job_detail_segments`, in a read-only pane shown
beside the job tables. `CollapsibleSection` is a titled block whose body folds
away on a header click — used to tame the long Settings form.
"""
from __future__ import annotations

import html
from typing import Callable

from PySide6 import QtCore, QtWidgets

from qt import theme


class CollapsibleSection(QtWidgets.QWidget):
    """A titled section whose body collapses/expands on a header click.

    The header is a flat tool button with a ▾ (open) / ▸ (collapsed) arrow; add
    the section's content with `add_widget` / `add_layout`. `on_toggled(collapsed)`
    fires on each user toggle so the caller can persist the state. The header
    carries no explicit font (so it scales with the app font); its weight comes
    from the `sectionHeader` QSS property.
    """

    def __init__(self, title: str, *, collapsed: bool = False,
                 on_toggled: Callable[[bool], None] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.title = title
        self._on_toggled = on_toggled or (lambda _c: None)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        self._header = QtWidgets.QToolButton()
        self._header.setText(title)
        self._header.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.setAutoRaise(True)
        self._header.setProperty("sectionHeader", True)
        self._header.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                   QtWidgets.QSizePolicy.Policy.Fixed)
        self._header.clicked.connect(self._on_header_clicked)
        outer.addWidget(self._header)

        self._body = QtWidgets.QWidget()
        self._body_layout = QtWidgets.QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(12, 0, 0, 6)
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
        self._header.setArrowType(
            QtCore.Qt.ArrowType.RightArrow if collapsed else QtCore.Qt.ArrowType.DownArrow)

    def _on_header_clicked(self) -> None:
        self.set_collapsed(not self.is_collapsed())
        self._on_toggled(self.is_collapsed())

_STYLES = {
    "h": f"color:{theme.ACCENT};font-weight:600",
    "muted": f"color:{theme.MUTED}",
    "good": f"color:{theme.GOOD}",
    "bad": f"color:{theme.DANGER}",
    "": f"color:{theme.TEXT}",
}
_EMPTY = (f'<span style="color:{theme.MUTED}">Select a job to see its '
          f'score breakdown, strengths, gaps, and a JD snippet.</span>')


class ScorePreview(QtWidgets.QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setMinimumHeight(120)
        self.show_segments([])

    def show_segments(self, segs: list[tuple[str, str]]) -> None:
        if not segs:
            self.setHtml(_EMPTY)
            return
        parts = []
        for text, style in segs:
            esc = html.escape(text).replace("\n", "<br>")
            css = _STYLES.get(style or "", _STYLES[""])
            parts.append(f'<span style="{css}">{esc}</span>')
        self.setHtml("".join(parts))
