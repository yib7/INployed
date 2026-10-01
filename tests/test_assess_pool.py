"""The parallel difficulty check's profile copies (cycle 22, SP1): what the
snapshot leaves out and keeps, and the sweep of leftover slot folders. Fake
profile trees under tmp_path; the real profile is never read."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import assess_pool as ap


def _write(root: Path, rel: str, text: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


KEPT = ["Local State", "Default/Preferences", "Default/Network/Cookies", "Default/Login Data",
        "Default/Local Storage/leveldb/000003.log", "Default/IndexedDB/https_x/1.leveldb",
        "Default/Sessions/Session_1", "First Run"]
SKIPPED = ["Default/Cache/Cache_Data/f_000001", "Default/Code Cache/js/index",
           "Default/GPUCache/data_0", "ShaderCache/GPUCache/data_0", "GrShaderCache/data_0",
           "DawnCache/data_0", "GraphiteDawnCache/data_0", "Default/DawnWebGPUCache/data_0",
           "Default/Service Worker/CacheStorage/1/entry", "Default/Service Worker/ScriptCache/a",
           "Crashpad/settings.dat", "component_crx_cache/x.crx", "BrowserMetrics/m.pma",
           "lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket",
           "Default/lockfile"]


@pytest.fixture
def fake_profile(tmp_path):
    src = tmp_path / "browser_profile"
    for rel in KEPT:
        _write(src, rel, rel)
    for rel in SKIPPED:
        _write(src, rel, rel)
    _write(tmp_path, "browser_profile.inuse")           # the sentinel sits beside the profile
    return src


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_the_snapshot_keeps_the_sign_ins_and_skips_caches_and_locks(fake_profile, tmp_path):
    dest = tmp_path / "slots" / "slot-1"
    assert ap.snapshot_profile(fake_profile, dest) == []
    got = _files(dest)
    assert got == set(KEPT)
    for rel in KEPT:
        assert (dest / rel).read_text(encoding="utf-8") == rel      # the bytes came along
    assert not any(part.lower() in ap.CACHE_DIRS
                   for rel in got for part in Path(rel).parts[:-1])


def test_the_snapshot_leaves_the_source_whole(fake_profile, tmp_path):
    before = _files(fake_profile)
    ap.snapshot_profile(fake_profile, tmp_path / "copy")
    assert _files(fake_profile) == before == set(KEPT) | set(SKIPPED)


def test_an_inuse_file_is_a_lock_wherever_it_sits(tmp_path):
    src = tmp_path / "p"
    _write(src, "Default/Preferences")
    _write(src, "Default/x.inuse")
    _write(src, "Default/Cache.txt")                    # a cache-like name is not a cache folder
    ap.snapshot_profile(src, tmp_path / "d")
    assert _files(tmp_path / "d") == {"Default/Preferences", "Default/Cache.txt"}


def test_a_cache_name_matches_by_folder_not_by_file(tmp_path):
    src = tmp_path / "p"
    _write(src, "Default/Cache")                        # a FILE named Cache is kept
    ap.snapshot_profile(src, tmp_path / "d")
    assert _files(tmp_path / "d") == {"Default/Cache"}


def test_empty_folders_come_along(tmp_path):
    src = tmp_path / "p"
    (src / "Default" / "Extensions").mkdir(parents=True)
    ap.snapshot_profile(src, tmp_path / "d")
    assert (tmp_path / "d" / "Default" / "Extensions").is_dir()


def test_a_missing_profile_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        ap.snapshot_profile(tmp_path / "nope", tmp_path / "d")


def test_a_file_that_will_not_copy_is_listed_and_the_rest_copies(fake_profile, tmp_path,
                                                                 monkeypatch):
    real = ap.shutil.copy2

    def picky(src, dst, *a, **kw):
        if Path(src).name == "Login Data":
            raise PermissionError("in use")
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(ap.shutil, "copy2", picky)
    failed = ap.snapshot_profile(fake_profile, tmp_path / "d")
    assert [Path(p).name for p in failed] == ["Login Data"]
    assert _files(tmp_path / "d") == set(KEPT) - {"Default/Login Data"}


def test_the_slot_root_follows_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ap.slot_root() == tmp_path / "linkedin_watcher" / "assess_profiles"
    assert ap.slot_dir(3) == ap.slot_root() / "slot-3"
    assert ap.slot_dir(2, tmp_path / "r") == tmp_path / "r" / "slot-2"


def test_the_slot_root_is_beside_the_profile_not_in_it(monkeypatch, tmp_path):
    import profile_lock
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ap.slot_root().parent == profile_lock.default_profile_dir().parent
    assert profile_lock.default_profile_dir() not in ap.slot_root().parents


# --- the sweep -----------------------------------------------------------------------------

def _slots(root: Path, n: int = 3) -> list[Path]:
    made = []
    for k in range(1, n + 1):
        _write(root, f"slot-{k}/Default/Network/Cookies", "cookies")
        _write(root, f"slot-{k}/Local State", "{}")
        made.append(root / f"slot-{k}")
    return made


def test_the_sweep_deletes_every_leftover_slot(tmp_path):
    root = tmp_path / "assess_profiles"
    slots = _slots(root)
    assert ap.sweep_slots(root) == []
    assert not any(s.exists() for s in slots)


def test_the_sweep_leaves_what_is_not_a_slot(tmp_path):
    root = tmp_path / "assess_profiles"
    _slots(root, 1)
    _write(root, "notes.txt")
    assert ap.sweep_slots(root) == []
    assert [p.name for p in root.iterdir()] == ["notes.txt"]


def test_the_sweep_of_a_missing_root_is_a_no_op(tmp_path):
    assert ap.sweep_slots(tmp_path / "never_made") == []


def test_the_sweep_removes_a_read_only_file(tmp_path):
    root = tmp_path / "assess_profiles"
    (slot,) = _slots(root, 1)
    os.chmod(slot / "Local State", 0o444)
    assert ap.sweep_slots(root, wait_s=0) == []
    assert not slot.exists()


def test_the_sweep_retries_a_slot_that_will_not_delete_then_gives_it_up(tmp_path, monkeypatch):
    root = tmp_path / "assess_profiles"
    slots = _slots(root, 2)
    real = ap.shutil.rmtree
    calls = []

    def stubborn(path, *a, **kw):
        calls.append(Path(path).name)
        if Path(path).name == "slot-1":
            raise PermissionError("a browser still has it open")
        return real(path, *a, **kw)
    monkeypatch.setattr(ap.shutil, "rmtree", stubborn)
    assert ap.sweep_slots(root, tries=3, wait_s=0) == [slots[0]]    # logged, never raised
    assert calls.count("slot-1") == 3 and calls.count("slot-2") == 1
    assert slots[0].exists() and not slots[1].exists()


def test_the_sweep_succeeds_on_a_retry(tmp_path, monkeypatch):
    root = tmp_path / "assess_profiles"
    (slot,) = _slots(root, 1)
    real = ap.shutil.rmtree
    tries = []

    def flaky(path, *a, **kw):
        tries.append(1)
        if len(tries) < 2:
            raise PermissionError("a browser is still closing")
        return real(path, *a, **kw)
    monkeypatch.setattr(ap.shutil, "rmtree", flaky)
    assert ap.sweep_slots(root, tries=3, wait_s=0) == []
    assert len(tries) == 2 and not slot.exists()


@pytest.mark.skipif(os.name != "nt", reason="a file held open blocks delete on Windows")
def test_the_sweep_keeps_a_slot_with_a_locked_file_and_deletes_it_once_free(tmp_path):
    import ctypes
    from ctypes import wintypes
    root = tmp_path / "assess_profiles"
    (slot,) = _slots(root, 1)
    held = slot / "Default" / "Network" / "Cookies"
    create = ctypes.windll.kernel32.CreateFileW
    create.restype = wintypes.HANDLE
    handle = create(str(held), 0x80000000, 0, None, 3, 0x80, None)     # read, no sharing
    assert handle != wintypes.HANDLE(-1).value
    try:
        assert ap.sweep_slots(root, tries=2, wait_s=0) == [slot]
        assert held.exists()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    assert ap.sweep_slots(root, wait_s=0) == []
    assert not slot.exists()
