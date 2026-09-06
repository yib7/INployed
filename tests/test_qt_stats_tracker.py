"""SP5: Stats tab (summary/calibration/export) + Tracker extras (status/follow-up/remove/prep)."""
import os
from datetime import date, timedelta
from unittest.mock import MagicMock

import pandas as pd
from PySide6 import QtWidgets

from qt import main_window as mw
from qt.main_window import MainWindow
from qt.stats_tab import StatsTab


def _fake_registry(status_rows=None):
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = status_rows or []
    return reg


def _win(qtbot, status_rows=None):
    w = MainWindow(csv_paths=[], registry=_fake_registry(status_rows))
    qtbot.addWidget(w)
    return w


def test_stats_tab_set_stats(qtbot):
    tab = StatsTab()
    qtbot.addWidget(tab)
    df = pd.DataFrame([{"timestamp": "t1", "rows_in": 5, "llm_scored": 4}])
    tab.set_stats(df, "1 run logged", "Calibration: none")
    assert tab.model.rowCount() == 1
    assert "1 run logged" in tab.summary.text()
    assert "Calibration" in tab.calibration.text()


def test_stats_freshness_badge_fresh(qtbot):
    tab = StatsTab()
    qtbot.addWidget(tab)
    tab.set_freshness("fresh", 4.0)
    assert "Fresh" in tab.badge.text() and not tab.badge.isHidden()


def test_stats_freshness_badge_stale(qtbot):
    tab = StatsTab()
    qtbot.addWidget(tab)
    tab.set_freshness("stale", 50.0)
    assert "Stale" in tab.badge.text()


def test_refresh_stats_updates_freshness_badge(qtbot, monkeypatch):
    w = _win(qtbot)
    # Hermetic: gdrive_root_dir([]) falls back to the user's real Drive folder, so
    # without this stub the badge reflects whatever run_stats.csv is actually synced
    # there (a real VM run makes it 'fresh'). Pin the test's stated case — no run
    # stats found -> no run -> stale.
    monkeypatch.setattr(mw, "gdrive_root_dir", lambda paths: None)
    captured = {}
    monkeypatch.setattr(w.stats_tab, "set_freshness",
                        lambda state, age: captured.update(state=state, age=age))
    w._refresh_stats()
    assert captured["state"] == "stale"


def test_stats_summary_text(qtbot):
    w = _win(qtbot)
    df = pd.DataFrame([
        {"timestamp": "t1", "rows_in": 10, "llm_scored": 8, "prompt_tokens": 100, "output_tokens": 50},
        {"timestamp": "t2", "rows_in": 6, "llm_scored": 5, "prompt_tokens": 60, "output_tokens": 40},
    ])
    text = w._stats_summary(df)
    assert "2 run(s) logged" in text and "t2" in text


def test_calibration_text_no_labels(qtbot):
    w = _win(qtbot, status_rows=[])
    assert "no labels yet" in w._calibration_text()


def test_tracker_followed_up_calls_registry(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w.tracker_tab, "selected_ids", lambda: ["1"])
    w._tracker_followed_up()
    w.registry.mark_followed_up.assert_called_once_with(["1"])


def test_tracker_remove_confirms_and_clears(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w.tracker_tab, "selected_ids", lambda: ["1", "2"])
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    w._tracker_remove()
    assert w.registry.clear_status.call_count == 2


def test_tracker_prep_runs_worker(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w.tracker_tab, "selected_ids", lambda: ["1"])
    monkeypatch.setattr(w, "_job_payload",
                        lambda jid: {"job_posting_id": "1", "company_name": "A", "job_title": "T"})
    w.registry.resume_path.return_value = None
    ran = {}
    monkeypatch.setattr(mw.workers, "run_async",
                        lambda owner, fn, on_done=None, on_error=None: ran.setdefault("fn", fn))
    w._tracker_prep()
    assert "fn" in ran


def test_due_only_filters_tracker(qtbot):
    old = (date.today() - timedelta(days=99)).isoformat()
    today = date.today().isoformat()
    rows = [
        {"job_posting_id": "1", "status": "applied", "applied_date": old},
        {"job_posting_id": "2", "status": "applied", "applied_date": today},
    ]
    w = _win(qtbot, status_rows=rows)
    w._refresh_tracker()
    assert w.tracker_tab.model.rowCount() == 2     # both shown
    w.tracker_due_only.setChecked(True)            # stateChanged -> _refresh_tracker
    assert w.tracker_tab.model.rowCount() == 1     # only the overdue one


def test_tracker_chip_filters_by_status_and_counts(qtbot):
    # Cycle 40 3d: the pipeline ChipBar filters the recs list (not the proxy)
    # and each chip shows its FULL bucket count regardless of the selection.
    old = (date.today() - timedelta(days=99)).isoformat()
    rows = [
        {"job_posting_id": "1", "status": "applied", "applied_date": old},
        {"job_posting_id": "2", "status": "interviewing", "applied_date": old},
        {"job_posting_id": "3", "status": "offer", "applied_date": old},
    ]
    w = _win(qtbot, status_rows=rows)
    w._refresh_tracker()
    assert w.tracker_tab.model.rowCount() == 3
    assert w.tracker_chips.chip("all").count() == 3
    assert w.tracker_chips.chip("applied").count() == 1
    assert w.tracker_chips.chip("due").count() == 1     # only "applied" goes DUE

    w.tracker_chips.chip("applied").click()             # filter to applied
    assert w.tracker_tab.model.rowCount() == 1
    assert w.tracker_chips.chip("all").count() == 3     # counts stay unfiltered

    w.tracker_chips.chip("all").click()
    assert w.tracker_tab.model.rowCount() == 3


def test_followup_due_chip_proxies_the_popup_checkbox(qtbot):
    # The "Follow-up due" chip PROXIES tracker_due_only — the checkbox stays in
    # the Filters popup (test-coupled) and remains the filter's source of truth.
    old = (date.today() - timedelta(days=99)).isoformat()
    today = date.today().isoformat()
    rows = [
        {"job_posting_id": "1", "status": "applied", "applied_date": old},
        {"job_posting_id": "2", "status": "applied", "applied_date": today},
    ]
    w = _win(qtbot, status_rows=rows)
    w._refresh_tracker()
    w.tracker_chips.chip("due").click()
    assert w.tracker_due_only.isChecked()               # chip drove the checkbox
    assert w.tracker_tab.model.rowCount() == 1
    w.tracker_chips.chip("all").click()
    assert not w.tracker_due_only.isChecked()           # cleared on leaving "due"
    assert w.tracker_tab.model.rowCount() == 2
    # and the checkbox drives the chip back (popup direction)
    w.tracker_due_only.setChecked(True)
    assert w.tracker_chips.checked_key() == "due"
    assert w.tracker_tab.model.rowCount() == 1


def test_apply_auth_env_sets_var(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(mw.jobsdata, "_load_cfg", lambda: {"gemini_auth": "api_key"})
    # _apply_auth_env writes the REAL os.environ (that is its job — the tailor
    # reads the var at call time). conftest's _restore_environ fixture puts the
    # environment back afterwards so this does not leak into later tests; the
    # setenv here just pins the pre-state explicitly for the reader.
    monkeypatch.setenv("RESUME_TAILOR_GEMINI_AUTH", "")
    w._apply_auth_env()
    assert os.environ["RESUME_TAILOR_GEMINI_AUTH"] == "api_key"


# ---- 3.13: the degenerate run_stats frames the Stats tab actually receives ----
#
# run_stats.csv is written by the VM, column-union-concatenated by merge_incoming
# and then synced through Drive, so the frame that reaches this tab is not always
# the tidy one the happy-path tests build. Every shape below has a documented
# cause in this repo: the header self-heal in score_jobs.append_run_stats drops
# and re-adds columns, that concat upcasts int64 to float64 so an integer counter
# arrives spelled "1.0", keep_default_na=False turns a missing cell into "", and
# _load_frames deliberately hands over None when the file failed to parse.


def test_stats_tab_survives_a_none_frame(qtbot):
    w = _win(qtbot)
    w._stats_df = None
    w._refresh_stats()
    assert "not synced yet" in w.stats_tab.summary.text()
    assert "never" in w.stats_tab.badge.text()


def test_stats_tab_survives_an_empty_frame(qtbot):
    w = _win(qtbot)
    w._stats_df = pd.DataFrame()
    w._refresh_stats()
    assert "not synced yet" in w.stats_tab.summary.text()


def test_stats_summary_tolerates_string_typed_numbers_and_blanks(qtbot):
    """"1.0" and "" are what the column-union concat + keep_default_na=False emit."""
    w = _win(qtbot)
    w._stats_df = pd.DataFrame([
        {"timestamp": "2026-09-01T10:00:00", "rows_in": "12.0",
         "llm_scored": "9.0", "prompt_tokens": "1000", "output_tokens": ""},
        {"timestamp": "2026-09-02T10:00:00", "rows_in": "",
         "llm_scored": "3", "prompt_tokens": "not a number", "output_tokens": "500"},
    ])
    w._refresh_stats()
    text = w.stats_tab.summary.text()
    assert "2 run(s) logged" in text
    assert "7-run avg" in text          # the averages computed rather than raising


def test_stats_summary_tolerates_missing_columns(qtbot):
    """The header self-heal can produce a frame with no token columns at all."""
    w = _win(qtbot)
    w._stats_df = pd.DataFrame([{"timestamp": "2026-09-01T10:00:00"}])
    w._refresh_stats()
    assert "1 run(s) logged" in w.stats_tab.summary.text()
    assert "0 tokens/run" in w.stats_tab.summary.text()


def test_a_malformed_timestamp_reads_as_never_not_as_a_crash(qtbot):
    w = _win(qtbot)
    w._stats_df = pd.DataFrame([{"timestamp": "yesterday-ish", "rows_in": 1}])
    w._refresh_stats()
    assert "never" in w.stats_tab.badge.text()
    assert "Stale" in w.stats_tab.badge.text()


def test_calibration_counts_a_tracked_job_that_is_no_longer_in_the_master(qtbot):
    """A job deleted (or aged out of the master) still has a status row.

    The row is the user's own label and outlives the posting, so the calibration
    line must place it somewhere rather than fail looking the job up.
    """
    w = _win(qtbot, status_rows=[{"job_posting_id": "gone-1", "status": "applied"}])
    w.df = pd.DataFrame()
    w._row_by_id = {}
    text = w._calibration_text()
    assert "1 labeled application(s)" in text
    assert "unscored: 1" in text
