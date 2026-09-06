"""Hidden-column config load/save (local/jobsdata.py).

Only the persistence half lives here. The visibility logic itself is JobsTab's
(`set_column_hidden` holds the never-hide-everything invariant), so it is covered
in test_qt_jobs against the real widget rather than against a pure helper -- the
pure helper `visible_columns` had no production caller at all and was deleted.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402


def test_load_hidden_columns_shapes_and_guards(monkeypatch):
    monkeypatch.setattr(jobsdata, "_load_cfg", lambda: {"hidden_columns": {
        "all": ["url", 123], "bad": "notalist"}})
    out = jobsdata.load_hidden_columns()
    assert out == {"all": ["url", "123"]}          # stringified, non-list dropped


def test_load_hidden_columns_missing_key(monkeypatch):
    monkeypatch.setattr(jobsdata, "_load_cfg", lambda: {})
    assert jobsdata.load_hidden_columns() == {}
