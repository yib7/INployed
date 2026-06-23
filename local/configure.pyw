"""Double-click launcher for the standalone configuration window (no console).

Thin wrapper around configure.main(): pythonw discards stderr, so any crash is
written to the same ui_error.log the dashboard launcher uses instead of dying
silently.
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _log_error(exc: BaseException) -> None:
    import os

    appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "linkedin_watcher"
    try:
        appdata.mkdir(parents=True, exist_ok=True)
        with open(appdata / "ui_error.log", "a", encoding="utf-8") as f:
            f.write(f"\n=== configure crash @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            traceback.print_exception(exc, file=f)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        import configure

        sys.exit(configure.main())
    except Exception as e:  # noqa: BLE001 - pythonw discards stderr; log instead
        _log_error(e)
        sys.exit(1)
