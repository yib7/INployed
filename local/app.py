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

import jobsdata  # noqa: E402
from jobsdata import UI_LOCK, _UILock, load_ui_scale_pct  # noqa: E402
from qt import wheelguard  # noqa: E402
from qt.main_window import MainWindow  # noqa: E402
from qt.theme import apply_theme  # noqa: E402


def _with_local_runs(csv_paths: list[Path]) -> list[Path]:
    """Sources plus any repo-root local scrape/manual files not already present.

    This is the ONE owner of the local-runs fold: watcher launches pass only the
    Drive master as argv, so without this a watcher-popped window can never show a
    local 'Find new jobs' or manual add. Best-effort — a data-dir hiccup must not
    stop the dashboard from opening."""
    out = list(csv_paths)
    try:
        for p in jobsdata.local_run_files():
            if p not in out:
                out.append(p)
    except Exception:  # noqa: BLE001 - opening the window matters more
        pass
    return out


def _startup_scale() -> float:
    """The saved interface scale as a factor (ui_scale_pct/100), defaulting to 1.0."""
    return load_ui_scale_pct() / 100.0


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
    csv_paths = _with_local_runs([Path(a) for a in argv[1:] if not a.startswith("-")])
    app = build_app(argv)

    lock = _UILock(UI_LOCK)
    if not lock.acquire():
        # Exit silently (no modal) -- the already-running instance's own
        # FS-watcher/poll picks up any new files this relaunch would have
        # opened, so interrupting the user here only annoys them.
        return 0
    win = None
    rc = 0
    try:
        win = MainWindow(csv_paths)
        win.showMaximized()
        win.start()  # load data AFTER the window paints, off the UI thread
        rc = app.exec()
    finally:
        lock.release()
    # The Restart button asks for a relaunch: the lock is now released, so a fresh
    # process can take it. Spawn it detached and let this one exit.
    if win is not None and getattr(win, "_restart_requested", False):
        import subprocess
        subprocess.Popen([sys.executable, *sys.argv])
        return 0
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
