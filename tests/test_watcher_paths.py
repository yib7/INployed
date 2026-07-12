"""Regression: the watcher must launch the CURRENT dashboard entry point.

The Tkinter `ui.py` was deleted in the Qt port, but `watcher.launch_ui` still
pointed `UI_PATH` at it — so the scheduled-task auto-pop launched a missing file.
It must target `app.py` (the Qt entry point), which exists and accepts csv-path
arguments exactly the way `launch_ui` passes them.
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import locks  # noqa: E402
import watcher  # noqa: E402


def test_ui_path_targets_existing_app_entrypoint():
    assert watcher.UI_PATH.name == "app.py"
    assert watcher.UI_PATH.exists()


# P1-2: save_state must write via atomic_write_json (tmp + os.replace), not a
# naked write_text, so a crash mid-write never leaves state.json truncated.

def test_save_state_round_trips_valid_json(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(watcher, "STATE_PATH", state_path)
    state = {"reconciled_mtimes": {"a.csv.gz": 123.0}, "acknowledged_on_startup": True}

    watcher.save_state(state)

    assert json.loads(state_path.read_text(encoding="utf-8")) == state
    assert watcher.load_state() == state


def test_save_state_leaves_file_untouched_on_replace_failure(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    original = {"reconciled_mtimes": {}, "acknowledged_on_startup": True}
    state_path.write_text(json.dumps(original), encoding="utf-8")
    before = state_path.read_bytes()
    monkeypatch.setattr(watcher, "STATE_PATH", state_path)

    def boom_replace(*a, **k):
        raise OSError("simulated crash right before the rename")
    monkeypatch.setattr(os, "replace", boom_replace)

    with pytest.raises(OSError):
        watcher.save_state({"reconciled_mtimes": {"x": 1}, "acknowledged_on_startup": True})

    assert state_path.read_bytes() == before        # untouched: os.replace never landed


# P1-8: list_target_files / latest_for_ui must iterate the canonical RUN_LABELS
# (local/run_labels.py: morning/afternoon/evening/night), not a hardcoded
# morning/evening pair. run_scraper.sh uploads to afternoon/ and night/ too --
# scored files there must not be invisible to the watcher.

def test_list_target_files_sees_afternoon_and_night_run_folders(tmp_path):
    gdrive_root = tmp_path
    for sub in ("morning", "afternoon", "evening", "night"):
        d = gdrive_root / sub
        d.mkdir()
        (d / f"{sub}_scored.csv.gz").write_bytes(b"")

    found = watcher.list_target_files(gdrive_root)
    found_names = {p.name for p in found}

    assert "afternoon_scored.csv.gz" in found_names
    assert "night_scored.csv.gz" in found_names
    # existing morning/evening behavior must keep working unchanged
    assert "morning_scored.csv.gz" in found_names
    assert "evening_scored.csv.gz" in found_names


def test_latest_for_ui_includes_afternoon_and_night_labels(tmp_path):
    gdrive_root = tmp_path
    paths = {}
    for sub in ("morning", "afternoon", "evening", "night"):
        d = gdrive_root / sub
        d.mkdir()
        p = d / f"{sub}_scored.csv.gz"
        p.write_bytes(b"")
        paths[sub] = p

    result = watcher.latest_for_ui(list(paths.values()))
    result_labels = {p.parent.name for p in result}

    assert result_labels == {"morning", "afternoon", "evening", "night"}


def test_list_target_files_still_includes_master(tmp_path):
    gdrive_root = tmp_path
    (gdrive_root / "morning").mkdir()
    (gdrive_root / "morning" / "morning_scored.csv.gz").write_bytes(b"")
    master = gdrive_root / "linkedin_jobs_master.csv.gz"
    master.write_bytes(b"")

    found = watcher.list_target_files(gdrive_root)

    assert master in found


# P2-5: the master-staleness check must honor `stale_after_hours` from config
# (the same setting the dashboard's Stats tab reads), defaulting to 36 when
# absent -- not a hardcoded 36.

def test_master_is_stale_honors_configured_threshold():
    # cfg raises the threshold to 100h -- a 50h-old run must read as fresh
    # (not stale), where the old hardcoded 36h check would have flagged it.
    assert watcher.master_is_stale(50.0, {"stale_after_hours": 100}) is False


def test_master_is_stale_defaults_to_36_when_absent():
    assert watcher.master_is_stale(35.0, {}) is False
    assert watcher.master_is_stale(37.0, {}) is True


def test_master_is_stale_boundary_exactly_at_threshold_is_fresh():
    """The threshold is strict: a run EXACTLY stale_after_hours old is not yet stale."""
    assert watcher.master_is_stale(36.0, {}) is False
    assert watcher.master_is_stale(100.0, {"stale_after_hours": 100}) is False


# P2-9: local/locks.py is the single shared lock class -- watcher.SingleInstance
# and jobsdata._UILock are now both aliases of locks.SingleInstance (previously
# byte-for-byte duplicated in each module). Test the shared module directly, not
# just through one caller's alias.

def test_locks_single_instance_direct(tmp_path):
    p = tmp_path / "shared.lock"
    first, second = locks.SingleInstance(p), locks.SingleInstance(p)

    assert first.acquire() is True
    assert second.acquire() is False   # a second instance is blocked

    first.release()
    assert second.acquire() is True    # released -> the next instance can take it
    second.release()


def test_watcher_uses_shared_lock_class():
    assert watcher.SingleInstance is locks.SingleInstance


# P2-3: load_state must tolerate a parseable-but-WRONG-SHAPE state.json. A
# corrupt shape (a JSON list, or a dict whose reconciled_mtimes isn't a dict)
# would make the downstream state["reconciled_mtimes"] access raise on EVERY
# future fire — and the file is only rewritten on success, so the watcher would
# be bricked permanently. load_state must fall back to / repair to the safe
# default so that access never raises.

_DEFAULT_STATE = {"reconciled_mtimes": {}, "acknowledged_on_startup": False}


def _load_state_from(tmp_path, monkeypatch, payload: str) -> dict:
    state_path = tmp_path / "state.json"
    state_path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(watcher, "STATE_PATH", state_path)
    return watcher.load_state()


def test_load_state_json_list_returns_default(tmp_path, monkeypatch):
    state = _load_state_from(tmp_path, monkeypatch, "[1, 2, 3]")
    assert state == _DEFAULT_STATE
    assert state["reconciled_mtimes"] == {}          # downstream access is safe


def test_load_state_dict_missing_reconciled_mtimes_returns_default(tmp_path, monkeypatch):
    state = _load_state_from(tmp_path, monkeypatch, json.dumps({"acknowledged_on_startup": True}))
    assert isinstance(state.get("reconciled_mtimes"), dict)
    assert state["reconciled_mtimes"] == {}          # downstream access is safe


def test_load_state_non_dict_reconciled_mtimes_returns_default(tmp_path, monkeypatch):
    state = _load_state_from(
        tmp_path, monkeypatch,
        json.dumps({"reconciled_mtimes": "oops", "acknowledged_on_startup": True}),
    )
    assert isinstance(state["reconciled_mtimes"], dict)
    assert state["reconciled_mtimes"] == {}          # downstream access is safe


def test_load_state_valid_dict_missing_ack_key_is_repaired(tmp_path, monkeypatch):
    # A dict with a valid reconciled_mtimes but no acknowledged_on_startup is
    # repaired in place (setdefault), not discarded — the mtimes are preserved.
    state = _load_state_from(
        tmp_path, monkeypatch, json.dumps({"reconciled_mtimes": {"a.csv.gz": 1.0}}),
    )
    assert state["reconciled_mtimes"] == {"a.csv.gz": 1.0}   # preserved
    assert state["acknowledged_on_startup"] is False          # repaired


def test_load_state_valid_state_round_trips_unchanged(tmp_path, monkeypatch):
    good = {"reconciled_mtimes": {"a.csv.gz": 123.0}, "acknowledged_on_startup": True}
    state = _load_state_from(tmp_path, monkeypatch, json.dumps(good))
    assert state == good


def test_load_state_unparseable_json_returns_default(tmp_path, monkeypatch):
    # The already-handled corrupt (non-parseable) case must keep working.
    state = _load_state_from(tmp_path, monkeypatch, "{not valid json")
    assert state == _DEFAULT_STATE


# P2-5: the watcher's config write must be single-key / read-FRESH so it can't
# revert a key the dashboard (jobsdata._save_cfg) persisted between the watcher's
# load and its write. atomic_write_json stops torn files, not lost updates — a
# whole-dict write of the watcher's stale snapshot would silently drop the
# dashboard's key.

def test_save_config_key_preserves_concurrent_dashboard_keys(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(watcher, "CONFIG_PATH", cfg_path)
    # dashboard has already persisted some keys
    cfg_path.write_text(json.dumps({"min_score": 7, "followup_days": 9}), encoding="utf-8")

    # watcher auto-detects gdrive_root and persists ONLY that key
    watcher.save_config_key("gdrive_root", "E:/drive/LinkedInJobs")

    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["gdrive_root"] == "E:/drive/LinkedInJobs"
    assert saved["min_score"] == 7        # dashboard key survives
    assert saved["followup_days"] == 9    # dashboard key survives


def test_save_config_key_tolerates_missing_or_corrupt_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(watcher, "CONFIG_PATH", cfg_path)

    # missing file -> writes a fresh single-key config
    watcher.save_config_key("gdrive_root", "E:/drive")
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == {"gdrive_root": "E:/drive"}

    # corrupt file -> treated as empty, not crash
    cfg_path.write_text("{not json", encoding="utf-8")
    watcher.save_config_key("gdrive_root", "F:/drive")
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == {"gdrive_root": "F:/drive"}


def test_watcher_autodetect_does_not_revert_dashboard_write(tmp_path, monkeypatch):
    """End-to-end race sim: the dashboard writes key K AFTER the watcher's load,
    then the watcher runs its gdrive_root auto-detect write. K must survive — a
    whole-dict write-back of the watcher's stale snapshot would revert it."""
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(watcher, "CONFIG_PATH", cfg_path)

    # 1. watcher loads config at startup (gdrive_root not set yet). Its snapshot
    #    carries min_score=4 — what a whole-dict write-back would persist.
    cfg_path.write_text(json.dumps({"gdrive_root": "", "min_score": 4}), encoding="utf-8")
    watcher.load_config()

    # 2. dashboard saves a NEW min_score AFTER the watcher's load
    cfg_path.write_text(json.dumps({"gdrive_root": "", "min_score": 9}), encoding="utf-8")

    # 3. watcher auto-detects gdrive_root and persists only that key
    watcher.save_config_key("gdrive_root", "E:/drive")

    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["gdrive_root"] == "E:/drive"
    assert saved["min_score"] == 9    # NOT reverted to the watcher's stale snapshot (4)
