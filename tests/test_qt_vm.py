"""SP6: the Qt VM panel — crontab preview + confirm/refuse on the gcloud actions (mocked).
SP7 adds the local-watcher-task sync wiring (local_task.register monkeypatched)."""
import types

import pytest

from qt.vm_panel import VMPanel
import jobsdata
import local_task
import vm_sync


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    """Hermetic config for EVERY test here: the panel reads/writes an in-memory
    dict, never the user's real local/config.json (apply_schedule persists the
    pushed times since SP7, so an unmocked run would pollute the real file)."""
    store = {}
    monkeypatch.setattr(jobsdata, "_load_cfg", lambda: dict(store))
    monkeypatch.setattr(jobsdata, "_save_cfg", lambda updates: store.update(updates))
    return store


@pytest.fixture(autouse=True)
def _register_calls(monkeypatch):
    """Stub local_task.register for every test — no real schtasks, ever."""
    calls = []
    monkeypatch.setattr(local_task, "register",
                        lambda times, **kw: calls.append(list(times)) or (True, "registered"))
    return calls


class _FakeTarget:
    def __init__(self, configured=True):
        self._configured = configured
        self.user, self.instance, self.zone = "yib", "scraper-vm", "us-east1-c"

    def configured(self):
        return self._configured

    def install_crontab_cmd(self, cron):
        return ["gcloud", "crontab", cron]

    def set_pause_cmd(self, val):
        return ["gcloud", "pause", val]

    def resume_cmd(self):
        return ["gcloud", "resume"]

    def build_scp_cmd(self, local, remote):
        return ["gcloud", "scp", remote]


def _panel(qtbot, target, confirm=True):
    cmds, notes = [], []
    panel = VMPanel(
        runner=lambda cmd: cmds.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        confirm=lambda title, msg: confirm,
        notify=lambda title, msg: notes.append((title, msg)),
        target_factory=lambda: target,
    )
    qtbot.addWidget(panel)
    panel._cmds, panel._notes = cmds, notes
    return panel


def test_crontab_preview_updates(qtbot):
    panel = _panel(qtbot, _FakeTarget())
    panel.set_times(["09:00", "18:00"])
    assert panel.preview.toPlainText().strip()        # a non-empty crontab preview


def test_apply_schedule_confirmed_runs(qtbot):
    panel = _panel(qtbot, _FakeTarget(), confirm=True)
    panel.set_times(["09:00", "18:00"])
    panel.apply_schedule()
    assert any(c[:2] == ["gcloud", "crontab"] for c in panel._cmds)


def test_apply_schedule_refuses_when_unconfigured(qtbot):
    panel = _panel(qtbot, _FakeTarget(configured=False))
    panel.set_times(["09:00", "18:00"])
    panel.apply_schedule()
    assert panel._cmds == []                           # nothing run
    assert any("No VM configured" in m for _, m in panel._notes)


def test_apply_schedule_not_run_when_declined(qtbot):
    panel = _panel(qtbot, _FakeTarget(), confirm=False)
    panel.set_times(["09:00", "18:00"])
    panel.apply_schedule()
    assert panel._cmds == []                           # confirm said no


def test_pause_runs_set_pause(qtbot):
    panel = _panel(qtbot, _FakeTarget())
    panel.pause()
    assert any(c[:2] == ["gcloud", "pause"] for c in panel._cmds)


def test_push_config_runs_scp_per_file(qtbot):
    panel = _panel(qtbot, _FakeTarget())
    panel.push_config()
    scps = [c for c in panel._cmds if c[:2] == ["gcloud", "scp"]]
    assert len(scps) == len(vm_sync.TARGET_REMOTE_FILE)


# --- SP7: local watcher task sync -------------------------------------------------

WATCHER_SIX = ["12:30", "12:50", "13:10", "20:30", "20:50", "21:10"]


def test_apply_schedule_autosync_on_registers_watcher_times(qtbot, _cfg, _register_calls):
    _cfg.update({"local_task_autosync": True, "local_task_offsets": "30,50,70"})
    panel = _panel(qtbot, _FakeTarget(), confirm=True)
    panel.set_times(["12:00", "20:00"])
    panel.apply_schedule()
    assert _register_calls == [WATCHER_SIX]              # one register, exact times
    assert _cfg.get("vm_schedule_times") == ["12:00", "20:00"]  # pushed times persisted


def test_apply_schedule_autosync_off_never_registers(qtbot, _cfg, _register_calls):
    panel = _panel(qtbot, _FakeTarget(), confirm=True)   # autosync defaults off
    panel.set_times(["12:00", "20:00"])
    panel.apply_schedule()
    assert _register_calls == []
    assert _cfg.get("vm_schedule_times") == ["12:00", "20:00"]  # still recorded


def test_apply_schedule_failed_push_saves_nothing(qtbot, _cfg, _register_calls):
    _cfg["local_task_autosync"] = True
    panel = VMPanel(
        runner=lambda cmd: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        confirm=lambda *a: True, notify=lambda *a: None,
        target_factory=lambda: _FakeTarget(),
    )
    qtbot.addWidget(panel)
    panel.set_times(["12:00", "20:00"])
    panel.apply_schedule()
    assert _register_calls == [] and "vm_schedule_times" not in _cfg


def test_sync_local_task_uses_combo_times(qtbot, _cfg, _register_calls):
    _cfg["local_task_offsets"] = "30,50,70"
    panel = _panel(qtbot, _FakeTarget(), confirm=True)
    panel.set_times(["12:00", "20:00"])
    panel.sync_local_task()                              # no VM push involved
    assert _register_calls == [WATCHER_SIX]
    assert panel._cmds == []                             # nothing ran on the VM


def test_sync_local_task_declined_confirm_no_call(qtbot, _register_calls):
    panel = _panel(qtbot, _FakeTarget(), confirm=False)
    panel.set_times(["12:00", "20:00"])
    panel.sync_local_task()
    assert _register_calls == []


def test_restore_default_registers_defaults_and_flips_autosync_off(qtbot, _cfg,
                                                                   _register_calls):
    _cfg["local_task_autosync"] = True
    panel = _panel(qtbot, _FakeTarget(), confirm=True)
    panel.restore_local_task()
    assert _register_calls == [local_task.DEFAULT_TIMES]
    assert _cfg.get("local_task_autosync") is False      # the escape hatch kills autosync


def test_restore_default_declined_confirm_no_call(qtbot, _cfg, _register_calls):
    _cfg["local_task_autosync"] = True
    panel = _panel(qtbot, _FakeTarget(), confirm=False)
    panel.restore_local_task()
    assert _register_calls == []
    assert _cfg.get("local_task_autosync") is True       # toggle untouched


def test_panel_seeds_times_from_saved_vm_schedule(qtbot, _cfg):
    _cfg["vm_schedule_times"] = ["12:00", "20:00"]
    panel = _panel(qtbot, _FakeTarget())
    assert panel._times() == ["12:00", "20:00"]


def test_panel_seeds_defaults_when_nothing_saved(qtbot):
    panel = _panel(qtbot, _FakeTarget())
    assert panel._times() == ["10:00", "19:00"]
