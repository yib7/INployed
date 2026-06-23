"""Standalone configuration window (Qt).

Opens the same schema-driven settings form the dashboard's Settings tab uses, but
in its own window — so anyone can set up the pipeline (API keys, your Google Cloud
project, file locations, search terms, scoring, resume) without launching the full
dashboard. It saves to the same files (`.env` + the JSON configs beside it), so the
dashboard and the pipeline pick up changes on their next run.

Run:  python local/configure.py     (or double-click local/configure.pyw)
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    from dotenv import load_dotenv

    load_dotenv(HERE.parent / ".env")
except Exception:
    pass

from PySide6 import QtWidgets  # noqa: E402

from qt.settings_tab import build_config_window  # noqa: E402
from qt.theme import apply_theme  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(argv if argv is not None else sys.argv)
    apply_theme(app)
    win = build_config_window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
