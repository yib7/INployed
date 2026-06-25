"""INployed dashboard entry point (PySide6 / Qt).

Run:  python local/app.py [<scored.csv[.gz]> ...]

Builds the QApplication, applies the dark theme, takes the single-instance lock,
and shows the maximized main window. The pipeline/backend logic lives in the
toolkit-agnostic modules; this file only wires the UI together.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Load scrape_data/.env so LINKEDIN_CHROME_ACCOUNT and other local values are
# populated before chrome/settings read them. The VM never runs the UI, so a
# missing python-dotenv is harmless. Mirrors scraper.py / the old ui.py.
try:
    from dotenv import load_dotenv

    load_dotenv(HERE.parent / ".env")
except Exception:
    pass

from PySide6 import QtWidgets  # noqa: E402

import settings  # noqa: E402
from jobsdata import UI_LOCK, _UILock  # noqa: E402
from qt import wheelguard  # noqa: E402
from qt.main_window import MainWindow  # noqa: E402
from qt.theme import apply_theme  # noqa: E402


def _startup_scale() -> float:
    """The saved interface scale as a factor (ui_scale_pct/100), defaulting to 1.0."""
    try:
        pct = float(settings.load().get("ui_scale_pct", 100) or 100)
    except (TypeError, ValueError):
        pct = 100.0
    return pct / 100.0


def build_app(argv: list[str] | None = None) -> QtWidgets.QApplication:
    """Return the QApplication (reusing an existing one in tests) with the theme."""
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(argv if argv is not None else sys.argv)
    apply_theme(app, scale=_startup_scale())
    wheelguard.install(app)  # wheel-over must not silently edit combos/spins/sliders
    return app


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    csv_paths = [Path(a) for a in argv[1:] if not a.startswith("-")]
    app = build_app(argv)

    lock = _UILock(UI_LOCK)
    if not lock.acquire():
        QtWidgets.QMessageBox.information(
            None, "INployed", "The dashboard is already running.")
        return 0
    try:
        win = MainWindow(csv_paths)
        win.showMaximized()
        return app.exec()
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
