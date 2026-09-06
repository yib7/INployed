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


def test_push_config_skip_confirm_runs_scp_without_confirming(qtbot):
    # SP3: a save-time auto-push bypasses the confirm but still scp's every file.
    confirms, cmds = [], []
    panel = VMPanel(
        runner=lambda cmd: cmds.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        confirm=lambda title, msg: confirms.append((title, msg)) or True,
        notify=lambda title, msg: None,
        target_factory=lambda: _FakeTarget(),
    )
    qtbot.addWidget(panel)
    panel.push_config(skip_confirm=True)
    scps = [c for c in cmds if c[:2] == ["gcloud", "scp"]]
    assert len(scps) == len(vm_sync.TARGET_REMOTE_FILE)   # every file still pushed
    assert confirms == []                                 # confirm was skipped


def test_push_config_default_still_confirms(qtbot):
    # The button path (no arg) must keep confirming before it pushes.
    confirms = []
    panel = VMPanel(
        runner=lambda cmd: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        confirm=lambda title, msg: confirms.append((title, msg)) or True,
        notify=lambda title, msg: None,
        target_factory=lambda: _FakeTarget(),
    )
    qtbot.addWidget(panel)
    panel.push_config()
    assert confirms                                       # default path confirmed


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


# --- credentials section --------------------------------------------------
# Setting the VM's API keys used to mean an ssh session and a hand-written sed.
# These guard the two things that make the GUI version safe: the value never
# reaches the argv or a popup, and it does not linger in the widget afterwards.

def _secret_panel(qtbot, target=None, confirm=True, result=None, boom=None):
    calls, notes = [], []

    def setter(t, name, value):
        if boom:
            raise boom
        calls.append((name, value))
        return result if result is not None else types.SimpleNamespace(
            returncode=0, stdout="SECRET_SET: " + name, stderr="")

    panel = VMPanel(
        runner=lambda cmd: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        confirm=lambda title, msg: confirm,
        notify=lambda title, msg: notes.append((title, msg)),
        target_factory=lambda: target or _FakeTarget(),
        secret_setter=setter,
    )
    qtbot.addWidget(panel)
    panel._calls, panel._notes = calls, notes
    return panel


def _choose(panel, name):
    idx = panel.secret_name.findData(name)
    assert idx >= 0, f"{name} missing from the credential picker"
    panel.secret_name.setCurrentIndex(idx)


def test_credential_picker_offers_the_managed_secrets(qtbot):
    panel = _secret_panel(qtbot)
    offered = {panel.secret_name.itemData(i) for i in range(panel.secret_name.count())}
    assert offered == set(vm_sync.MANAGED_SECRETS)


def test_secret_field_is_masked(qtbot):
    """A shoulder-surfable token field is the whole reason this is a password box."""
    from PySide6 import QtWidgets
    panel = _secret_panel(qtbot)
    assert panel.secret_value.echoMode() == QtWidgets.QLineEdit.EchoMode.Password


def test_set_secret_passes_the_value_to_the_setter(qtbot):
    panel = _secret_panel(qtbot)
    _choose(panel, "BRIGHT_DATA_API_TOKEN")
    panel.secret_value.setText("tok-abc123")
    panel.set_secret()
    assert panel._calls == [("BRIGHT_DATA_API_TOKEN", "tok-abc123")]


def test_set_secret_clears_the_field_after_success(qtbot):
    panel = _secret_panel(qtbot)
    _choose(panel, "BRIGHT_DATA_API_TOKEN")
    panel.secret_value.setText("tok-abc123")
    panel.set_secret()
    assert panel.secret_value.text() == ""


def test_set_secret_keeps_the_field_after_a_failure(qtbot):
    """So a typo or a dropped connection doesn't force a full re-paste."""
    panel = _secret_panel(qtbot, result=types.SimpleNamespace(
        returncode=1, stdout="", stderr="ssh: connect failed"))
    _choose(panel, "BRIGHT_DATA_API_TOKEN")
    panel.secret_value.setText("tok-abc123")
    panel.set_secret()
    assert panel.secret_value.text() == "tok-abc123"


def test_set_secret_NEVER_shows_the_value_in_a_popup(qtbot):
    panel = _secret_panel(qtbot)
    _choose(panel, "BRIGHT_DATA_API_TOKEN")
    panel.secret_value.setText("tok-abc123")
    panel.set_secret()
    assert panel._notes, "expected a confirmation popup"
    for _title, msg in panel._notes:
        assert "tok-abc123" not in msg


def test_set_secret_refuses_an_empty_value(qtbot):
    panel = _secret_panel(qtbot)
    _choose(panel, "BRIGHT_DATA_API_TOKEN")
    panel.secret_value.setText("   ")
    panel.set_secret()
    assert panel._calls == []


def test_set_secret_refuses_a_shell_unsafe_value(qtbot):
    """The secrets file is sourced by bash; `$(...)` in it would execute."""
    panel = _secret_panel(qtbot)
    _choose(panel, "BRIGHT_DATA_API_TOKEN")
    panel.secret_value.setText("$(rm -rf ~)")
    panel.set_secret()
    assert panel._calls == []
    assert panel._notes and "letters" in panel._notes[-1][1]


def test_set_secret_does_nothing_when_the_confirm_is_declined(qtbot):
    panel = _secret_panel(qtbot, confirm=False)
    _choose(panel, "BRIGHT_DATA_API_TOKEN")
    panel.secret_value.setText("tok-abc123")
    panel.set_secret()
    assert panel._calls == []
    assert panel.secret_value.text() == "tok-abc123"


def test_set_secret_requires_a_configured_vm(qtbot):
    panel = _secret_panel(qtbot, target=_FakeTarget(configured=False))
    _choose(panel, "BRIGHT_DATA_API_TOKEN")
    panel.secret_value.setText("tok-abc123")
    panel.set_secret()
    assert panel._calls == []


def test_the_connected_target_label_is_plain_text(qtbot):
    """QLabel defaults to AutoText, which sniffs the START of the string for
    markup. This is the only label in the panel whose text starts with
    interpolated values (VM_USER / VM_INSTANCE / VM_ZONE from the .env), so it is
    the only one AutoText could misread. Cycle 7 found five labels rendering
    scraped job titles as HTML this way; this pins the new one shut."""
    from PySide6 import QtCore
    panel = _panel(qtbot, _FakeTarget())
    assert panel.status_label.textFormat() == QtCore.Qt.TextFormat.PlainText


def test_the_vm_result_messages_break_before_gcloud_output(qtbot):
    """The other half of the same class, and the reason the message boxes are safe
    without a format override. QMessageBox is AutoText too, but Qt's rich-text
    sniffer gives up at the first NEWLINE (measured, not assumed -- the obvious
    "it only looks at the start" story is wrong in a way that matters), so putting
    gcloud's output after a blank line keeps markup coming back from the VM out of
    the window the sniffer actually reads."""
    panel = _panel(qtbot, _FakeTarget())
    panel._runner = lambda cmd: types.SimpleNamespace(
        returncode=0, stdout="<b>ready</b>", stderr="")
    ok, text = panel._run_result(["gcloud", "whatever"])
    assert ok and text.startswith("Done.")
    panel._runner = lambda cmd: types.SimpleNamespace(
        returncode=1, stdout="", stderr="<img src=x>boom")
    ok, text = panel._run_result(["gcloud", "whatever"])
    assert not ok and text.startswith("Failed.")


def test_a_gcloud_timeout_does_not_dump_the_whole_script_into_the_dialog(qtbot):
    """subprocess.TimeoutExpired renders its whole argv in str(), which for the
    installer is the entire generated bash script plus the staged file's path.
    No credential (the value is never in argv), but a wall of internal detail
    where one line naming the fix belongs."""
    import subprocess
    panel = _secret_panel(qtbot)
    _choose(panel, "BRIGHT_DATA_API_TOKEN")
    argv = ["gcloud", "compute", "ssh", "--command=set -e\nIN=$HOME/.inployed_secret_in"]

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(argv, 300)

    panel._secret_setter = boom
    panel.secret_value.setText("tok-abc123")
    panel.set_secret()

    msg = panel._notes[-1][1]
    assert "TimeoutExpired" in msg
    assert "--command=" not in msg, "the dialog printed the generated remote script"
    assert ".inployed_secret_in" not in msg
    assert "gcloud login" in msg


def test_a_failure_detail_starts_on_its_own_line(qtbot):
    """Qt's rich-text sniffer gives up at the first newline. A one-line message
    ending in interpolated text is the one shape here that renders as HTML, so
    the detail always goes after a blank line."""
    panel = _secret_panel(qtbot)
    _choose(panel, "BRIGHT_DATA_API_TOKEN")

    def boom(*a, **k):
        raise ValueError("<b>nope</b>")

    panel._secret_setter = boom
    panel.secret_value.setText("tok-abc123")
    panel.set_secret()
    msg = panel._notes[-1][1]
    assert msg.startswith("Could not set the ")
    assert "\n\n<b>nope</b>" in msg


def test_a_leftover_staging_dir_is_named_in_the_dialog(qtbot, monkeypatch):
    """The only other notice is a print() into a console pythonw does not have."""
    from pathlib import Path
    panel = _secret_panel(qtbot)
    _choose(panel, "BRIGHT_DATA_API_TOKEN")
    monkeypatch.setattr(vm_sync, "leftover_staging_dirs",
                        lambda: [Path("C:/Temp/inployed-secret-xyz")])
    panel.secret_value.setText("tok-abc123")
    panel.set_secret()
    msg = panel._notes[-1][1]
    assert "inployed-secret-xyz" in msg
    assert "Delete by hand" in msg


# --- layout at other interface scales (Phase 7) -----------------------------


def test_crontab_preview_fits_four_lines_at_every_scale(qtbot):
    """A flat 90px measured at 100% swallows the END marker at 125% and the line
    above it at 150%, leaving the preview clipping the preview."""
    from PySide6 import QtWidgets
    from qt import theme
    app = QtWidgets.QApplication.instance()
    panel = _panel(qtbot, _FakeTarget())
    panel.set_times(["09:00", "18:00"])       # -> a four-line crontab
    try:
        for scale in (0.75, 1.0, 1.25, 1.5):
            theme.set_scale(app, scale)
            needed = panel.preview.fontMetrics().lineSpacing() * 4
            assert panel.preview.height() >= needed, scale
    finally:
        theme.set_scale(app, 1.0)


def test_credentials_row_keeps_the_action_next_to_the_field(qtbot):
    """picker -> field -> Set on VM, then a stretch, like every other action row
    in this panel. Unbounded, the field ate the width and stranded the button
    against the far edge of a maximised window."""
    panel = _panel(qtbot, _FakeTarget())
    row = None
    lay = panel.layout()
    for i in range(lay.count()):
        item = lay.itemAt(i).layout()
        if item is not None and item.indexOf(panel.secret_btn) >= 0:
            row = item
    assert row is not None
    assert row.itemAt(row.count() - 1).spacerItem() is not None   # trailing stretch
    assert panel.secret_value.maximumWidth() < 16777215           # bounded
    # ...and the picker carries no cap of its own, so it can never clip its label
    assert panel.secret_name.maximumWidth() == 16777215
    assert panel.secret_name.sizeHint().width() >= panel.secret_name.fontMetrics(
    ).horizontalAdvance("Bright Data token")
