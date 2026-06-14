"""On-demand launcher for the triage dashboard — the desktop-shortcut target.

Double-click any time. It resolves the synced LinkedInJobs master (or the
latest morning/evening run files as a fallback) exactly the way the watcher
does — so it survives a Drive drive-letter change — then opens ui.py.

ui.py self-deduplicates: if a dashboard is already open (e.g. one the watcher
popped), this just tells that window to reload instead of opening a second one.
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _resolve_sources() -> tuple[list[Path], str | None]:
    """(csv paths to open, error message or None). Reuses the watcher's config
    + Drive auto-detection so both entry points agree on where the data lives."""
    from watcher import (  # imported lazily so a bad import still hits the logger
        detect_gdrive_root,
        latest_for_ui,
        list_target_files,
        load_config,
    )

    cfg = load_config()
    root = cfg.get("gdrive_root") or detect_gdrive_root()
    if not root:
        return [], (
            "Could not find the LinkedInJobs folder.\n\n"
            "Make sure Google Drive is running and synced, or set 'gdrive_root' in:\n"
            f"{HERE / 'config.json'}"
        )
    root = Path(root)
    master = root / "linkedin_jobs_master.csv.gz"
    if master.exists():
        return [master], None
    # Master hasn't synced yet — fall back to the latest per-run files.
    fallback = latest_for_ui(list_target_files(root))
    if fallback:
        return fallback, None
    # Folder exists but nothing has synced — open the master path anyway so the
    # window appears (Refresh will pick the file up once Drive delivers it).
    return [master], None


def _log_error(exc: BaseException) -> None:
    import os

    appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "linkedin_watcher"
    try:
        appdata.mkdir(parents=True, exist_ok=True)
        with open(appdata / "ui_error.log", "a", encoding="utf-8") as f:
            f.write(f"\n=== open_dashboard crash @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            traceback.print_exception(exc, file=f)
    except OSError:
        pass


def _warn(message: str) -> None:
    """Pop a small message box (pythonw has no console to print to)."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("LinkedIn Jobs Dashboard", message)
        root.destroy()
    except Exception:  # noqa: BLE001 - last resort, fall back to the log
        pass


def main() -> int:
    sources, err = _resolve_sources()
    if err:
        _warn(err)
        return 1
    # Run the dashboard in-process: this launcher process *becomes* the UI, so
    # closing the window cleanly ends it. ui.main() owns the single-instance
    # lock + reload-flag handshake.
    import ui

    sys.argv = [str(HERE / "ui.py")] + [str(p) for p in sources]
    return ui.main()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - pythonw discards stderr; log instead
        _log_error(e)
        _warn(f"The dashboard failed to open:\n\n{e}\n\nSee ui_error.log for details.")
        sys.exit(1)
