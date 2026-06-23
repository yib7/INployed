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
    def __init__(self, on_export=None, parent=None):
        super().__init__(parent)
        self._on_export = on_export or (lambda: None)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

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

        self.calibration = QtWidgets.QLabel("")
        self.calibration.setWordWrap(True)
        self.calibration.setProperty("muted", True)
        v.addWidget(self.calibration)

        bar = QtWidgets.QHBoxLayout()
        bar.addStretch(1)
        export = QtWidgets.QPushButton("Export calibration CSV")
        export.clicked.connect(lambda: self._on_export())
        bar.addWidget(export)
        v.addLayout(bar)

    def set_stats(self, df: pd.DataFrame, summary: str, calibration: str) -> None:
        self.model.set_dataframe(df)
        self.summary.setText(summary)
        self.calibration.setText(calibration)
