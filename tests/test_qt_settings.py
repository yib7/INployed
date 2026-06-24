"""SP6: the Qt settings form — widget-by-type, secret masking, save/revert, VM toggle."""
from datetime import datetime

import envfile
import settings
import settings_archive
from PySide6 import QtCore, QtGui, QtWidgets
from qt import settings_tab as st
from qt.settings_tab import SettingsForm, build_config_window


def _targets(tmp_path):
    return {
        "config": tmp_path / "config.json",
        "search": tmp_path / "search_config.json",
        "scoring": tmp_path / "scoring_config.json",
        "apply": tmp_path / "apply_config.json",
        "env": tmp_path / ".env",
    }


def test_renders_widgets_by_type(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    # secret -> visible line (value read from the local .env) with a Hide toggle
    assert "GEMINI_API_KEYS" in form._secret_edits
    assert form._secret_edits["GEMINI_API_KEYS"].echoMode() == QtWidgets.QLineEdit.EchoMode.Normal
    assert "GEMINI_API_KEYS" in form._secret_hides
    # list + multichoice rendered into their containers
    assert "keywords" in form._lists
    assert "remote_types" in form._multi
    # a scalar getter exists for a choice field
    assert "time_range" in form._getters


def test_editable_combo_opens_popup_on_click(qtbot, monkeypatch):
    # Editable model selectors must drop down when their text field is clicked,
    # not just sit there looking like a text box.
    combo = QtWidgets.QComboBox()
    combo.setEditable(True)
    combo.addItems(["gemini-3.5-flash", "gemini-3.1-pro-preview"])
    qtbot.addWidget(combo)
    filt = st._PopupOnClick(combo)
    combo.lineEdit().installEventFilter(filt)
    opened = []
    monkeypatch.setattr(combo, "showPopup", lambda: opened.append(True))
    press = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonPress, QtCore.QPointF(5, 5), QtCore.QPointF(5, 5),
        QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier)
    assert filt.eventFilter(combo.lineEdit(), press) is True
    assert opened  # clicking the text field opened the dropdown


def test_editable_choice_field_has_popup_filter(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    # stage1_model is an editable_choice -> a popup filter was attached to it
    combo = next(c for c in form.findChildren(QtWidgets.QComboBox) if c.isEditable())
    assert combo.findChild(st._PopupOnClick) is not None


def test_secret_box_shows_saved_value(qtbot, tmp_path):
    targets = _targets(tmp_path)
    envfile.update(targets["env"], {"GEMINI_API_KEYS": "saved-key"})
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    # the saved value is shown straight from the local .env (no need to open it)
    assert form._secret_edits["GEMINI_API_KEYS"].text() == "saved-key"


def test_secret_collect_writes_box_as_is(qtbot, tmp_path):
    targets = _targets(tmp_path)
    envfile.update(targets["env"], {"GEMINI_API_KEYS": "saved-key"})
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    values, _ = form.collect()
    assert values["GEMINI_API_KEYS"] == "saved-key"   # unchanged box -> writes the saved value
    form._secret_edits["GEMINI_API_KEYS"].setText("new-key")
    values, _ = form.collect()
    assert values["GEMINI_API_KEYS"] == "new-key"     # edited -> written
    form._secret_edits["GEMINI_API_KEYS"].clear()
    values, _ = form.collect()
    assert values["GEMINI_API_KEYS"] == ""            # cleared box -> removes the key


def test_secret_hide_toggle_masks_box(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    edit = form._secret_edits["GEMINI_API_KEYS"]
    assert edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Normal
    form._secret_hides["GEMINI_API_KEYS"].setChecked(True)
    assert edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password


def test_changed_summary_hides_secret_value():
    before = {"min_score": 4, "GEMINI_API_KEYS": "old"}
    values = {"min_score": 5, "GEMINI_API_KEYS": "supersecret"}
    out = SettingsForm._changed_summary(before, values)
    assert any("Min score" in s and "4 -> 5" in s for s in out)
    assert any("updated" in s for s in out)
    assert not any("supersecret" in s for s in out)   # secret value never echoed


def test_save_writes_and_calls_on_saved(qtbot, tmp_path, monkeypatch):
    saved = []
    form = SettingsForm(targets=_targets(tmp_path), on_saved=lambda: saved.append(True))
    qtbot.addWidget(form)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    form._setters["min_score"](5)
    assert form.save() is True
    assert settings.load(_targets(tmp_path))["min_score"] == 5
    assert saved


def test_revert_restores_opening_values(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    opening = form._getters["location"]()
    form._setters["location"]("Mars")
    assert form._getters["location"]() == "Mars"
    form.revert()
    assert form._getters["location"]() == opening


def test_restore_defaults(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    form._setters["location"]("Mars")
    form.restore_defaults()
    default = next(f.default for f in settings.SETTINGS_SCHEMA if f.key == "location")
    assert form._getters["location"]() == default


def test_vm_section_collapses_with_toggle(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path),
                        vm_panel_factory=lambda parent: QtWidgets.QLabel("vm", parent))
    qtbot.addWidget(form)
    container = form._collapse["VM (cloud scraper)"]
    assert container.isHidden()                       # vm_enabled defaults off -> hidden
    form._setters["vm_enabled"](True)                 # toggles the gate checkbox
    assert not container.isHidden()                   # section body now visible


def test_revert_resets_vm_panel(qtbot, tmp_path):
    from qt.vm_panel import VMPanel
    form = SettingsForm(targets=_targets(tmp_path),
                        vm_panel_factory=lambda parent: VMPanel(parent=parent))
    qtbot.addWidget(form)
    assert form._vm_panel is not None
    form._vm_panel.set_times(["08:00"])
    assert form._vm_panel._times() == ["08:00"]
    form.revert()
    assert form._vm_panel._times() == ["10:00", "19:00"]  # back to its initial schedule


def test_build_config_window(qtbot, tmp_path):
    win = build_config_window(targets=_targets(tmp_path))
    qtbot.addWidget(win)
    assert win.windowTitle().startswith("Configure")
    assert win.findChild(st.SettingsForm) is not None


# --- settings archive (snapshot / restore) --------------------------------------

def _quiet_info(monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))


def test_save_writes_a_snapshot(qtbot, tmp_path, monkeypatch):
    targets = _targets(tmp_path)
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    _quiet_info(monkeypatch)
    form._setters["min_score"](5)
    assert form.save() is True
    snaps = settings_archive.list_snapshots(targets)
    assert len(snaps) == 1
    assert settings_archive.load_snapshot(snaps[0].path, targets)["min_score"] == 5


def test_save_skips_snapshot_when_archiving_disabled(qtbot, tmp_path, monkeypatch):
    targets = _targets(tmp_path)
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    _quiet_info(monkeypatch)
    form._setters["archive_enabled"](False)
    form._setters["min_score"](5)
    assert form.save() is True
    assert settings_archive.list_snapshots(targets) == []


def test_restore_loads_values_and_shows_secret(qtbot, tmp_path, monkeypatch):
    targets = _targets(tmp_path)
    # Build a snapshot that differs from the live state in a normal field and a secret.
    settings.save({"min_score": 5}, targets)
    envfile.update(targets["env"], {"GEMINI_API_KEYS": "snap-key"})
    snap = settings_archive.snapshot(targets)
    settings.save({"min_score": 2}, targets)
    envfile.update(targets["env"], {"GEMINI_API_KEYS": "live-key"})

    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    form.load_from_snapshot(snap)
    assert form._getters["min_score"]() == "5"                       # value loaded for review
    assert form._secret_edits["GEMINI_API_KEYS"].text() == "snap-key"  # snapshot secret shown

    _quiet_info(monkeypatch)
    assert form.save() is True
    assert envfile.read(targets["env"])["GEMINI_API_KEYS"] == "snap-key"  # restored on Save
    assert settings.load(targets)["min_score"] == 5


def test_archive_dialog_lists_snapshots_without_leaking_secrets(qtbot, tmp_path):
    targets = _targets(tmp_path)
    settings.save({"min_score": 5}, targets)
    envfile.update(targets["env"], {"GEMINI_API_KEYS": "topsecret"})
    settings_archive.snapshot(targets, when=datetime(2026, 6, 23, 10, 0, 0))
    settings_archive.snapshot(targets, when=datetime(2026, 6, 23, 11, 0, 0))

    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    dlg = st.ArchiveDialog(form)
    qtbot.addWidget(dlg)
    assert dlg.listw.count() == 2
    assert "topsecret" not in dlg.preview.toPlainText()    # secret values never previewed
