"""Phase 7 (ship cycle 6) UI polish: header/cell alignment, Title stretch, and
the accessible names screen readers need on inputs without a QFormLayout label.
"""
from unittest.mock import MagicMock

import pandas as pd
from PySide6 import QtCore, QtWidgets

from jobsdata import HIGH_SCORE_COLUMNS, TRACKER_COLUMNS
from qt import jobs_tab, theme
from qt.jobs_model import JobsTableModel
from qt.jobs_tab import JobsTab
from qt.main_window import MainWindow
from qt.stats_tab import StatsTab
from qt.vm_panel import VMPanel
from qt.widgets import ElidedLabel

_ALIGN = QtCore.Qt.ItemDataRole.TextAlignmentRole
_H = QtCore.Qt.Orientation.Horizontal


def _high_tab(qtbot) -> JobsTab:
    tab = JobsTab("high", HIGH_SCORE_COLUMNS)
    qtbot.addWidget(tab)
    return tab


def test_header_alignment_matches_cell_alignment():
    model = JobsTableModel([c for c, _ in HIGH_SCORE_COLUMNS])
    cols = {c: i for i, (c, _) in enumerate(HIGH_SCORE_COLUMNS)}

    def flag(cid):
        return QtCore.Qt.AlignmentFlag(model.headerData(cols[cid], _H, _ALIGN))

    assert flag("job_title") & QtCore.Qt.AlignmentFlag.AlignLeft
    assert flag("company_name") & QtCore.Qt.AlignmentFlag.AlignLeft
    assert flag("url") & QtCore.Qt.AlignmentFlag.AlignLeft
    assert flag("applicants") & QtCore.Qt.AlignmentFlag.AlignRight
    assert flag("score") & QtCore.Qt.AlignmentFlag.AlignHCenter
    assert flag("recommendation") & QtCore.Qt.AlignmentFlag.AlignHCenter
    # The display label is untouched by the new role.
    assert model.headerData(cols["job_title"], _H) == "Title"


def _shown(tab: JobsTab, width: int) -> JobsTab:
    """A tab wide enough for the stretch decision to be made against a real
    viewport (it is re-made on every viewport resize)."""
    tab.resize(width, 600)
    tab.show()
    QtWidgets.QApplication.processEvents()
    return tab


def test_title_column_takes_the_stretch_not_the_link_column(qtbot):
    tab = _shown(_high_tab(qtbot), 1400)
    hh = tab.table.horizontalHeader()
    idx = tab.col_ids.index("job_title")
    assert hh.stretchLastSection() is False
    assert hh.sectionResizeMode(idx) == QtWidgets.QHeaderView.ResizeMode.Stretch
    assert (hh.sectionResizeMode(tab.col_ids.index("url"))
            == QtWidgets.QHeaderView.ResizeMode.Interactive)


def test_title_column_keeps_a_floor_once_the_other_columns_stop_fitting(qtbot):
    """A Stretch section has no minimum: on a narrow window (or at 150%) Qt
    squeezed Title down to a bare "…". Below the floor it drops the stretch and
    lets the table scroll instead."""
    # Pin the interface scale: the floor rides it, and a test earlier in the
    # session may have left the app at another one.
    theme.set_scale(QtWidgets.QApplication.instance(), 1.0)
    tab = _shown(_high_tab(qtbot), 1400)
    hh = tab.table.horizontalHeader()
    idx = tab.col_ids.index("job_title")
    # Wide: Title takes the spare width. (Reaching this state is itself the
    # viewport filter at work — the build-time decision is made against a
    # not-yet-laid-out table and starts at the floor.)
    assert hh.sectionResizeMode(idx) == QtWidgets.QHeaderView.ResizeMode.Stretch
    # Squeezed: let the other columns eat the viewport.
    per = tab.table.viewport().width() // (len(tab.col_ids) - 1)
    for i, cid in enumerate(tab.col_ids):
        if cid != "job_title":
            tab.table.setColumnWidth(i, per)
    tab._update_stretch()
    assert hh.sectionResizeMode(idx) == QtWidgets.QHeaderView.ResizeMode.Interactive
    assert hh.sectionSize(idx) == jobs_tab.TITLE_FLOOR_PX


def test_column_widths_follow_the_interface_scale(qtbot):
    """The width constants are @100% but the delegate's text and pills scale, so
    at 150% every column opened a third too narrow ("Don't consi…")."""
    app = QtWidgets.QApplication.instance()
    idx = [c for c, _ in HIGH_SCORE_COLUMNS].index("recommendation")
    base = dict(HIGH_SCORE_COLUMNS)["recommendation"]
    try:
        theme.set_scale(app, 1.5)
        tab = _high_tab(qtbot)
        assert tab.table.columnWidth(idx) == round(base * 1.5)
    finally:
        theme.set_scale(app, 1.0)


def test_the_action_bar_hint_elides_instead_of_being_sliced(qtbot):
    label = ElidedLabel("Ctrl/Shift-click for multiple · Ctrl+A selects all")
    qtbot.addWidget(label)
    label.resize(60, 20)
    label.show()
    QtWidgets.QApplication.processEvents()
    # text() stays the full string (callers and tests read it); the RENDERED
    # string is shortened with an ellipsis rather than clipped mid-glyph.
    assert label.text() == "Ctrl/Shift-click for multiple · Ctrl+A selects all"
    assert label.toolTip() == label.text()
    rendered = QtWidgets.QLabel.text(label)
    assert rendered != label.text()
    assert rendered.endswith("…")


def test_hiding_title_falls_back_to_stretching_the_last_section(qtbot):
    tab = _high_tab(qtbot)
    tab.set_column_hidden("job_title", True)
    assert tab.table.horizontalHeader().stretchLastSection() is True
    tab.set_column_hidden("job_title", False)
    assert tab.table.horizontalHeader().stretchLastSection() is False


def test_tracker_resume_column_is_wide_enough_for_its_label():
    widths = dict(TRACKER_COLUMNS)
    # It used to be 60px and only looked right because it was the stretched
    # last section; with Title stretching instead it must stand on its own.
    assert widths["resume"] >= 80


def test_stats_numbers_are_right_aligned_and_grouped(qtbot):
    tab = StatsTab()
    qtbot.addWidget(tab)
    tab.set_stats(pd.DataFrame([{"timestamp": "2026-07-27T04:00:00",
                                 "input_csv": "run.csv", "prompt_tokens": 118000}]),
                  "summary", "calibration")
    cols = tab.model.columns
    opt = QtWidgets.QStyleOptionViewItem()
    idx = tab.model.index(0, cols.index("prompt_tokens"))
    tab._delegate.initStyleOption(opt, idx)
    assert opt.text == "118,000"
    assert opt.displayAlignment & QtCore.Qt.AlignmentFlag.AlignRight
    # Text columns keep their default left alignment and raw text.
    topt = QtWidgets.QStyleOptionViewItem()
    tab._delegate.initStyleOption(topt, tab.model.index(0, cols.index("input_csv")))
    assert topt.text == "run.csv"
    assert not (topt.displayAlignment & QtCore.Qt.AlignmentFlag.AlignRight)
    assert tab.table.horizontalHeader().stretchLastSection() is False


def test_filter_inputs_carry_accessible_names(qtbot):
    tab = _high_tab(qtbot)
    for widget, name in ((tab.search_col, "Search in column"),
                         (tab.minscore, "Minimum score"),
                         (tab.day, "Day"), (tab.time, "Run time"),
                         (tab.reco, "Recommendation"), (tab.easy, "Easy Apply")):
        assert widget.accessibleName() == name


def test_vm_panel_inputs_carry_accessible_names(qtbot):
    panel = VMPanel()
    qtbot.addWidget(panel)
    assert panel.freq.accessibleName() == "Frequency"
    assert panel.weekday.accessibleName() == "Weekday"
    assert panel.pause_date.accessibleName() == "Pause until date"
    assert panel.pause_time.accessibleName() == "Pause until time"
    assert panel.preview.accessibleName() == "crontab preview"
    assert all(c.accessibleName() for c in panel.time_combos)


def test_scale_slider_is_named_and_the_bar_never_shrinks(qtbot):
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    win = MainWindow(csv_paths=[], registry=reg)
    qtbot.addWidget(win)
    assert win._scale_slider.accessibleName() == "Interface size"
    bar = win._scale_slider.parentWidget()
    assert (bar.sizePolicy().horizontalPolicy()
            == QtWidgets.QSizePolicy.Policy.Fixed)
