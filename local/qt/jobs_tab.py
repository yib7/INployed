"""A job-list tab: a filter bar over a sortable, virtualized QTableView.

Shared by High Score, All Jobs, and Tracker (different column sets + persistence
keys). Filtering reuses `jobsdata.filter_and_sort` for exact parity with the old
dashboard; the table sorts on a header click via a numeric-aware proxy. Columns
can be hidden via a checkbox dialog (persisted to config.json). Row actions —
open URL, set status, block company, selection -> preview — are injected callbacks
so the widget is decoupled and testable without the full app.
"""
from __future__ import annotations

import pandas as pd
from PySide6 import QtCore, QtWidgets

import jobsdata
from jobsdata import COLUMN_LABELS, LABEL_TO_COLUMN
from qt.jobs_model import SORT_ROLE, JobsTableModel
from seen_db import APP_STATUSES
from vm_schedule import RUN_LABELS


class JobsTab(QtWidgets.QWidget):
    def __init__(self, table_key: str, columns, *, on_open_url=None, on_set_status=None,
                 on_block=None, on_selection=None, hidden_columns=None, save_hidden=None,
                 parent=None):
        super().__init__(parent)
        self.table_key = table_key
        self.col_ids = [c for c, _ in columns]
        self._on_open_url = on_open_url or (lambda jid: None)
        self._on_set_status = on_set_status or (lambda ids, status: None)
        self._on_block = on_block or (lambda company: None)
        self._on_selection = on_selection or (lambda jid: None)
        self._hidden: set[str] = set((hidden_columns or {}).get(table_key, []))
        self._save_hidden = save_hidden or (lambda key, hidden: None)
        self._base = pd.DataFrame()
        self._resume_ids = frozenset()

        self.model = JobsTableModel(self.col_ids)
        self.proxy = QtCore.QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)

        self._build()
        self._apply_column_visibility()

    # ---- construction --------------------------------------------------------

    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        bar = QtWidgets.QHBoxLayout()
        layout.addLayout(bar)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search title / company / URL...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._debounced_filter)
        bar.addWidget(QtWidgets.QLabel("Search:"))
        bar.addWidget(self.search, 1)

        self.search_col = QtWidgets.QComboBox()
        self.search_col.addItem("All")
        self.search_col.addItems([COLUMN_LABELS.get(c, c) for c in self.col_ids])
        self.search_col.currentIndexChanged.connect(self._apply_filters)
        bar.addWidget(QtWidgets.QLabel("In:"))
        bar.addWidget(self.search_col)

        self.minscore = QtWidgets.QComboBox()
        self.minscore.addItems(["Any", "1", "2", "3", "4", "5"])
        self.minscore.currentIndexChanged.connect(self._apply_filters)
        bar.addWidget(QtWidgets.QLabel("Min score:"))
        bar.addWidget(self.minscore)

        self.day = QtWidgets.QComboBox()
        self.day.addItem("All")
        self.day.currentIndexChanged.connect(self._apply_filters)
        bar.addWidget(QtWidgets.QLabel("Day:"))
        bar.addWidget(self.day)

        self.time = QtWidgets.QComboBox()
        self.time.addItems(["All", *RUN_LABELS])
        self.time.currentIndexChanged.connect(self._apply_filters)
        bar.addWidget(QtWidgets.QLabel("Time:"))
        bar.addWidget(self.time)

        self.reco = QtWidgets.QComboBox()
        self.reco.addItems(["All", "apply", "consider", "skip"])
        self.reco.currentIndexChanged.connect(self._apply_filters)
        bar.addWidget(QtWidgets.QLabel("Reco:"))
        bar.addWidget(self.reco)

        self.easy = QtWidgets.QCheckBox("Easy Apply")
        self.easy.stateChanged.connect(self._apply_filters)
        bar.addWidget(self.easy)

        reset = QtWidgets.QPushButton("Reset")
        reset.clicked.connect(self.reset_filters)
        bar.addWidget(reset)
        cols_btn = QtWidgets.QPushButton("Columns...")
        cols_btn.clicked.connect(self._choose_columns)
        bar.addWidget(cols_btn)

        self.count_label = QtWidgets.QLabel("")
        self.count_label.setProperty("muted", True)
        bar.addWidget(self.count_label)

        self.table = QtWidgets.QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.selectionModel().selectionChanged.connect(self._on_select)
        self.table.setSortingEnabled(True)
        # Start in the model's default order (sort_query); a header click sorts.
        self.table.horizontalHeader().setSortIndicator(
            -1, QtCore.Qt.SortOrder.AscendingOrder)
        self.proxy.sort(-1)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        self._set_column_widths(columns_with_widths(self.table_key, self.col_ids))
        layout.addWidget(self.table, 1)

        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self._apply_filters)

    def _set_column_widths(self, widths: list[int]) -> None:
        for i, w in enumerate(widths):
            self.table.setColumnWidth(i, w)

    # ---- data feed -----------------------------------------------------------

    def set_source_df(self, df: pd.DataFrame | None, resume_ids=frozenset()) -> None:
        self._base = df if df is not None else pd.DataFrame()
        self._resume_ids = frozenset(resume_ids)
        self._refresh_day_combo()
        self._apply_filters()

    def _debounced_filter(self) -> None:
        self._debounce.start()

    def _apply_filters(self) -> None:
        label = self.search_col.currentText()
        col = LABEL_TO_COLUMN.get(label, label)
        view = jobsdata.filter_and_sort(
            self._base, self.search.text().strip().lower(), self.minscore.currentText(),
            self.day.currentText(), self.time.currentText(), self.reco.currentText(),
            self.easy.isChecked(), col)
        self.model.set_dataframe(view, self._resume_ids)
        total = 0 if self._base is None or self._base.empty else len(self._base)
        self.count_label.setText(f"{len(view)} of {total} shown")

    def reset_filters(self) -> None:
        self.search.clear()
        for cb in (self.search_col, self.minscore, self.day, self.time, self.reco):
            cb.setCurrentIndex(0)
        self.easy.setChecked(False)
        self._apply_filters()

    def _refresh_day_combo(self) -> None:
        cur = self.day.currentText()
        days: list[str] = []
        if not self._base.empty and "extracted_date" in self._base.columns:
            days = sorted({d for d in self._base["extracted_date"].astype(str)
                           if d and d.lower() != "nan"}, reverse=True)
        self.day.blockSignals(True)
        self.day.clear()
        self.day.addItems(["All", *days])
        idx = self.day.findText(cur)
        self.day.setCurrentIndex(idx if idx >= 0 else 0)
        self.day.blockSignals(False)

    # ---- columns -------------------------------------------------------------

    def _apply_column_visibility(self) -> None:
        for i, cid in enumerate(self.col_ids):
            self.table.setColumnHidden(i, cid in self._hidden)

    def set_column_hidden(self, cid: str, hidden: bool) -> None:
        """Hide/show one column; never lets every column be hidden (blank table)."""
        target = set(self._hidden)
        target.discard(cid)
        if hidden:
            target.add(cid)
        if len(target) >= len(self.col_ids):
            return
        self._hidden = target
        self._save_hidden(self.table_key, sorted(self._hidden))
        self._apply_column_visibility()

    def _choose_columns(self) -> None:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Columns")
        v = QtWidgets.QVBoxLayout(dlg)
        v.addWidget(QtWidgets.QLabel("Show these columns:"))
        for cid in self.col_ids:
            cb = QtWidgets.QCheckBox(COLUMN_LABELS.get(cid, cid))
            cb.setChecked(cid not in self._hidden)
            cb.toggled.connect(lambda checked, c=cid: self.set_column_hidden(c, not checked))
            v.addWidget(cb)
        close = QtWidgets.QPushButton("Close")
        close.setProperty("accent", True)
        close.clicked.connect(dlg.accept)
        v.addWidget(close)
        dlg.exec()

    # ---- selection / actions -------------------------------------------------

    def selected_ids(self) -> list[str]:
        ids = []
        for idx in self.table.selectionModel().selectedRows():
            src = self.proxy.mapToSource(idx)
            jid = self.model.job_id(src.row())
            if jid:
                ids.append(jid)
        return ids

    def _ids_at(self, pos) -> list[str]:
        ids = self.selected_ids()
        if ids:
            return ids
        idx = self.table.indexAt(pos)
        if idx.isValid():
            jid = self.model.job_id(self.proxy.mapToSource(idx).row())
            if jid:
                return [jid]
        return []

    def _on_double_click(self, index) -> None:
        jid = self.model.job_id(self.proxy.mapToSource(index).row())
        if jid:
            self._on_open_url(jid)

    def _on_select(self, *_) -> None:
        ids = self.selected_ids()
        self._on_selection(ids[0] if ids else "")

    def apply_status(self, ids: list[str], status: str) -> None:
        if ids:
            self._on_set_status(ids, status)

    def block_company(self, jid: str) -> None:
        company = self._company_for(jid)
        if company:
            self._on_block(company)

    def _company_for(self, jid: str) -> str:
        if self._base.empty or "company_name" not in self._base.columns:
            return ""
        rows = self._base.loc[self._base["job_posting_id"].astype(str) == str(jid), "company_name"]
        return str(rows.iloc[0]) if len(rows) else ""

    def _context_menu(self, pos) -> None:
        ids = self._ids_at(pos)
        if not ids:
            return
        menu = QtWidgets.QMenu(self)
        open_act = menu.addAction("Open in browser")
        status_menu = menu.addMenu("Set status")
        status_acts = {status_menu.addAction(st): st for st in APP_STATUSES}
        menu.addSeparator()
        block_act = menu.addAction("Block company")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is open_act:
            self._on_open_url(ids[0])
        elif chosen is block_act:
            self.block_company(ids[0])
        elif chosen in status_acts:
            self.apply_status(ids, status_acts[chosen])


def columns_with_widths(table_key: str, col_ids: list[str]) -> list[int]:
    """Initial pixel widths per column for `table_key`, falling back to a default."""
    from jobsdata import ALL_COLUMNS, HIGH_SCORE_COLUMNS, TRACKER_COLUMNS
    source = {"high": HIGH_SCORE_COLUMNS, "all": ALL_COLUMNS,
              "tracker": TRACKER_COLUMNS}.get(table_key, [])
    widths = {c: w for c, w in source}
    return [widths.get(c, 120) for c in col_ids]
