"""Tkinter dashboard for triaging LinkedIn job runs.

Usage: ui.py <csv.gz path> [<csv.gz path> ...]

Tab 1 — High Score (Unseen): rows where score >= 4 AND is_seen == "no",
         sorted score desc then fewest applicants first (best apply window).
Tab 2 — All Jobs: every row from the loaded files (deduped on job_posting_id).
Tab 3 — Tracker: applications (applied / interviewing / rejected / offer),
         follow-up nudges, links to each job's tailored-resume folder.
Tab 4 — Stats: per-run pipeline metrics (run_stats.csv synced from the VM)
         plus the applied-vs-recommendation calibration readout.

Double-click any row to open its LinkedIn URL. Right-click for the context
menu (open / mark seen / mark applied / resume folder / block company).
Selecting a row fills the details pane (reason, strengths, gaps, salary, JD).
"Mark seen" records ids in the local SQLite registry and rewrites the source
.csv.gz files in place.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections import Counter
from datetime import date
from functools import lru_cache
from pathlib import Path
from tkinter import messagebox, ttk

import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Load scrape_data/.env so LINKEDIN_CHROME_ACCOUNT (and other local secrets) are
# populated even when the dashboard is launched directly. The VM never runs the
# UI, so a missing python-dotenv is harmless. Mirrors scraper.py.
try:
    from dotenv import load_dotenv

    load_dotenv(HERE.parent / ".env")  # HERE is local/, so .parent is scrape_data/
except Exception:
    pass

from jsonutil import atomic_write_json  # noqa: E402  (needs HERE on sys.path)

from csv_io import read_csv_gz, reconcile_is_seen, write_csv_gz_atomic  # noqa: E402
from seen_db import APP_STATUSES, SeenRegistry  # noqa: E402


APPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "linkedin_watcher"
APPDATA.mkdir(parents=True, exist_ok=True)
RELOAD_FLAG = APPDATA / "reload.flag"
UI_LOCK = APPDATA / "ui.lock"

# Open job links in Chrome under this Google account's profile (falls back to the
# default browser if Chrome or the profile can't be resolved).
CHROME_ACCOUNT = os.environ.get("LINKEDIN_CHROME_ACCOUNT", "")


def _find_chrome() -> str | None:
    """Locate chrome.exe via the usual install dirs, then the registry App Paths."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    try:
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                    root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
                ) as k:
                    path, _ = winreg.QueryValueEx(k, None)
                    if path and Path(path).is_file():
                        return path
            except OSError:
                continue
    except Exception:
        pass
    return None


def _chrome_profile_dir(account: str) -> str:
    """Profile directory whose signed-in account matches `account` (default 'Default').

    Matches user_name, then gaia_name, then the email local-part, so a profile that
    stores the address differently still resolves. An empty `account` short-circuits
    to 'Default' so it never matches a blank-user_name (signed-out) profile. Prints a
    warning when a non-empty account finds no match, so a silent fallback is visible.
    """
    if not account:
        return "Default"
    want = account.lower()
    want_local = want.split("@", 1)[0]
    local_state = Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data/Local State"
    try:
        info = json.loads(local_state.read_text(encoding="utf-8")).get("profile", {}).get("info_cache", {})
    except (OSError, ValueError):
        return "Default"
    for directory, meta in info.items():
        user_name = (meta.get("user_name") or "").lower()
        gaia_name = (meta.get("gaia_name") or "").lower()
        if user_name == want or gaia_name == want or (user_name and user_name.split("@", 1)[0] == want_local):
            return directory
    print(f"[chrome] no Chrome profile matched {account!r}; using Default profile")
    return "Default"


@lru_cache(maxsize=1)
def _chrome_launcher() -> tuple[str, str] | None:
    """(chrome_exe, profile_dir) for CHROME_ACCOUNT, or None if Chrome isn't found."""
    chrome = _find_chrome()
    if not chrome:
        return None
    return chrome, _chrome_profile_dir(CHROME_ACCOUNT)


def open_in_chrome(url: str) -> None:
    """Open `url` in Chrome under the configured profile; fall back to the default browser.

    With LINKEDIN_CHROME_ACCOUNT resolved, --profile-directory opens the URL in
    that profile's window. A profile cannot be force-switched inside an already-
    running Chrome via CLI; the resolved-Default case is the one that matters here.
    """
    launcher = _chrome_launcher()
    if launcher:
        chrome, profile = launcher
        try:
            subprocess.Popen([chrome, f"--profile-directory={profile}", url])
            return
        except OSError:
            pass
    webbrowser.open(url)

# --------------------------------------------------------------------------- theme (modern dark)
# A cohesive deep-slate palette: a near-black blue base, two surface elevations
# for depth, hairline borders to separate regions, and a single vivid sky accent
# (plus green/amber/red status hues) so the UI reads lively but not noisy.
BG = "#0f131a"          # app background (deepest)
SURFACE = "#181d27"     # panels, tables, details pane
SURFACE_ALT = "#141922" # row stripe (one notch under SURFACE)
ELEV = "#212834"        # raised band: column headings
INPUT_BG = "#1f2632"    # entry / combobox field
BORDER = "#2b3340"      # hairline separators + input borders
TEXT = "#e7ecf3"        # primary text
MUTED = "#8b94a7"       # secondary text
FAINT = "#5b6473"       # tertiary (scrollbar arrows, placeholders)
ACCENT = "#38bdf8"      # primary accent (sky)
ACCENT_HOVER = "#7dd3fc"
ACCENT_DEEP = "#0ea5e9" # pressed accent
ACCENT_INK = "#06222f"  # text on an accent fill
GOOD = "#5ee89a"        # apply / offer
AMBER = "#fcd34d"       # consider / interviewing
DANGER = "#fca5a5"      # gaps / errors
SEL = "#173a4d"         # selected row (teal-tinted)
SEL_TEXT = "#ffffff"
BTN = "#232b38"         # secondary button
BTN_HOVER = "#2c3543"
FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI Semibold", 10)
FONT_TITLE = ("Segoe UI Semibold", 17)
FONT_SUB = ("Segoe UI", 11)
FONT_BADGE = ("Segoe UI Semibold", 9)

# Per-row coloring: recommendation wins; otherwise alternating stripe.
# The tracker tab reuses the palette for application statuses, plus an
# orange "due" tag for follow-ups that are overdue.
TAG_STYLES = {
    "apply":    {"background": "#102a1e", "foreground": GOOD},
    "consider": {"background": "#2a2410", "foreground": AMBER},
    "skip":     {"background": SURFACE,   "foreground": MUTED},
    "even":     {"background": SURFACE,   "foreground": TEXT},
    "odd":      {"background": SURFACE_ALT, "foreground": TEXT},
    "applied":      {"background": SURFACE,   "foreground": TEXT},
    "interviewing": {"background": "#2a2410", "foreground": AMBER},
    "offer":        {"background": "#102a1e", "foreground": GOOD},
    "rejected":     {"background": SURFACE,   "foreground": MUTED},
    "due":          {"background": "#2e1d12", "foreground": "#fdba74"},
}


def _lighten(color: str, amt: int = 13) -> str:
    """Nudge a #rrggbb color lighter by `amt` per channel (used for row hover)."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02x}{:02x}{:02x}".format(*(min(255, c + amt) for c in (r, g, b)))


# A faint "lifted" variant of every row tag, swapped in under the cursor so the
# hovered row glows without losing its status text color. Selection still wins
# (ttk paints the selected state over any tag), so hovering the selected row is
# a visual no-op — exactly right.
HOVER_OF = {tag: f"{tag}_hover" for tag in TAG_STYLES}
TAG_STYLES.update({
    f"{tag}_hover": {"background": _lighten(opts["background"]),
                     "foreground": opts["foreground"]}
    for tag, opts in list(TAG_STYLES.items())
})

# Human-friendly column headings (display only — the underlying column ids that
# every filter/sort/populate path keys on are unchanged). Keeps the grid looking
# polished instead of exposing raw snake_case field names.
COLUMN_LABELS = {
    "score": "Score", "deep_score": "Deep", "recommendation": "Reco",
    "applicants": "Applicants", "is_seen": "Seen", "extracted_date": "Scraped",
    "run_label": "Run", "job_title": "Title", "company_name": "Company",
    "job_location": "Location", "url": "URL", "job_posted_date": "Posted",
    "status": "Status", "status_date": "Updated", "applied_date": "Applied",
    "days": "Days", "follow_up": "Follow-up", "resume": "Resume",
    "timestamp": "When", "input_csv": "Input file", "rows_in": "Rows",
    "filtered_out": "Filtered", "llm_scored": "Scored", "llm_errors": "Errors",
    "stage2_done": "Stage 2", "rescore_attempted": "Rescore try",
    "rescore_scored": "Rescored", "llm_calls": "Calls",
    "prompt_tokens": "In tok", "output_tokens": "Out tok",
}
LABEL_TO_COLUMN = {v: k for k, v in COLUMN_LABELS.items()}


def apply_theme(root: tk.Tk) -> None:
    """Restyle ttk into a modern dark theme. Uses the 'clam' base because the
    default Windows themes (vista/xpnative) ignore Treeview tag colors."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=BG)

    style.configure(".", background=BG, foreground=TEXT, fieldbackground=SURFACE,
                    font=FONT, bordercolor=BORDER, focuscolor=ACCENT)

    # Table: roomier rows, a raised heading band, and a teal selection that wins
    # over the per-row status tint.
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                    foreground=TEXT, rowheight=31, borderwidth=0, relief="flat")
    style.map("Treeview", background=[("selected", SEL)],
              foreground=[("selected", SEL_TEXT)])
    style.configure("Treeview.Heading", background=ELEV, foreground=ACCENT,
                    font=FONT_BOLD, relief="flat", padding=(10, 9), borderwidth=0)
    style.map("Treeview.Heading", background=[("active", "#283141")],
              foreground=[("active", ACCENT_HOVER)])

    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(8, 6, 8, 0))
    style.configure("TNotebook.Tab", background=BG, foreground=MUTED,
                    padding=(18, 9), font=FONT, borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", SURFACE), ("active", ELEV)],
              foreground=[("selected", ACCENT), ("active", TEXT)])

    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=SURFACE)
    style.configure("TLabel", background=BG, foreground=TEXT, font=FONT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT)
    style.configure("Title.TLabel", background=BG, foreground=TEXT, font=FONT_TITLE)
    style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=FONT_SUB)
    # A filled "pill" — square corners, but reads as a count badge.
    style.configure("Badge.TLabel", background=ACCENT, foreground=ACCENT_INK,
                    font=FONT_BADGE, padding=(10, 3))
    style.configure("TSeparator", background=BORDER)

    style.configure("TButton", background=BTN, foreground=TEXT, padding=(13, 7),
                    relief="flat", borderwidth=0, font=FONT)
    style.map("TButton", background=[("active", BTN_HOVER), ("pressed", BTN_HOVER)],
              foreground=[("disabled", FAINT)])
    style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_INK,
                    padding=(13, 7), relief="flat", borderwidth=0, font=FONT_BOLD)
    style.map("Accent.TButton",
              background=[("active", ACCENT_HOVER), ("pressed", ACCENT_DEEP)],
              foreground=[("active", ACCENT_INK)])
    style.configure("Green.TButton", background=GOOD, foreground="#06222f",
                    padding=(13, 7), relief="flat", borderwidth=0, font=FONT_BOLD)
    style.map("Green.TButton",
              background=[("active", "#7df0b4"), ("pressed", "#34d399")],
              foreground=[("active", "#06222f")])

    style.configure("TEntry", fieldbackground=INPUT_BG, foreground=TEXT,
                    insertcolor=ACCENT, borderwidth=1, relief="flat", padding=6,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.map("TEntry", bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])

    style.configure("TCheckbutton", background=BG, foreground=TEXT, font=FONT,
                    focuscolor=BG, padding=2)
    style.map("TCheckbutton",
              background=[("active", BG)],
              foreground=[("active", ACCENT_HOVER)],
              indicatorcolor=[("selected", ACCENT), ("!selected", INPUT_BG)])

    style.configure("TCombobox", fieldbackground=INPUT_BG, background=BTN,
                    foreground=TEXT, arrowcolor=MUTED, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, borderwidth=1,
                    relief="flat", padding=5)
    style.map("TCombobox",
              fieldbackground=[("readonly", INPUT_BG)],
              foreground=[("readonly", TEXT)],
              background=[("active", BTN_HOVER)],
              bordercolor=[("focus", ACCENT), ("hover", ACCENT)],
              lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)],
              selectbackground=[("readonly", INPUT_BG)],
              selectforeground=[("readonly", TEXT)],
              arrowcolor=[("active", ACCENT_HOVER)])
    # The dropdown popup is a classic tk Listbox — style it via the option DB.
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", SEL)
    root.option_add("*TCombobox*Listbox.selectForeground", SEL_TEXT)
    root.option_add("*TCombobox*Listbox.font", FONT)

    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=BTN, troughcolor=BG,
                        bordercolor=BG, arrowcolor=FAINT, relief="flat",
                        borderwidth=0, arrowsize=13)
        style.map(f"{orient}.TScrollbar", background=[("active", BTN_HOVER)])


def _enable_dark_titlebar(root: tk.Tk) -> None:
    """Paint the OS window title bar dark on Windows 10 1809+/11. No-ops elsewhere."""
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20 = newer, 19 = older)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
            )
    except Exception:
        pass


class _UILock:
    """Single-instance guard for the Tkinter window. Same pattern as watcher.py."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self._fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
            self._fh = None

HIGH_SCORE_COLUMNS = [
    ("score", 50),
    ("deep_score", 70),
    ("recommendation", 100),
    ("applicants", 80),
    ("extracted_date", 105),
    ("run_label", 80),
    ("job_title", 250),
    ("company_name", 170),
    ("job_location", 140),
    ("url", 240),
]

ALL_COLUMNS = [
    ("score", 50),
    ("deep_score", 70),
    ("recommendation", 100),
    ("is_seen", 60),
    ("extracted_date", 105),
    ("run_label", 80),
    ("job_title", 240),
    ("company_name", 170),
    ("job_location", 140),
    ("url", 220),
    ("job_posted_date", 120),
]

TRACKER_COLUMNS = [
    ("status", 90),
    ("status_date", 95),
    ("applied_date", 95),
    ("days", 46),
    ("follow_up", 80),
    ("score", 50),
    ("deep_score", 70),
    ("job_title", 240),
    ("company_name", 170),
    ("url", 220),
    ("resume", 60),
]

STATS_COLUMNS = [
    ("timestamp", 145),
    ("input_csv", 225),
    ("rows_in", 65),
    ("filtered_out", 80),
    ("llm_scored", 78),
    ("llm_errors", 75),
    ("stage2_done", 85),
    ("rescore_attempted", 110),
    ("rescore_scored", 100),
    ("llm_calls", 70),
    ("prompt_tokens", 95),
    ("output_tokens", 95),
]


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Which job ids a run file contains never changes (only its is_seen column gets
# rewritten), so cache per path — reload_data fires on every refresh/mark-seen
# and rescanning every historical gz gets slow as runs accumulate.
_RUN_FILE_IDS: dict[str, list[str]] = {}


def extraction_dates_from_runs(paths: list[Path]) -> dict[str, str]:
    """Map job_posting_id -> the day it was scraped, read from the per-run files.

    The scraper writes each run to morning/ or evening/ with the date baked into
    the filename (linkedin_jobs_<YYYY-MM-DD>_<label>_scored.csv.gz). We scan those
    sibling folders of the loaded master and take the EARLIEST date a job appears
    in (its first scrape = when it was extracted). Cheap: a few small gz files.
    """
    id_date: dict[str, str] = {}
    seen_dirs: set[Path] = set()
    for p in paths:
        parent = Path(p).resolve().parent
        for sub in ("morning", "evening"):
            d = parent / sub
            if d in seen_dirs or not d.exists():
                continue
            seen_dirs.add(d)
            for f in sorted(d.glob("*.csv.gz")):
                m = _DATE_RE.search(f.name)
                if not m:
                    continue
                day = m.group(1)
                key = str(f)
                ids = _RUN_FILE_IDS.get(key)
                if ids is None:
                    try:
                        rdf = read_csv_gz(f)
                    except (OSError, ValueError):
                        continue
                    if "job_posting_id" not in rdf.columns:
                        continue
                    ids = rdf["job_posting_id"].astype(str).tolist()
                    _RUN_FILE_IDS[key] = ids
                for jid in ids:
                    prev = id_date.get(jid)
                    if prev is None or day < prev:
                        id_date[jid] = day
    return id_date


def add_extracted_date(df: pd.DataFrame, id_date: dict[str, str]) -> pd.DataFrame:
    """Ensure an 'extracted_date' column (the day a job was scraped).

    Priority, highest first:
      1. a value already stored in the master (scraper.py now writes one),
      2. the date parsed from the per-run filename it first appeared in,
      3. the date portion of job_posted_date (scrape filter is 'Past 24 hours',
         so the posting date is within ~a day of extraction),
      4. blank.
    """
    if df.empty:
        return df
    if "job_posted_date" in df.columns:
        posted = pd.to_datetime(df["job_posted_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    else:
        posted = pd.Series([None] * len(df), index=df.index)
    from_runs = df["job_posting_id"].astype(str).map(id_date)
    if "extracted_date" in df.columns:
        stored = df["extracted_date"].astype(str).str.strip()
        stored = stored.mask(stored.isin(["", "nan", "NaN", "NaT", "None"]))
    else:
        stored = pd.Series([None] * len(df), index=df.index)
    df["extracted_date"] = stored.fillna(from_runs).fillna(posted).fillna("")
    return df


def load_files(paths: list[Path]) -> tuple[pd.DataFrame, dict[str, Path]]:
    """Load and concatenate CSVs; return (df, id_to_source_path)."""
    frames: list[pd.DataFrame] = []
    id_to_path: dict[str, Path] = {}
    for p in paths:
        if not p.exists():
            continue
        try:
            df = read_csv_gz(p)
        except (OSError, ValueError):
            continue
        if "job_posting_id" not in df.columns:
            continue
        df["job_posting_id"] = df["job_posting_id"].astype(str)
        df["_source"] = str(p)
        frames.append(df)
        for jid in df["job_posting_id"]:
            id_to_path.setdefault(jid, p)
    if not frames:
        return pd.DataFrame(), {}
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["job_posting_id"], keep="last")
    combined = add_extracted_date(combined, extraction_dates_from_runs(paths))
    # Display-friendly applicant count (Bright Data's job_num_applicants): used
    # to prioritize the apply window — fewer applicants = better odds.
    if "job_num_applicants" in combined.columns:
        n = pd.to_numeric(combined["job_num_applicants"], errors="coerce")
        combined["applicants"] = [("" if pd.isna(v) else str(int(v))) for v in n]
    else:
        combined["applicants"] = ""
    # Precomputed lowercase search haystack so per-keystroke filtering matches
    # against one ready column instead of rebuilding it across the whole frame.
    scols = [c for c in ("job_title", "company_name", "url") if c in combined.columns]
    combined["_search"] = (
        combined[scols].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
        if scols else ""
    )
    return combined, id_to_path


def _load_cfg() -> dict:
    """config.json (shared with the watcher), {} when unreadable."""
    try:
        return json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cfg(updates: dict) -> None:
    """Merge updates into local/config.json (best-effort; never crash the UI)."""
    cfg = _load_cfg()
    cfg.update(updates)
    try:
        atomic_write_json(HERE / "config.json", cfg)
    except OSError:
        pass


_ENGINE_LABELS = {"vertex": "Engine: Vertex Gemini", "api_key": "Engine: Gemini API key"}
_LABEL_TO_AUTH = {v: k for k, v in _ENGINE_LABELS.items()}


def load_min_score(default: int = 4) -> int:
    try:
        return int(_load_cfg().get("min_score", default))
    except (TypeError, ValueError):
        return default


def load_followup_days(default: int = 5) -> int:
    """Days after 'applied' before a follow-up nudge shows in the tracker."""
    try:
        return int(_load_cfg().get("followup_days", default))
    except (TypeError, ValueError):
        return default


def gdrive_root_dir(csv_paths: list[Path]) -> Path | None:
    """The synced LinkedInJobs folder: config.json's gdrive_root, else inferred
    from the loaded files' location (run files sit one level deeper)."""
    root = str(_load_cfg().get("gdrive_root", "") or "")
    if root and Path(root).exists():
        return Path(root)
    for p in csv_paths:
        parent = Path(p).resolve().parent
        if parent.name in ("morning", "evening"):
            parent = parent.parent
        if parent.exists():
            return parent
    return None


def blocklist_path(csv_paths: list[Path]) -> Path | None:
    root = gdrive_root_dir(csv_paths)
    return (root / "company_blocklist.txt") if root else None


def load_local_blocklist(csv_paths: list[Path]) -> list[str]:
    """Companies blocked from the UI. The file lives in the synced Drive folder
    so run_scraper.sh pulls it down for scraper.py on every VM run."""
    p = blocklist_path(csv_paths)
    if not p or not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def append_to_blocklist(csv_paths: list[Path], company: str) -> Path:
    p = blocklist_path(csv_paths)
    if not p:
        raise OSError("could not resolve the synced LinkedInJobs folder")
    existing = {b.lower() for b in load_local_blocklist(csv_paths)}
    if company.lower() not in existing:
        with open(p, "a", encoding="utf-8") as f:
            f.write(company + "\n")
    return p


def drop_blocklisted(df: pd.DataFrame, names: list[str]) -> pd.DataFrame:
    """Mirror scraper.py's substring/case-insensitive company filter locally so
    a UI block takes effect immediately, not just on the next VM run."""
    if df.empty or not names or "company_name" not in df.columns:
        return df
    hay = df["company_name"].fillna("").astype(str).str.lower()
    mask = pd.Series(False, index=df.index)
    for bad in names:
        mask = mask | hay.str.contains(bad.lower(), na=False, regex=False)
    return df[~mask]


def filter_high_unseen(df: pd.DataFrame, min_score: int = 4) -> pd.DataFrame:
    if df.empty or "score" not in df.columns:
        return df.iloc[0:0]
    score = pd.to_numeric(df["score"], errors="coerce").fillna(0)
    is_seen = df["is_seen"].astype(str) if "is_seen" in df.columns else pd.Series(["no"] * len(df))
    mask = (score >= min_score) & (is_seen == "no")
    out = df.loc[mask].copy()
    out["__score_num"] = score[mask]
    out["__deep_num"] = pd.to_numeric(out.get("deep_score", 0), errors="coerce").fillna(0)
    # Fewest applicants first within a score band: early applications convert
    # far better, so the freshest apply window floats to the top. Unknown
    # applicant counts sort last.
    if "job_num_applicants" in out.columns:
        out["__appl_num"] = pd.to_numeric(out["job_num_applicants"], errors="coerce").fillna(float("inf"))
    else:
        out["__appl_num"] = float("inf")
    out = out.sort_values(
        ["__score_num", "__appl_num", "__deep_num"], ascending=[False, True, False]
    )
    return out.drop(columns=["__score_num", "__deep_num", "__appl_num"])


# --------------------------------------------------------------------------- Treeview helpers

def make_treeview(parent: tk.Widget, columns: list[tuple[str, int]]) -> ttk.Treeview:
    tv = ttk.Treeview(parent, columns=[c for c, _ in columns], show="headings", selectmode="extended")
    for col, width in columns:
        tv.heading(col, text=COLUMN_LABELS.get(col, col),
                   command=lambda c=col, t=tv: sort_treeview(t, c))
        tv.column(col, width=width, anchor="w", stretch=True)
    for tag, opts in TAG_STYLES.items():
        tv.tag_configure(tag, **opts)
    vsb = ttk.Scrollbar(parent, orient="vertical", command=tv.yview)
    hsb = ttk.Scrollbar(parent, orient="horizontal", command=tv.xview)
    tv.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tv.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    parent.rowconfigure(0, weight=1)
    parent.columnconfigure(0, weight=1)
    return tv


def sort_treeview(tv: ttk.Treeview, col: str) -> None:
    rows = [(tv.set(iid, col), iid) for iid in tv.get_children("")]
    try:
        rows.sort(key=lambda r: float(r[0]))
    except ValueError:
        rows.sort(key=lambda r: r[0].lower())
    current = tv.heading(col).get("text", col)
    reverse = current.endswith(" ▼")
    if reverse:
        rows.reverse()
    for i, (_, iid) in enumerate(rows):
        tv.move(iid, "", i)
    for c in tv["columns"]:
        tv.heading(c, text=COLUMN_LABELS.get(c, c))
    label = COLUMN_LABELS.get(col, col)
    tv.heading(col, text=label + (" ▲" if reverse else " ▼"))


def populate(tv: ttk.Treeview, df: pd.DataFrame, columns: list[str]) -> None:
    tv.delete(*tv.get_children())
    if df.empty:
        return
    n = len(df)
    # Pull each needed column to a Python list once (fast), then index by row.
    # This avoids df.iterrows(), which builds a fresh Series per row.
    col_lists = [
        df[c].astype(str).tolist() if c in df.columns else [""] * n
        for c in columns
    ]
    ids = df["job_posting_id"].astype(str).tolist()
    recos = (
        df["recommendation"].astype(str).str.strip().str.lower().tolist()
        if "recommendation" in df.columns else [""] * n
    )
    insert = tv.insert
    for i in range(n):
        rec = recos[i]
        tag = rec if rec in ("apply", "consider", "skip") else ("odd" if i % 2 else "even")
        insert("", "end", iid=ids[i], values=[cl[i] for cl in col_lists], tags=(tag,))


# --------------------------------------------------------------------------- App

class App:
    def __init__(self, csv_paths: list[Path]) -> None:
        self.csv_paths = csv_paths
        self.registry = SeenRegistry()
        self.min_score = load_min_score()
        self.followup_days = load_followup_days()
        self._tracked: dict[str, dict] = {}
        # Last-seen mtimes of the loaded source files, so the open window can
        # self-refresh when Drive syncs a new file even if the watcher didn't
        # drop a reload flag. Initialised lazily on the first poll.
        self._last_mtimes: dict[str, float | None] | None = None
        # job_posting_id -> integer row position in self.df, for O(1) detail/row
        # lookups instead of a full-DataFrame scan on every selection.
        self._row_by_id: dict[str, int] = {}
        # Which tree/row currently shows the lifted hover tag, so we can restore
        # its original tag when the cursor moves off it.
        self._hover_tv: ttk.Treeview | None = None
        self._hover_row: str = ""
        self._hover_tags: tuple[str, ...] = ()

        self.root = tk.Tk()
        self.root.title("LinkedIn Jobs — Triage")
        self.root.geometry("1300x760")
        self.root.minsize(900, 520)
        self.root.withdraw()                  # hide while we theme it
        apply_theme(self.root)

        self._build()

        self.root.update_idletasks()
        _enable_dark_titlebar(self.root)      # paint the OS title bar dark
        self.root.deiconify()                 # re-map so the dark frame takes effect
        self.root.attributes("-topmost", True)
        self.root.after(500, lambda: self.root.attributes("-topmost", False))  # not always on top
        self.root.lift()
        self.root.focus_force()

        self.reload_data()
        self._poll_reload_flag()

    def _build(self) -> None:
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=16, pady=(13, 11))
        ttk.Label(header, text="LinkedIn Jobs", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Triage & Apply", style="Subtitle.TLabel").pack(
            side="left", padx=(12, 0), pady=(7, 0))
        self.lbl_header = ttk.Label(header, text="", style="Muted.TLabel")
        self.lbl_header.pack(side="right")
        self.badge_unseen = ttk.Label(header, text="0", style="Badge.TLabel")
        self.badge_unseen.pack(side="right", padx=(0, 10))
        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

        self.nb = nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=12, pady=(8, 8))

        # Tab 1 — High Score (Unseen): same multi-column filter bar as All Jobs,
        # applied over the already score>=4 + unseen set.
        f1 = ttk.Frame(nb)
        nb.add(f1, text="High Score (Unseen)")
        hbar = ttk.Frame(f1)
        hbar.pack(fill="x", padx=4, pady=6)

        ttk.Label(hbar, text="Search:").pack(side="left")
        self.search_h_var = tk.StringVar()
        self.search_h_var.trace_add("write", lambda *_: self._debounce("_deb_high", self._apply_filters_high))
        ttk.Entry(hbar, textvariable=self.search_h_var, width=24).pack(side="left", padx=(4, 12))

        self.searchcol_h_var = tk.StringVar(value="All")
        _high_choices = ["All"] + [COLUMN_LABELS.get(c, c) for c, _ in HIGH_SCORE_COLUMNS]
        ttk.Label(hbar, text="In:").pack(side="left")
        cb_col_h = ttk.Combobox(hbar, textvariable=self.searchcol_h_var, state="readonly",
                                width=12, values=_high_choices)
        cb_col_h.pack(side="left", padx=(4, 12))
        cb_col_h.bind("<<ComboboxSelected>>", lambda *_: self._apply_filters_high())

        ttk.Label(hbar, text="Min score:").pack(side="left")
        self.minscore_h_var = tk.StringVar(value="Any")
        hb_s = ttk.Combobox(hbar, textvariable=self.minscore_h_var, state="readonly",
                            width=5, values=["Any", "4", "5"])
        hb_s.pack(side="left", padx=(4, 12))
        hb_s.bind("<<ComboboxSelected>>", lambda *_: self._apply_filters_high())

        ttk.Label(hbar, text="Day:").pack(side="left")
        self.day_h_var = tk.StringVar(value="All")
        self.day_h_cb = ttk.Combobox(hbar, textvariable=self.day_h_var, state="readonly",
                                     width=12, values=["All"])
        self.day_h_cb.pack(side="left", padx=(4, 12))
        self.day_h_cb.bind("<<ComboboxSelected>>", lambda *_: self._apply_filters_high())

        ttk.Label(hbar, text="Time:").pack(side="left")
        self.runlabel_h_var = tk.StringVar(value="All")
        hb_t = ttk.Combobox(hbar, textvariable=self.runlabel_h_var, state="readonly",
                            width=8, values=["All", "morning", "evening"])
        hb_t.pack(side="left", padx=(4, 12))
        hb_t.bind("<<ComboboxSelected>>", lambda *_: self._apply_filters_high())

        ttk.Label(hbar, text="Reco:").pack(side="left")
        self.reco_h_var = tk.StringVar(value="All")
        hb_r = ttk.Combobox(hbar, textvariable=self.reco_h_var, state="readonly",
                            width=9, values=["All", "apply", "consider", "skip"])
        hb_r.pack(side="left", padx=(4, 12))
        hb_r.bind("<<ComboboxSelected>>", lambda *_: self._apply_filters_high())

        self.easy_h_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(hbar, text="Easy Apply", variable=self.easy_h_var,
                        command=self._apply_filters_high).pack(side="left", padx=(0, 8))

        ttk.Button(hbar, text="Reset", command=self._reset_filters_high).pack(side="left", padx=4)
        self.lbl_high = ttk.Label(hbar, text="", style="Muted.TLabel")
        self.lbl_high.pack(side="right")

        tv_holder_h = ttk.Frame(f1)
        tv_holder_h.pack(fill="both", expand=True)
        self.tv_high = make_treeview(tv_holder_h, HIGH_SCORE_COLUMNS)
        self.tv_high.bind("<Double-1>", self._on_double_click_high)
        self.tv_high.bind("<Control-a>", self._select_all)
        self.tv_high.bind("<Control-A>", self._select_all)
        self.tv_high.bind("<<TreeviewSelect>>", self._on_select_row)
        self.tv_high.bind("<Button-3>", self._on_right_click)
        self.tv_high.bind("<Motion>", self._on_row_motion)
        self.tv_high.bind("<Leave>", self._clear_hover)

        # Global action bar (below the notebook + details pane) — acts on
        # whichever tab is active.
        self.bar1 = bar1 = ttk.Frame(self.root)
        bar1.pack(fill="x", padx=12, pady=(2, 10))
        self.lbl_status = ttk.Label(
            bar1, style="Muted.TLabel",
            text="Tip: Ctrl/Shift-click to select multiple · Ctrl+A selects all shown · right-click for more",
        )
        self.lbl_status.pack(side="left")

        ttk.Button(bar1, text="Refresh", command=self.reload_data).pack(side="right", padx=4)
        ttk.Button(bar1, text="Resume folder", command=self._open_resume_folder).pack(side="right", padx=4)
        ttk.Button(bar1, text="Resume layout…", command=self._open_resume_layout_dialog).pack(side="right", padx=4)
        ttk.Button(bar1, text="Mark all shown seen", command=self._mark_all_shown_seen,
                   style="Accent.TButton").pack(side="right", padx=4)
        ttk.Button(bar1, text="Mark seen (selected)", command=self._mark_seen_selected,
                   style="Accent.TButton").pack(side="right", padx=4)
        ttk.Button(bar1, text="Mark applied", command=self._mark_applied_selected,
                   style="Accent.TButton").pack(side="right", padx=4)
        self.btn_tailor = ttk.Button(bar1, text="Tailor resume", command=self._tailor_selected,
                                     style="Green.TButton")
        self.btn_tailor.pack(side="right", padx=4)
        self.engine_var = tk.StringVar(
            value=_ENGINE_LABELS.get(_load_cfg().get("gemini_auth", "vertex"), _ENGINE_LABELS["vertex"])
        )
        eng_cb = ttk.Combobox(
            bar1, textvariable=self.engine_var, state="readonly", width=24,
            values=[_ENGINE_LABELS["vertex"], _ENGINE_LABELS["api_key"]],
        )
        eng_cb.bind("<<ComboboxSelected>>", lambda *_: self._on_engine_change())
        eng_cb.pack(side="right", padx=(4, 10))
        self._apply_auth_env()

        # Tab 2 — All Jobs (multi-column filter + query view)
        f2 = ttk.Frame(nb)
        nb.add(f2, text="All Jobs")
        fbar = ttk.Frame(f2)
        fbar.pack(fill="x", padx=4, pady=6)

        ttk.Label(fbar, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._debounce("_deb_all", self._apply_filters))
        ttk.Entry(fbar, textvariable=self.search_var, width=24).pack(side="left", padx=(4, 12))

        self.searchcol_var = tk.StringVar(value="All")
        _all_choices = ["All"] + [COLUMN_LABELS.get(c, c) for c, _ in ALL_COLUMNS]
        ttk.Label(fbar, text="In:").pack(side="left")
        cb_col = ttk.Combobox(fbar, textvariable=self.searchcol_var, state="readonly",
                              width=12, values=_all_choices)
        cb_col.pack(side="left", padx=(4, 12))
        cb_col.bind("<<ComboboxSelected>>", lambda *_: self._apply_filters())

        ttk.Label(fbar, text="Min score:").pack(side="left")
        self.minscore_var = tk.StringVar(value="Any")
        cb_s = ttk.Combobox(fbar, textvariable=self.minscore_var, state="readonly",
                            width=5, values=["Any", "1", "2", "3", "4", "5"])
        cb_s.pack(side="left", padx=(4, 12))
        cb_s.bind("<<ComboboxSelected>>", lambda *_: self._apply_filters())

        ttk.Label(fbar, text="Day:").pack(side="left")
        self.day_var = tk.StringVar(value="All")
        self.day_cb = ttk.Combobox(fbar, textvariable=self.day_var, state="readonly",
                                   width=12, values=["All"])
        self.day_cb.pack(side="left", padx=(4, 12))
        self.day_cb.bind("<<ComboboxSelected>>", lambda *_: self._apply_filters())

        ttk.Label(fbar, text="Time:").pack(side="left")
        self.runlabel_var = tk.StringVar(value="All")
        cb_t = ttk.Combobox(fbar, textvariable=self.runlabel_var, state="readonly",
                            width=8, values=["All", "morning", "evening"])
        cb_t.pack(side="left", padx=(4, 12))
        cb_t.bind("<<ComboboxSelected>>", lambda *_: self._apply_filters())

        ttk.Label(fbar, text="Reco:").pack(side="left")
        self.reco_var = tk.StringVar(value="All")
        cb_r = ttk.Combobox(fbar, textvariable=self.reco_var, state="readonly",
                            width=9, values=["All", "apply", "consider", "skip"])
        cb_r.pack(side="left", padx=(4, 12))
        cb_r.bind("<<ComboboxSelected>>", lambda *_: self._apply_filters())

        self.easy_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(fbar, text="Easy Apply", variable=self.easy_var,
                        command=self._apply_filters).pack(side="left", padx=(0, 8))

        ttk.Button(fbar, text="Reset", command=self._reset_filters).pack(side="left", padx=4)
        self.lbl_all = ttk.Label(fbar, text="", style="Muted.TLabel")
        self.lbl_all.pack(side="right")

        tv_holder = ttk.Frame(f2)
        tv_holder.pack(fill="both", expand=True)
        self.tv_all = make_treeview(tv_holder, ALL_COLUMNS)
        self.tv_all.bind("<Double-1>", self._on_double_click_all)
        self.tv_all.bind("<Control-a>", self._select_all)
        self.tv_all.bind("<Control-A>", self._select_all)
        self.tv_all.bind("<<TreeviewSelect>>", self._on_select_row)
        self.tv_all.bind("<Button-3>", self._on_right_click)
        self.tv_all.bind("<Motion>", self._on_row_motion)
        self.tv_all.bind("<Leave>", self._clear_hover)

        # Tab 3 — Tracker: jobs the user acted on (applied / interviewing /
        # rejected / offer), with follow-up nudges and resume-folder links.
        f3 = ttk.Frame(nb)
        nb.add(f3, text="Tracker")
        tbar = ttk.Frame(f3)
        tbar.pack(fill="x", padx=4, pady=6)

        ttk.Label(tbar, text="Status:").pack(side="left")
        self.track_status_var = tk.StringVar(value="interviewing")
        cb_st = ttk.Combobox(tbar, textvariable=self.track_status_var, state="readonly",
                             width=12, values=list(APP_STATUSES))
        cb_st.pack(side="left", padx=(4, 4))
        ttk.Button(tbar, text="Set status", command=self._set_status_selected).pack(side="left", padx=4)
        ttk.Button(tbar, text="Mark followed up", command=self._mark_followed_up_selected).pack(side="left", padx=4)
        ttk.Button(tbar, text="Interview prep", command=self._prep_selected,
                   style="Accent.TButton").pack(side="left", padx=4)
        ttk.Button(tbar, text="Remove from tracker", command=self._remove_tracked_selected).pack(side="left", padx=4)
        self.due_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tbar, text="Follow-up due only", variable=self.due_only_var,
                        command=self._refresh_tracker).pack(side="left", padx=(8, 0))
        self.lbl_tracker = ttk.Label(tbar, text="", style="Muted.TLabel")
        self.lbl_tracker.pack(side="right")

        tv_holder_t = ttk.Frame(f3)
        tv_holder_t.pack(fill="both", expand=True)
        self.tv_tracker = make_treeview(tv_holder_t, TRACKER_COLUMNS)
        self.tv_tracker.bind("<Double-1>", self._on_double_click_tracker)
        self.tv_tracker.bind("<Control-a>", self._select_all)
        self.tv_tracker.bind("<Control-A>", self._select_all)
        self.tv_tracker.bind("<<TreeviewSelect>>", self._on_select_row)
        self.tv_tracker.bind("<Button-3>", self._on_right_click)
        self.tv_tracker.bind("<Motion>", self._on_row_motion)
        self.tv_tracker.bind("<Leave>", self._clear_hover)

        # Tab 4 — Stats: per-run pipeline metrics from run_stats.csv (synced
        # from the VM) + the apply-vs-recommendation calibration readout.
        f4 = ttk.Frame(nb)
        nb.add(f4, text="Stats")
        sbar = ttk.Frame(f4)
        sbar.pack(fill="x", padx=4, pady=6)
        self.lbl_stats_summary = ttk.Label(sbar, text="", style="Muted.TLabel")
        self.lbl_stats_summary.pack(side="left")
        ttk.Button(sbar, text="Export calibration CSV",
                   command=self._export_calibration).pack(side="right", padx=4)
        self.lbl_calibration = ttk.Label(f4, text="", style="Muted.TLabel")
        self.lbl_calibration.pack(fill="x", padx=8, pady=(0, 6))

        tv_holder_s = ttk.Frame(f4)
        tv_holder_s.pack(fill="both", expand=True)
        self.tv_stats = make_treeview(tv_holder_s, STATS_COLUMNS)
        self.tv_stats.bind("<Motion>", self._on_row_motion)
        self.tv_stats.bind("<Leave>", self._clear_hover)

        # Details pane (between the notebook and the action bar): the model's
        # stage-2 analysis — reason, strengths, gaps — plus salary/applicants
        # and a JD snippet for whichever row is selected.
        det = ttk.Frame(self.root, style="Card.TFrame")
        det.pack(fill="x", padx=12, pady=(2, 2), before=self.bar1)
        # Hairline above the action bar, between the details card and the buttons.
        ttk.Separator(self.root, orient="horizontal").pack(
            fill="x", padx=12, pady=(4, 0), before=self.bar1)
        self.details = tk.Text(det, height=9, wrap="word", bg=SURFACE, fg=TEXT,
                               insertbackground=ACCENT, relief="flat", padx=14, pady=11,
                               font=FONT, state="disabled", highlightthickness=0,
                               selectbackground=SEL, selectforeground=SEL_TEXT)
        det_sb = ttk.Scrollbar(det, orient="vertical", command=self.details.yview)
        self.details.configure(yscrollcommand=det_sb.set)
        self.details.pack(side="left", fill="both", expand=True)
        det_sb.pack(side="right", fill="y")
        self.details.tag_configure("h", foreground=ACCENT, font=FONT_BOLD,
                                   spacing1=3, spacing3=2)
        self.details.tag_configure("muted", foreground=MUTED)
        self.details.tag_configure("good", foreground=GOOD)
        self.details.tag_configure("bad", foreground=DANGER)
        self._set_details([("Select a row to see the model's analysis "
                            "(reason, strengths, gaps), salary, and a JD snippet.", "muted")])

    # ------------------------------------------------------------------- data

    def reload_data(self) -> None:
        self._clear_hover()  # stale row refs before the tree is rebuilt
        self.df, self.id_to_path = load_files(self.csv_paths)
        # Hide UI-blocked companies immediately (the VM purge happens next run).
        self.df = drop_blocklisted(self.df, load_local_blocklist(self.csv_paths))
        # The local registry is the source of truth for seen-state: overlay it so
        # a job you marked seen STAYS seen even after the VM ships a fresh master
        # with is_seen="no" (or the job is re-scraped). Only ever flips no->yes,
        # so it can never hide a genuinely-unseen job.
        if not self.df.empty:
            if "is_seen" not in self.df.columns:
                self.df["is_seen"] = "no"
            self.df, _ = reconcile_is_seen(self.df, self.registry)
        # O(1) row lookups for the details pane / row actions.
        self._row_by_id = (
            {jid: i for i, jid in enumerate(self.df["job_posting_id"])}
            if not self.df.empty else {}
        )
        self.df_high = filter_high_unseen(self.df, self.min_score)
        # Refresh both Day dropdowns (newest first), keeping a still-valid pick.
        if hasattr(self, "day_cb"):
            self._refresh_day_combo(self.day_cb, self.day_var, self.df)
        if hasattr(self, "day_h_cb"):
            self._refresh_day_combo(self.day_h_cb, self.day_h_var, self.df_high)
        self._apply_filters_high()
        self._apply_filters()
        self._refresh_tracker()
        self._refresh_stats()
        if hasattr(self, "lbl_header"):
            total = 0 if self.df.empty else len(self.df)
            self.badge_unseen.config(text=f"{len(self.df_high)} unseen ≥4")
            self.lbl_header.config(text=f"{total:,} jobs total")

    def _refresh_day_combo(self, cb: ttk.Combobox, var: tk.StringVar, base: pd.DataFrame) -> None:
        days: list[str] = []
        if not base.empty and "extracted_date" in base.columns:
            days = sorted(
                {d for d in base["extracted_date"].astype(str) if d and d.lower() != "nan"},
                reverse=True,
            )
        cb["values"] = ["All"] + days
        if var.get() not in (["All"] + days):
            var.set("All")

    @staticmethod
    def _sort_query(view: pd.DataFrame) -> pd.DataFrame:
        """Default 'All Jobs' ordering: most-recent extracted day first, then
        highest score, then highest deep_score. (Header clicks still re-sort by
        a single column on top of this.)"""
        if view.empty:
            return view
        keys: list[str] = []
        asc: list[bool] = []
        tmp = view
        if "extracted_date" in tmp.columns:
            tmp = tmp.assign(__d=tmp["extracted_date"].astype(str))
            keys.append("__d")
            asc.append(False)
        if "score" in tmp.columns:
            tmp = tmp.assign(__s=pd.to_numeric(tmp["score"], errors="coerce").fillna(-1))
            keys.append("__s")
            asc.append(False)
        if "deep_score" in tmp.columns:
            tmp = tmp.assign(__ds=pd.to_numeric(tmp["deep_score"], errors="coerce").fillna(-1))
            keys.append("__ds")
            asc.append(False)
        if keys:
            tmp = tmp.sort_values(keys, ascending=asc, kind="stable")
            tmp = tmp.drop(columns=["__d", "__s", "__ds"], errors="ignore")
        return tmp

    def _filter_and_sort(self, base: pd.DataFrame, search: str, minscore: str,
                         day: str, time_: str, reco: str, easy: bool = False,
                         search_column: str | None = None) -> pd.DataFrame:
        """Apply the shared multi-column filters (AND) + default sort to a base set.
        search_column: a column id to restrict the text search to; None/"All" = all."""
        view = base
        if view.empty:
            return view
        if search:
            if search_column and search_column not in ("", "All") and search_column in view.columns:
                hay = view[search_column].fillna("").astype(str).str.lower()
                view = view.loc[hay.str.contains(search, na=False, regex=False)]
            elif "_search" in view.columns:
                view = view.loc[view["_search"].str.contains(search, na=False, regex=False)]
            else:
                cols = [c for c in ("job_title", "company_name", "url") if c in view.columns]
                if cols:
                    hay = view[cols].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
                    view = view.loc[hay.str.contains(search, na=False, regex=False)]
        if minscore not in ("", "Any") and "score" in view.columns:
            sc = pd.to_numeric(view["score"], errors="coerce")
            view = view.loc[sc >= float(minscore)]
        if day not in ("", "All") and "extracted_date" in view.columns:
            view = view.loc[view["extracted_date"].astype(str) == day]
        if time_ not in ("", "All") and "run_label" in view.columns:
            view = view.loc[view["run_label"].astype(str).str.lower() == time_.lower()]
        if reco not in ("", "All") and "recommendation" in view.columns:
            view = view.loc[view["recommendation"].astype(str).str.lower() == reco.lower()]
        if easy and "is_easy_apply" in view.columns:
            view = view.loc[view["is_easy_apply"].astype(str).str.lower().isin(("true", "1", "yes"))]
        return self._sort_query(view)

    def _reset_filters(self) -> None:
        self.search_var.set("")
        self.searchcol_var.set("All")
        self.minscore_var.set("Any")
        self.day_var.set("All")
        self.runlabel_var.set("All")
        self.reco_var.set("All")
        self.easy_var.set(False)
        self._apply_filters()

    def _reset_filters_high(self) -> None:
        self.search_h_var.set("")
        self.searchcol_h_var.set("All")
        self.minscore_h_var.set("Any")
        self.day_h_var.set("All")
        self.runlabel_h_var.set("All")
        self.reco_h_var.set("All")
        self.easy_h_var.set(False)
        self._apply_filters_high()

    def _debounce(self, key: str, fn, ms: int = 250) -> None:
        """Coalesce rapid triggers (typing) into one deferred call, so the table
        only re-filters/redraws after the user pauses instead of per keystroke."""
        pending = getattr(self, key, None)
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        setattr(self, key, self.root.after(ms, fn))

    def _apply_filters(self) -> None:
        """All Jobs tab: filter over the full dataset."""
        col = LABEL_TO_COLUMN.get(self.searchcol_var.get(), self.searchcol_var.get())
        view = self._filter_and_sort(
            self.df, self.search_var.get().strip().lower(), self.minscore_var.get(),
            self.day_var.get(), self.runlabel_var.get(), self.reco_var.get(),
            self.easy_var.get(), col,
        )
        populate(self.tv_all, view, [c for c, _ in ALL_COLUMNS])
        if hasattr(self, "lbl_all"):
            total = 0 if self.df.empty else len(self.df)
            self.lbl_all.config(text=f"{len(view)} of {total} shown")

    def _apply_filters_high(self) -> None:
        """High Score (Unseen) tab: filter over the score>=4 + unseen set."""
        col_h = LABEL_TO_COLUMN.get(self.searchcol_h_var.get(), self.searchcol_h_var.get())
        view = self._filter_and_sort(
            self.df_high, self.search_h_var.get().strip().lower(), self.minscore_h_var.get(),
            self.day_h_var.get(), self.runlabel_h_var.get(), self.reco_h_var.get(),
            self.easy_h_var.get(), col_h,
        )
        populate(self.tv_high, view, [c for c, _ in HIGH_SCORE_COLUMNS])
        if hasattr(self, "lbl_high"):
            base = 0 if self.df_high.empty else len(self.df_high)
            self.lbl_high.config(
                text=f"{len(view)} of {base} unseen ≥4 shown  ·  {len(self.csv_paths)} file(s)"
            )

    # ------------------------------------------------------------------- hover

    def _on_row_motion(self, event: tk.Event) -> None:
        """Lift the row under the cursor by swapping its tag for a _hover variant.
        Cheap: early-returns unless the cursor crossed into a different row."""
        tv = event.widget
        row = tv.identify_row(event.y)
        if tv is self._hover_tv and row == self._hover_row:
            return
        self._clear_hover()
        if not row:
            return
        tags = tuple(tv.item(row, "tags"))
        variant = HOVER_OF.get(tags[0]) if tags else None
        if not variant:
            return
        self._hover_tv, self._hover_row, self._hover_tags = tv, row, tags
        tv.item(row, tags=(variant, *tags[1:]))

    def _clear_hover(self, *_event) -> None:
        """Restore the previously-hovered row's original tag (if any)."""
        tv, row, tags = self._hover_tv, self._hover_row, self._hover_tags
        self._hover_tv, self._hover_row, self._hover_tags = None, "", ()
        if tv is not None and row:
            try:
                if tv.exists(row):
                    tv.item(row, tags=tags)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------- actions

    def _active_tv(self) -> ttk.Treeview:
        """The treeview on the currently selected tab (actions act on it).
        The Stats tab has no job rows, so it falls back to High Score."""
        try:
            idx = self.nb.index(self.nb.select())
        except tk.TclError:
            return self.tv_high
        return {0: self.tv_high, 1: self.tv_all, 2: self.tv_tracker}.get(idx, self.tv_high)

    def _select_all(self, event: tk.Event) -> str:
        tv = event.widget
        tv.selection_set(tv.get_children(""))
        return "break"

    def _selected_ids(self) -> list[str]:
        return list(self._active_tv().selection())

    def _set_status(self, msg: str) -> None:
        """Thread-safe status-line update (callable from worker threads)."""
        self.root.after(0, lambda: self.lbl_status.config(text=msg))

    def _open_url(self, jid: str) -> None:
        url = ""
        if not self.df.empty:
            row = self.df.loc[self.df["job_posting_id"] == jid]
            if not row.empty:
                v = row.iloc[0].get("url", "")
                url = "" if pd.isna(v) else str(v).strip()
        if not url:
            # Tracked jobs survive the master CSV turning over — use the snapshot.
            url = str(self._tracked.get(jid, {}).get("url", "") or "").strip()
        if url and url.lower() != "nan":
            open_in_chrome(url)

    def _on_double_click_high(self, _event: tk.Event) -> None:
        sel = self.tv_high.selection()
        if sel:
            self._open_url(sel[0])

    def _on_double_click_all(self, _event: tk.Event) -> None:
        sel = self.tv_all.selection()
        if sel:
            self._open_url(sel[0])

    def _on_double_click_tracker(self, _event: tk.Event) -> None:
        sel = self.tv_tracker.selection()
        if sel:
            self._open_url(sel[0])

    def _open_selected_url(self) -> None:
        for jid in self._selected_ids():
            self._open_url(jid)

    # ---- seen-state ----

    def _mark_ids_seen(self, ids: list[str]) -> None:
        """Shared: record ids in the registry, rewrite their source CSVs, reload."""
        if not ids:
            return
        self.registry.mark(ids)
        idset = set(ids)
        affected_paths: set[Path] = {self.id_to_path[i] for i in ids if i in self.id_to_path}
        for path in affected_paths:
            try:
                df = read_csv_gz(path)
                df["job_posting_id"] = df["job_posting_id"].astype(str)
                mask = df["job_posting_id"].isin(idset)
                if mask.any():
                    df.loc[mask, "is_seen"] = "yes"
                    write_csv_gz_atomic(df, path)
            except (OSError, ValueError):
                pass
        self.reload_data()

    def _mark_seen_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select one or more rows to mark seen.")
            return
        self._mark_ids_seen(ids)
        self._set_status(f"Marked {len(ids)} job(s) as seen.")

    def _mark_all_shown_seen(self) -> None:
        tv = self._active_tv()
        ids = list(tv.get_children(""))
        if not ids:
            self._set_status("Nothing shown to mark.")
            return
        if not messagebox.askyesno(
            "Mark all as seen?",
            f"Mark all {len(ids)} currently shown jobs as seen?",
            icon="warning", parent=self.root,
        ):
            return
        self._mark_ids_seen(ids)
        self._set_status(f"Marked all {len(ids)} shown job(s) as seen.")

    # ---- row helpers ----

    def _row_for(self, jid: str) -> pd.Series | None:
        i = self._row_by_id.get(jid)
        if i is None or self.df.empty:
            return None
        try:
            return self.df.iloc[i]
        except (IndexError, KeyError):
            return None

    @staticmethod
    def _cell(row: pd.Series | None, col: str) -> str:
        if row is None:
            return ""
        v = row.get(col, "")
        return "" if pd.isna(v) else str(v)

    def _job_payload(self, jid: str) -> dict | None:
        """The job dict the tailor/prep pipeline consumes, or None if the job
        is no longer in the loaded data (no JD available)."""
        row = self._row_for(jid)
        if row is None:
            return None
        return {
            "job_posting_id": jid,
            "company_name": self._cell(row, "company_name"),
            "job_title": self._cell(row, "job_title"),
            # Full description preferred by the tailor; the summary is often
            # truncated or empty. All passed, tailor picks the richest.
            "job_description_formatted": self._cell(row, "job_description_formatted"),
            "job_description": self._cell(row, "job_description"),
            "job_summary": self._cell(row, "job_summary"),
            "url": self._cell(row, "url"),
        }

    # ---- application tracker ----

    def _mark_applied_selected(self) -> None:
        """Record selected jobs as applied (with a company/title/url snapshot)
        and mark them seen — an applied job is by definition triaged."""
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select one or more rows to mark applied.")
            return
        for jid in ids:
            row = self._row_for(jid)
            self.registry.set_status(
                jid, "applied",
                company=self._cell(row, "company_name"),
                job_title=self._cell(row, "job_title"),
                url=self._cell(row, "url"),
            )
        self._mark_ids_seen(ids)  # reloads, which also refreshes the tracker
        self._set_status(f"Marked {len(ids)} job(s) as applied — tracking on the Tracker tab.")

    def _set_status_selected(self) -> None:
        ids = list(self.tv_tracker.selection())
        if not ids:
            self._set_status("Select tracker rows to set a status on.")
            return
        status = self.track_status_var.get()
        for jid in ids:
            row = self._row_for(jid)
            self.registry.set_status(
                jid, status,
                company=self._cell(row, "company_name"),
                job_title=self._cell(row, "job_title"),
                url=self._cell(row, "url"),
            )
        self._refresh_tracker()
        self._set_status(f"Set {len(ids)} job(s) to '{status}'.")

    def _mark_followed_up_selected(self) -> None:
        ids = list(self.tv_tracker.selection())
        if not ids:
            self._set_status("Select tracker rows to mark followed up.")
            return
        self.registry.mark_followed_up(ids)
        self._refresh_tracker()
        self._set_status(f"Marked follow-up done on {len(ids)} job(s).")

    def _remove_tracked_selected(self) -> None:
        ids = list(self.tv_tracker.selection())
        if not ids:
            self._set_status("Select tracker rows to remove.")
            return
        if not messagebox.askyesno(
            "Remove from tracker?",
            f"Remove {len(ids)} job(s) from the application tracker?",
            icon="warning", parent=self.root,
        ):
            return
        for jid in ids:
            self.registry.clear_status(jid)
        self._refresh_tracker()
        self._set_status(f"Removed {len(ids)} job(s) from the tracker.")

    def _refresh_tracker(self) -> None:
        if not hasattr(self, "tv_tracker"):
            return
        rows = self.registry.status_rows()
        self._tracked = {r["job_posting_id"]: r for r in rows}
        rpaths = self.registry.resume_paths()
        today = date.today()
        due_count = 0
        display: list[tuple[str, list[str], str]] = []  # (jid, values, tag)
        for r in rows:
            jid = r["job_posting_id"]
            row = self._row_for(jid)
            company = r.get("company") or self._cell(row, "company_name")
            title = r.get("job_title") or self._cell(row, "job_title")
            url = r.get("url") or self._cell(row, "url")
            days = ""
            due = ""
            days_n = None
            if r.get("applied_date"):
                try:
                    days_n = (today - date.fromisoformat(r["applied_date"])).days
                    days = str(days_n)
                except ValueError:
                    pass
            if r.get("followed_up_at"):
                due = "done"
            elif (r["status"] == "applied" and days_n is not None
                  and days_n >= self.followup_days):
                due = "DUE"
                due_count += 1
            tag = "due" if due == "DUE" else (
                r["status"] if r["status"] in TAG_STYLES else "even")
            if self.due_only_var.get() and due != "DUE":
                continue
            values = [
                r["status"], r.get("status_date") or "", r.get("applied_date") or "",
                days, due, self._cell(row, "score"), self._cell(row, "deep_score"),
                title, company, url, "✓" if jid in rpaths else "",
            ]
            display.append((jid, values, tag))

        tv = self.tv_tracker
        tv.delete(*tv.get_children())
        for jid, values, tag in display:
            tv.insert("", "end", iid=jid, values=values, tags=(tag,))
        if hasattr(self, "lbl_tracker"):
            self.lbl_tracker.config(
                text=f"{len(rows)} tracked · {due_count} follow-up(s) due "
                     f"(≥ {self.followup_days} days since applied)"
            )

    def _open_resume_folder(self) -> None:
        """One click back to the tailored PDF for the selected job."""
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select a row to open its resume folder.")
            return
        path = self.registry.resume_path(ids[0])
        if not path or not Path(path).exists():
            self._set_status("No tailored resume recorded for this job — use 'Tailor resume' first.")
            return
        try:
            os.startfile(path)  # noqa: S606 - open the recorded folder for the user
        except OSError as e:
            self._set_status(f"Could not open {path}: {e}")

    # ---- context menu + blocklist ----

    def _on_right_click(self, event: tk.Event) -> None:
        tv = event.widget
        iid = tv.identify_row(event.y)
        if iid and iid not in tv.selection():
            tv.selection_set(iid)
        sel = tv.selection()
        if not sel:
            return
        jid = sel[0]
        company = self._cell(self._row_for(jid), "company_name").strip()
        if not company:
            company = str(self._tracked.get(jid, {}).get("company", "") or "").strip()
        menu = tk.Menu(self.root, tearoff=0, bg=SURFACE, fg=TEXT,
                       activebackground=SEL, activeforeground="#ffffff", bd=0)
        menu.add_command(label="Open URL", command=self._open_selected_url)
        menu.add_command(label="Mark seen", command=self._mark_seen_selected)
        menu.add_command(label="Mark applied", command=self._mark_applied_selected)
        menu.add_command(label="Open resume folder", command=self._open_resume_folder)
        if company:
            menu.add_separator()
            menu.add_command(label=f"Block company “{company}”",
                             command=lambda c=company: self._block_company(c))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _block_company(self, company: str) -> None:
        if not messagebox.askyesno(
            "Block company?",
            f"Add “{company}” to the company blocklist?\n\n"
            "The dashboard hides its postings immediately; the scraper drops and "
            "purges them on the next VM run (the blocklist file syncs via Drive).",
            icon="warning", parent=self.root,
        ):
            return
        try:
            path = append_to_blocklist(self.csv_paths, company)
        except OSError as e:
            self._set_status(f"Could not write the blocklist: {e}")
            return
        self.reload_data()
        self._set_status(f"Blocked “{company}” → {path}")

    # ---- details pane ----

    def _set_details(self, segments: list[tuple[str, str | None]]) -> None:
        if not hasattr(self, "details"):
            return
        self.details.config(state="normal")
        self.details.delete("1.0", "end")
        for text, tag in segments:
            self.details.insert("end", text, tag or ())
        self.details.config(state="disabled")

    def _on_select_row(self, event: tk.Event) -> None:
        sel = event.widget.selection()
        if sel:
            self._show_details(sel[0])

    def _show_details(self, jid: str) -> None:
        row = self._row_for(jid)
        if row is None:
            snap = self._tracked.get(jid)
            if snap:
                self._set_details([
                    (f"{snap.get('job_title') or '?'} — {snap.get('company') or '?'}\n", "h"),
                    ("No longer in the loaded data (tracker snapshot only).\n", "muted"),
                    (str(snap.get("url") or ""), "muted"),
                ])
            return
        segs: list[tuple[str, str | None]] = []
        title = self._cell(row, "job_title") or "?"
        company = self._cell(row, "company_name") or "?"
        loc = self._cell(row, "job_location")
        segs.append((f"{title} — {company}" + (f"  ({loc})" if loc else "") + "\n", "h"))

        meta: list[str] = []
        for label, col in (("score", "score"), ("deep", "deep_score"),
                           ("reco", "recommendation"), ("applicants", "applicants"),
                           ("posted", "job_posted_date"), ("salary", "job_base_pay_range")):
            v = self._cell(row, col).strip()
            if col == "job_posted_date" and v:
                v = v[:10]
            if v:
                meta.append(f"{label}: {v}")
        if meta:
            segs.append(("  ·  ".join(meta) + "\n\n", "muted"))

        reason = self._cell(row, "reason").strip()
        if reason:
            segs.append(("Reason  ", "h"))
            segs.append((reason + "\n", None))
        strengths = [s.strip() for s in self._cell(row, "strengths").split("|") if s.strip()]
        if strengths:
            segs.append(("Strengths\n", "h"))
            segs.extend((f"  + {s}\n", "good") for s in strengths)
        gaps = [g.strip() for g in self._cell(row, "gaps").split("|") if g.strip()]
        if gaps:
            segs.append(("Gaps\n", "h"))
            segs.extend((f"  − {g}\n", "bad") for g in gaps)

        jd = self._cell(row, "job_summary").strip()
        if len(jd) < 40:
            raw = self._cell(row, "job_description_formatted")
            jd = re.sub(r"<[^>]+>", " ", raw)
            jd = re.sub(r"\s+", " ", jd).strip()
        if jd:
            segs.append(("\nJD snippet  ", "h"))
            segs.append((jd[:700] + ("…" if len(jd) > 700 else ""), "muted"))
        self._set_details(segs)

    # ---- stats + calibration ----

    def _refresh_stats(self) -> None:
        if not hasattr(self, "tv_stats"):
            return
        tv = self.tv_stats
        tv.delete(*tv.get_children())
        stats_df = None
        root = gdrive_root_dir(self.csv_paths)
        path = (root / "run_stats.csv") if root else None
        if path and path.exists():
            try:
                stats_df = pd.read_csv(path)
            except (OSError, ValueError, pd.errors.ParserError):
                stats_df = None
        if stats_df is None or stats_df.empty:
            if hasattr(self, "lbl_stats_summary"):
                self.lbl_stats_summary.config(
                    text="run_stats.csv not synced yet — metrics appear after the next VM run."
                )
        else:
            cols = [c for c, _ in STATS_COLUMNS]
            for i, (_, r) in enumerate(stats_df.iloc[::-1].iterrows()):
                vals = []
                for c in cols:
                    v = r.get(c, "")
                    vals.append("" if pd.isna(v) else str(v))
                tv.insert("", "end", iid=f"stats{i}", values=vals,
                          tags=("odd" if i % 2 else "even",))
            if hasattr(self, "lbl_stats_summary"):
                last = stats_df.iloc[-1]
                recent = stats_df.tail(7)
                empty = pd.Series(0, index=recent.index)
                tok = pd.to_numeric(recent.get("prompt_tokens", empty), errors="coerce").fillna(0) \
                    + pd.to_numeric(recent.get("output_tokens", empty), errors="coerce").fillna(0)
                rows_in = pd.to_numeric(recent.get("rows_in", empty), errors="coerce").fillna(0)
                self.lbl_stats_summary.config(
                    text=f"{len(stats_df)} run(s) logged · last: {last.get('timestamp', '?')} — "
                         f"{last.get('rows_in', 0)} new, {last.get('llm_scored', 0)} scored · "
                         f"7-run avg: {rows_in.mean():.0f} new, {tok.mean():,.0f} tokens/run"
                )
        if hasattr(self, "lbl_calibration"):
            self.lbl_calibration.config(text=self._calibration_text())

    def _calibration_text(self) -> str:
        """Item-5 readout: how the user's real decisions line up with the
        model's recommendation. ~100 labels = enough signal to tune prompts."""
        rows = self.registry.status_rows()
        if not rows:
            return ("Calibration: no labels yet — use 'Mark applied' to start building "
                    "the applied-vs-recommendation dataset (target ~100 labels).")
        by_reco: Counter[str] = Counter()
        for r in rows:
            reco = self._cell(self._row_for(r["job_posting_id"]), "recommendation").strip().lower()
            by_reco[reco if reco in ("apply", "consider", "skip") else "unscored"] += 1
        parts = " · ".join(f"{k}: {v}" for k, v in by_reco.most_common())
        n = len(rows)
        note = " — enough to start tuning the scoring prompts" if n >= 100 else f" (target ~100, at {n})"
        return f"Calibration: {n} labeled application(s){note} · by model reco — {parts}"

    def _export_calibration(self) -> None:
        rows = self.registry.status_rows()
        if not rows:
            self._set_status("No tracked applications to export yet.")
            return
        recs = []
        for r in rows:
            jid = r["job_posting_id"]
            row = self._row_for(jid)
            recs.append({
                "job_posting_id": jid,
                "company": r.get("company") or self._cell(row, "company_name"),
                "job_title": r.get("job_title") or self._cell(row, "job_title"),
                "score": self._cell(row, "score"),
                "deep_score": self._cell(row, "deep_score"),
                "recommendation": self._cell(row, "recommendation"),
                "status": r["status"],
                "applied_date": r.get("applied_date") or "",
                "status_date": r.get("status_date") or "",
            })
        out = APPDATA / "calibration_labels.csv"
        try:
            pd.DataFrame(recs).to_csv(out, index=False, encoding="utf-8")
        except OSError as e:
            self._set_status(f"Export failed: {e}")
            return
        self._set_status(f"Calibration labels → {out}")

    # ---- interview prep ----

    def _prep_selected(self) -> None:
        if getattr(self, "_prepping", False):
            return
        ids = list(self.tv_tracker.selection()) or self._selected_ids()
        if not ids:
            self._set_status("Select a job to generate an interview prep sheet for.")
            return
        jid = ids[0]
        job = self._job_payload(jid)
        if job is None:
            self._set_status("Job description not available (job aged out of the master) — cannot build prep sheet.")
            return
        resume_dir = self.registry.resume_path(jid)
        self._prepping = True
        self._set_status(f"Generating interview prep for {job['company_name']} — {job['job_title']} …")
        self._apply_auth_env()
        threading.Thread(target=self._prep_worker, args=(job, resume_dir), daemon=True).start()

    def _prep_worker(self, job: dict, resume_dir: str | None) -> None:
        try:
            from resume_tailor.prep import generate_prep_sheet
            out_dir = Path(resume_dir) if resume_dir and Path(resume_dir).exists() else None
            path = generate_prep_sheet(job, out_dir)
        except Exception as exc:  # noqa: BLE001 - never crash the UI
            self._log_error("interview prep failed", exc)
            self._set_status(f"Interview prep FAILED — {exc}")
        else:
            self._set_status(f"Interview prep ready → {path}")
            try:
                os.startfile(str(path))  # noqa: S606 - open the sheet for the user
            except OSError:
                pass
        finally:
            self._prepping = False

    # ---- engine selector ----

    def _current_auth(self) -> str:
        return _LABEL_TO_AUTH.get(self.engine_var.get(), "vertex")

    def _apply_auth_env(self) -> None:
        """Seed the env var the in-process tailor reads at call time."""
        os.environ["RESUME_TAILOR_GEMINI_AUTH"] = self._current_auth()

    def _open_resume_layout_dialog(self) -> None:
        """Edit per-bullet line targets for the constant resume blocks. Each block:
        a Bullets spinbox + one 'lines' spinbox per bullet (1-3). Saved to config.json."""
        from resume_tailor import assets, config as rt_config
        try:
            blocks = assets.blocks()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Resume layout", f"Could not read blocks: {exc}", parent=self.root)
            return
        names = [b["name"] for b in blocks.get("experience", [])] + \
                [b["name"] for b in blocks.get("leadership", [])]

        win = tk.Toplevel(self.root)
        win.title("Resume layout")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        rows: dict[str, dict] = {}

        for r, name in enumerate(names):
            ttk.Label(win, text=name).grid(row=r, column=0, sticky="w", padx=8, pady=6)
            targets = rt_config.block_targets(name)
            n_var = tk.IntVar(value=len(targets))
            line_holder = ttk.Frame(win)
            line_holder.grid(row=r, column=4, sticky="w", padx=8)
            line_vars: list[tk.IntVar] = []

            def _render_lines(_=None, name=name, n_var=n_var, holder=line_holder, lv=line_vars, base=targets):
                for w in holder.winfo_children():
                    w.destroy()
                lv.clear()
                for i in range(max(1, min(5, n_var.get()))):
                    v = tk.IntVar(value=base[i] if i < len(base) else 2)
                    lv.append(v)
                    ttk.Spinbox(holder, from_=1, to=3, width=3, textvariable=v).pack(side="left", padx=2)

            ttk.Label(win, text="Bullets").grid(row=r, column=1, padx=(8, 2))
            ttk.Spinbox(win, from_=1, to=5, width=3, textvariable=n_var,
                        command=_render_lines).grid(row=r, column=2, sticky="w")
            _render_lines()
            rows[name] = {"n": n_var, "lines": line_vars}

        def _save():
            layout_cfg = {nm: {"line_targets": [v.get() for v in d["lines"]]}
                          for nm, d in rows.items()}
            _save_cfg({"resume_layout": layout_cfg})
            win.destroy()
            self._set_status("Resume layout saved (applies on the next tailor run).")

        btnbar = ttk.Frame(win)
        btnbar.grid(row=len(names), column=0, columnspan=5, pady=10)
        ttk.Button(btnbar, text="Save", command=_save, style="Accent.TButton").pack(side="left", padx=6)
        ttk.Button(btnbar, text="Cancel", command=win.destroy).pack(side="left", padx=6)
        # Block here until the modal closes so the dialog is fully modal (not just
        # input-grabbed) and a second editor can't be opened over the first.
        win.wait_window(win)

    def _on_engine_change(self) -> None:
        auth = self._current_auth()
        _save_cfg({"gemini_auth": auth})
        self._apply_auth_env()
        self._set_status(f"LLM engine: {self.engine_var.get().replace('Engine: ', '')}")

    # ---- resume tailor ----

    def _tailor_selected(self) -> None:
        if getattr(self, "_tailoring", False):
            return
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select one or more jobs to tailor a resume for.")
            return
        jobs: list[dict] = []
        for jid in ids:
            job = self._job_payload(jid)
            if job is not None:
                jobs.append(job)
        if not jobs:
            self._set_status("Could not find job data for the selection.")
            return
        cover = messagebox.askyesno(
            "Cover letter",
            f"Also generate a cover letter for the selected {len(jobs)} job(s)?",
            parent=self.root,
        )
        self._tailoring = True
        self.btn_tailor.config(state="disabled")
        self._apply_auth_env()
        threading.Thread(
            target=self._tailor_worker, args=(jobs, cover), daemon=True
        ).start()

    def _tailor_worker(self, jobs: list[dict], cover: bool) -> None:
        try:
            from resume_tailor import tailor as tailor_resume
        except Exception as exc:  # missing deps / import error — never crash the UI
            self._log_error("resume_tailor import failed", exc)
            self._set_status(f"Resume tailor unavailable: {exc}")
            self._finish_tailor(None)
            return
        last_dir = None
        n = len(jobs)
        for i, job in enumerate(jobs, 1):
            label = f"{job['company_name']} — {job['job_title']}"
            self._set_status(f"Tailoring {i}/{n}: {label} …")
            try:
                last_dir = tailor_resume(
                    job, cover_letter=cover,
                    on_status=lambda m, i=i, n=n, label=label: self._set_status(f"[{i}/{n}] {label}: {m}"),
                )
            except Exception as exc:
                self._log_error(f"tailor failed for {label}", exc)
                self._set_status(f"[{i}/{n}] {label}: FAILED — {exc}")
            else:
                # Remember where this job's resume landed so "Resume folder"
                # can jump back to it. Fresh connection: sqlite3 objects are
                # bound to the thread that created them, and this is a worker.
                if last_dir and job.get("job_posting_id"):
                    try:
                        with SeenRegistry() as reg:
                            reg.record_resume(job["job_posting_id"], str(last_dir))
                    except Exception as exc:  # noqa: BLE001 - bookkeeping only
                        self._log_error(f"recording resume path failed for {label}", exc)
        self._finish_tailor(last_dir)

    def _finish_tailor(self, out_dir) -> None:
        def done() -> None:
            self._tailoring = False
            self.btn_tailor.config(state="normal")
            if out_dir:
                self._set_status(f"Resume(s) ready → {out_dir}")
                try:
                    os.startfile(str(out_dir))  # noqa: S606 - open the output folder for the user
                except OSError:
                    pass
        self.root.after(0, done)

    def _log_error(self, context: str, exc: BaseException) -> None:
        import traceback
        try:
            with open(APPDATA / "ui_error.log", "a", encoding="utf-8") as f:
                f.write(f"\n=== {context} @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                traceback.print_exception(exc, file=f)
        except OSError:
            pass

    # ------------------------------------------------------------------- reload-flag polling

    def _source_mtimes(self) -> dict[str, float | None]:
        """Current mtime of each loaded source file (None if missing/unreadable)."""
        out: dict[str, float | None] = {}
        for p in self.csv_paths:
            try:
                out[str(p)] = p.stat().st_mtime
            except OSError:
                out[str(p)] = None
        return out

    def _poll_reload_flag(self) -> None:
        try:
            reloaded = False
            if RELOAD_FLAG.exists():
                try:
                    payload = json.loads(RELOAD_FLAG.read_text(encoding="utf-8"))
                    paths = [Path(p) for p in payload.get("paths", [])]
                    if paths:
                        self.csv_paths = paths
                except (OSError, json.JSONDecodeError):
                    pass
                try:
                    RELOAD_FLAG.unlink()
                except OSError:
                    pass
                self.reload_data()
                reloaded = True

            # Direct file-change detection: reload when a source file changed on
            # disk (e.g. Google Drive synced a fresh master) even if the watcher
            # never dropped a reload flag. First poll just records the baseline.
            if not reloaded:
                current = self._source_mtimes()
                if self._last_mtimes is None:
                    self._last_mtimes = current
                elif current != self._last_mtimes:
                    self.reload_data()
                    reloaded = True

            if reloaded:
                # Re-baseline against the (possibly new) csv_paths after any reload.
                self._last_mtimes = self._source_mtimes()
        finally:
            self.root.after(5000, self._poll_reload_flag)

    def run(self) -> None:
        self.root.mainloop()
        self.registry.close()


def main() -> int:
    paths = [Path(a) for a in sys.argv[1:]]

    lock = _UILock(UI_LOCK)
    if not lock.acquire():
        # A UI is already running — signal it to reload with the new paths and exit.
        try:
            RELOAD_FLAG.write_text(
                json.dumps({"paths": [str(p) for p in paths], "ts": time.time()}),
                encoding="utf-8",
            )
        except OSError:
            pass
        return 0

    try:
        App(paths).run()
    finally:
        lock.release()
    return 0


def _log_startup_error(exc: BaseException) -> None:
    """Last-resort error sink — pythonw discards stderr, so we mirror tracebacks
    into the watcher log directory where the user can find them."""
    import traceback
    log_path = APPDATA / "ui_error.log"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== ui.py crash @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            traceback.print_exception(exc, file=f)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        _log_startup_error(e)
        sys.exit(1)
