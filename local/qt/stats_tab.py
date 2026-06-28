"""The Stats tab: per-run pipeline metrics + the applied-vs-recommendation readout.

A dumb view — the controller (`MainWindow`) reads `run_stats.csv` and the registry
and calls `set_stats`. Reuses `JobsTableModel` for the read-only metrics grid.
"""
from __future__ import annotations

import pandas as pd
from PySide6 import QtWidgets

from jobsdata import STATS_COLUMNS
from qt.jobs_model import JobsTableModel


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

        self.model = JobsTableModel([c for c, _ in STATS_COLUMNS])
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, (_, w) in enumerate(STATS_COLUMNS):
            self.table.setColumnWidth(i, w)
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
        from qt import theme
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
