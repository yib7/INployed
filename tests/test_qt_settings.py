"""SP6: the Qt settings form — widget-by-type, secret masking, save/revert, VM toggle."""
from PySide6 import QtWidgets

import settings
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
    # secret -> masked write-only line
    assert "GEMINI_API_KEYS" in form._secret_edits
    assert form._secret_edits["GEMINI_API_KEYS"].echoMode() == QtWidgets.QLineEdit.EchoMode.Password
    # list + multichoice rendered into their containers
    assert "keywords" in form._lists
    assert "remote_types" in form._multi
    # a scalar getter exists for a choice field
    assert "time_range" in form._getters


def test_secret_collect_blank_keeps_typed_clears(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    values, _ = form.collect()
    assert "GEMINI_API_KEYS" not in values            # blank -> omitted (keeps existing)
    form._secret_edits["GEMINI_API_KEYS"].setText("new-key")
    values, _ = form.collect()
    assert values["GEMINI_API_KEYS"] == "new-key"     # typed -> written
    form._secret_edits["GEMINI_API_KEYS"].clear()
    form._secret_clears["GEMINI_API_KEYS"].setChecked(True)
    values, _ = form.collect()
    assert values["GEMINI_API_KEYS"] == ""            # clear -> explicit unset


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


def test_build_config_window(qtbot, tmp_path):
    win = build_config_window(targets=_targets(tmp_path))
    qtbot.addWidget(win)
    assert win.windowTitle().startswith("Configure")
    assert win.findChild(st.SettingsForm) is not None
