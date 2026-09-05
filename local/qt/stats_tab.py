"""The Stats tab: per-run pipeline metrics + the applied-vs-recommendation readout.

A dumb view — the controller (`MainWindow`) reads `run_stats.csv` and the registry
and calls `set_stats`. Reuses `JobsTableModel` for the read-only metrics grid.
"""
from __future__ import annotations

import pandas as pd
from PySide6 import QtCore, QtWidgets

from jobsdata import STATS_COLUMNS
from qt import theme
from qt.jobs_model import JobsTableModel

# Every metrics column except the two identifiers is a count -> right-align it
# and group the thousands, so token counts stay scannable down the column.
_TEXT_COLUMNS = {"timestamp", "input_csv"}


class _MetricDelegate(QtWidgets.QStyledItemDelegate):
    """Right-aligns numeric metric cells and renders 118000 as 118,000."""

    def __init__(self, columns: list[str], parent=None):
        super().__init__(parent)
        self._columns = columns

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        col = index.column()
        if not (0 <= col < len(self._columns)):
            return
        if self._columns[col] in _TEXT_COLUMNS:
            return
        option.displayAlignment = (QtCore.Qt.AlignmentFlag.AlignRight
                                   | QtCore.Qt.AlignmentFlag.AlignVCenter)
        try:
            option.text = f"{int(float(option.text)):,}"
        except (TypeError, ValueError):
            pass


class StatsTab(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

        self.badge = QtWidgets.QLabel("")
        self.badge.setWordWrap(True)
        self.badge.hide()
        v.addWidget(self.badge)

        self.summary = QtWidgets.QLabel("")
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)

        stats_cols = [c for c, _ in STATS_COLUMNS]
        self.model = JobsTableModel(stats_cols)
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(False)
        theme.register_table(self.table)
        self._delegate = _MetricDelegate(stats_cols, self.table)
        self.table.setItemDelegate(self._delegate)
        # Stretching the last section made "Out tok" a half-empty column the
        # width of the window; leave the spare width as table background.
        self.table.horizontalHeader().setStretchLastSection(False)
        for i, (_, w) in enumerate(STATS_COLUMNS):
            self.table.setColumnWidth(i, round(w * theme._current_scale))
        v.addWidget(self.table, 1)

        # A passive readout of the applied-vs-recommendation labels (no export).
        self.calibration = QtWidgets.QLabel("")
        self.calibration.setWordWrap(True)
        self.calibration.setProperty("muted", True)
        v.addWidget(self.calibration)

    def set_stats(self, df: pd.DataFrame, summary: str, calibration: str) -> None:
        self.model.set_dataframe(df)
        self.summary.setText(summary)
        self.calibration.setText(calibration)

    def set_freshness(self, state: str, age_hours: float) -> None:
        """Show a fresh/stale badge for the latest pipeline run."""
        if state == "fresh":
            self.badge.setText(f"● Fresh — last run {_human_age(age_hours)}")
            color = theme.GOOD
        else:
            when = "never" if age_hours == float("inf") else _human_age(age_hours)
            self.badge.setText(
                f"● Stale — last run {when}; the cloud job search may have failed")
            color = theme.AMBER
        self.badge.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.badge.show()


def _human_age(hours: float) -> str:
    if hours < 1:
        return "under an hour ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"
