"""Who holds the auto-apply browser profile.

One browser at a time may open the profile: two browsers on one profile can
lose its saved sign-ins. Two signs say a browser holds it:

- Chrome's own lock (`chrome_holds`): on Windows its `lockfile` stays open
  for writing, so a second writer is refused; elsewhere `SingletonLock`
  names the host and the process. The bundled Playwright Chromium leaves no
  such lock.
- The sentinel (`sentinel_held`): `browser_profile.inuse` beside the
  profile, an OS lock (`locks.SingleInstance`) that `apply_run.launch_profile`
  takes for every browser it opens on the profile (a drain, `one`, the
  sign-in, a probe on the profile, the difficulty check) and keeps until that
  browser's context closes or its process ends.

`busy` is either sign. The Auto-apply panel reads it for Start and for
Check difficulty, `apply_run.py drain` and `one` read it before they claim
a job, and `apply_assess.py` reads it before it opens the browser.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import locks

SENTINEL_SUFFIX = ".inuse"
HOLD_WAIT_S = 1.0           # a panel probe holds the sentinel for an instant; wait that out
HOLD_RETRY_S = 0.05
BUSY_LEAD = ("The auto-apply browser is open: a run, a sign-in or a difficulty check holds "
             "its profile.")
RUN_BUSY = BUSY_LEAD + " Start the run once that window closes."


class ProfileBusy(RuntimeError):
    """Another browser holds the auto-apply profile; the message is the
    sentence to print."""


def default_profile_dir() -> Path:
    """The auto-apply browser profile (`apply_run.default_profile_dir`), read
    here without importing the runner, so the dashboard stays light."""
    appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return appdata / "linkedin_watcher" / "browser_profile"


def sentinel_path(profile_dir: Path | None = None) -> Path:
    """The sentinel file beside the profile folder."""
    folder = Path(profile_dir) if profile_dir else default_profile_dir()
    return folder.with_name(folder.name + SENTINEL_SUFFIX)


def chrome_holds(profile_dir: Path | None = None) -> bool:
    """Does a running Chrome hold the profile by its own lock? A missing
    profile is free."""
    folder = Path(profile_dir) if profile_dir else default_profile_dir()
    lock = folder / "lockfile"
    if lock.exists():
        try:
            fd = os.open(str(lock), os.O_WRONLY)
        except PermissionError:
            return True
        except OSError:
            return False
        os.close(fd)
        return False
    if os.name == "nt":
        return False
    try:
        target = os.readlink(str(folder / "SingletonLock"))
    except OSError:
        return False
    try:
        pid = int(target.rsplit("-", 1)[-1])
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def sentinel_held(profile_dir: Path | None = None) -> bool:
    """Does a browser `launch_profile` opened hold the sentinel? The probe
    takes the lock for an instant and gives it back; a missing sentinel file
    is free and is not made."""
    path = sentinel_path(profile_dir)
    if not path.exists():
        return False
    probe = locks.SingleInstance(path)
    try:
        got = probe.acquire()
    except OSError:
        return False
    probe.release()
    return not got


def busy(profile_dir: Path | None = None) -> bool:
    """Does any browser hold the profile: Chrome's lock or the sentinel?"""
    return chrome_holds(profile_dir) or sentinel_held(profile_dir)


def hold(profile_dir: Path | None = None, *,
         wait_s: float | None = None) -> locks.SingleInstance | None:
    """Take the sentinel for a browser about to open the profile: the held
    lock (release it when the browser closes; the process ending releases
    it too), or None while another holder keeps it. A panel's probe holds it
    for an instant, so a taken lock is tried again for up to `wait_s`
    (`HOLD_WAIT_S` unless given)."""
    path = sentinel_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + (HOLD_WAIT_S if wait_s is None else wait_s)
    while True:
        guard = locks.SingleInstance(path)
        if guard.acquire():
            return guard
        guard.release()
        if time.monotonic() >= deadline:
            return None
        time.sleep(HOLD_RETRY_S)
