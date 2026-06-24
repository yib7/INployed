"""Headless smoke test for the Qt dashboard — builds the main window offscreen.

Run:  QT_QPA_PLATFORM=offscreen python tests/smoke_qt.py

Exits 0 and prints 'SMOKE TEST OK' when the seven-tab window constructs cleanly.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))

from PySide6 import QtWidgets  # noqa: E402

from qt.main_window import TAB_TITLES, MainWindow  # noqa: E402
from qt.theme import apply_theme  # noqa: E402


def main() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    apply_theme(app)
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    win = MainWindow(csv_paths=[], registry=reg)
    assert win.tab_count() == len(TAB_TITLES), win.tab_count()
    assert win.tab_titles() == TAB_TITLES
    print("SMOKE TEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
