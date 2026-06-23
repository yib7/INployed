"""Headless test for local/configure.py — the standalone config window.

Verifies the window builds and hosts the shared config form (so a non-technical
user can configure everything without the dashboard). Skips if Tk is unavailable.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

pytest.importorskip("tkinter")

import config_form  # noqa: E402
import configure  # noqa: E402

# `root` is the session-scoped Tk fixture from conftest.py (one interpreter
# shared across all GUI tests to avoid Windows multi-root flakiness).


def test_build_returns_form_with_expected_fields(root, tmp_path):
    targets = {
        "config": tmp_path / "config.json",
        "search": tmp_path / "search_config.json",
        "scoring": tmp_path / "scoring_config.json",
        "apply": tmp_path / "apply_config.json",
        "env": tmp_path / ".env",
    }
    form = configure.build(root, targets=targets)
    assert isinstance(form, config_form.ConfigForm)
    # secrets, engine, and a path field are all present in one window
    assert "BRIGHT_DATA_API_TOKEN" in form.vars
    assert "gemini_auth" in form.vars
    assert "RESUME_TAILOR_OUTPUT" in form.vars
    assert root.title().lower().startswith("configure")
