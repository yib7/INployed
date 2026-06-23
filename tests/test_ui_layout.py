"""Layout/sizing concerns for the dashboard (Cycle 6 SP1 + SP5).

SP1: fonts are bumped a notch and there's a guarded `maximize_window` helper so
the app opens using the whole monitor instead of a small centered window.
SP5: the score-preview only belongs on the job-list tabs.

These run headless against the shared withdrawn Tk root (a throwaway Toplevel is
used where a real top-level is needed) so they never pop a window.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

pytest.importorskip("tkinter")

import tkinter as tk  # noqa: E402

import ui  # noqa: E402


def test_fonts_are_bumped():
    # Cycle 6 SP1: bigger base fonts so the bigger window isn't full of tiny text.
    assert ui.FONT[1] >= 11
    assert ui.FONT_BOLD[1] >= 11
    assert ui.FONT_SUB[1] >= 12
    assert ui.FONT_TITLE[1] >= 18


def test_maximize_window_does_not_raise(root):
    # Must degrade gracefully on any platform / headless WM (guarded fallbacks).
    top = tk.Toplevel(root)
    top.withdraw()
    try:
        ui.maximize_window(top)  # should never raise
    finally:
        top.destroy()
