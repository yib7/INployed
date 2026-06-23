"""Tests for the toggleable table columns (local/ui.py).

Hiding columns is the cheap lever for table scroll cost (~10 ms repaint per
visible column on a maximized window). These verify the pure visibility logic,
the config shaping, that a table's displaycolumns follows the saved hidden set,
and that the chooser popup persists + applies a toggle live.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

tk = pytest.importorskip("tkinter")

import ui  # noqa: E402


def test_visible_columns_removes_hidden_keeps_order():
    cols = ["score", "deep_score", "job_title", "url"]
    assert ui.visible_columns(cols, {"deep_score", "url"}) == ["score", "job_title"]


def test_visible_columns_never_empty():
    cols = ["score", "job_title"]
    # hiding everything falls back to showing all — a blank table is never useful
    assert ui.visible_columns(cols, set(cols)) == cols


def test_load_hidden_columns_shapes_and_guards(monkeypatch):
    monkeypatch.setattr(ui, "_load_cfg", lambda: {"hidden_columns": {
        "all": ["url", 123], "bad": "notalist"}})
    out = ui.load_hidden_columns()
    assert out == {"all": ["url", "123"]}          # stringified, non-list dropped


def test_load_hidden_columns_missing_key(monkeypatch):
    monkeypatch.setattr(ui, "_load_cfg", lambda: {})
    assert ui.load_hidden_columns() == {}


def _bare_app(root):
    """An App instance without the heavy __init__/window — enough to exercise the
    column methods over a real Treeview."""
    app = ui.App.__new__(ui.App)
    app.root = root
    frame = tk.Frame(root)
    tv = ui.make_treeview(frame, ui.ALL_COLUMNS)
    app._table_cols = {"all": ([c for c, _ in ui.ALL_COLUMNS], tv)}
    app.hidden_columns = {}
    return app, tv


def test_apply_column_visibility_sets_displaycolumns(root):
    app, tv = _bare_app(root)
    app.hidden_columns = {"all": ["url", "deep_score"]}
    app._apply_column_visibility("all")
    shown = list(tv.cget("displaycolumns"))
    assert "url" not in shown and "deep_score" not in shown
    assert "job_title" in shown and "score" in shown


def test_apply_all_column_visibility_runs_every_table(root):
    app, tv = _bare_app(root)
    app.hidden_columns = {"all": ["url"]}
    app._apply_all_column_visibility()
    assert "url" not in list(tv.cget("displaycolumns"))


def test_choose_columns_toggle_persists_and_applies(root, monkeypatch):
    app, tv = _bare_app(root)
    saved = {}
    monkeypatch.setattr(ui, "save_hidden_columns", lambda h: saved.update(h))
    app._choose_columns("all")
    # find the popup and its first column Checkbutton, then uncheck it
    win = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
    checks = [w for w in win.winfo_children() if isinstance(w, tk.ttk.Checkbutton)]
    assert checks, "no column checkboxes in the chooser"
    first_col = [c for c, _ in ui.ALL_COLUMNS][0]
    checks[0].invoke()                                   # uncheck the first column
    assert first_col in app.hidden_columns["all"]        # recorded as hidden
    assert first_col in saved["all"]                     # persisted
    assert first_col not in list(tv.cget("displaycolumns"))  # applied live
    win.destroy()
