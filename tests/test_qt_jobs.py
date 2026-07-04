"""SP3: the jobs model + proxy + JobsTab (filter, sort, column toggle, coloring, actions)."""
import pandas as pd
from PySide6 import QtCore, QtWidgets

from qt import theme
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
    # Default mode ("high"/unseen): tailored-resume wins, then recommendation;
    # a "skip" reco is the untinted "don't consider" default.
    m = JobsTableModel(COL_IDS)
    m.set_dataframe(_df(), resume_ids={"2"})
    bg = m.data(m.index(1, 0), QtCore.Qt.ItemDataRole.BackgroundRole)
    assert bg is not None and bg.isValid()          # id 2 has a resume -> tinted
    assert m.row_tag(1) == "has_resume"
    assert m.row_tag(0) == "apply"                   # id 1 reco apply -> apply tint
    assert m.row_tag(2) == ""                        # skip -> default (no tint)


def test_all_mode_is_untinted(qapp):
    # The All Jobs tab is a plain list: no row gets a color tag.
    m = JobsTableModel(COL_IDS, mode="all")
    m.set_dataframe(_df(), resume_ids={"1", "2", "3"})
    assert [m.row_tag(i) for i in range(3)] == ["", "", ""]
    bg = m.data(m.index(0, 0), QtCore.Qt.ItemDataRole.BackgroundRole)
    assert bg is None


def _tracker_df():
    # status x follow_up combinations the tracker feeds the model.
    return pd.DataFrame([
        {"job_posting_id": "a", "status": "applied", "follow_up": "",
         "job_title": "A", "company_name": "Co", "url": "u"},
        {"job_posting_id": "b", "status": "applied", "follow_up": "DUE",
         "job_title": "B", "company_name": "Co", "url": "u"},
        {"job_posting_id": "c", "status": "applied", "follow_up": "done",
         "job_title": "C", "company_name": "Co", "url": "u"},
        {"job_posting_id": "d", "status": "interviewing", "follow_up": "",
         "job_title": "D", "company_name": "Co", "url": "u"},
        {"job_posting_id": "e", "status": "offer", "follow_up": "done",
         "job_title": "E", "company_name": "Co", "url": "u"},
        {"job_posting_id": "f", "status": "rejected", "follow_up": "",
         "job_title": "F", "company_name": "Co", "url": "u"},
    ])


def test_tracker_mode_status_and_followup_tags(qapp):
    cols = ["status", "follow_up", "job_title", "company_name", "url"]
    m = JobsTableModel(cols, mode="tracker")
    m.set_dataframe(_tracker_df())
    tags = [m.row_tag(i) for i in range(6)]
    assert tags == ["applied", "followup", "pending", "interviewing", "offer", "rejected"]
    # the two new tints resolve to real colors
    assert theme.row_color("followup").isValid()     # follow-up due -> orange
    assert theme.row_color("pending").isValid()      # follow-up sent -> pink


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


def test_easy_filter_is_a_three_state_combo(qtbot):
    # SP3: the Easy Apply checkbox became a combo with a Not-Easy-Apply state.
    tab = JobsTab("all", COLS)
    qtbot.addWidget(tab)
    assert isinstance(tab.easy, QtWidgets.QComboBox)
    items = [tab.easy.itemText(i) for i in range(tab.easy.count())]
    assert items == ["All", "Easy Apply", "Not Easy Apply"]
    assert tab.easy.currentText() == "All"


def test_easy_combo_counts_and_resets(qtbot):
    tab = JobsTab("all", COLS)
    qtbot.addWidget(tab)
    tab.set_source_df(_df())
    assert tab._filters_btn.text() == "Filters"
    tab.easy.setCurrentText("Not Easy Apply")     # currentIndexChanged -> _apply_filters
    assert "(1)" in tab._filters_btn.text()
    tab.easy.setCurrentText("Easy Apply")
    assert "(1)" in tab._filters_btn.text()
    tab.reset_filters()
    assert tab.easy.currentText() == "All"
    assert tab._filters_btn.text() == "Filters"


def test_easy_combo_filters_rows(qtbot):
    tab = JobsTab("all", COLS)
    qtbot.addWidget(tab)
    df = _df()
    df["is_easy_apply"] = ["True", "false", None]
    tab.set_source_df(df)
    tab.easy.setCurrentText("Easy Apply")
    assert tab.model.rowCount() == 1
    tab.easy.setCurrentText("Not Easy Apply")     # NaN counts as not-easy
    assert tab.model.rowCount() == 2
    tab.easy.setCurrentText("All")
    assert tab.model.rowCount() == 3


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


def test_high_tab_legend_matches_reco_and_resume(qtbot):
    # High Score (Unseen): yellow=consider, blue=tailored/resume, green=apply,
    # default=don't consider. No tracker-only meanings (rejected/offer) here.
    tab = JobsTab("high", COLS)
    qtbot.addWidget(tab)
    legend = tab._legend
    texts = " ".join(legend.labels()).lower()
    assert "consider" in texts and "apply" in texts and "tailored" in texts
    assert "rejected" not in texts and "offer" not in texts
    by_color = {c: t.lower() for c, t in legend.items}
    assert "consider" in by_color[theme.ROW_CONSIDER]
    assert "apply" in by_color[theme.ROW_APPLY]


def test_all_tab_has_no_legend(qtbot):
    # All Jobs is a plain list — no color legend (and no row tint, see model test).
    tab = JobsTab("all", COLS)
    qtbot.addWidget(tab)
    assert tab._legend is None


def test_tracker_tab_legend_covers_status_and_followups(qtbot):
    tab = JobsTab("tracker", COLS)
    qtbot.addWidget(tab)
    legend = tab._legend
    by_color = {c: t.lower() for c, t in legend.items}
    assert "applied" in by_color[theme.ROW_HAS_RESUME]
    assert "offer" in by_color[theme.ROW_APPLY]
    assert "interview" in by_color[theme.ROW_CONSIDER]
    assert "reject" in by_color[theme.ROW_REJECTED]
    assert "follow-up due" in by_color[theme.ROW_FOLLOWUP]
    assert "follow-up sent" in by_color[theme.ROW_PENDING]


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
