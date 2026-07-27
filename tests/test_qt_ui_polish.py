"""Phase 7 (ship cycle 6) UI polish: header/cell alignment, Title stretch, and
the accessible names screen readers need on inputs without a QFormLayout label.
"""
from unittest.mock import MagicMock

import pandas as pd
from PySide6 import QtCore, QtWidgets

from jobsdata import HIGH_SCORE_COLUMNS, TRACKER_COLUMNS
from qt.jobs_model import JobsTableModel
from qt.jobs_tab import JobsTab
from qt.main_window import MainWindow
from qt.stats_tab import StatsTab
from qt.vm_panel import VMPanel

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


def test_title_column_takes_the_stretch_not_the_link_column(qtbot):
    tab = _high_tab(qtbot)
    hh = tab.table.horizontalHeader()
    idx = tab.col_ids.index("job_title")
    assert hh.stretchLastSection() is False
    assert hh.sectionResizeMode(idx) == QtWidgets.QHeaderView.ResizeMode.Stretch
    assert (hh.sectionResizeMode(tab.col_ids.index("url"))
            == QtWidgets.QHeaderView.ResizeMode.Interactive)


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
