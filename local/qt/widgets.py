"""Small shared widgets for the Qt dashboard.

`ScorePreview` renders the per-job analysis (reason / strengths / gaps / JD
snippet) built by `jobsdata.job_detail_segments`, in a read-only pane shown
beside the job tables.
"""
from __future__ import annotations

import html

from PySide6 import QtWidgets

from qt import theme

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
