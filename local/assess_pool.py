"""The parallel difficulty check's profile copies (cycle 22, SP1).

Two browsers on one profile can lose its saved sign-ins, so a check of several
jobs at once gives each worker its own temporary copy of the auto-apply
profile, one slot folder per concurrent worker, under
`%LOCALAPPDATA%\\linkedin_watcher\\assess_profiles`. The copies hold the
sign-in cookies, so they never outlive a run: `sweep_slots` clears the root
before a run starts and again when it ends.

`snapshot_profile` leaves out what a browser rebuilds (its caches) and what
only a running browser's own copy may hold (its lock files). The sign-ins are
in what stays: the cookies, Local State, Preferences and the storage folders.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import time
from pathlib import Path

SLOT_PREFIX = "slot-"
ROOT_NAME = "assess_profiles"
SWEEP_TRIES = 3
SWEEP_WAIT_S = 0.3

# Folders (matched by name, at any depth) a browser fills again by itself.
CACHE_DIRS = frozenset(name.lower() for name in (
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "DawnCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "GraphiteDawnCache", "Crashpad", "component_crx_cache",
    "extensions_crx_cache", "BrowserMetrics", "Media Cache", "ScriptCache", "CacheStorage",
    "optimization_guide_model_store"))
# What a running browser holds open: Chrome's lock files and the sentinel.
LOCK_NAMES = frozenset({"lockfile"})
LOCK_PREFIXES = ("singleton",)
LOCK_SUFFIXES = (".inuse",)

log = logging.getLogger("assess_pool")


def slot_root() -> Path:
    """Where the slot folders live, derived from LOCALAPPDATA the way
    `profile_lock.default_profile_dir` is."""
    appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return appdata / "linkedin_watcher" / ROOT_NAME


def slot_dir(number: int, root: Path | None = None) -> Path:
    """Slot `number` (1 up) under `root` (the slot root unless given)."""
    return (Path(root) if root else slot_root()) / f"{SLOT_PREFIX}{int(number)}"


def skipped(name: str, *, is_dir: bool) -> bool:
    """Does the copy leave this entry out: a cache folder or a lock file?"""
    low = name.lower()
    if is_dir:
        return low in CACHE_DIRS
    return low in LOCK_NAMES or low.startswith(LOCK_PREFIXES) or low.endswith(LOCK_SUFFIXES)


def snapshot_profile(src: Path, dest: Path) -> list[str]:
    """Copy the profile folder `src` into `dest`, leaving out its caches and
    lock files (`skipped`). A file that will not copy is logged and left out;
    the list returned is those files' paths (empty when the copy is whole).
    `dest` is made, and must not hold a copy already. Raises FileNotFoundError
    when `src` is not a folder."""
    src, dest = Path(src), Path(dest)
    if not src.is_dir():
        raise FileNotFoundError(f"no browser profile at {src}")
    failed: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for here, dirs, files in os.walk(src):
        rel = Path(here).relative_to(src)
        dirs[:] = [d for d in dirs if not skipped(d, is_dir=True)]
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            if skipped(name, is_dir=False):
                continue
            try:
                shutil.copy2(Path(here) / name, dest / rel / name)
            except OSError as e:
                failed.append(str(Path(here) / name))
                log.warning("profile copy: %s was not copied (%s)", rel / name,
                            type(e).__name__)
    return failed


def _writable_then_retry(func, path, error) -> None:
    """`shutil.rmtree`'s error hook: a read-only entry is made writable and
    removed again; any other error is raised for the sweep to count."""
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    func(path)


def sweep_slots(root: Path | None = None, *, tries: int = SWEEP_TRIES,
                wait_s: float = SWEEP_WAIT_S) -> list[Path]:
    """Delete every leftover slot folder under `root` (the slot root unless
    given). Best effort: a folder that will not delete (a browser still has a
    file open) is tried `tries` times, `wait_s` apart, then logged and kept in
    the list returned (empty when the root is clean). Never raises."""
    root = Path(root) if root else slot_root()
    try:
        slots = sorted(p for p in root.iterdir() if p.name.startswith(SLOT_PREFIX))
    except OSError:
        return []
    left: list[Path] = []
    for folder in slots:
        for attempt in range(max(1, tries)):
            try:
                if folder.is_dir() and not folder.is_symlink():
                    shutil.rmtree(folder, onexc=_writable_then_retry)
                else:
                    folder.unlink()
            except FileNotFoundError:
                break
            except OSError as e:
                if attempt + 1 < max(1, tries):
                    time.sleep(wait_s)
                    continue
                log.warning("profile copy %s was not deleted (%s)", folder.name,
                            type(e).__name__)
                left.append(folder)
            else:
                break
    return left
