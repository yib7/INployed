"""Phase 2 (cycle 40 restyle): JobRowDelegate + the TAG_ROLE plumbing.

The delegate owns ALL cell painting (category tint, selection lines, stripes,
badges, pills) — these tests pin the *data* contracts it relies on, which are
invisible in a pixel diff:

  * TAG_ROLE (UserRole + 2) reaches the delegate THROUGH the sort proxy;
  * a "skip" reco carries the paint-only "skip" tag while the legacy
    `row_tag()` / BackgroundRole contract stays exactly as before (those are
    pinned by test_qt_jobs.py — DisplayRole/BackgroundRole never changed);
  * the jobs/tracker/apply tables actually install the delegate (zebra off);
  * the apply-queue table stamps each item with its raw status under TAG_ROLE
    while DisplayRole keeps the RAW status text (pill labels are paint-time).

Hermetic: offscreen QApplication, tmp queue file, no real user files.
"""
import sys
from pathlib import Path

import pandas as pd
from PySide6 import QtCore, QtWidgets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_queue  # noqa: E402
from qt import theme  # noqa: E402
from qt.apply_queue_panel import COLUMNS as APPLY_COLUMNS  # noqa: E402
from qt.apply_queue_panel import ApplyQueuePanel  # noqa: E402
from qt.delegates import (  # noqa: E402
    STATUS_LABELS,
    STATUS_TAGS,
    TAG_ROLE,
    JobRowDelegate,
)
from qt.jobs_model import SORT_ROLE, JobsTableModel  # noqa: E402
from qt.jobs_tab import JobsTab  # noqa: E402

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


def test_tag_role_is_distinct_from_sort_role(qapp):
    assert TAG_ROLE == QtCore.Qt.ItemDataRole.UserRole + 2
    assert TAG_ROLE != SORT_ROLE


def test_tag_role_forwards_through_the_proxy(qtbot):
    # The delegate reads TAG_ROLE off the PROXY index — the tag must survive
    # the mapToSource hop (QSortFilterProxyModel forwards custom roles).
    tab = JobsTab("high", COLS)
    qtbot.addWidget(tab)
    tab.set_source_df(_df(), resume_ids={"2"})
    # Derive per-row expectations from the source model (filter_and_sort may
    # reorder rows), then read the tag back through the PROXY index.
    by_id = {"1": "apply", "2": "has_resume", "3": "skip"}
    for r in range(tab.proxy.rowCount()):
        src_row = tab.proxy.mapToSource(tab.proxy.index(r, 0)).row()
        expected = by_id[tab.model.job_id(src_row)]
        assert tab.proxy.data(tab.proxy.index(r, 3), TAG_ROLE) == expected


def test_skip_reco_paint_tag_without_touching_legacy_contract(qapp):
    # The neutral "Don't consider" tint rides TAG_ROLE only: row_tag() and
    # BackgroundRole keep the pre-restyle behavior (pinned by test_qt_jobs.py).
    m = JobsTableModel(COL_IDS, mode="high")
    m.set_dataframe(_df())
    assert m.data(m.index(2, 0), TAG_ROLE) == "skip"
    assert m.row_tag(2) == ""                                    # legacy: untinted
    bg = m.data(m.index(2, 0), QtCore.Qt.ItemDataRole.BackgroundRole)
    assert bg is None
    # "skip" resolves to the neutral family for the delegate's tint/stripe.
    assert theme.TAG_SEMANTIC["skip"] == "neutral"


def test_all_mode_rows_carry_no_paint_tag(qapp):
    m = JobsTableModel(COL_IDS, mode="all")
    m.set_dataframe(_df(), resume_ids={"1", "2", "3"})
    assert [m.data(m.index(r, 0), TAG_ROLE) for r in range(3)] == ["", "", ""]


def test_jobs_tab_installs_the_delegate(qtbot):
    tab = JobsTab("high", COLS)
    qtbot.addWidget(tab)
    dlg = tab.table.itemDelegate()
    assert isinstance(dlg, JobRowDelegate)
    assert not tab.table.alternatingRowColors()      # tint layers, not zebra
    assert not tab.table.showGrid()                  # delegate paints separators


def test_url_click_opens_the_job(qtbot):
    opened = []
    tab = JobsTab("high", COLS, on_open_url=opened.append)
    qtbot.addWidget(tab)
    tab.set_source_df(_df())
    url_col = tab.col_ids.index("url")
    row0_jid = tab.model.job_id(tab.proxy.mapToSource(tab.proxy.index(0, 0)).row())
    tab.table.clicked.emit(tab.proxy.index(0, url_col))   # "Open ↗" cell
    assert opened == [row0_jid]
    tab.table.clicked.emit(tab.proxy.index(0, 0))         # any other column: no-op
    assert opened == [row0_jid]


def test_delegate_sizehint_tracks_row_token(qtbot):
    tab = JobsTab("high", COLS)
    qtbot.addWidget(tab)
    tab.set_source_df(_df())
    dlg = tab.table.itemDelegate()
    opt = QtWidgets.QStyleOptionViewItem()
    opt.widget = tab.table
    hint = dlg.sizeHint(opt, tab.proxy.index(0, 0))
    assert hint.height() == round(theme.SIZES["row"] * theme._current_scale)


def test_jobs_and_tracker_tables_paint_offscreen(qtbot):
    # Full-custom paint smoke: grab() renders every visible cell through
    # JobRowDelegate.paint (badges, bars, pills, stripes, selection lines).
    for key in ("high", "tracker", "all"):
        tab = JobsTab(key, COLS)
        qtbot.addWidget(tab)
        tab.set_source_df(_df(), resume_ids={"2"})
        tab.table.selectRow(0)
        pixmap = tab.table.grab()
        assert not pixmap.isNull() and pixmap.width() > 0


def test_status_maps_cover_every_queue_status(qapp):
    assert set(STATUS_TAGS) == set(apply_queue.STATUSES)
    assert all(sem in theme.SEMANTICS for sem in STATUS_TAGS.values())
    assert STATUS_LABELS["needs_human"] == "Needs review"
    assert STATUS_LABELS["in_progress"] == "In progress"
    assert STATUS_LABELS["ready_to_submit"] == "Ready to submit"


def test_apply_panel_items_carry_status_tag_and_raw_display(qtbot, tmp_path):
    qfile = tmp_path / "apply_queue.json"
    apply_queue.enqueue(apply_queue.new_entry(
        "1", company="Acme", title="Analyst", apply_url="https://x/1"), path=qfile)
    apply_queue.finish("1", "needs_human", path=qfile)
    p = ApplyQueuePanel(queue_path=qfile)
    qtbot.addWidget(p)
    assert isinstance(p.table.itemDelegate(), JobRowDelegate)
    assert not p.table.alternatingRowColors()
    status_col = APPLY_COLUMNS.index("Status")
    item = p.table.item(0, status_col)
    assert item.text() == "needs_human"              # DisplayRole stays RAW
    assert item.data(TAG_ROLE) == "needs_human"      # pill label is paint-time
    # every cell in the row is tagged (the tint covers the full row)
    assert all(p.table.item(0, c).data(TAG_ROLE) == "needs_human"
               for c in range(len(APPLY_COLUMNS)))
    pixmap = p.table.grab()                          # paint smoke (apply kind)
    assert not pixmap.isNull()
