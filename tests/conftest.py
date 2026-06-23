"""Shared pytest fixtures.

`root` is a single, **session-scoped** Tk interpreter shared by every GUI test.
Creating more than one Tk() per process is flaky on Windows (intermittent
TclError on the 2nd+ root), so all GUI tests reuse this one. It's withdrawn
(never shown) and destroyed at the end of the session.
"""
import pytest

try:
    import tkinter as tk
except ImportError:  # pragma: no cover - tkinter missing
    tk = None


@pytest.fixture(scope="session")
def root():
    if tk is None:
        pytest.skip("tkinter not available")
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass
