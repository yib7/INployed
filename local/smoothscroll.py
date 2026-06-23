"""Smooth, coalesced mouse-wheel scrolling for the dashboard.

Tk repaints a maximized themed Treeview or a Canvas full of fields in ~40-120 ms
(software rendering; cost grows with on-screen cells/widgets and window size). A
fast wheel flick fires many <MouseWheel> events; handled the default way each one
forces its own synchronous repaint, and because every repaint is slow the events
back up in the OS queue and grind through one-at-a-time — a multi-second freeze
that feels like the machine is choking.

The fix here is to *coalesce*: a burst of wheel events accumulates a scroll
distance and schedules a single deferred `yview_scroll` via `after_idle`. All the
queued events are processed (cheaply, just summing) before that one flush runs, so
the widget repaints at most once per idle cycle no matter how fast the wheel spins.
The backlog is dropped — you jump straight to where the flick would have landed.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable


def _coalescing_handler(widget: tk.Misc, target) -> Callable[[tk.Event], str]:
    """Return a <MouseWheel> callback that batches rapid events into one scroll.

    `widget` schedules the idle flush; `target` is the scrollable (anything with
    `yview_scroll`). Returns "break" so it overrides Tk's per-event default.
    """
    state = {"units": 0, "pending": False}

    def flush() -> None:
        units = state["units"]
        state["units"] = 0
        state["pending"] = False
        if units:
            try:
                target.yview_scroll(units, "units")
            except tk.TclError:
                pass  # widget went away between the flick and the idle flush

    def on_wheel(event: tk.Event) -> str:
        # Windows delivers event.delta in multiples of 120 (one notch = 120).
        # Scroll one line per notch; oversized deltas (some mice) scroll more.
        notches = int(event.delta / 120) or (1 if event.delta > 0 else -1)
        state["units"] -= notches            # wheel up (delta>0) => scroll toward top
        if not state["pending"]:
            state["pending"] = True
            widget.after_idle(flush)
        return "break"

    return on_wheel


def bind_treeview_wheel(tree: tk.Misc) -> None:
    """Give a Treeview coalesced vertical wheel scrolling.

    Binds on the instance and returns "break", so Tk's built-in per-event class
    binding (the source of the repaint pile-up) no longer runs.
    """
    tree.bind("<MouseWheel>", _coalescing_handler(tree, tree))


def bind_canvas_wheel(canvas: tk.Canvas, body: tk.Misc, *, skip_text: bool = True) -> None:
    """Give a scrollable Canvas coalesced wheel scrolling.

    Binds the canvas plus every descendant of `body` to ONE shared accumulator, so
    the wheel works wherever the cursor sits and a burst across several fields still
    collapses into a single scroll. `tk.Text` widgets are skipped so multi-line
    boxes keep scrolling their own contents.
    """
    handler = _coalescing_handler(canvas, canvas)
    canvas.bind("<MouseWheel>", handler)

    def _bind(widget: tk.Misc) -> None:
        if not (skip_text and isinstance(widget, tk.Text)):
            widget.bind("<MouseWheel>", handler)
        for child in widget.winfo_children():
            _bind(child)

    _bind(body)
