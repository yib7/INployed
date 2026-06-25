"""LinkedIn jobs watcher — one-shot.

Launched by Windows Task Scheduler on Logon / SessionUnlock / Resume,
plus six scheduled fires per day (10:10/20/30 + 19:10/20/30 ET) covering
the windows when the VM is expected to drop new files into Google Drive.

Each invocation:
  1. Acquires a single-instance lock so concurrent triggers don't pile up.
  2. Resolves the synced LinkedInJobs folder.
  3. Reconciles is_seen against the local SQLite registry on any file
     whose mtime has advanced since we last processed it.
  4. Launches the Tkinter UI iff a file was newly reconciled AND it has
     unseen score>=4 rows. Otherwise exits silently.

No polling loop — the process exits after one pass.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from csv_io import read_csv_gz, reconcile_file  # noqa: E402
from jsonutil import atomic_write_json  # noqa: E402
from seen_db import SeenRegistry  # noqa: E402


# --------------------------------------------------------------------------- paths

APPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "linkedin_watcher"
APPDATA.mkdir(parents=True, exist_ok=True)
LOG_PATH = APPDATA / "watcher.log"
STATE_PATH = APPDATA / "state.json"
LOCK_PATH = APPDATA / "watcher.lock"
RELOAD_FLAG = APPDATA / "reload.flag"

CONFIG_PATH = HERE / "config.json"
UI_PATH = HERE / "app.py"   # the Qt dashboard entry point (was the deleted ui.py)


# --------------------------------------------------------------------------- logging

log = logging.getLogger("watcher")
log.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)


# --------------------------------------------------------------------------- single-instance lock

class SingleInstance:
    """Concurrent-trigger guard. Uses msvcrt.locking on Windows; fcntl elsewhere."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self._fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)  # msvcrt.locking is byte-range; always lock byte 0
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


# --------------------------------------------------------------------------- config + gdrive auto-detect

DEFAULT_CONFIG = {
    "gdrive_root": "",
    "mtime_stable_seconds": 30,
    "min_score": 4,
    "followup_days": 5,  # used by the UI's tracker tab, kept here so config.json stays one schema
}

FALLBACK_PATHS = [
    r"E:\My Drive\LinkedInJobs",
    r"G:\My Drive\LinkedInJobs",
    r"H:\My Drive\LinkedInJobs",
    str(Path.home() / "My Drive" / "LinkedInJobs"),
    str(Path.home() / "Google Drive" / "LinkedInJobs"),
]


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("config.json unreadable (%s) — using defaults", e)
    return cfg


def save_config(cfg: dict) -> None:
    atomic_write_json(CONFIG_PATH, cfg)


def detect_gdrive_root() -> str | None:
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\DriveFS") as k:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    candidate = Path(rf"{sub}:\My Drive\LinkedInJobs")
                    if candidate.exists():
                        return str(candidate)
        except OSError:
            pass
    for p in FALLBACK_PATHS:
        if Path(p).exists():
            return p
    return None


# --------------------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"reconciled_mtimes": {}, "acknowledged_on_startup": False}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


# --------------------------------------------------------------------------- file scanning

def list_target_files(gdrive_root: Path) -> list[Path]:
    out: list[Path] = []
    for sub in ("morning", "evening"):
        d = gdrive_root / sub
        if d.is_dir():
            out.extend(sorted(d.glob("*_scored.csv.gz")))
    master = gdrive_root / "linkedin_jobs_master.csv.gz"
    if master.exists():
        out.append(master)
    return out


def mtime_stable(path: Path, settle_seconds: float) -> bool:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) >= settle_seconds


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def latest_for_ui(files: list[Path]) -> list[Path]:
    """Fallback UI source: latest morning + latest evening.

    Used only if the master .csv.gz hasn't synced yet; normally the UI reads
    the master (the complete scored record). The watcher reconciles all files
    against the seen registry regardless, so every is_seen column stays accurate.
    """
    by_label: dict[str, Path] = {}
    for f in files:
        if f.parent.name not in ("morning", "evening"):
            continue
        cur = by_label.get(f.parent.name)
        if cur is None or _safe_mtime(f) > _safe_mtime(cur):
            by_label[f.parent.name] = f
    return list(by_label.values())


def has_unseen_high_score(path: Path, min_score: int) -> bool:
    try:
        df = read_csv_gz(path)
    except (OSError, ValueError) as e:
        log.warning("Could not read %s: %s", path, e)
        return False
    if "score" not in df.columns or "is_seen" not in df.columns:
        return False
    score = pd.to_numeric(df["score"], errors="coerce").fillna(0)
    mask = (score >= min_score) & (df["is_seen"].astype(str) == "no")
    return bool(mask.any())


# --------------------------------------------------------------------------- UI launch

def _pythonw_executable() -> str:
    if os.name == "nt":
        exe_dir = Path(sys.executable).parent
        candidate = exe_dir / "pythonw.exe"
        if candidate.exists():
            return str(candidate)
    return sys.executable


def launch_ui(csv_paths: list[Path]) -> None:
    """Spawn the UI as a detached process and return immediately.

    Critically, the UI must OUTLIVE this watcher. When the watcher is started
    by Task Scheduler, it runs inside a job object that Task Scheduler tears
    down shortly after the task completes — which kills any child still in the
    job. So we launch the UI with CREATE_BREAKAWAY_FROM_JOB (plus a new process
    group) so it escapes the job and keeps running after we exit. If breakaway
    is denied, we fall back to a plain detached launch.

    The UI self-deduplicates: if one is already running, the new process writes
    reload.flag and exits silently.
    """
    pythonw = _pythonw_executable()
    args = [pythonw, str(UI_PATH)] + [str(p) for p in csv_paths]
    log.info("Launching UI: %s", args)
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        base = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(args, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
            log.info("UI launched with job breakaway")
            return
        except OSError as e:
            log.warning("Job breakaway denied (%s) — launching without it", e)
            subprocess.Popen(args, creationflags=base, close_fds=True)
    else:
        subprocess.Popen(args, close_fds=True, start_new_session=True)


# --------------------------------------------------------------------------- main

def main() -> int:
    log.info("=== watcher fired (pid=%d) ===", os.getpid())

    lock = SingleInstance(LOCK_PATH)
    if not lock.acquire():
        log.info("Another watcher instance is running — exiting.")
        return 0

    try:
        cfg = load_config()
        if not cfg["gdrive_root"]:
            detected = detect_gdrive_root()
            if detected:
                cfg["gdrive_root"] = detected
                save_config(cfg)
                log.info("Auto-detected gdrive root: %s", detected)
            else:
                log.error("Could not locate LinkedInJobs folder. Set 'gdrive_root' in %s.", CONFIG_PATH)
                return 1

        gdrive_root = Path(cfg["gdrive_root"])
        log.info("Root=%s settle=%ds min_score=%d", gdrive_root, cfg["mtime_stable_seconds"], cfg["min_score"])

        state = load_state()
        registry = SeenRegistry()
        try:
            files = list_target_files(gdrive_root)

            # Companion to the VM-side healthcheck: if the master hasn't been
            # refreshed in 36h, the VM pipeline or Drive sync is likely broken.
            master_path = gdrive_root / "linkedin_jobs_master.csv.gz"
            if master_path.exists():
                age_h = (time.time() - _safe_mtime(master_path)) / 3600
                if age_h > 36:
                    log.warning(
                        "Master is %.0f h old — VM pipeline or Drive sync may be broken "
                        "(check ~/scraper.log on the VM)", age_h,
                    )

            # Prune state entries for run files that no longer exist, so
            # reconciled_mtimes doesn't grow forever.
            stale_keys = [
                k for k in state["reconciled_mtimes"]
                if not Path(k).exists()
            ]
            if stale_keys:
                for k in stale_keys:
                    del state["reconciled_mtimes"][k]
                save_state(state)
                log.info("Pruned %d stale state entries", len(stale_keys))

            # First-ever run: acknowledge whatever already exists so we don't
            # spam popups for historical files.
            if not state.get("acknowledged_on_startup"):
                for f in files:
                    try:
                        state["reconciled_mtimes"][str(f)] = f.stat().st_mtime
                    except OSError:
                        pass
                state["acknowledged_on_startup"] = True
                save_state(state)
                log.info("First run: acknowledged %d existing files — exiting without popup", len(files))
                return 0

            changed: list[Path] = []
            for f in files:
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                last = state["reconciled_mtimes"].get(str(f))
                if last == mtime:
                    continue
                if not mtime_stable(f, cfg["mtime_stable_seconds"]):
                    log.info("Skipping %s — mtime not yet stable", f.name)
                    continue
                try:
                    n = reconcile_file(f, registry)
                except (OSError, ValueError, pd.errors.ParserError) as e:
                    # Read mid-sync, partial gzip, etc. Skip this round — next
                    # fire will retry once mtime stabilizes again.
                    log.warning("Reconcile failed for %s: %s — will retry next run", f.name, e)
                    continue
                except Exception:
                    log.exception("Unexpected error reconciling %s — skipping", f.name)
                    continue
                if n:
                    log.info("Reconciled %d rows in %s", n, f.name)
                    new_mtime = _safe_mtime(f)
                    if new_mtime:
                        mtime = new_mtime
                state["reconciled_mtimes"][str(f)] = mtime
                changed.append(f)

            if changed:
                save_state(state)
                log.info("Newly processed this run: %s", [p.name for p in changed])

            # Only pop the UI when this run actually discovered new content.
            # Repeated fires after the same file lands stay silent.
            if changed:
                # The master is the complete scored record (scraping is
                # incremental, so morning/evening only hold that run's new
                # batch). Show the master so every unseen high-score job
                # surfaces, not just the latest batch. Fall back to
                # morning/evening if the master hasn't synced yet.
                master = gdrive_root / "linkedin_jobs_master.csv.gz"
                active = [master] if master.exists() else latest_for_ui(files)
                to_show = [p for p in active if has_unseen_high_score(p, cfg["min_score"])]
                if to_show:
                    launch_ui(to_show)
                else:
                    log.info("No unseen high-score rows — skipping UI launch.")
            else:
                log.info("No file changes since last run — exiting silently.")

            return 0
        finally:
            registry.close()
    finally:
        lock.release()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
