"""Who holds the auto-apply browser profile: Chrome's own
lock, or the sentinel every browser `apply_run.launch_profile` opens takes.
No browser starts here: Chrome's lock is simulated and the sentinel is held
by a second lock handle, as another process would hold it."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import locks
import profile_lock


def hold_chrome_lock(profile: Path):
    """Hold the profile as a running Chrome does: on Windows its `lockfile`
    open with no sharing, elsewhere a SingletonLock naming this live process.
    Returns the undo."""
    profile.mkdir(parents=True, exist_ok=True)
    lock = profile / "lockfile"
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        create = ctypes.windll.kernel32.CreateFileW
        create.restype = wintypes.HANDLE
        handle = create(str(lock), 0x40000000, 0, None, 4, 0x80, None)
        assert handle != wintypes.HANDLE(-1).value
        return lambda: ctypes.windll.kernel32.CloseHandle(handle)
    os.symlink(f"testhost-{os.getpid()}", str(profile / "SingletonLock"))
    return lambda: os.unlink(str(profile / "SingletonLock"))


def hold_sentinel(profile: Path) -> locks.SingleInstance:
    """Hold the sentinel as another browser's process would."""
    path = profile_lock.sentinel_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    guard = locks.SingleInstance(path)
    assert guard.acquire()
    return guard


def test_the_sentinel_sits_beside_the_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    profile = tmp_path / "linkedin_watcher" / "browser_profile"
    assert profile_lock.default_profile_dir() == profile
    assert profile_lock.sentinel_path() == tmp_path / "linkedin_watcher" / "browser_profile.inuse"
    assert profile_lock.sentinel_path(tmp_path / "p") == tmp_path / "p.inuse"


def test_a_missing_or_idle_profile_is_free_and_the_probe_makes_no_file(tmp_path):
    profile = tmp_path / "profile"
    assert profile_lock.busy(profile) is False
    assert not profile_lock.sentinel_path(profile).exists()
    profile.mkdir()
    (profile / "lockfile").write_text("", encoding="utf-8")
    assert profile_lock.busy(profile) is False


def test_chromes_lock_reads_busy(tmp_path):
    release = hold_chrome_lock(tmp_path / "profile")
    try:
        assert profile_lock.chrome_holds(tmp_path / "profile") is True
        assert profile_lock.busy(tmp_path / "profile") is True
    finally:
        release()
    assert profile_lock.busy(tmp_path / "profile") is False


def test_a_held_sentinel_reads_busy_and_a_released_one_free(tmp_path):
    guard = hold_sentinel(tmp_path / "profile")
    try:
        assert profile_lock.sentinel_held(tmp_path / "profile") is True
        assert profile_lock.busy(tmp_path / "profile") is True
        assert profile_lock.chrome_holds(tmp_path / "profile") is False
    finally:
        guard.release()
    assert profile_lock.busy(tmp_path / "profile") is False


def test_hold_takes_the_sentinel_and_refuses_while_another_holds_it(tmp_path):
    profile = tmp_path / "profile"
    guard = profile_lock.hold(profile, wait_s=0)
    assert guard is not None
    try:
        assert profile_lock.busy(profile) is True
        assert profile_lock.hold(profile, wait_s=0) is None
    finally:
        guard.release()
    again = profile_lock.hold(profile, wait_s=0)
    assert again is not None
    again.release()


def test_the_run_sentence():
    assert profile_lock.RUN_BUSY == (
        "The auto-apply browser is open: a run, a sign-in or a difficulty check holds its "
        "profile. Start the run once that window closes.")
    assert issubclass(profile_lock.ProfileBusy, RuntimeError)


@pytest.mark.skipif(os.name == "nt", reason="the SingletonLock is the POSIX lock")
def test_a_singleton_lock_of_a_gone_process_is_free(tmp_path):
    (tmp_path / "profile").mkdir()
    os.symlink("testhost-999999999", str(tmp_path / "profile" / "SingletonLock"))
    assert profile_lock.busy(tmp_path / "profile") is False
