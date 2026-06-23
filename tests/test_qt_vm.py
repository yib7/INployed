"""SP6: the Qt VM panel — crontab preview + confirm/refuse on the gcloud actions (mocked)."""
import types

from qt.vm_panel import VMPanel
import vm_sync


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
