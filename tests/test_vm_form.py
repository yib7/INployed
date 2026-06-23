"""Headless tests for the VM panel (local/vm_form.py).

The panel must never run gcloud without an explicit confirm, must refuse when the
VM isn't configured, and must block an invalid schedule. The runner / confirm /
notify / target are injected so no real gcloud ever runs.
"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

tk = pytest.importorskip("tkinter")

import vm_form  # noqa: E402
import vm_sync  # noqa: E402


def _cfg_target():
    return vm_sync.VMTarget(gcloud="gcloud", instance="vm", zone="z", project="p",
                            user="u", remote_dir="~")


def _ok_runner(calls):
    def runner(cmd):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return runner


def _panel(root, **kw):
    kw.setdefault("notify", lambda *a, **k: None)
    return vm_form.VMPanel(tk.Frame(root), **kw)


def test_panel_builds(root):
    p = _panel(root)
    assert p.frame is not None


def test_crontab_preview_reflects_times(root):
    p = _panel(root)
    p.set_times(["10:00", "19:00"])
    cron = p.crontab_text()
    assert "0 10 * * *" in cron and "0 19 * * *" in cron


def test_apply_schedule_blocked_without_confirm(root):
    calls = []
    p = _panel(root, runner=lambda c: calls.append(c), confirm=lambda *a, **k: False,
               target_factory=_cfg_target)
    p.set_times(["10:00", "19:00"])
    p.apply_schedule()
    assert calls == []


def test_apply_schedule_runs_install_crontab_on_confirm(root):
    calls = []
    p = _panel(root, runner=_ok_runner(calls), confirm=lambda *a, **k: True,
               target_factory=_cfg_target)
    p.set_times(["10:00", "19:00"])
    p.apply_schedule()
    assert calls and "crontab -" in calls[0][-1]


def test_invalid_schedule_does_not_run(root):
    calls = []
    p = _panel(root, runner=lambda c: calls.append(c), confirm=lambda *a, **k: True,
               target_factory=_cfg_target)
    p.set_times(["10:00", "11:00"])  # < 2h apart -> invalid
    p.apply_schedule()
    assert calls == []


def test_pause_runs_set_pause_on_confirm(root):
    calls = []
    p = _panel(root, runner=_ok_runner(calls), confirm=lambda *a, **k: True,
               target_factory=_cfg_target)
    p.set_pause_inputs("2026-07-01", "09:00")
    p.pause()
    assert calls and "pause_until" in calls[0][-1]


def test_push_config_scps_both_vm_files(root):
    calls = []
    p = _panel(root, runner=_ok_runner(calls), confirm=lambda *a, **k: True,
               target_factory=_cfg_target)
    p.push_config()
    pushed = " ".join(" ".join(c) for c in calls)
    assert "search_config.json" in pushed and "scoring_config.json" in pushed


def test_unconfigured_target_blocks_actions(root):
    calls = []
    p = _panel(root, runner=lambda c: calls.append(c), confirm=lambda *a, **k: True,
               target_factory=lambda: vm_sync.VMTarget())  # not configured
    p.set_times(["10:00", "19:00"])
    p.apply_schedule()
    p.pause()
    p.push_config()
    assert calls == []
