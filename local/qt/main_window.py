"""The dashboard main window: a QMainWindow hosting the seven-tab QTabWidget.

SP2 builds the shell with placeholder tabs; later phases swap each placeholder for
its real widget (jobs tables, stats, settings, the editors). Keeping the tab list
and titles here means the rest of the port plugs into a stable frame.
"""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtWidgets

TAB_TITLES = [
    "High Score (Unseen)",
    "All Jobs",
    "Tracker",
    "Stats",
    "Resume Data",
    "Apply Answers",
    "Settings",
]


class MainWindow(QtWidgets.QMainWindow):
    """Top-level window. `csv_paths` are the scored run files to load (SP3+)."""

    def __init__(self, csv_paths: list[Path] | None = None,
                 parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.csv_paths: list[Path] = list(csv_paths or [])
        self.setWindowTitle("INployed")
        self.setMinimumSize(960, 640)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.setCentralWidget(self.tabs)

        self._tab_widgets: dict[str, QtWidgets.QWidget] = {}
        self._build_tabs()

    def _build_tabs(self) -> None:
        for title in TAB_TITLES:
            page = QtWidgets.QWidget()
            self._tab_widgets[title] = page
            self.tabs.addTab(page, title)

    # ---- small accessors (used by tests + later phases) ----------------------

    def tab_count(self) -> int:
        return self.tabs.count()

    def tab_titles(self) -> list[str]:
        return [self.tabs.tabText(i) for i in range(self.tabs.count())]
