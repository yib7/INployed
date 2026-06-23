"""Pure column-visibility + hidden-column config logic (local/jobsdata.py).

The Qt table's column show/hide drives `displaycolumns` via these helpers; the
widget-level toggle is covered in test_qt_jobs.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402


def test_visible_columns_removes_hidden_keeps_order():
    cols = ["score", "deep_score", "job_title", "url"]
    assert jobsdata.visible_columns(cols, {"deep_score", "url"}) == ["score", "job_title"]


def test_visible_columns_never_empty():
    cols = ["score", "job_title"]
    # hiding everything falls back to showing all — a blank table is never useful
    assert jobsdata.visible_columns(cols, set(cols)) == cols


def test_load_hidden_columns_shapes_and_guards(monkeypatch):
    monkeypatch.setattr(jobsdata, "_load_cfg", lambda: {"hidden_columns": {
        "all": ["url", 123], "bad": "notalist"}})
    out = jobsdata.load_hidden_columns()
    assert out == {"all": ["url", "123"]}          # stringified, non-list dropped


def test_load_hidden_columns_missing_key(monkeypatch):
    monkeypatch.setattr(jobsdata, "_load_cfg", lambda: {})
    assert jobsdata.load_hidden_columns() == {}
