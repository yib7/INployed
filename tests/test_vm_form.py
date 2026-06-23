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


def test_run_times_are_hour_dropdowns(root):
    p = _panel(root)
    assert len(p.time_vars) == 6                       # up to 6 picks
    assert "10:00" in vm_form.HOUR_OPTIONS and "23:00" in vm_form.HOUR_OPTIONS
    # set via the dropdown vars round-trips through _times()
    p.time_vars[0].set("08:00")
    p.time_vars[1].set("20:00")
    p.time_vars[2].set(vm_form.BLANK)                  # blank rows are ignored
    assert p._times() == ["08:00", "20:00"]


def test_six_times_make_six_crontab_lines(root):
    p = _panel(root)
    p.set_times(["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"])  # 6, all >=2h apart
    assert len(p.crontab_text().splitlines()) == 6                       # one line per run
    lines = [ln for ln in p.preview.get("1.0", "end").splitlines() if ln.strip()]
    assert len(lines) == 6


def test_preview_rebuilds_not_appends(root):
    p = _panel(root)
    p.set_times(["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"])  # 6 lines
    p.set_times(["10:00", "19:00"])                                      # now only 2
    lines = [ln for ln in p.preview.get("1.0", "end").splitlines() if ln.strip()]
    assert len(lines) == 2                                               # replaced, not appended


def test_run_slots_are_numbered(root):
    from tkinter import ttk
    p = _panel(root)
    texts = []

    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, ttk.Label):
                texts.append(str(c.cget("text")))
            walk(c)

    walk(p.frame)
    assert "Run 1" in texts and "Run 6" in texts   # all six slots clearly labelled


def test_time_dropdown_change_updates_preview(root):
    p = _panel(root)
    p.set_times([])
    p.time_vars[0].set("08:00")                        # picking a time updates the preview
    assert "0 8 * * *" in p.preview.get("1.0", "end")


def test_weekday_change_updates_preview(root):
    p = _panel(root)
    p.set_times(["08:00"])
    p.freq_var.set("weekly")
    p.weekday_var.set("Wednesday")                     # the reported bug: must refresh now
    assert "* * 3" in p.preview.get("1.0", "end")      # Wednesday -> cron dow 3


def test_is_future_excludes_today_and_past():
    from datetime import date, timedelta
    assert vm_form.is_future(date.today() + timedelta(days=1)) is True
    assert vm_form.is_future(date.today()) is False
    assert vm_form.is_future(date.today() - timedelta(days=1)) is False


def test_date_picker_disables_today_enables_future(root):
    from datetime import date, timedelta
    dp = vm_form.DatePicker(tk.Frame(root), on_pick=lambda s: None)
    try:
        assert str(dp.day_buttons[date.today()].cget("state")) == "disabled"
        future = date.today() + timedelta(days=1)
        if future in dp.day_buttons:  # same month
            assert str(dp.day_buttons[future].cget("state")) == "normal"
    finally:
        dp.win.destroy()


def test_unconfigured_target_blocks_actions(root):
    calls = []
    p = _panel(root, runner=lambda c: calls.append(c), confirm=lambda *a, **k: True,
               target_factory=lambda: vm_sync.VMTarget())  # not configured
    p.set_times(["10:00", "19:00"])
    p.apply_schedule()
    p.pause()
    p.push_config()
    assert calls == []
