"""SP3: the jobs model + proxy + JobsTab (filter, sort, column toggle, coloring, actions)."""
import pandas as pd
from PySide6 import QtCore, QtWidgets

from qt.jobs_model import SORT_ROLE, JobsTableModel
from qt.jobs_tab import JobsTab

COLS = [("score", 50), ("deep_score", 70), ("recommendation", 100),
        ("job_title", 240), ("company_name", 170), ("url", 220)]
COL_IDS = [c for c, _ in COLS]


def _df():
    return pd.DataFrame([
        {"job_posting_id": "1", "score": "5", "deep_score": "4", "recommendation": "apply",
         "job_title": "Data Analyst", "company_name": "Acme", "url": "https://x/1",
         "is_seen": "no", "extracted_date": "2026-06-20"},
        {"job_posting_id": "2", "score": "4", "deep_score": "5", "recommendation": "consider",
         "job_title": "ML Engineer", "company_name": "Globex", "url": "https://x/2",
         "is_seen": "no", "extracted_date": "2026-06-21"},
        {"job_posting_id": "3", "score": "2", "deep_score": "1", "recommendation": "skip",
         "job_title": "QA Tester", "company_name": "Initech", "url": "https://x/3",
         "is_seen": "no", "extracted_date": "2026-06-19"},
    ])


def test_model_shape_and_display(qapp):
    m = JobsTableModel(COL_IDS)
    m.set_dataframe(_df())
    assert m.rowCount() == 3
    assert m.columnCount() == len(COL_IDS)
    assert m.headerData(0, QtCore.Qt.Orientation.Horizontal) == "Score"
    cell = m.index(0, COL_IDS.index("company_name"))
    assert m.data(cell) == "Acme"


def test_model_background_role_tags(qapp):
    m = JobsTableModel(COL_IDS)
    m.set_dataframe(_df(), resume_ids={"2"})
    bg = m.data(m.index(1, 0), QtCore.Qt.ItemDataRole.BackgroundRole)
    assert bg is not None and bg.isValid()          # id 2 has a resume -> tinted
    assert m.row_tag(1) == "has_resume"
    assert m.row_tag(0) == "apply"                   # id 1 reco apply -> apply tint
    assert m.row_tag(2) == "skip"


def test_proxy_numeric_sort(qapp):
    m = JobsTableModel(COL_IDS)
    m.set_dataframe(_df())
    proxy = QtCore.QSortFilterProxyModel()
    proxy.setSourceModel(m)
    proxy.setSortRole(SORT_ROLE)
    proxy.sort(COL_IDS.index("score"), QtCore.Qt.SortOrder.AscendingOrder)
    order = [m.job_id(proxy.mapToSource(proxy.index(r, 0)).row()) for r in range(3)]
    assert order == ["3", "2", "1"]                  # numeric 2 < 4 < 5, not "2"<"4"<"5" text


def test_tab_filters_by_search(qtbot):
    tab = JobsTab("all", COLS)
    qtbot.addWidget(tab)
    tab.set_source_df(_df())
    assert tab.model.rowCount() == 3
    tab.search.setText("globex")
    tab._apply_filters()
    assert tab.model.rowCount() == 1


def test_tab_min_score_filter(qtbot):
    tab = JobsTab("all", COLS)
    qtbot.addWidget(tab)
    tab.set_source_df(_df())
    tab.minscore.setCurrentText("4")   # currentIndexChanged -> _apply_filters
    assert tab.model.rowCount() == 2


def test_discovery_filters_live_in_popup(qtbot):
    # Cycle 16 SP3: Min score / Day / Time / Reco / Easy moved into a Filters popup.
    tab = JobsTab("all", COLS)
    qtbot.addWidget(tab)
    for w in (tab.minscore, tab.day, tab.time, tab.reco, tab.easy):
        assert w.parentWidget() is tab._filters_popup
    # filtering from inside the popup still narrows the table
    tab.set_source_df(_df())
    tab.minscore.setCurrentText("4")
    assert tab.model.rowCount() == 2


def test_filters_button_shows_active_count(qtbot):
    tab = JobsTab("all", COLS)
    qtbot.addWidget(tab)
    tab.set_source_df(_df())
    assert tab._filters_btn.text() == "Filters"
    tab.minscore.setCurrentText("4")
    tab.reco.setCurrentText("apply")
    assert "(2)" in tab._filters_btn.text()       # badge counts the active filters
    tab.reset_filters()
    assert tab._filters_btn.text() == "Filters"


def test_add_filter_row_lives_in_popup_and_counts(qtbot):
    # Cycle 17 SP2: an extra filter (the Tracker's Follow-up-due checkbox) mounts in
    # the Filters popup and, when active, counts toward the badge.
    tab = JobsTab("tracker", COLS)
    qtbot.addWidget(tab)
    cb = QtWidgets.QCheckBox("Follow-up due only")
    tab.add_filter_row(cb, is_active=cb.isChecked)
    assert cb.parentWidget() is tab._filters_popup
    tab.set_source_df(_df())
    assert tab._filters_btn.text() == "Filters"   # nothing active yet
    cb.setChecked(True)
    tab._apply_filters()                          # (in the app, stateChanged drives this)
    assert "(1)" in tab._filters_btn.text()


def test_tab_column_toggle_hides_and_persists(qtbot):
    saved = {}
    tab = JobsTab("all", COLS, save_hidden=lambda k, h: saved.__setitem__(k, h))
    qtbot.addWidget(tab)
    url_idx = tab.col_ids.index("url")
    assert not tab.table.isColumnHidden(url_idx)
    tab.set_column_hidden("url", True)
    assert tab.table.isColumnHidden(url_idx)
    assert "url" in saved["all"]


def test_tab_never_hides_every_column(qtbot):
    tab = JobsTab("all", COLS)
    qtbot.addWidget(tab)
    for c in tab.col_ids:
        tab.set_column_hidden(c, True)
    assert any(not tab.table.isColumnHidden(i) for i in range(len(tab.col_ids)))


def test_empty_widget_toggles_with_data(qtbot):
    tab = JobsTab("high", COLS)
    qtbot.addWidget(tab)
    hint = QtWidgets.QWidget()
    tab.set_empty_widget(hint)
    tab.set_source_df(pd.DataFrame())            # no data -> hint shown, table hidden
    assert not hint.isHidden() and tab.table.isHidden()
    tab.set_source_df(_df())                     # data -> table shown, hint hidden
    assert hint.isHidden() and not tab.table.isHidden()
    # filters hiding every row is NOT the empty state (data still exists)
    tab.search.setText("nomatchxyz")
    tab._apply_filters()
    assert hint.isHidden() and not tab.table.isHidden()


def test_tab_double_click_opens_url(qtbot):
    opened = []
    tab = JobsTab("all", COLS, on_open_url=opened.append)
    qtbot.addWidget(tab)
    tab.set_source_df(_df())
    tab._on_double_click(tab.proxy.index(0, 0))
    assert len(opened) == 1


def test_tab_apply_status_and_block(qtbot):
    statuses, blocked = [], []
    tab = JobsTab("all", COLS,
                  on_set_status=lambda ids, st: statuses.append((ids, st)),
                  on_block=blocked.append)
    qtbot.addWidget(tab)
    tab.set_source_df(_df())
    tab.apply_status(["1"], "applied")
    assert statuses == [(["1"], "applied")]
    tab.block_company("1")
    assert blocked == ["Acme"]
