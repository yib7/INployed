"""config.json read-modify-write serialization (audit P1-1).

atomic_write_json makes each write all-or-nothing but does nothing about lost
updates: two writers that both read before either writes each persist their own
snapshot and the second one wins. That is not hypothetical here — deletes run on
the dashboard's background SerialTaskQueue (removed_jobs), every Settings save
and column-hide runs on the UI thread, and the watcher writes gdrive_root from a
separate process. The symptom is silent: a deleted job reappears, or a page of
just-saved settings reverts.

Every one of these fails without the sidecar lock in jsonutil.update_json_locked.
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402
import jsonutil  # noqa: E402
import locks  # noqa: E402
import settings  # noqa: E402
import watcher  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A throwaway config.json every writer in this module points at."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"seed": True}), encoding="utf-8")
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    monkeypatch.setattr(watcher, "CONFIG_PATH", path)
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_update_json_locked_merges_and_returns(cfg):
    out = jsonutil.update_json_locked(cfg, {"a": 1})
    assert out == {"seed": True, "a": 1}
    assert _read(cfg) == {"seed": True, "a": 1}


def test_update_json_locked_starts_fresh_on_a_corrupt_file(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    assert jsonutil.update_json_locked(p, {"a": 1}) == {"a": 1}


def test_update_json_locked_starts_fresh_on_a_non_object(tmp_path):
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert jsonutil.update_json_locked(p, {"a": 1}) == {"a": 1}


def test_a_slow_writer_blocks_the_other_instead_of_racing(cfg, monkeypatch):
    """The interleaving that loses an update: writer A reads, sleeps, writes;
    writer B reads (stale) and writes over it. With the lock, B waits."""
    real_write = jsonutil.atomic_write_json
    gate = threading.Event()

    def slow_write(path, data):
        if "slow" in data:
            gate.set()
            time.sleep(0.4)      # hold the lock across B's whole attempt
        real_write(path, data)

    monkeypatch.setattr(jsonutil, "atomic_write_json", slow_write)

    a = threading.Thread(target=jobsdata._save_cfg, args=({"slow": 1},))
    a.start()
    assert gate.wait(5)
    jobsdata._save_cfg({"fast": 2})      # blocks until A releases
    a.join(10)
    assert _read(cfg) == {"seed": True, "slow": 1, "fast": 2}


def test_two_threads_hammering_config_lose_no_key(cfg):
    """A delete-shaped writer and a settings-shaped writer, interleaved. Without
    the lock one of the two key sets comes out short."""
    errors: list[BaseException] = []

    def writer(prefix, n):
        try:
            for i in range(n):
                jobsdata._save_cfg({f"{prefix}{i}": i})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=writer, args=("removed_", 25))
    t2 = threading.Thread(target=writer, args=("hidden_", 25))
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)
    assert not errors
    out = _read(cfg)
    assert [k for k in out if k.startswith("removed_")] != []
    assert len(out) == 51        # seed + 25 + 25, nothing overwritten


def test_settings_save_and_jobsdata_save_share_one_lock(cfg, monkeypatch):
    """settings.save() and jobsdata._save_cfg() write the same file from
    different call paths; they must contend on the same sidecar lock."""
    monkeypatch.setattr(settings, "_resolve_targets",
                        lambda targets=None: {"config": cfg})
    started = threading.Event()
    released = threading.Event()

    def hold():
        with locks.file_lock(cfg):
            started.set()
            time.sleep(0.35)
            released.set()

    t = threading.Thread(target=hold)
    t.start()
    assert started.wait(5)
    settings.save({"followup_days": 9})
    assert released.is_set(), "settings.save did not wait for the config lock"
    t.join(5)
    assert _read(cfg)["followup_days"] == 9


def test_watcher_save_config_key_preserves_a_concurrent_dashboard_key(cfg):
    jobsdata._save_cfg({"removed_jobs": ["j1"]})
    watcher.save_config_key("gdrive_root", "E:/drive/LinkedInJobs")
    out = _read(cfg)
    assert out["removed_jobs"] == ["j1"]
    assert out["gdrive_root"] == "E:/drive/LinkedInJobs"


def test_watcher_skips_rather_than_crashing_when_the_lock_is_held(cfg, monkeypatch, caplog):
    """A contended config write must never take down the watcher loop."""
    monkeypatch.setattr(locks, "LOCK_TIMEOUT", 0.05)
    with locks.file_lock(cfg):
        watcher.save_config_key("gdrive_root", "E:/drive")
    assert "gdrive_root" not in _read(cfg)


def test_jobsdata_save_cfg_swallows_a_lock_timeout(cfg, monkeypatch):
    """_save_cfg is best-effort by contract — it runs on the UI thread."""
    monkeypatch.setattr(locks, "LOCK_TIMEOUT", 0.05)
    with locks.file_lock(cfg):
        jobsdata._save_cfg({"ui_scale_pct": 130})   # must not raise
    assert "ui_scale_pct" not in _read(cfg)


def test_file_lock_raises_after_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, "LOCK_TIMEOUT", 0.05)
    p = tmp_path / "x.json"
    with locks.file_lock(p):
        with pytest.raises(locks.FileLockTimeout):
            with locks.file_lock(p, timeout=0.05):
                pass


def test_apply_queue_lock_timeout_is_still_its_own_exception(tmp_path, monkeypatch):
    """apply_queue delegates to locks.file_lock but keeps its own error type,
    so `except QueueLockTimeout` in the dashboard keeps working."""
    import apply_queue

    monkeypatch.setenv("APPLY_QUEUE_PATH", str(tmp_path / "q.json"))
    with locks.file_lock(tmp_path / "q.json"):
        with pytest.raises(apply_queue.QueueLockTimeout):
            with apply_queue.locked(timeout=0.05):
                pass
