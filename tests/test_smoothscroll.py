"""Tests for local/smoothscroll.py — coalesced mouse-wheel scrolling.

The whole point: a burst of N wheel events must collapse into ONE deferred
yview_scroll (so the widget repaints once per idle, not N synchronous repaints
that pile up into a multi-second freeze). These tests drive the handler with a
fake scroll target + fake after_idle so no real rendering or timing is involved.
"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

tk = pytest.importorskip("tkinter")
from tkinter import ttk  # noqa: E402

import smoothscroll  # noqa: E402


class _FakeTarget:
    """Stand-in scrollable: records yview_scroll calls."""
    def __init__(self):
        self.scrolls = []

    def yview_scroll(self, n, what):
        self.scrolls.append((n, what))


class _FakeWidget:
    """Stand-in widget: captures the single after_idle callback and lets the test
    fire it on demand (simulating the event loop reaching idle)."""
    def __init__(self):
        self.idle = []

    def after_idle(self, fn):
        self.idle.append(fn)

    def run_idle(self):
        cbs, self.idle = self.idle, []
        for fn in cbs:
            fn()


def _evt(delta):
    return types.SimpleNamespace(delta=delta)


def test_burst_of_events_coalesces_to_one_scroll():
    w, target = _FakeWidget(), _FakeTarget()
    on_wheel = smoothscroll._coalescing_handler(w, target)
    # 8 wheel events arrive before the loop ever reaches idle.
    for _ in range(8):
        on_wheel(_evt(-120))           # 8 notches "down"
    assert target.scrolls == []        # nothing scrolled yet — deferred
    assert len(w.idle) == 1            # only ONE flush scheduled for the whole burst
    w.run_idle()
    assert target.scrolls == [(8, "units")]   # one scroll of the summed distance


def test_handler_returns_break_to_suppress_default():
    w, target = _FakeWidget(), _FakeTarget()
    on_wheel = smoothscroll._coalescing_handler(w, target)
    assert on_wheel(_evt(120)) == "break"


def test_scroll_direction_matches_wheel():
    w, target = _FakeWidget(), _FakeTarget()
    on_wheel = smoothscroll._coalescing_handler(w, target)
    on_wheel(_evt(120))    # wheel up
    w.run_idle()
    assert target.scrolls == [(-1, "units")]   # up = negative units (toward top)


def test_opposing_events_net_out():
    w, target = _FakeWidget(), _FakeTarget()
    on_wheel = smoothscroll._coalescing_handler(w, target)
    for _ in range(3):
        on_wheel(_evt(-120))   # down 3
    for _ in range(3):
        on_wheel(_evt(120))    # up 3
    w.run_idle()
    assert target.scrolls == []            # net zero -> no scroll, no wasted repaint


def test_second_burst_after_idle_schedules_again():
    w, target = _FakeWidget(), _FakeTarget()
    on_wheel = smoothscroll._coalescing_handler(w, target)
    on_wheel(_evt(-120))
    w.run_idle()
    on_wheel(_evt(-120))
    w.run_idle()
    assert target.scrolls == [(1, "units"), (1, "units")]


def test_bind_treeview_wheel_installs_instance_binding(root):
    tv = ttk.Treeview(root)
    smoothscroll.bind_treeview_wheel(tv)
    assert tv.bind("<MouseWheel>")            # non-empty instance binding


def test_bind_canvas_wheel_binds_canvas_and_descendants(root):
    canvas = tk.Canvas(root)
    body = ttk.Frame(canvas)
    entry = ttk.Entry(body)
    text = tk.Text(body)
    smoothscroll.bind_canvas_wheel(canvas, body)
    assert canvas.bind("<MouseWheel>")
    assert entry.bind("<MouseWheel>")
    assert not text.bind("<MouseWheel>")      # tk.Text scrolls itself (skip_text)
