"""The dashboard main window: a QMainWindow hosting the seven-tab QTabWidget.

The three job tabs (High Score / All Jobs / Tracker) are real `JobsTab`s wired to
the data; Stats / Resume Data / Apply Answers / Settings are placeholders filled in
later phases. `reload_data` loads the scored CSVs and feeds the tables — it grows
as later phases add the registry, tracker, stats, and worker-backed actions.
"""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtWidgets

import chrome
import jobsdata
from jobsdata import (
    ALL_COLUMNS,
    HIGH_SCORE_COLUMNS,
    TRACKER_COLUMNS,
    drop_blocklisted,
    filter_high_unseen,
    load_files,
    load_hidden_columns,
    load_local_blocklist,
    load_min_score,
)
from qt.jobs_tab import JobsTab

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
    """Top-level window. `csv_paths` are the scored run files to load."""

    def __init__(self, csv_paths: list[Path] | None = None,
                 parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.csv_paths: list[Path] = list(csv_paths or [])
        self.setWindowTitle("INployed")
        self.setMinimumSize(960, 640)

        self.min_score = load_min_score()
        self.hidden_columns = load_hidden_columns()
        self._url_by_id: dict[str, str] = {}

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.setCentralWidget(self.tabs)

        self._tab_widgets: dict[str, QtWidgets.QWidget] = {}
        self._build_tabs()
        self.reload_data()

    # ---- construction --------------------------------------------------------

    def _make_jobs_tab(self, key: str, columns) -> JobsTab:
        return JobsTab(
            key, columns,
            on_open_url=self._open_url,
            on_set_status=self._set_status,
            on_block=self._block_company,
            on_selection=self._on_selection,
            hidden_columns=self.hidden_columns,
            save_hidden=self._save_hidden,
        )

    def _build_tabs(self) -> None:
        self.high_tab = self._make_jobs_tab("high", HIGH_SCORE_COLUMNS)
        self.all_tab = self._make_jobs_tab("all", ALL_COLUMNS)
        self.tracker_tab = self._make_jobs_tab("tracker", TRACKER_COLUMNS)
        pages: dict[str, QtWidgets.QWidget] = {
            "High Score (Unseen)": self.high_tab,
            "All Jobs": self.all_tab,
            "Tracker": self.tracker_tab,
        }
        for title in TAB_TITLES:
            page = pages.get(title) or QtWidgets.QWidget()
            self._tab_widgets[title] = page
            self.tabs.addTab(page, title)

    # ---- data ----------------------------------------------------------------

    def reload_data(self) -> None:
        df, _ = load_files(self.csv_paths)
        df = drop_blocklisted(df, load_local_blocklist(self.csv_paths))
        self.df = df
        self._url_by_id = (
            dict(zip(df["job_posting_id"].astype(str), df["url"].astype(str)))
            if not df.empty and "url" in df.columns else {}
        )
        df_high = filter_high_unseen(df, self.min_score)
        resume_ids = self._resume_ids()
        self.high_tab.set_source_df(df_high, resume_ids)
        self.all_tab.set_source_df(df, resume_ids)
        # Tracker data prep (status/days/follow-up frame) arrives in SP5.

    def _resume_ids(self) -> frozenset:
        return frozenset()  # registry overlay arrives in SP4

    # ---- callbacks (the registry-backed ones are completed in SP4/SP5) -------

    def _open_url(self, jid: str) -> None:
        url = self._url_by_id.get(jid, "")
        if url:
            chrome.open_in_chrome(url)

    def _set_status(self, ids: list[str], status: str) -> None:
        pass  # completed in SP5 (registry write + refresh)

    def _block_company(self, company: str) -> None:
        pass  # completed in SP5 (append_to_blocklist + reload)

    def _on_selection(self, jid: str) -> None:
        pass  # completed in SP4 (score preview)

    def _save_hidden(self, key: str, hidden: list[str]) -> None:
        self.hidden_columns[key] = list(hidden)
        jobsdata.save_hidden_columns(self.hidden_columns)

    # ---- small accessors (used by tests + later phases) ----------------------

    def tab_count(self) -> int:
        return self.tabs.count()

    def tab_titles(self) -> list[str]:
        return [self.tabs.tabText(i) for i in range(self.tabs.count())]
