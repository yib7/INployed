"""Regression: the watcher must launch the CURRENT dashboard entry point.

The Tkinter `ui.py` was deleted in the Qt port, but `watcher.launch_ui` still
pointed `UI_PATH` at it — so the scheduled-task auto-pop launched a missing file.
It must target `app.py` (the Qt entry point), which exists and accepts csv-path
arguments exactly the way `launch_ui` passes them.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import watcher  # noqa: E402


def test_ui_path_targets_existing_app_entrypoint():
    assert watcher.UI_PATH.name == "app.py"
    assert watcher.UI_PATH.exists()
