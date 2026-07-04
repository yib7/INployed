"""Startup must never freeze on a slow data source.

The dashboard's source files can live on Google Drive File Stream, where a cold
mount makes even a directory read block for minutes. Loading them on the UI
thread during window construction meant the window never painted -- Windows
flagged it "Not Responding" and nothing appeared (the "app won't open" bug).

The fix: build + show the window first, then load off the UI thread via
qt.workers.run_async and apply the result back on the UI thread. These pin that
contract: construction does not load, reload_data_async does (through the worker),
concurrent loads coalesce, and a load failure is reported instead of crashing.
"""
import gzip
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from qt import main_window as mw  # noqa: E402
from qt.main_window import MainWindow  # noqa: E402


def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    reg.all_ids.return_value = set()
    return reg


def _master(tmp_path, ids):
    """A valid, fully-dated master, so load_files returns rows and never scans."""
    p = tmp_path / "linkedin_jobs_master.csv.gz"
    df = pd.DataFrame({"job_posting_id": ids, "job_title": ids,
                       "score": ["5"] * len(ids),
                       "extracted_date": ["2026-07-01"] * len(ids)})
    with gzip.open(p, "wt", encoding="utf-8", newline="") as fh:
        df.to_csv(fh, index=False)
    return p


def _sync_run_async(owner, fn, on_done=None, on_error=None):
    """Stand-in for qt.workers.run_async that runs inline on the calling thread."""
    on_done(fn())


def test_construction_does_not_load_synchronously(qtbot, tmp_path):
    p = _master(tmp_path, ["1", "2"])
    w = MainWindow(csv_paths=[p], registry=_fake_registry())
    qtbot.addWidget(w)
    # Load is deferred so the window can paint before touching a slow source.
    assert w.df.empty


def test_reload_data_async_populates_off_thread(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(mw.workers, "run_async", _sync_run_async)
    p = _master(tmp_path, ["1", "2"])
    w = MainWindow(csv_paths=[p], registry=_fake_registry())
    qtbot.addWidget(w)

    w.reload_data_async()

    assert set(w.df["job_posting_id"]) == {"1", "2"}


def test_start_kicks_off_the_load(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(mw.workers, "run_async", _sync_run_async)
    p = _master(tmp_path, ["1"])
    w = MainWindow(csv_paths=[p], registry=_fake_registry())
    qtbot.addWidget(w)

    w.start()

    assert not w.df.empty


def test_concurrent_async_loads_coalesce(qtbot, tmp_path, monkeypatch):
    started = []

    def hold(owner, fn, on_done=None, on_error=None):
        started.append(fn)  # simulate an in-flight worker: capture, never complete

    monkeypatch.setattr(mw.workers, "run_async", hold)
    p = _master(tmp_path, ["1"])
    w = MainWindow(csv_paths=[p], registry=_fake_registry())
    qtbot.addWidget(w)

    w.reload_data_async()   # starts the (held) worker
    w.reload_data_async()   # must NOT start a second while one is in flight

    assert len(started) == 1
    assert w._reload_pending is True


def test_reload_data_async_reports_errors_without_crashing(qtbot, tmp_path, monkeypatch):
    def boom(owner, fn, on_done=None, on_error=None):
        on_error(RuntimeError("drive unavailable"))

    monkeypatch.setattr(mw.workers, "run_async", boom)
    p = _master(tmp_path, ["1"])
    w = MainWindow(csv_paths=[p], registry=_fake_registry())
    qtbot.addWidget(w)

    w.reload_data_async()   # must not raise

    assert w._loading is False
