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


def test_apply_auth_env_sets_var(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(mw.jobsdata, "_load_cfg", lambda: {"gemini_auth": "api_key"})
    w._apply_auth_env()
    assert os.environ["RESUME_TAILOR_GEMINI_AUTH"] == "api_key"
