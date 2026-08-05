"""SP2: the Qt shell builds with its full tab set, a dark theme, and a
single-instance lock. (Cycle 33 SP3 grew the set to eight — Auto-apply.)

The bottom block covers the window's outer vertical splitter: opening the
detail card's description grows the detail pane to ~half the splitter, and
closing it hands the height back.
"""
from unittest.mock import MagicMock

from PySide6 import QtGui, QtWidgets

import app as qt_app
from jobsdata import _UILock
from qt import theme
from qt.main_window import TAB_TITLES, MainWindow


def test_eight_tabs_with_titles(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    assert w.tab_count() == 8      # cycle 33 SP3 added the Auto-apply tab
    assert w.tab_titles() == TAB_TITLES
    assert "Auto-apply" in TAB_TITLES


def test_theme_is_dark(qapp):
    theme.apply_theme(qapp)
    win_color = qapp.palette().color(QtGui.QPalette.ColorRole.Window)
    assert win_color.lightnessF() < 0.3  # window background is near-black


def test_build_app_applies_theme(qapp):
    # build_app applies the theme: a non-empty stylesheet and the dark palette.
    app = qt_app.build_app([])
    assert app.styleSheet().strip()
    assert app.palette().color(QtGui.QPalette.ColorRole.Window).lightnessF() < 0.3


def test_single_instance_lock(tmp_path):
    p = tmp_path / "ui.lock"
    first, second = _UILock(p), _UILock(p)
    assert first.acquire() is True
    assert second.acquire() is False   # a second instance is blocked
    first.release()
    assert second.acquire() is True     # released -> the next instance can take it
    second.release()


def test_main_exits_silently_when_lock_already_held(monkeypatch):
    # P2-2: a second instance must exit(0) quietly -- the live instance's own
    # FS-watcher/poll already picks up new files, so a modal here only interrupts
    # the user for no reason.
    monkeypatch.setattr(qt_app._UILock, "acquire", lambda self: False)

    def _boom(*a, **k):
        raise AssertionError("second instance must not show a modal dialog")

    monkeypatch.setattr(QtWidgets.QMessageBox, "information", _boom)
    assert qt_app.main([]) == 0


# ---- the detail pane grows for the description, then gives the height back ----

_JD = "\n".join(f"Requirement {i} spelled out in full." for i in range(120))


def _flush():
    """Let Qt run the pending layout pass — a QSplitter hands out no real sizes
    until the window has actually been laid out."""
    QtWidgets.QApplication.processEvents()


def _window(qtbot, w=1400, h=1000):
    """A realized MainWindow with a mock registry, sized so the splitter has
    real height to hand out. qtbot owns it, so conftest's widget drain still
    collects it."""
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    win = MainWindow(csv_paths=[], registry=reg)
    qtbot.addWidget(win)
    win.resize(w, h)
    win.show()
    _flush()
    return win


def _select_a_job(win):
    """Render a job with a description into the card, without any data load."""
    win.preview.set_fields({"title": "T", "company": "C", "jd": _JD}, jid="1")
    _flush()


def test_expanding_the_description_grows_the_detail_pane(qtbot):
    win = _window(qtbot)
    _select_a_job(win)
    before = win.splitter.sizes()
    total = sum(before)
    assert before[1] < total // 2          # precondition: the card starts small

    win.preview.desc_toggle.setChecked(True)
    _flush()

    after = win.splitter.sizes()
    assert sum(after) == total             # the split moved, the budget did not
    assert after[1] > before[1]
    assert abs(after[1] - total // 2) <= 2  # ~half the splitter


def test_collapsing_restores_the_sizes_from_before_the_expand(qtbot):
    win = _window(qtbot)
    _select_a_job(win)
    before = win.splitter.sizes()

    win.preview.desc_toggle.setChecked(True)
    _flush()
    assert win.splitter.sizes() != before

    win.preview.desc_toggle.setChecked(False)
    _flush()
    assert win.splitter.sizes() == before


def test_the_collapse_restores_a_dragged_size_not_the_constructor_default(qtbot):
    win = _window(qtbot)
    _select_a_job(win)
    before = win.splitter.sizes()
    # A real drag: moveSplitter is what QSplitterHandle calls as the mouse moves.
    win.splitter.moveSplitter(before[0] - 120, 1)
    _flush()
    dragged = win.splitter.sizes()
    assert dragged != before                # the drag took, and it isn't [640, 300]

    win.preview.desc_toggle.setChecked(True)
    _flush()
    win.preview.desc_toggle.setChecked(False)
    _flush()
    assert win.splitter.sizes() == dragged


def test_expanding_never_shrinks_a_pane_that_is_already_past_half(qtbot):
    win = _window(qtbot)
    _select_a_job(win)
    total = sum(win.splitter.sizes())
    win.splitter.setSizes([total // 3, total - total // 3])
    _flush()
    big = win.splitter.sizes()
    assert big[1] > total // 2             # precondition: already past the target

    win.preview.desc_toggle.setChecked(True)
    _flush()
    assert win.splitter.sizes() == big     # left alone, not pulled back to half

    # ...and the collapse still restores what was recorded on the way in.
    win.preview.desc_toggle.setChecked(False)
    _flush()
    assert win.splitter.sizes() == big


def test_a_drag_while_expanded_survives_the_collapse(qtbot):
    # The user's newest word on the pane's height wins over the pre-expand
    # memory: the collapse must not yank back a size they just set by hand.
    win = _window(qtbot)
    _select_a_job(win)
    win.preview.desc_toggle.setChecked(True)
    _flush()
    win.splitter.moveSplitter(win.splitter.sizes()[0] - 90, 1)
    _flush()
    dragged = win.splitter.sizes()

    win.preview.desc_toggle.setChecked(False)
    _flush()
    assert win.splitter.sizes() == dragged


def test_expanding_does_not_grow_the_pane_the_apply_panel_hid(qtbot):
    win = _window(qtbot)
    _select_a_job(win)
    normal = win.splitter.sizes()
    win._apply_panel_open = True
    win._apply_preview_visibility()        # the panel's own hide path
    _flush()
    assert not win._preview_shown
    hidden = win.splitter.sizes()
    assert hidden[1] == 0                  # a hidden pane reports no height

    win.preview.desc_toggle.setChecked(True)
    _flush()
    assert win.splitter.sizes() == hidden   # nothing grown behind the panel

    win._apply_panel_open = False
    win._apply_preview_visibility()         # the panel closes, the pane returns
    _flush()
    assert win.splitter.sizes() == normal

    # The expand that happened behind the panel must not have recorded the
    # hidden [n, 0] as the sizes to hand back — that would collapse the pane.
    win.preview.desc_toggle.setChecked(False)
    _flush()
    assert win.splitter.sizes() == normal


def test_a_detour_through_a_non_preview_tab_keeps_the_restore_memory(qtbot):
    # Settings has no preview: the pane hides and the card is cleared, so the
    # collapse arrives while hidden. The height still has to come back.
    win = _window(qtbot)
    _select_a_job(win)
    before = win.splitter.sizes()
    win.preview.desc_toggle.setChecked(True)
    _flush()
    assert win.splitter.sizes() != before

    win.tabs.setCurrentIndex(TAB_TITLES.index("Settings"))
    _flush()
    assert not win._preview_shown
    win.tabs.setCurrentIndex(TAB_TITLES.index("All Jobs"))
    _select_a_job(win)                     # the tab switch re-renders the row
    _flush()

    win.preview.desc_toggle.setChecked(False)
    _flush()
    assert win.splitter.sizes() == before   # not stuck at half for the session


def test_switching_to_the_tracker_hands_the_height_back(qtbot):
    # The Tracker card carries no JD, so the split closes on the switch — and
    # the pane it grew has to shrink back with it.
    win = _window(qtbot)
    _select_a_job(win)
    before = win.splitter.sizes()
    win.preview.desc_toggle.setChecked(True)
    _flush()
    assert win.splitter.sizes() != before

    win.tabs.setCurrentIndex(TAB_TITLES.index("Tracker"))
    _flush()
    assert win._preview_shown              # the Tracker does show the card
    assert win.splitter.sizes() == before


def test_a_repeated_expand_signal_keeps_the_pre_expand_sizes(qtbot):
    # The card only emits on real transitions today, but the slot fires from
    # inside set_fields/set_empty and its contract is to be idempotent: a repeat
    # must not re-record the ALREADY GROWN sizes as the ones to restore.
    win = _window(qtbot)
    _select_a_job(win)
    before = win.splitter.sizes()
    win.preview.desc_toggle.setChecked(True)
    _flush()
    grown = win.splitter.sizes()
    assert grown != before

    win.preview.descriptionToggled.emit(True)
    _flush()
    assert win.splitter.sizes() == grown
    win.preview.descriptionToggled.emit(False)
    _flush()
    assert win.splitter.sizes() == before


def test_rendering_a_job_while_expanded_does_not_re_enter_the_resize(qtbot,
                                                                     monkeypatch):
    # descriptionToggled fires from inside set_fields/set_empty, so the slot has
    # to be inert when the state did not actually change. This is the same
    # idempotence claim as the test above, driven through the real emitter.
    win = _window(qtbot)
    _select_a_job(win)
    before = win.splitter.sizes()
    win.preview.desc_toggle.setChecked(True)
    _flush()
    grown = win.splitter.sizes()

    calls = []
    real = win.splitter.setSizes
    monkeypatch.setattr(win.splitter, "setSizes",
                        lambda s: (calls.append(list(s)), real(s))[1])
    for n in (2, 3):
        win.preview.set_fields({"title": f"T{n}", "company": "C",
                                "jd": _JD + f"\nBody {n}."}, jid=str(n))
        _flush()
    assert calls == []                      # sticky expand is not a transition
    assert win.splitter.sizes() == grown
    monkeypatch.undo()

    # and the collapse still knows the pre-expand sizes after all that
    win.preview.desc_toggle.setChecked(False)
    _flush()
    assert win.splitter.sizes() == before
