"""SP6: the Qt settings form — widget-by-type, secret masking, save/revert, VM toggle."""
import json
from datetime import datetime

import envfile
import pytest
import settings
import settings_archive
from PySide6 import QtCore, QtGui, QtWidgets
from qt import settings_tab as st
from qt.settings_tab import SettingsForm


def _targets(tmp_path):
    return {
        "config": tmp_path / "config.json",
        "search": tmp_path / "search_config.json",
        "scoring": tmp_path / "scoring_config.json",
        "env": tmp_path / ".env",
    }


def test_renders_widgets_by_type(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    # secret -> masked line by default (restyle 3f, locked user decision) with
    # a Hide toggle that starts CHECKED; unticking it reveals the saved value.
    assert "GEMINI_API_KEYS" in form._secret_edits
    assert form._secret_edits["GEMINI_API_KEYS"].echoMode() == QtWidgets.QLineEdit.EchoMode.Password
    assert "GEMINI_API_KEYS" in form._secret_hides
    assert form._secret_hides["GEMINI_API_KEYS"].isChecked()
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


def _form(tmp_path, **kw):
    """A SettingsForm that touches nothing outside tmp_path.

    Without `collapsed_sections`/`save_collapsed` the constructor falls through to
    `jobsdata.load_collapsed_sections()`, which reads the DEVELOPER's real
    local/config.json — so a test's widget visibility would depend on which
    sections that person happens to have folded.
    """
    return SettingsForm(targets=_targets(tmp_path), collapsed_sections=[],
                        save_collapsed=lambda s: None, **kw)


def test_editable_choice_field_has_popup_filter(qtbot, tmp_path):
    form = _form(tmp_path)
    qtbot.addWidget(form)
    # stage1_model is an editable_choice -> a popup filter was attached to it.
    # Looked up BY KEY, not by "first editable combo in the tree": conditional
    # visibility and the advanced toggle both reorder/hide widgets, and a
    # positional probe would then silently assert about a different field.
    combo = form._widgets["stage1_model"]
    assert isinstance(combo, QtWidgets.QComboBox) and combo.isEditable()
    assert combo.findChild(st._PopupOnClick) is not None


# --- cycle 18 P0: widget / form-row registries -----------------------------------

def test_widget_registry_covers_every_schema_key(qtbot, tmp_path):
    """Every Field must be addressable by key. This is the guard that stops a
    newly added setting from shipping unregistered — conditional visibility,
    the advanced toggle, dirty markers and search all address fields by key."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    missing = [f.key for f in settings.SETTINGS_SCHEMA if f.key not in form._widgets]
    assert missing == []
    assert all(isinstance(w, QtWidgets.QWidget) for w in form._widgets.values())


def test_every_field_registers_the_widget_in_its_own_row(qtbot, tmp_path):
    """The registered widget must live in the field's OWN first row.

    Coverage alone can't catch a mis-registration (a copy-pasted
    `self._widgets[f.key] = <some other widget>`, or a future composite type that
    registers its wrapper). Walking every field also makes this the guard on the
    stale-index hazard: an `insertRow` anywhere would shift the stored indices and
    land some field's lookup on a neighbour's row.
    """
    form = _form(tmp_path)
    qtbot.addWidget(form)
    role = QtWidgets.QFormLayout.ItemRole.FieldRole
    for f in settings.SETTINGS_SCHEMA:
        rows = form._rows[f.key]
        if not rows:
            continue                       # vm_enabled: the section gate, no form row
        layout, row = rows[0]
        cell = layout.itemAt(row, role).widget()
        w = form._widgets[f.key]
        assert w is cell or cell.isAncestorOf(w), f"{f.key} is registered outside its own row"


def test_row_registry_holds_both_rows_for_every_field(qtbot, tmp_path):
    """Each field renders TWO form rows — label+storage chip, then the muted help
    paragraph. Both are recorded so hiding a field takes its explanation with it.
    `vm_enabled` is the one exception: it is the VM section's master checkbox,
    added straight to the section body rather than to a QFormLayout. (Every field
    currently sets `help`, so the one-row arm below is defensive, not exercised.)"""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    for f in settings.SETTINGS_SCHEMA:
        assert f.key in form._rows, f"{f.key} has no row entry"
        expected = 0 if f.key == "vm_enabled" else (2 if f.help else 1)
        assert len(form._rows[f.key]) == expected, f"{f.key} rows"
    # and the recorded (form, index) pairs really point at that field's rows
    form_layout, label_row = form._rows["min_score"][0]
    _, help_row = form._rows["min_score"][1]
    assert help_row == label_row + 1
    label_cell = form_layout.itemAt(label_row, QtWidgets.QFormLayout.ItemRole.LabelRole).widget()
    assert "Min score" in label_cell.findChild(QtWidgets.QLabel).text()
    help_widget = form_layout.itemAt(help_row, QtWidgets.QFormLayout.ItemRole.FieldRole).widget()
    assert help_widget.property("muted") is True


def test_composite_cells_register_the_inner_control(qtbot, tmp_path):
    """The four composite cells wrap their real control in a container. The
    registry must hold the INNER control — dirty tracking and error styling
    attach to the thing the user types in, not to a layout box."""
    form = _form(tmp_path)
    qtbot.addWidget(form)

    slider = form._widgets["followup_days"]          # _slider_widget: slider + readout
    assert isinstance(slider, QtWidgets.QSlider)
    slider.setValue(9)
    assert form._getters["followup_days"]() == "9"   # the registered widget IS the value

    path = form._widgets["gdrive_root"]              # _path_widget: line edit + Browse
    assert isinstance(path, QtWidgets.QLineEdit)
    path.setText("D:/jobs")
    assert form._getters["gdrive_root"]() == "D:/jobs"

    secret = form._widgets["GEMINI_API_KEYS"]        # _secret_widget: line edit + Hide
    assert isinstance(secret, QtWidgets.QLineEdit)
    assert secret is form._secret_edits["GEMINI_API_KEYS"]

    # multichoice is the documented exception: N checkboxes, no single inner
    # control, so the cell is registered and the per-choice boxes stay in _multi.
    multi = form._widgets["remote_types"]
    assert type(multi) is QtWidgets.QWidget
    boxes = set(form._multi["remote_types"].values())
    assert boxes and boxes <= set(multi.findChildren(QtWidgets.QCheckBox))


def test_set_field_visible_hides_and_restores_both_rows(qtbot, tmp_path):
    form = _form(tmp_path)
    qtbot.addWidget(form)
    rows = form._rows["min_score"]
    assert len(rows) == 2
    assert all(f.isRowVisible(r) for f, r in rows)

    form._set_field_visible("min_score", False)
    assert not any(f.isRowVisible(r) for f, r in rows)   # help row hidden too

    form._set_field_visible("min_score", True)
    assert all(f.isRowVisible(r) for f, r in rows)


def test_set_field_visible_is_a_noop_for_a_field_with_no_form_rows(qtbot, tmp_path):
    # vm_enabled is the VM section's master checkbox, not a QFormLayout row, so
    # it registers zero rows: the call must be a harmless no-op, not a KeyError
    # and not a hidden gate the user can no longer untick.
    form = _form(tmp_path)
    qtbot.addWidget(form)
    check = form._widgets["vm_enabled"]
    assert form._rows["vm_enabled"] == []
    before = check.isHidden()
    form._set_field_visible("vm_enabled", False)
    assert check.isHidden() is before


def test_set_field_visible_raises_on_an_unregistered_key(qtbot, tmp_path):
    # Every schema key is registered, so a miss is a typo or a key deleted from
    # the schema — surfacing it beats silently hiding nothing.
    form = _form(tmp_path)
    qtbot.addWidget(form)
    with pytest.raises(KeyError):
        form._set_field_visible("no_such_setting", False)


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


def test_secret_hide_toggle_reveals_and_remasks_box(qtbot, tmp_path):
    # Masked by default (restyle 3f): Password echo until Hide is unticked.
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    edit = form._secret_edits["GEMINI_API_KEYS"]
    assert edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
    form._secret_hides["GEMINI_API_KEYS"].setChecked(False)
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


# --- SP3: prompt to push config to the VM after a VM-read setting changes -------

class _StubVMPanel:
    """Records push_config calls so a test can assert the save-time auto-push."""

    def __init__(self):
        self.pushed = []

    def push_config(self, skip_confirm=False):
        self.pushed.append(skip_confirm)


def _stub_question(monkeypatch, answer):
    """Stub QMessageBox.question to a fixed answer; return the recorded calls."""
    calls = []

    def q(parent, title, text, *a, **k):
        calls.append((title, text))
        return answer

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(q))
    return calls


def test_no_vm_push_prompt_when_vm_disabled(qtbot, tmp_path, monkeypatch):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    _quiet_info(monkeypatch)
    calls = _stub_question(monkeypatch, QtWidgets.QMessageBox.StandardButton.Yes)
    form._setters["drop_easy_apply"](True)            # a VM-read setting changed...
    # ...but vm_enabled defaults off, so no push prompt appears.
    assert form.save() is True
    assert calls == []


def test_vm_push_prompt_yes_calls_panel_push(qtbot, tmp_path, monkeypatch):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    stub = _StubVMPanel()
    form._vm_panel = stub
    _quiet_info(monkeypatch)
    calls = _stub_question(monkeypatch, QtWidgets.QMessageBox.StandardButton.Yes)
    form._setters["vm_enabled"](True)
    form._setters["drop_easy_apply"](True)
    assert form.save() is True
    assert calls                                       # the push prompt was shown
    assert any("score_jobs.py" in text for _, text in calls)  # drop_easy_apply reminder
    assert stub.pushed == [True]                       # pushed with skip_confirm=True


def test_vm_push_prompt_no_does_not_push(qtbot, tmp_path, monkeypatch):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    stub = _StubVMPanel()
    form._vm_panel = stub
    _quiet_info(monkeypatch)
    calls = _stub_question(monkeypatch, QtWidgets.QMessageBox.StandardButton.No)
    form._setters["vm_enabled"](True)
    form._setters["drop_easy_apply"](True)
    assert form.save() is True
    assert calls                                       # prompt still shown
    assert stub.pushed == []                           # ...but declined, so no push


def test_no_vm_push_prompt_for_non_vm_field_change(qtbot, tmp_path, monkeypatch):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    stub = _StubVMPanel()
    form._vm_panel = stub
    _quiet_info(monkeypatch)
    calls = _stub_question(monkeypatch, QtWidgets.QMessageBox.StandardButton.Yes)
    form._setters["vm_enabled"](True)                  # VM on...
    form._setters["min_score"](5)                      # ...but only a config-target field changed
    assert form.save() is True
    assert calls == []
    assert stub.pushed == []


def test_vm_push_prompt_yes_without_panel_shows_info(qtbot, tmp_path, monkeypatch):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    form._vm_panel = None                              # no VM ops panel mounted
    infos = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda parent, title, text, *a, **k: infos.append((title, text))))
    _stub_question(monkeypatch, QtWidgets.QMessageBox.StandardButton.Yes)
    form._setters["vm_enabled"](True)
    form._setters["drop_easy_apply"](True)
    assert form.save() is True
    # the saved-summary info + the "use the Push button" info both fired
    assert any("Push config to VM" in title for title, _ in infos)


# --- collapsible sections (cycle 16 SP4) ----------------------------------------

def test_collapsible_section_toggles_body(qtbot):
    from qt.widgets import CollapsibleSection
    toggles = []
    sec = CollapsibleSection("Demo", on_toggled=toggles.append)
    qtbot.addWidget(sec)
    sec.add_widget(QtWidgets.QLabel("x"))
    assert not sec.is_collapsed()
    sec._on_header_clicked()
    assert sec.is_collapsed() and toggles == [True]
    sec._on_header_clicked()
    assert not sec.is_collapsed() and toggles == [True, False]


def test_collapsible_section_subtitle_stays_visible_when_collapsed(qtbot):
    # Cycle 17 SP3: a tagline next to the header tells you what a collapsed section is for.
    from qt.widgets import CollapsibleSection
    sec = CollapsibleSection("Demo", subtitle="short hint", collapsed=True)
    qtbot.addWidget(sec)
    assert sec.is_collapsed()
    assert sec._subtitle.text() == "short hint"
    assert not sec._subtitle.isHidden()      # tagline visible even when collapsed


def test_every_settings_section_has_a_tagline(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path))
    qtbot.addWidget(form)
    for section, sec in form._section_widgets.items():
        assert sec._subtitle.text().strip(), f"{section} is missing a tagline"


def test_sections_are_collapsible_and_persist(qtbot, tmp_path):
    saved = {}
    form = SettingsForm(targets=_targets(tmp_path), collapsed_sections=[],
                        save_collapsed=lambda s: saved.__setitem__("v", list(s)))
    qtbot.addWidget(form)
    sec = form._section_widgets["Scraper"]
    assert not sec.is_collapsed()                 # default expanded
    sec._on_header_clicked()                      # collapse it
    assert sec.is_collapsed()
    assert "Scraper" in saved["v"]                # persisted via the callback

    form2 = SettingsForm(targets=_targets(tmp_path), collapsed_sections=saved["v"])
    qtbot.addWidget(form2)
    assert form2._section_widgets["Scraper"].is_collapsed()       # restored collapsed
    assert not form2._section_widgets["Dashboard"].is_collapsed()  # others expanded


def test_collapsed_section_still_saves_its_fields(qtbot, tmp_path, monkeypatch):
    targets = _targets(tmp_path)
    form = SettingsForm(targets=targets, collapsed_sections=["Dashboard"])
    qtbot.addWidget(form)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", staticmethod(lambda *a, **k: None))
    form._setters["min_score"](5)                 # field in a collapsed section
    assert form.save() is True
    assert settings.load(targets)["min_score"] == 5


def test_load_save_collapsed_sections_roundtrip(tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    assert jobsdata.load_collapsed_sections() == []
    jobsdata.save_collapsed_sections(["Scraper", "Scoring"])
    assert jobsdata.load_collapsed_sections() == ["Scraper", "Scoring"]


def test_load_save_ui_scale_pct_roundtrip_and_clamp(tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    assert jobsdata.load_ui_scale_pct() == 100          # default
    jobsdata.save_ui_scale_pct(130)
    assert jobsdata.load_ui_scale_pct() == 130
    jobsdata.save_ui_scale_pct(9999)                    # clamped on save (150 ceiling)
    assert jobsdata.load_ui_scale_pct() == 150
    jobsdata.save_ui_scale_pct(10)
    assert jobsdata.load_ui_scale_pct() == 75           # 75 floor


def test_load_save_resume_layout_enabled_roundtrip(tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    assert jobsdata.load_resume_layout_enabled() is True   # default on (absent = enabled)
    jobsdata.save_resume_layout_enabled(False)
    assert jobsdata.load_resume_layout_enabled() is False
    jobsdata.save_resume_layout_enabled(True)
    assert jobsdata.load_resume_layout_enabled() is True


def test_load_save_layout_maps_roundtrip(tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    assert jobsdata.load_resume_layout() == {}
    assert jobsdata.load_project_layout() == {}
    jobsdata.save_resume_layout({"Example Corp": {"line_targets": [2, 1]}})
    jobsdata.save_project_layout({"ProjOne": {"line_targets": [3, 2, 1]}})
    assert jobsdata.load_resume_layout() == {"Example Corp": {"line_targets": [2, 1]}}
    assert jobsdata.load_project_layout() == {"ProjOne": {"line_targets": [3, 2, 1]}}


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
    form._setters["archive_mode"]("Off")     # was the archive_enabled checkbox
    form._setters["min_score"](5)
    assert form.save() is True
    assert settings_archive.list_snapshots(targets) == []


def test_settings_history_renders_one_dropdown(qtbot, tmp_path):
    """The four-key retention DSL merged into one choice: the section must render
    a single control, not a checkbox + mode + two counts."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    keys = [f.key for f in settings.SETTINGS_SCHEMA if f.section == "Settings history"]
    assert keys == ["archive_mode"]
    assert isinstance(form._widgets["archive_mode"], QtWidgets.QComboBox)
    assert [form._widgets["archive_mode"].itemText(i)
            for i in range(form._widgets["archive_mode"].count())] == [
        "Keep everything", "Keep newest 20", "Keep newest 100", "Off"]


def test_a_legacy_archive_config_opens_reading_keep_newest_20(qtbot, tmp_path):
    """End to end through the real form: the repo owner's saved `keep: 10` shows
    as the rounded-UP option, so nobody's snapshots start disappearing."""
    targets = _targets(tmp_path)
    targets["config"].write_text(
        json.dumps({"archive_enabled": True, "archive_prune_mode": "Keep newest N",
                    "archive_prune_keep": 10}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._getters["archive_mode"]() == "Keep newest 20"


def test_an_unrecognised_stored_archive_mode_falls_back_to_the_default(qtbot, tmp_path):
    """Why "Keep everything" is choices[0] and not "Off": QComboBox falls back to
    the FIRST item when a stored value matches none of them, so a hand-edited typo
    must land on the harmless default rather than silently stopping snapshots."""
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({"archive_mode": "Keep newest 7"}),
                                 encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._getters["archive_mode"]() == "Keep everything"


def test_save_forwards_the_archive_mode_to_prune(qtbot, tmp_path, monkeypatch):
    """The count now rides in the mode string, so Save hands `prune` the whole mode
    and no count keyword. (What that mode then DELETES is
    test_prune_honours_a_counted_archive_mode_without_the_keep_kwarg's job.)"""
    targets = _targets(tmp_path)
    form = SettingsForm(targets=targets)
    qtbot.addWidget(form)
    _quiet_info(monkeypatch)
    pruned = []
    monkeypatch.setattr(settings_archive, "prune",
                        lambda mode, **kw: pruned.append((mode, kw)) or [])
    form._setters["archive_mode"]("Keep newest 20")
    form._setters["min_score"](5)
    assert form.save() is True
    assert [mode for mode, _ in pruned] == ["Keep newest 20"]
    assert "keep" not in pruned[0][1] and "days" not in pruned[0][1]
    assert len(settings_archive.list_snapshots(targets)) == 1


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


# --- cycle 18 P3: show_if conditional visibility --------------------------------

def _rows_visible(form, key):
    """Is EVERY form row this field occupies on screen? (label row + help row)"""
    return all(layout.isRowVisible(row) for layout, row in form._rows[key])


def test_flipping_the_provider_swaps_which_model_pair_is_visible(qtbot, tmp_path):
    """Both Claude pickers sit on screen at the shipped defaults today, advertising
    machinery the default configuration cannot run."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert _rows_visible(form, "stage1_model") and _rows_visible(form, "stage2_model")
    assert not _rows_visible(form, "stage1_model_claude")
    assert not _rows_visible(form, "stage2_model_claude")

    form._widgets["provider"].setCurrentText("claude")
    assert not _rows_visible(form, "stage1_model")
    assert not _rows_visible(form, "stage2_model")
    assert _rows_visible(form, "stage1_model_claude")
    assert _rows_visible(form, "stage2_model_claude")

    form._widgets["provider"].setCurrentText("gemini")
    assert _rows_visible(form, "stage1_model") and _rows_visible(form, "stage2_model")
    assert not _rows_visible(form, "stage1_model_claude")


def test_gate_signals_are_wired_when_the_gate_renders_after_its_dependent(qtbot, tmp_path):
    """The reason gate signals are connected in a SECOND pass at the end of
    `_build()` rather than as each dependent is built.

    Two gates render after what they gate: `provider` follows `stage1_model` in
    the Scoring section, and `gemini_auth` lives in Engine while it gates
    `RESUME_TAILOR_GEMINI_API_KEY` in Credentials — which SECTION_ORDER renders
    FIRST. Connect at dependent-build time and neither of these flips anything.
    """
    form = _form(tmp_path)
    qtbot.addWidget(form)

    # gate declared after its dependents, same section
    assert _rows_visible(form, "stage1_model")
    form._widgets["provider"].setCurrentText("claude")
    assert not _rows_visible(form, "stage1_model")

    # gate in a LATER section than the field it gates
    assert not _rows_visible(form, "RESUME_TAILOR_GEMINI_API_KEY")
    form._widgets["gemini_auth"].setCurrentText("api_key")
    assert _rows_visible(form, "RESUME_TAILOR_GEMINI_API_KEY")


def test_form_hides_a_gated_field_transitively(qtbot, tmp_path):
    """Through the real widgets: switching the tailor to Claude must take the
    Gemini API-key box with `gemini_auth`, even with 'api_key' selected."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    form._widgets["gemini_auth"].setCurrentText("api_key")
    assert _rows_visible(form, "gemini_auth")
    assert _rows_visible(form, "RESUME_TAILOR_GEMINI_API_KEY")

    form._widgets["tailor_provider"].setCurrentText("claude")
    assert not _rows_visible(form, "gemini_auth")
    assert not _rows_visible(form, "RESUME_TAILOR_GEMINI_API_KEY")   # orphan removed
    assert _rows_visible(form, "RESUME_TAILOR_CLAUDE_MODEL_PRO")
    assert not _rows_visible(form, "RESUME_TAILOR_MODEL_PRO")


def test_hidden_field_still_collects_its_stored_value(qtbot, tmp_path):
    """`collect()` iterates the SCHEMA and reads `self._getters`, so a hidden
    widget still returns what it holds. This is the round-trip guarantee: hiding
    is a rendering decision, never a data one."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    form._setters["stage1_model"]("gemini-9-custom")
    form._widgets["provider"].setCurrentText("claude")       # hides the Gemini pair
    assert not _rows_visible(form, "stage1_model")

    values, errors = form.collect()
    assert errors == {}
    assert values["stage1_model"] == "gemini-9-custom"       # hidden is not dropped
    assert values["provider"] == "claude"


def test_provider_round_trip_does_not_wipe_hidden_model_choices(qtbot, tmp_path, monkeypatch):
    """The test this whole phase has to pass.

    Type a custom Gemini model id, save, switch the scorer to Claude, save,
    switch back, save — and the custom id must still be on disk.

    THREE saves, for the reason mutation testing actually showed rather than the
    one it looks like. `settings.save()` MERGES into the backing file, so a key
    merely OMITTED from `collect()` is not wiped from disk — the on-disk
    assertions alone do not catch a `collect()` "optimised" to iterate visible
    fields. What they do catch is a hidden field whose value is REPLACED (e.g. a
    visibility pass that "tidies" hidden widgets back to their defaults), and
    what save 3 adds over save 2 is the end state the checkpoint asks for: BOTH
    provider's model choices coexisting in one scoring_config.json. The omission
    mutant is caught by the `collect()` assertion below, and independently by
    test_hidden_field_still_collects_its_stored_value.
    """
    targets = _targets(tmp_path)
    _quiet_info(monkeypatch)
    form = _form(tmp_path)
    qtbot.addWidget(form)

    def on_disk():
        return json.loads(targets["scoring"].read_text(encoding="utf-8"))

    form._setters["stage1_model"]("gemini-9-custom")
    form._setters["stage2_model"]("gemini-9-deep")
    assert form.save() is True                                    # save 1
    assert on_disk()["stage1_model"] == "gemini-9-custom"

    form._widgets["provider"].setCurrentText("claude")            # hides both
    form._setters["stage1_model_claude"]("claude-99-custom")
    assert not _rows_visible(form, "stage1_model")
    # The hidden key must be IN the dict Save writes, not merely left undisturbed
    # on disk by the merge — this is the assertion that kills a visible-only collect().
    assert form.collect()[0]["stage1_model"] == "gemini-9-custom"
    assert form.save() is True                                    # save 2
    assert on_disk()["provider"] == "claude"
    assert on_disk()["stage1_model"] == "gemini-9-custom"         # hidden, still written
    assert on_disk()["stage2_model"] == "gemini-9-deep"

    form._widgets["provider"].setCurrentText("gemini")            # back again
    assert form.save() is True                                    # save 3
    both = on_disk()
    assert both["stage1_model"] == "gemini-9-custom"
    assert both["stage2_model"] == "gemini-9-deep"
    assert both["stage1_model_claude"] == "claude-99-custom"      # the other pair too
    # ...and the widget shows it again now that it is back on screen
    assert _rows_visible(form, "stage1_model")
    assert form._getters["stage1_model"]() == "gemini-9-custom"


def _form_opened_on_claude(qtbot, tmp_path):
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({"tailor_provider": "claude"}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert not _rows_visible(form, "gemini_auth")           # opened on claude
    return form


def test_revert_and_restore_defaults_re_evaluate_visibility(qtbot, tmp_path):
    """Revert, Restore defaults and snapshot-load all go through `_repopulate`."""
    form = _form_opened_on_claude(qtbot, tmp_path)

    form.restore_defaults()                                 # tailor_provider -> gemini
    assert _rows_visible(form, "gemini_auth")
    assert _rows_visible(form, "RESUME_TAILOR_MODEL_PRO")
    assert not _rows_visible(form, "RESUME_TAILOR_CLAUDE_MODEL_PRO")

    form.revert()                                           # back to the stored claude
    assert not _rows_visible(form, "gemini_auth")
    assert _rows_visible(form, "RESUME_TAILOR_CLAUDE_MODEL_PRO")


def test_repopulate_re_evaluates_visibility_without_relying_on_setter_signals(qtbot, tmp_path):
    """`_repopulate` must re-render visibility ITSELF, not get there by luck.

    Every gate is a QComboBox today, and `_set_combo` emits `currentTextChanged`,
    so the test above passes even with `_apply_field_visibility()` deleted from
    `_repopulate` (verified by mutation). That is a property of the widget type,
    not a contract — a gate rendered by a setter that changes its value silently
    would leave Revert showing a stale form. Block the gate's signals to prove
    `_repopulate` does not depend on them.
    """
    form = _form_opened_on_claude(qtbot, tmp_path)
    gate = form._widgets["tailor_provider"]

    gate.blockSignals(True)
    try:
        form.restore_defaults()                             # -> gemini, no signal emitted
    finally:
        gate.blockSignals(False)
    assert gate.currentText() == "gemini"                   # the value really did change
    assert _rows_visible(form, "gemini_auth")
    assert _rows_visible(form, "RESUME_TAILOR_MODEL_PRO")
    assert not _rows_visible(form, "RESUME_TAILOR_CLAUDE_MODEL_PRO")

    gate.blockSignals(True)
    try:
        form.revert()                                       # back to the stored claude
    finally:
        gate.blockSignals(False)
    assert not _rows_visible(form, "gemini_auth")
    assert _rows_visible(form, "RESUME_TAILOR_CLAUDE_MODEL_PRO")


def test_hidden_rows_survive_a_vm_gate_cycle(qtbot, tmp_path):
    """`show_if` composes with the section gate rather than fighting it: the VM
    master switch must not un-hide a field `show_if` closed, and vice versa."""
    form = SettingsForm(targets=_targets(tmp_path), collapsed_sections=[],
                        save_collapsed=lambda s: None,
                        vm_panel_factory=lambda parent: QtWidgets.QLabel("vm", parent))
    qtbot.addWidget(form)
    form._widgets["provider"].setCurrentText("claude")
    assert not _rows_visible(form, "stage1_model")
    form._setters["vm_enabled"](True)
    assert not _rows_visible(form, "stage1_model")
    form._setters["vm_enabled"](False)
    assert not _rows_visible(form, "stage1_model")


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
