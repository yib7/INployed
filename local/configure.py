"""Standalone configuration window.

Opens the same schema-driven config form the dashboard's Settings tab uses, but
in its own window — so anyone can set up the pipeline (API keys, your Google
Cloud project, file locations, search terms, scoring, resume, and apply answers)
without launching the full dashboard. It saves to the same files (`.env` + the
JSON configs next to it), so the dashboard and the pipeline pick up changes on
their next run.

Run:  python local/configure.py     (or double-click local/configure.pyw)
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from config_form import ConfigForm  # noqa: E402  (needs HERE on sys.path)

_INTRO = ("Set everything up in one place. Your entries are saved to a private "
          ".env file and the config files beside it — the dashboard and the "
          "scraper read them on their next run. Hover help explains each field.")


def build(root: tk.Tk, targets: dict | None = None) -> ConfigForm:
    """Title the window, apply the dark theme, add a header, and mount the form."""
    root.title("Configure - INployed")
    try:
        root.geometry("940x860")
        root.minsize(760, 560)
    except tk.TclError:
        pass
    # The theme is cosmetic; importing the dashboard just to reuse its palette is
    # best-effort so the window still works if that import ever fails.
    try:
        import ui

        ui.apply_theme(root)
    except Exception:  # noqa: BLE001
        pass

    header = ttk.Frame(root, padding=(16, 14, 16, 0))
    header.pack(side="top", fill="x")
    ttk.Label(header, text="Configuration", style="Title.TLabel").pack(anchor="w")
    ttk.Label(header, text=_INTRO, style="Muted.TLabel", wraplength=720).pack(
        anchor="w", pady=(2, 0))

    return ConfigForm(root, targets=targets)


def main() -> int:
    try:
        import ui

        ui._enable_dpi_awareness()  # crisp text on scaled displays; before Tk()
    except Exception:  # noqa: BLE001 - best-effort; window still works without it
        pass
    root = tk.Tk()
    build(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
