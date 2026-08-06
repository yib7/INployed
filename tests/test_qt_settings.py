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

    Without `collapsed_sections`/`save_collapsed` — and, since P4, without
    `show_advanced`/`save_show_advanced` — the constructor falls through to
    `jobsdata.load_collapsed_sections()` / `load_show_advanced()`, which read the
    DEVELOPER's real local/config.json. A test's widget visibility would then
    depend on which sections that person happens to have folded and whether they
    last left the advanced toggle on.

    `show_advanced` defaults to False here (the shipped default) so a test sees
    what a fresh profile sees; pass `show_advanced=True` to isolate `show_if`
    behaviour on a field that is also advanced.
    """
    kw.setdefault("collapsed_sections", [])
    kw.setdefault("save_collapsed", lambda s: None)
    kw.setdefault("show_advanced", False)
    kw.setdefault("save_show_advanced", lambda v: None)
    return SettingsForm(targets=_targets(tmp_path), **kw)


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
    # findChildren, not findChild: the label cell holds three QLabels since P6 —
    # the (initially empty) unsaved-change dot, the label, the storage chip — and
    # the dot is the first one.
    assert any("Min score" in lab.text()
               for lab in label_cell.findChildren(QtWidgets.QLabel))
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
    machinery the default configuration cannot run.

    Opened with advanced shown: all four model pickers are ALSO `advanced`, so
    without this the assertions below would pass for the wrong reason (everything
    hidden) and stop testing the gate at all."""
    form = _form(tmp_path, show_advanced=True)
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
    form = _form(tmp_path, show_advanced=True)   # stage1_model is advanced too
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
    form = _form(tmp_path, show_advanced=True)   # the tailor model pickers are advanced
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
    form = _form(tmp_path, show_advanced=True)               # else it starts hidden
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
    form = _form(tmp_path, show_advanced=True)   # the model pickers are advanced too
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
    form = _form(tmp_path, show_advanced=True)   # the tailor model pickers are advanced
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
    form = _form(tmp_path, show_advanced=True,
                 vm_panel_factory=lambda parent: QtWidgets.QLabel("vm", parent))
    qtbot.addWidget(form)
    form._widgets["provider"].setCurrentText("claude")
    assert not _rows_visible(form, "stage1_model")
    form._setters["vm_enabled"](True)
    assert not _rows_visible(form, "stage1_model")
    form._setters["vm_enabled"](False)
    assert not _rows_visible(form, "stage1_model")


# --- cycle 18 P4: the advanced flag + progressive disclosure --------------------

def _visible_field_keys(form):
    """Every schema key whose form ROWS are all flipped on right now.

    Row-flag level, which is what `_field_visible` decides. Use
    `_on_screen_field_keys` for claims about what a user actually sees."""
    return {f.key for f in settings.SETTINGS_SCHEMA
            if form._rows[f.key] and _rows_visible(form, f.key)}


def _on_screen_field_keys(form):
    """Every schema key that would really REACH THE SCREEN: its rows are flipped
    on AND no ancestor is hidden.

    `QFormLayout.isRowVisible` reports the row's own flag and knows nothing about
    a hidden container, so a row inside the switched-off VM section reads True
    while showing nothing. That gap is how the disclosure count first shipped
    over-claiming by three."""
    return {f.key for f in settings.SETTINGS_SCHEMA
            if form._rows[f.key] and _rows_visible(form, f.key)
            and form._widgets[f.key].isVisibleTo(form)}


def test_advanced_fields_are_hidden_until_the_box_is_ticked(qtbot, tmp_path):
    """The phase in one assertion: a fresh profile hides every advanced field,
    ticking the box reveals exactly the ones a gate is not also holding shut."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._advanced_check.isChecked() is False
    assert not _rows_visible(form, "stage1_concurrency")     # advanced, ungated
    assert _rows_visible(form, "stage2_threshold")           # plain, same section

    before = _visible_field_keys(form)
    form._advanced_check.setChecked(True)
    revealed = _visible_field_keys(form) - before
    assert _rows_visible(form, "stage1_concurrency")
    # exactly the advanced fields whose `show_if` gate is open at the shipped defaults
    stored = settings.load(_targets(tmp_path))
    assert revealed == {f.key for f in settings.SETTINGS_SCHEMA
                        if f.advanced and settings.is_visible(f, stored)}
    assert not revealed & {"stage1_model_claude", "RESUME_TAILOR_CLAUDE_MODEL_PRO"}

    form._advanced_check.setChecked(False)
    assert _visible_field_keys(form) == before               # and back again


def test_the_three_deliberate_keeps_are_on_screen_on_a_fresh_profile(qtbot, tmp_path):
    """The schema half of this lives in test_settings.py
    (test_advanced_set_excludes_country_pdflatex_and_max_scored); this is the
    same claim through the real form, because a second visibility path added
    later could hide them without touching `Field.advanced`."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    for key in ("country", "PDFLATEX_PATH", "max_scored_per_run"):
        assert _rows_visible(form, key), f"{key} must be visible without ticking advanced"


def test_the_advanced_toggle_persists_through_the_injected_callback(qtbot, tmp_path):
    """Toggling writes through the injected saver — never the developer's real
    local/config.json — and a form built from what was written comes back ticked.
    That second half IS the restart: the constructor is the only reader.

    Opened ALREADY TICKED so "building must not write" is a real guard: Qt emits
    nothing for `setChecked(False)` on a box that is already unticked, so a form
    built at the default would pass that assertion even with the connect moved
    above the `setChecked` — and every launch that remembered the box on would
    then rewrite config.json at build.
    """
    saved = []
    form = _form(tmp_path, show_advanced=True, save_show_advanced=saved.append)
    qtbot.addWidget(form)
    assert form._advanced_check.isChecked() is True
    assert saved == []                                  # building must not write

    form._advanced_check.setChecked(False)
    assert saved == [False]
    form._advanced_check.setChecked(True)
    assert saved == [False, True]

    reopened = _form(tmp_path, show_advanced=True)      # as a restart would load it
    qtbot.addWidget(reopened)
    assert reopened._advanced_check.isChecked() is True
    assert _rows_visible(reopened, "stage1_concurrency")


def test_a_failing_advanced_save_never_breaks_the_toggle(qtbot, tmp_path):
    """Persisting the disclosure state is a convenience; an unwritable config.json
    must not take the checkbox down with it (same posture as `_on_section_toggled`)."""
    def boom(_value):
        raise OSError("read-only config.json")

    form = _form(tmp_path, save_show_advanced=boom)
    qtbot.addWidget(form)
    form._advanced_check.setChecked(True)
    assert _rows_visible(form, "stage1_concurrency")     # still applied


def test_an_advanced_field_behind_a_closed_gate_stays_hidden(qtbot, tmp_path):
    """Composition with P3, the AND direction: `stage1_model_claude` is advanced
    AND gated on provider=claude. Ticking 'show advanced' must not put it on
    screen while the scorer runs on Gemini — that is the machinery-that-cannot-run
    row P3 removed."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    form._advanced_check.setChecked(True)
    assert _rows_visible(form, "stage1_model")           # advanced, gate open
    assert not _rows_visible(form, "stage1_model_claude")   # advanced, gate closed

    form._widgets["provider"].setCurrentText("claude")   # gate opens...
    assert _rows_visible(form, "stage1_model_claude")     # ...now both halves hold
    assert not _rows_visible(form, "stage1_model")

    form._advanced_check.setChecked(False)               # advanced half closes again
    assert not _rows_visible(form, "stage1_model_claude")


def test_ticking_advanced_does_not_reveal_a_plain_gated_off_field(qtbot, tmp_path):
    """The other direction: `RESUME_TAILOR_GEMINI_API_KEY` is NOT advanced, just
    gated (gemini_auth == api_key). The disclosure toggle must not be a master
    key that overrides `show_if` — it composes with it."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert not _rows_visible(form, "RESUME_TAILOR_GEMINI_API_KEY")
    form._advanced_check.setChecked(True)
    assert not _rows_visible(form, "RESUME_TAILOR_GEMINI_API_KEY")
    form._widgets["gemini_auth"].setCurrentText("api_key")
    assert _rows_visible(form, "RESUME_TAILOR_GEMINI_API_KEY")   # the gate is what opens it


def test_the_checkbox_label_counts_what_ticking_would_reveal(qtbot, tmp_path):
    """The count is computed at RUNTIME and means "advanced settings that APPLY
    to this configuration and are being withheld" — not "how many advanced fields
    exist". Of the eighteen, five belong to whichever provider is not selected
    and three to a VM the user may not run, so the raw total would promise rows a
    tick cannot deliver. Until search ships this label is the only signal the
    hidden settings exist, so it must not lie in either direction.

    Measured against what REACHES THE SCREEN, not against row flags — the count's
    first cut passed a row-flag assertion while over-claiming by three.
    """
    form = _form(tmp_path)
    qtbot.addWidget(form)
    before = _on_screen_field_keys(form)
    label = form._advanced_check.text()

    form._advanced_check.setChecked(True)
    revealed = _on_screen_field_keys(form) - before
    assert revealed                                      # the tick did something
    assert f"({len(revealed)} hidden)" in label          # ...and the promise was exact
    assert len(revealed) < len([f for f in settings.SETTINGS_SCHEMA if f.advanced])

    # nothing is being withheld now, so the parenthetical goes away
    assert "hidden" not in form._advanced_check.text()
    form._advanced_check.setChecked(False)
    assert form._advanced_check.text() == label          # ...and comes back


def test_the_count_excludes_advanced_fields_in_a_switched_off_section(qtbot, tmp_path):
    """The VM section's master switch is a CONFIGURATION gate, not a view fold:
    with `vm_enabled` off its three advanced fields cannot reach the screen
    whatever this checkbox says, because their whole container is hidden. Counting
    them over-claims by three on every fresh install — which is what shipped in
    the first cut of this phase. Turning the VM on must add them back, label and
    all, without going through a `show_if` gate."""
    form = _form(tmp_path, vm_panel_factory=lambda parent: QtWidgets.QLabel("vm", parent))
    qtbot.addWidget(form)
    off = form._advanced_hidden_count()
    assert f"({off} hidden)" in form._advanced_check.text()

    form._setters["vm_enabled"](True)
    assert form._advanced_hidden_count() == off + 3
    assert f"({off + 3} hidden)" in form._advanced_check.text()   # label followed the switch

    form._setters["vm_enabled"](False)
    assert form._advanced_hidden_count() == off

    # ...and with the VM off, ticking really does leave those three off screen
    form._advanced_check.setChecked(True)
    on_screen = _on_screen_field_keys(form)
    assert not {"VM_REMOTE_DIR", "VM_GCLOUD_PATH", "local_task_offsets"} & on_screen


def test_a_collapsed_section_does_not_shrink_the_count(qtbot, tmp_path):
    """The other side of that line. A collapsed section is a fold the user set,
    with a header on screen naming what is inside, and the settings still apply —
    so it must NOT subtract. It also cannot: this repo's owner runs with 9 of the
    10 sections folded, so counting that way would report ~0 and destroy the only
    signal (until search ships) that the hidden settings exist at all."""
    expanded = _form(tmp_path)
    qtbot.addWidget(expanded)
    folded = _form(tmp_path, collapsed_sections=list(st.SECTION_ORDER))
    qtbot.addWidget(folded)
    assert folded._section_widgets["Scoring"].is_collapsed()
    assert folded._advanced_hidden_count() == expanded._advanced_hidden_count()
    assert folded._advanced_hidden_count() > 0


def test_the_count_is_recomputed_rather_than_frozen_at_build(qtbot, tmp_path, monkeypatch):
    """The shipped schema can't show this: `provider` and `tailor_provider` each
    swap a same-sized pair of advanced pickers, so the real count is the same
    number in every reachable gate state. Shrink one arm with a monkeypatched
    schema to prove the label re-reads the gates instead of caching a constant.
    """
    form = _form(tmp_path)
    qtbot.addWidget(form)
    baseline = form._advanced_hidden_count()
    assert f"({baseline} hidden)" in form._advanced_check.text()

    # Flipping `provider` normally swaps a same-sized pair (two Gemini pickers
    # out, two Claude ones in), so the count comes back identical. Drop one of the
    # incoming pair from the schema and the same flip must now read one lower.
    thinner = [f for f in settings.SETTINGS_SCHEMA if f.key != "stage1_model_claude"]
    monkeypatch.setattr(settings, "SETTINGS_SCHEMA", thinner)
    form._widgets["provider"].setCurrentText("claude")   # gate change -> recount
    assert form._advanced_hidden_count() == baseline - 1
    assert f"({baseline - 1} hidden)" in form._advanced_check.text()


def test_load_save_show_advanced_roundtrip(tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    assert jobsdata.load_show_advanced() is False        # off on a fresh profile
    jobsdata.save_show_advanced(True)
    assert jobsdata.load_show_advanced() is True
    jobsdata.save_show_advanced(False)
    assert jobsdata.load_show_advanced() is False
    # A hand-mangled value falls back to the shipped default rather than to
    # truthiness: `bool("false")` is True, which would turn disclosure ON for
    # someone who hand-edited the file trying to turn it off.
    (tmp_path / "config.json").write_text('{"settings_show_advanced": "false"}', encoding="utf-8")
    assert jobsdata.load_show_advanced() is False


# --- cycle 18 P5: spin boxes ----------------------------------------------------

SPIN_KEYS = ("min_score", "stale_after_hours", "limit_per_input",
             "max_scored_per_run", "rescore_cap", "auto_apply_batch_cap")


def test_every_non_slider_int_renders_as_a_spin_box(qtbot, tmp_path):
    """A bounded whole number typed into a free-text box is the one input in this
    form that can be wrong in a way the box itself could have prevented. The set
    is derived from the schema, not listed, so a new int field is covered the day
    it lands."""
    form = _form(tmp_path, show_advanced=True)   # two of the six are advanced
    qtbot.addWidget(form)
    expected = tuple(f.key for f in settings.SETTINGS_SCHEMA
                     if f.type == "int" and not f.slider)
    assert expected == SPIN_KEYS
    for key in expected:
        spin = form._widgets[key]
        assert isinstance(spin, QtWidgets.QSpinBox), key
        f = next(x for x in settings.SETTINGS_SCHEMA if x.key == key)
        assert spin.minimum() == f.min and spin.maximum() == f.max, key


def test_the_spin_box_getter_returns_a_string(qtbot, tmp_path):
    """`_coerce` and the archive-restore test both read getters as text, and
    `_changed_summary` formats them. A getter returning an int would pass a
    casual round-trip and break those."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    form._widgets["min_score"].setValue(3)
    assert form._getters["min_score"]() == "3"
    values, errors = form.collect()
    assert errors == {}
    assert values["min_score"] == 3                  # coerced back to int for the file


def test_spin_box_round_trips_through_collect_and_save(qtbot, tmp_path, monkeypatch):
    targets = _targets(tmp_path)
    form = SettingsForm(targets=targets, collapsed_sections=[], save_collapsed=lambda s: None,
                        show_advanced=False, save_show_advanced=lambda v: None)
    qtbot.addWidget(form)
    _quiet_info(monkeypatch)
    form._widgets["max_scored_per_run"].setValue(1234)
    assert form.save() is True
    assert json.loads(targets["scoring"].read_text("utf-8"))["max_scored_per_run"] == 1234
    reopened = _form(tmp_path)
    qtbot.addWidget(reopened)
    assert reopened._widgets["max_scored_per_run"].value() == 1234


def test_a_spin_box_cannot_be_driven_out_of_range(qtbot, tmp_path):
    """Why this phase swapped the widget: the box now enforces the bound the
    schema declares, so `_coerce`'s 'must be a whole number' arm and
    `settings.validate`'s range arm are unreachable from these six rows."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    spin = form._widgets["min_score"]
    spin.setValue(99)
    assert spin.value() == 5                          # clamped to Field.max
    values, errors = form.collect()
    assert errors == {}
    assert settings.validate(values) == {}


# --- cycle 18 P5: the clamp gotcha ----------------------------------------------

def _note(form, key):
    lab = form._notes[key]
    return lab.text() if not lab.isHidden() else ""


def _focused(form, key):
    """Did Save put the caret in this field?

    `hasFocus()` is False for every widget in a headless run — it also requires
    the window to be ACTIVE, which an offscreen form never is. `focusWidget()` is
    the form's own record of where `setFocus` landed, which is the claim here."""
    return form.focusWidget() is form._widgets[key]


def test_every_note_label_is_built_hidden_and_empty(qtbot, tmp_path):
    """Trap 1, made structural. `self._rows` holds POSITIONAL QFormLayout indices
    captured at build, so a note inserted on demand would silently re-point every
    later field's rows at a neighbour. The note is therefore built for every field
    up front — inside the field's own cell, so there is no row index to shift —
    and merely toggled. Built VISIBLE it would also reserve a blank line under all
    ~60 rows on a form whose whole problem is length."""
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    keyed = {f.key for f in settings.SETTINGS_SCHEMA}
    # every field except the VM section's master checkbox, which is not a form row
    assert set(form._notes) == keyed - {"vm_enabled"}
    for key, note in form._notes.items():
        assert note.text() == "", key
        assert note.isHidden(), key
        # ...and it reserves no space: a hidden widget's layout item is "empty",
        # so the cell is exactly as tall as its input. (The QLabel's own sizeHint
        # is a font line height either way — measuring that would prove nothing.)
        cell = note.parentWidget()
        item = cell.layout().itemAt(1)
        assert item.widget() is note and item.isEmpty(), key
        assert cell.sizeHint().height() == cell.layout().itemAt(0).sizeHint().height()
    assert form._errors == {}

    form._set_field_note("min_score", "something went wrong")
    cell = form._notes["min_score"].parentWidget()
    assert cell.sizeHint().height() > cell.layout().itemAt(0).sizeHint().height()


def test_a_stored_out_of_range_int_is_flagged_not_silently_rewritten(qtbot, tmp_path):
    """The real bug in the feature. `QSpinBox.setValue` clamps SILENTLY, so a
    hand-edited `max_scored_per_run: 99999` would become 5000 and be written back
    on the next Save with no visible cause — a "safety" feature quietly rewriting
    the user's config."""
    targets = _targets(tmp_path)
    targets["scoring"].write_text(json.dumps({"max_scored_per_run": 99999}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)

    assert form._widgets["max_scored_per_run"].value() == 5000     # clamped, as Qt does
    note = _note(form, "max_scored_per_run")
    assert note                                                    # ...but not silently
    assert "99999" in note and "5000" in note
    assert form._notes["max_scored_per_run"].property("warn") is True
    # A clamp is not a validation failure: Save still works, it just told you first.
    assert "max_scored_per_run" not in form._errors


def test_an_unreadable_stored_int_is_flagged_too(qtbot, tmp_path):
    """Same silent-rewrite class, different cause: a non-numeric value falls back
    to the Field default instead of being clamped."""
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({"min_score": "lots"}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._widgets["min_score"].value() == 4                 # the schema default
    assert "lots" in _note(form, "min_score")


def test_the_clamp_flag_covers_slider_ints_too(qtbot, tmp_path):
    """`QSlider.setValue` clamps exactly like `QSpinBox`, so the flag is keyed off
    `Field.type == "int"` rather than off which widget got built. Otherwise the
    five slider ints keep the silent rewrite this phase exists to remove."""
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({"followup_days": 900}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._widgets["followup_days"].value() == 60
    assert "900" in _note(form, "followup_days")


def test_an_in_range_stored_int_is_not_flagged(qtbot, tmp_path):
    """Including a value stored as a STRING: '5' displays as 5 with nothing lost,
    so comparing the raw stored object against the widget would cry wolf."""
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({"min_score": "5", "followup_days": 7}),
                                 encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._widgets["min_score"].value() == 5
    assert _note(form, "min_score") == ""
    assert _note(form, "followup_days") == ""


def test_editing_a_clamped_field_clears_its_note(qtbot, tmp_path):
    """Once the user touches the box, the widget IS the value and the note about
    what was on disk is stale."""
    targets = _targets(tmp_path)
    targets["scoring"].write_text(json.dumps({"max_scored_per_run": 99999}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert _note(form, "max_scored_per_run")
    form._widgets["max_scored_per_run"].setValue(900)
    assert _note(form, "max_scored_per_run") == ""


@pytest.mark.parametrize("key, stored, edit", [
    ("followup_days", 900, lambda w: w.setValue(9)),            # slider: composite cell
    ("min_score", 99, lambda w: w.setValue(2)),                 # spin: returned directly
])
def test_editing_clears_the_note_through_a_composite_cell_too(qtbot, tmp_path,
                                                              key, stored, edit):
    """The change signal must be taken off the REGISTERED control, not off what
    `_make_widget` returned. For the four composite cells those differ — a slider,
    a path box and a credential box all come back wrapped — and a wrapper QWidget
    has no `valueChanged`/`textChanged` at all, so the connection silently finds
    nothing and the note never clears."""
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({key: stored}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert _note(form, key)
    edit(form._widgets[key])
    assert _note(form, key) == ""


def test_a_leading_zero_is_not_reported_as_unreadable(qtbot, tmp_path):
    """`readable` is decided by PARSING the stored value, not by
    `str(stored) == str(int(stored))`.

    The string test called '05' and '+3' unreadable — both of which `int()` takes
    happily — and then printed a note claiming a whole number is not one and
    calling the value on screen "the default" when the default is 4. A false alarm
    that misdescribes the file it is warning about is worse than no alarm: it
    teaches the user to distrust the ones that are real.
    """
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({"min_score": "05"}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._widgets["min_score"].value() == 5
    assert _note(form, "min_score") == ""


def test_every_field_got_a_change_signal_or_is_the_documented_exception(qtbot, tmp_path):
    """A structural version of the test above, so a NEW composite type cannot ship
    unwired. `multichoice` has no single inner control (P0's documented
    exception); `vm_enabled` is the section master switch, not a form row."""
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    signals = ("textChanged", "valueChanged", "currentTextChanged", "toggled")
    for f in settings.SETTINGS_SCHEMA:
        if f.type == "multichoice" or f.key == "vm_enabled":
            continue
        w = form._widgets[f.key]
        assert any(getattr(w, s, None) is not None for s in signals), f.key


# --- cycle 18 P5: inline validation, replacing the modal dump -------------------

def _no_modals(monkeypatch):
    """Record every modal this save would have popped. The point of the phase is
    that the validation path pops none."""
    seen = []
    for name in ("critical", "information", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, _n=name, **k: seen.append((_n, a))))
    return seen


def test_junk_in_local_task_offsets_is_a_red_field_and_a_status_line(qtbot, tmp_path,
                                                                     monkeypatch):
    """The phase checkpoint. No modal, a red box, and a status line that counts."""
    modals = _no_modals(monkeypatch)
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    form._setters["vm_enabled"](True)                     # so the row is on screen
    form._setters["local_task_offsets"]("every half hour")

    assert form.save() is False
    assert modals == []                                   # the dump is gone
    assert form.status.text().startswith("1 setting needs fixing")
    widget = form._widgets["local_task_offsets"]
    assert widget.property("error") is True               # ...and the field is red
    assert "30,50,70" in _note(form, "local_task_offsets")
    assert list(form._errors) == ["local_task_offsets"]


def test_the_error_styling_uses_a_selector_the_widget_actually_matches(qtbot, tmp_path):
    """`theme.py` styled only QLineEdit/QPlainTextEdit/QTextEdit, so the red
    outline would silently never appear on the six new QSpinBoxes. A property
    nothing renders is worse than no property: the test passes and the user sees
    a status line pointing at a field that looks fine.

    The candidate set is derived from the SCHEMA, not from the field that happens
    to carry a rule today: `settings.TEXT_TYPES` is the schema's own statement of
    where a `pattern` may be declared, and it includes `editable_choice`, which
    renders as a QComboBox — a class the first cut of the QSS left out, i.e. the
    exact defect this test exists for, one field type over.
    """
    from qt import theme
    qss = theme._qss()
    for cls in ("QLineEdit", "QPlainTextEdit", "QSpinBox", "QComboBox"):
        assert f'{cls}[error="true"]' in qss, cls
    assert 'QLabel[danger="true"]' in qss

    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    # every widget an error CAN land on — a pattern may be declared on any
    # TEXT_TYPES field, and every non-slider int gets a range message
    styled = (QtWidgets.QLineEdit, QtWidgets.QPlainTextEdit, QtWidgets.QSpinBox,
              QtWidgets.QComboBox)
    checked = 0
    for f in settings.SETTINGS_SCHEMA:
        if f.type in settings.TEXT_TYPES or (f.type == "int" and not f.slider):
            assert isinstance(form._widgets[f.key], styled), f.key
            cls = type(form._widgets[f.key]).__name__
            assert f'{cls}[error="true"]' in qss, f"{f.key} renders as an unstyled {cls}"
            checked += 1
    assert checked >= 30            # not vacuous if the schema is refactored


def test_the_status_line_pluralises_on_the_count(qtbot, tmp_path, monkeypatch):
    """Only one field carries a format rule today, so drive `settings.validate`
    directly to prove the sentence is built from the count rather than hardcoded."""
    modals = _no_modals(monkeypatch)
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    form._setters["vm_enabled"](True)
    monkeypatch.setattr(settings, "validate",
                        lambda values: {"location": "bad", "country": "bad"})
    assert form.save() is False
    assert form.status.text() == "2 settings need fixing"
    assert modals == []
    assert form._widgets["location"].property("error") is True
    assert form._widgets["country"].property("error") is True


def test_a_fixed_field_clears_its_error_as_you_type(qtbot, tmp_path, monkeypatch):
    _no_modals(monkeypatch)
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    form._setters["vm_enabled"](True)
    form._setters["local_task_offsets"]("nope")
    assert form.save() is False
    assert form._widgets["local_task_offsets"].property("error") is True

    form._widgets["local_task_offsets"].setText("30,50")
    assert form._widgets["local_task_offsets"].property("error") is False
    assert _note(form, "local_task_offsets") == ""
    assert form._errors == {}
    assert form.status.text() == ""                 # the count went with it
    assert form.save() is True


def test_focus_out_validates_without_waiting_for_save(qtbot, tmp_path):
    """Finding out at Save time that a box has been wrong for ten minutes is the
    modal's other failure. Leaving the field is the moment to say so."""
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    form._setters["vm_enabled"](True)
    edit = form._widgets["local_task_offsets"]
    edit.setText("half past")
    assert edit.property("error") is not True        # not while you are still typing

    QtWidgets.QApplication.sendEvent(
        edit, QtGui.QFocusEvent(QtCore.QEvent.Type.FocusOut))
    assert edit.property("error") is True
    assert "30,50,70" in _note(form, "local_task_offsets")

    edit.setText("30,50,70")
    QtWidgets.QApplication.sendEvent(
        edit, QtGui.QFocusEvent(QtCore.QEvent.Type.FocusOut))
    assert edit.property("error") is False


def test_the_disk_failure_modal_survives(qtbot, tmp_path, monkeypatch):
    """The one arm that stays modal. A rejected field is a thing the user can see
    and fix in place; an unwritable config.json is neither, and silently leaving
    it on a status line would let someone walk away believing they saved."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        staticmethod(lambda parent, title, text, *a, **k: shown.append(text)))
    monkeypatch.setattr(settings, "save",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    form._widgets["min_score"].setValue(5)
    assert form.save() is False
    assert shown and "disk full" in shown[0]
    assert form.status.text() == "Save failed."


def test_a_successful_save_clears_a_previous_error(qtbot, tmp_path, monkeypatch):
    _no_modals(monkeypatch)
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    form._setters["vm_enabled"](True)
    form._setters["local_task_offsets"]("junk")
    assert form.save() is False
    form._setters["local_task_offsets"]("30,50,70")
    assert form.save() is True
    assert form._errors == {}
    assert form._widgets["local_task_offsets"].property("error") is False
    assert _note(form, "local_task_offsets") == ""


# --- cycle 18 P5: an error in a field the user cannot see -----------------------

def test_an_error_behind_a_view_fold_reveals_itself(qtbot, tmp_path, monkeypatch):
    """A status line claiming a problem the user cannot find is worse than the
    modal it replaces. A COLLAPSED SECTION and the ADVANCED DISCLOSURE are both
    view folds — things the user closed, changing nothing about the
    configuration — so Save opens them to put the offender on screen."""
    _no_modals(monkeypatch)
    saved_collapse, saved_adv = [], []
    form = _form(tmp_path, collapsed_sections=list(st.SECTION_ORDER),
                 save_collapsed=saved_collapse.append,
                 show_advanced=False, save_show_advanced=saved_adv.append)
    qtbot.addWidget(form)
    form._setters["vm_enabled"](True)          # the CONFIGURATION gate is open...
    form._setters["local_task_offsets"]("junk")
    assert form._section_widgets["VM (cloud scraper)"].is_collapsed()
    assert not _rows_visible(form, "local_task_offsets")   # ...but two folds are shut

    assert form.save() is False
    assert form.status.text() == "1 setting needs fixing"  # no "you can't see it" caveat
    assert not form._section_widgets["VM (cloud scraper)"].is_collapsed()
    assert form._advanced_check.isChecked() is True
    assert "local_task_offsets" in _on_screen_field_keys(form)
    assert _focused(form, "local_task_offsets")

    # The user did not choose either reveal, so neither is written back.
    assert saved_collapse == [] and saved_adv == []


def test_an_error_behind_a_configuration_gate_is_named_not_flipped(qtbot, tmp_path,
                                                                   monkeypatch):
    """The other half of the line P4 drew. `vm_enabled` off does not mean "folded
    away", it means "this user does not run a VM" — flipping it would edit their
    configuration to make a message true. So the status line names the field and
    the switch that brings it into view, and the field stays flagged for when they
    do."""
    _no_modals(monkeypatch)
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._getters["vm_enabled"]() is False
    form._setters["local_task_offsets"]("junk")

    assert form.save() is False
    status = form.status.text()
    assert status.startswith("1 setting needs fixing")
    assert "Watcher check offsets (minutes)" in status     # named...
    assert "Enable VM features" in status                  # ...with the way to reach it
    assert form._getters["vm_enabled"]() is False          # never flipped
    assert form._widgets["local_task_offsets"].property("error") is True

    # turning the VM on later brings the still-flagged row into view
    form._setters["vm_enabled"](True)
    form._advanced_check.setChecked(True)
    assert "local_task_offsets" in _on_screen_field_keys(form)


def test_a_show_if_gate_is_named_the_same_way(qtbot, tmp_path, monkeypatch):
    """Same reasoning, the other kind of configuration gate: a gated-off field's
    value can still be invalid on disk, and switching the provider to reveal it
    would change which models the pipeline runs."""
    _no_modals(monkeypatch)
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    form._widgets["provider"].setCurrentText("claude")     # hides the Gemini pair
    monkeypatch.setattr(settings, "validate", lambda values: {"stage1_model": "bad"})

    assert form.save() is False
    status = form.status.text()
    assert "Stage-1 model" in status and "Scoring provider" in status
    assert "gemini" in status
    assert form._widgets["provider"].currentText() == "claude"   # never flipped


def test_the_first_reachable_offender_is_the_one_focused(qtbot, tmp_path, monkeypatch):
    """When some offenders are reachable and some are not, focus goes to the first
    one the user can act on — scrolling to a hidden row would be the same lie in a
    different place.

    The pair is chosen so schema order and reachability DISAGREE: `stage1_model`
    (Scoring) precedes `resume_tone` (Resume), and with the scorer on Claude it is
    the unreachable one. Pick two where the first is also the reachable one and
    this test passes with `reachable[0]` replaced by `ordered[0]` — verified by
    mutation, which is how the first draft of it got caught.
    """
    _no_modals(monkeypatch)
    form = _form(tmp_path)
    qtbot.addWidget(form)
    form._widgets["provider"].setCurrentText("claude")
    keys = [f.key for f in settings.SETTINGS_SCHEMA]
    assert keys.index("stage1_model") < keys.index("resume_tone")
    monkeypatch.setattr(settings, "validate",
                        lambda values: {"stage1_model": "bad", "resume_tone": "bad"})

    assert form.save() is False
    assert _focused(form, "resume_tone")                   # not the gated-off one
    assert not _focused(form, "stage1_model")
    assert "2 settings need fixing" in form.status.text()
    assert "Stage-1 model" in form.status.text()           # named, since it is hidden


def test_the_gate_hint_survives_fixing_the_reachable_offender(qtbot, tmp_path, monkeypatch):
    """The reachable half is the half the user fixes FIRST, so the count they are
    left staring at is the one that most needs its "and here is where it lives".

    The first cut rebuilt the sentence without hints on every clear-as-you-type,
    so fixing the visible field dropped a fully-explained "2 settings need fixing
    (Stage-1 model — set ...)" to a bare "1 setting needs fixing" naming a row that
    is nowhere on the form — a status line counting something the user cannot see
    or even identify, which is precisely what this phase replaced the modal to
    avoid.
    """
    _no_modals(monkeypatch)
    form = _form(tmp_path)
    qtbot.addWidget(form)
    form._widgets["provider"].setCurrentText("claude")     # hides the Gemini pair
    monkeypatch.setattr(settings, "validate",
                        lambda values: {"stage1_model": "bad", "resume_tone": "bad"})
    assert form.save() is False
    assert form.status.text().startswith("2 settings need fixing (Stage-1 model")

    monkeypatch.setattr(settings, "validate", lambda values: {"stage1_model": "bad"})
    form._widgets["resume_tone"].setCurrentIndex(1)        # fix the one on screen
    assert list(form._errors) == ["stage1_model"]
    assert form._widgets["stage1_model"].isVisibleTo(form) is False
    assert form.status.text().startswith("1 setting needs fixing (Stage-1 model")
    assert "Scoring provider" in form.status.text()        # ...and how to reach it


def test_a_successful_save_clears_a_stale_clamp_note(qtbot, tmp_path, monkeypatch):
    """The clamp note describes the FILE ("scoring_config.json has 99999"). The
    Save is what makes that false — it just wrote 5000 there — so leaving the note
    up turns the one honest thing on the row into a lie."""
    _no_modals(monkeypatch)
    targets = _targets(tmp_path)
    targets["scoring"].write_text(json.dumps({"max_scored_per_run": 99999}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert "99999" in _note(form, "max_scored_per_run")

    form._widgets["min_score"].setValue(2)                 # something to save
    assert form.save() is True
    assert json.loads(targets["scoring"].read_text("utf-8"))["max_scored_per_run"] == 5000
    assert _note(form, "max_scored_per_run") == ""


def test_repopulate_reflags_a_value_it_had_to_clamp(qtbot, tmp_path):
    """The silent rewrite, restored by the back door. `_repopulate` (Discard
    changes / Restore defaults / Load snapshot) drives the same setters the
    constructor does, and `QSpinBox.setValue` clamps just as silently there — so a
    snapshot holding `max_scored_per_run: 99999` loaded as 5000 with nothing said,
    which is the exact behaviour this phase exists to remove."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert _note(form, "max_scored_per_run") == ""

    form._repopulate(lambda f: 99999 if f.key == "max_scored_per_run" else f.default)
    assert form._widgets["max_scored_per_run"].value() == 5000
    note = _note(form, "max_scored_per_run")
    assert "99999" in note and "5000" in note

    # ...and a repopulate that clamps nothing leaves the row clean again
    form._repopulate(lambda f: f.default)
    assert _note(form, "max_scored_per_run") == ""


def test_repopulate_drops_a_note_about_the_values_it_replaced(qtbot, tmp_path, monkeypatch):
    """Discard changes has to clear the notes ITSELF, not lean on the setters.

    A setter that writes the value already on screen emits no change signal, so
    `_on_field_edited` never runs — and that is exactly the shape of the case that
    matters: a field flagged WITHOUT being edited (rejected on Save because of what
    was on disk) is reverted to the same value it is holding, and the red outline
    survives the action whose whole job is to put the form back. `resume_tone` is
    untouched here for precisely that reason; revert it to a DIFFERENT value and
    this test passes with the fix removed.
    """
    _no_modals(monkeypatch)
    form = _form(tmp_path)
    qtbot.addWidget(form)
    monkeypatch.setattr(settings, "validate", lambda values: {"resume_tone": "bad"})
    before = form._widgets["resume_tone"].currentText()
    assert form.save() is False
    assert form._errors and form._widgets["resume_tone"].property("error") is True

    monkeypatch.setattr(settings, "validate", lambda values: {})
    form.revert()
    assert form._widgets["resume_tone"].currentText() == before   # nothing to signal
    assert form._errors == {}
    assert _note(form, "resume_tone") == ""
    assert form._widgets["resume_tone"].property("error") is False
    assert form.status.text().startswith("Reverted")              # not a leftover count


# --- cycle 18 P6: dirty markers, Save count, per-field reset --------------------

def _dot(form, key):
    """The field's unsaved-change dot as (text, dirty-property)."""
    lab = form._dots[key]
    return lab.text(), lab.property("dirty")


def test_a_change_in_a_collapsed_section_shows_up_in_its_header(qtbot, tmp_path):
    """The phase checkpoint, and the piece that earns it for THIS repo.

    The owner runs with 9 of the 10 sections folded, so the body a dirty dot lives
    in is usually not on screen at all — the header is the only surface left, and
    before this a collapsed section gave zero signal that it was holding unsaved
    edits. The section must STAY collapsed: a badge that pops its own section open
    to be read has solved nothing.
    """
    form = _form(tmp_path, collapsed_sections=list(st.SECTION_ORDER))
    qtbot.addWidget(form)
    sec = form._section_widgets["Dashboard"]
    assert sec.is_collapsed()
    assert sec._subtitle.text() == st.SECTION_TAGLINE["Dashboard"]
    assert "changed" not in sec._subtitle.text()

    form._widgets["min_score"].setValue(2)

    assert "· 1 changed" in sec._subtitle.text()
    assert sec._subtitle.text().startswith(st.SECTION_TAGLINE["Dashboard"])
    assert sec._subtitle.property("changed") is True      # stops reading as muted
    assert sec.is_collapsed()                             # ...without unfolding it
    assert _dot(form, "min_score") == ("●", True)
    assert form._dots["min_score"].toolTip()              # says what the glyph means
    # and only that section's header moved
    assert all("changed" not in w._subtitle.text()
               for s, w in form._section_widgets.items() if s != "Dashboard")

    # The `dirty` property is not decoration: it is what the QSS colours by, so a
    # dot whose property never moved would render in body text rather than accent.
    from qt import theme
    assert 'QLabel[dirtyDot="true"][dirty="true"]' in theme._qss()


def test_the_section_badge_counts_and_clears(qtbot, tmp_path):
    """Two fields in one section read "· 2 changed"; putting one back reads
    "· 1 changed"; putting both back removes the badge and restores the tagline
    exactly (the count is appended to it, not written over it)."""
    form = _form(tmp_path, collapsed_sections=list(st.SECTION_ORDER))
    qtbot.addWidget(form)
    sec = form._section_widgets["Scraper"]
    tagline = st.SECTION_TAGLINE["Scraper"]

    form._widgets["location"].setText("Remote")
    form._widgets["country"].setText("CA")
    assert sec._subtitle.text() == f"{tagline} · 2 changed"

    form._widgets["country"].setText(form._opening_values["country"])
    assert sec._subtitle.text() == f"{tagline} · 1 changed"

    form._widgets["location"].setText(form._opening_values["location"])
    assert sec._subtitle.text() == tagline
    assert sec._subtitle.property("changed") is False


def test_a_section_with_no_tagline_still_gets_the_count(qtbot):
    """The badge rides on the subtitle label, which is HIDDEN when a section has
    no tagline — so the count has to un-hide it rather than assume it is there."""
    from qt.widgets import CollapsibleSection
    sec = CollapsibleSection("Bare", subtitle="")
    qtbot.addWidget(sec)
    assert sec._subtitle.text() == "" and sec._subtitle.isHidden() is True
    sec.set_changed_count(3)
    assert sec._subtitle.text() == "3 changed"     # no stray leading separator
    assert sec._subtitle.isHidden() is False
    sec.set_changed_count(0)
    assert sec._subtitle.text() == "" and sec._subtitle.isHidden() is True


def test_the_save_button_counts_the_changes_and_stays_pressable(qtbot, tmp_path):
    """"Save 3 changes" while dirty, singular at one, "Save settings" when clean —
    and enabled throughout. Disabling it when clean would break Restore defaults on
    a form already at its defaults, and would delete the "No changes to save"
    feedback that tells someone their edit did not take."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._save_btn.text() == "Save settings"
    assert form._save_btn.isEnabled()

    form._widgets["min_score"].setValue(2)
    assert form._save_btn.text() == "Save 1 change"
    form._widgets["location"].setText("Remote")
    assert form._save_btn.text() == "Save 2 changes"
    form._widgets["country"].setText("CA")
    assert form._save_btn.text() == "Save 3 changes"

    form._widgets["min_score"].setValue(form._opening_values["min_score"])
    assert form._save_btn.text() == "Save 2 changes"
    assert form._save_btn.isEnabled()

    form.revert()
    assert form._save_btn.text() == "Save settings"
    assert form._save_btn.isEnabled()          # never disabled, clean or not


def test_the_reset_button_restores_the_default_and_clears_both_markers(qtbot, tmp_path):
    """The second half of the checkpoint: ↺ clears the field dot AND the header
    count, because it puts the value back where it started."""
    form = _form(tmp_path, collapsed_sections=list(st.SECTION_ORDER))
    qtbot.addWidget(form)
    default = next(f for f in settings.SETTINGS_SCHEMA if f.key == "min_score").default
    sec = form._section_widgets["Dashboard"]

    form._widgets["min_score"].setValue(2)
    assert form._resets["min_score"].isHidden() is False
    form._resets["min_score"].click()

    assert form._widgets["min_score"].value() == default
    assert _dot(form, "min_score") == ("", False)
    assert "changed" not in sec._subtitle.text()
    assert form._save_btn.text() == "Save settings"
    assert form._resets["min_score"].isHidden()     # nothing left to reset


def test_no_reset_button_is_built_for_a_secret(qtbot, tmp_path):
    """Not hidden — NOT BUILT. A secret's schema default is `""`, so the button
    would be offering to clear a live API key: one stray click on a row someone
    opened to read, and the value is gone from the box and from .env with nothing
    to type back. Every other field's default is a value you can retype."""
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    secrets = [f.key for f in settings.SETTINGS_SCHEMA if f.secret]
    assert secrets                                   # the test is not vacuous
    for key in secrets:
        assert key not in form._resets, key
        cell = form._notes[key].parentWidget()
        assert cell.findChildren(QtWidgets.QToolButton) == [], key

    # ...and every other field that has a row does have one
    expected = {f.key for f in settings.SETTINGS_SCHEMA
                if not f.secret and f.key != "vm_enabled"}
    assert set(form._resets) == expected


def test_a_reset_button_shows_only_while_the_value_differs_from_the_default(qtbot, tmp_path):
    """Against the DEFAULT, not the opening value: a field saved away from its
    default months ago still offers the way back, which is the only affordance in
    the form that says what the shipped value even was."""
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({"min_score": 1}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._widgets["min_score"].value() == 1
    assert _dot(form, "min_score") == ("", False)          # saved that way: not dirty
    assert form._resets["min_score"].isHidden() is False    # ...but off the default

    assert form._resets["location"].isHidden()             # untouched, at its default
    form._widgets["location"].setText("Remote")
    assert form._resets["location"].isHidden() is False

    # ...and pressing it goes to the DEFAULT, not back to what was on disk. This is
    # the one config where "reset to default" and "undo my edit" disagree, so it is
    # the only place that can tell them apart — swap the two in `_reset_field` and
    # every other test in this file still passes.
    form._resets["min_score"].click()
    assert form._widgets["min_score"].value() == 4          # the default, not the stored 1
    assert _dot(form, "min_score") == ("●", True)           # so ↺ can CREATE dirt...
    assert form._save_btn.text() == "Save 2 changes"        # ...and it counts (with location)


def test_no_reset_button_is_a_keyboard_tab_stop(qtbot, tmp_path):
    """A QToolButton is a tab stop by DEFAULT, and this form would add one per
    off-default field — on a real profile ~19 one-keypress "overwrite this value"
    controls in the tab chain, some holding text nobody can retype, and double the
    number of stops between one setting and the next. Qt's own inline auxiliary
    control (QLineEdit's clear button) is not a tab stop either."""
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    assert form._resets                                   # not vacuous
    for key, btn in form._resets.items():
        assert btn.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus, key
        assert btn.accessibleName()                       # ...still announced


@pytest.mark.parametrize("key, expected", [
    ("min_score", "4"),                                   # int
    ("location", "United States"),                        # str
    ("remote_types", "Hybrid, On-site"),                  # multichoice: joined
    ("gdrive_root", "blank"),                             # path with an empty default
])
def test_the_reset_tooltip_names_the_value_it_would_write(qtbot, tmp_path, key, expected):
    """The button overwrites a value that is then gone, so it has to say what it
    would put there BEFORE it is pressed — the only place in the form that states
    what the shipped default even was."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert expected in form._resets[key].toolTip()
    assert form._resets[key].toolTip().startswith("Reset to the default")


@pytest.mark.parametrize("default, expected", [
    (["item-%02d" % i for i in range(40)], None),         # truncated to 60
    ([], "empty"),                                        # an empty LIST reads "empty"
    ("   ", "blank"),                                     # ...and empty TEXT reads "blank"
])
def test_the_default_label_handles_the_shapes_a_tooltip_cannot_render(default, expected):
    """A tooltip is not a place for a 40-item list, and "Reset to the default ()"
    tells nobody anything."""
    ftype = "list" if isinstance(default, list) else "str"
    label = st.SettingsForm._default_label(
        settings.Field("k", "L", ftype, default, "S", "config"))
    if expected is None:
        assert len(label) == 60 and label.endswith("…")
    else:
        assert label == expected


def test_a_dirty_field_a_configuration_gate_hides_still_counts(qtbot, tmp_path):
    """The line this phase draws, and it is the OPPOSITE of P4's advanced count.

    That count promises "ticking this box reveals N rows", so a field a gate holds
    shut has to be subtracted or the promise is false. This one promises "Save
    writes N changes" — and `collect()` walks the SCHEMA, so a gated-off field's
    edit is written exactly like any other. Leaving it out would understate the
    number in the direction that loses an edit quietly. P5's reachability answer
    does not apply either: it names an unreachable ERROR because the user must
    reach it to act, while a dirty field asks nothing of them.
    """
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    form._widgets["stage1_model"].setCurrentText("gemini-9-custom")
    assert form._save_btn.text() == "Save 1 change"

    form._widgets["provider"].setCurrentText("claude")     # hides the row just edited
    assert form._widgets["stage1_model"].isVisibleTo(form) is False
    assert "stage1_model" in form._dirty                   # ...and it still counts
    assert form._save_btn.text() == "Save 2 changes"       # the model AND the provider
    subtitle = form._section_widgets["Scoring"]._subtitle
    assert "· 2 changed" in subtitle.text()

    # ...and the badge says which of the two expanding the section will NOT show.
    # A count the user can audit and find short by one is worse than no count: the
    # header's claim is "open me and you will find them", which holds for a view
    # fold and does not hold for a gate.
    assert "Stage-1 model" in subtitle.toolTip()
    assert "Scoring provider" in subtitle.toolTip() and "gemini" in subtitle.toolTip()

    form._widgets["provider"].setCurrentText("gemini")     # gate back open
    assert "· 1 changed" in subtitle.text()                # provider is home again
    assert subtitle.toolTip() == ""                        # nothing hidden any more


def test_an_advanced_field_folded_away_still_counts(qtbot, tmp_path):
    """Same rule for a VIEW fold, arrived at from the other direction: P4 does not
    subtract a collapsed section, and nothing here subtracts the disclosure — the
    edit is written either way."""
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    form._widgets["stale_after_hours"].setValue(9)
    assert form._save_btn.text() == "Save 1 change"

    form._advanced_check.setChecked(False)
    assert "stale_after_hours" not in _on_screen_field_keys(form)
    assert form._save_btn.text() == "Save 1 change"
    assert "· 1 changed" in form._section_widgets["Dashboard"]._subtitle.text()


def test_the_vm_master_switch_counts_without_a_dot(qtbot, tmp_path):
    """`vm_enabled` never goes through `_add_field`, so it has no row, no dot and
    no ↺ — the same deliberate no-op it is for `_set_field_note` and
    `_set_field_visible`. It is still a setting the Save writes, so it still
    counts."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert "vm_enabled" not in form._dots and "vm_enabled" not in form._resets

    form._widgets["vm_enabled"].setChecked(True)
    assert form._dirty == {"vm_enabled"}
    assert form._save_btn.text() == "Save 1 change"
    assert "· 1 changed" in form._section_widgets["VM (cloud scraper)"]._subtitle.text()


def test_a_save_clears_every_marker(qtbot, tmp_path, monkeypatch):
    """Save moves the baseline, so the dots, the badges and the count all go with
    it — the form now matches disk."""
    _no_modals(monkeypatch)
    form = _form(tmp_path)
    qtbot.addWidget(form)
    form._widgets["min_score"].setValue(2)
    form._widgets["location"].setText("Remote")
    assert form._save_btn.text() == "Save 2 changes"

    assert form.save() is True
    assert form._dirty == set()
    assert form._save_btn.text() == "Save settings"
    assert _dot(form, "min_score") == ("", False)
    assert all("changed" not in w._subtitle.text()
               for w in form._section_widgets.values())
    # the value stayed off its default, so the way back is still offered
    assert form._resets["min_score"].isHidden() is False


def test_restore_defaults_marks_everything_it_moved_and_leaves_save_pressable(
        qtbot, tmp_path):
    """Why the Save button must not disable when clean: Restore defaults produces
    a form that differs from DISK and needs a Save, and on a profile already at its
    defaults it produces no change at all — a disabled button would strand the
    first case and lie about the second."""
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({"min_score": 1}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._dirty == set()

    form.restore_defaults()
    assert form._dirty == {"min_score"}
    assert form._save_btn.text() == "Save 1 change"
    assert form._save_btn.isEnabled()
    assert form._resets["min_score"].isHidden()      # it IS the default now


def test_a_setter_that_raises_mid_fill_still_leaves_honest_markers(qtbot, tmp_path):
    """A snapshot with a junk slider value makes `_repopulate` raise part-way
    through (the slider setter does `int(v)`). The fill cannot be completed, but a
    half-filled form whose every dot, badge and count still describes the values it
    replaced is the worst of both states — so the markers are re-read in the
    `finally` and the exception carries on."""
    form = _form(tmp_path)
    qtbot.addWidget(form)
    keys = [f.key for f in settings.SETTINGS_SCHEMA]
    assert keys.index("min_score") < keys.index("followup_days")   # order matters here

    form._widgets["min_score"].setValue(2)
    assert form._dirty == {"min_score"}

    with pytest.raises(ValueError):
        form._repopulate(lambda f: "junk" if f.key == "followup_days" else f.default)

    assert form._widgets["min_score"].value() == 4          # this one did land
    assert form._dirty == set()                             # ...and the markers say so
    assert form._save_btn.text() == "Save settings"


def test_a_clamped_int_opens_dirty(qtbot, tmp_path):
    """P5's clamp note and P6's dirty dot are the same fact said twice, so they
    must agree: the form is holding 5000 where the file says 99999 and the next
    Save writes it, which is exactly what "changed, unsaved" means."""
    targets = _targets(tmp_path)
    targets["scoring"].write_text(json.dumps({"max_scored_per_run": 99999}), encoding="utf-8")
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    assert "99999" in _note(form, "max_scored_per_run")
    assert _dot(form, "max_scored_per_run") == ("●", True)
    assert form._save_btn.text() == "Save 1 change"


def test_a_stored_string_int_does_not_open_dirty(qtbot, tmp_path, monkeypatch):
    """`settings.load()` returns what is literally in the file, so a hand-edited
    `"min_score": "5"` reaches the comparison as text while the spin box reports
    5. Compared raw, that field opens permanently dirty with nothing to undo —
    which is why the baseline goes through the same `_coerce` the widgets do.

    And the post-save summary is normalised the SAME way, so the two claims about
    "did anything change" cannot disagree: before this, the button read "Save
    settings" while pressing it announced "Min score to highlight: 5 -> 5" and took
    an archive snapshot for the privilege.
    """
    modals = _no_modals(monkeypatch)
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({"min_score": "5"}), encoding="utf-8")
    form = _form(tmp_path)
    qtbot.addWidget(form)
    assert form._widgets["min_score"].value() == 5
    assert form._dirty == set()
    assert form._save_btn.text() == "Save settings"

    assert form.save() is True
    assert form.status.text() == "Saved — no changes."
    assert [m for m in modals if "No changes to save" in str(m[1])], modals


def test_a_flagged_field_keeps_its_dot_and_a_reset_clears_the_flag(qtbot, tmp_path,
                                                                   monkeypatch):
    """The two markers are independent claims — "this is wrong" and "this is
    unsaved" — so an error must not swallow the dot. And ↺ goes through the same
    `_on_field_edited` hook every other edit does, so it clears P5's error state
    exactly as typing a fix would."""
    _no_modals(monkeypatch)
    form = _form(tmp_path, show_advanced=True)
    qtbot.addWidget(form)
    form._setters["vm_enabled"](True)
    form._widgets["local_task_offsets"].setText("every half hour")

    assert form.save() is False
    assert form._widgets["local_task_offsets"].property("error") is True
    assert _dot(form, "local_task_offsets") == ("●", True)      # both, at once

    form._resets["local_task_offsets"].click()
    assert form._errors == {}
    assert form._widgets["local_task_offsets"].property("error") is False
    assert _note(form, "local_task_offsets") == ""
    assert _dot(form, "local_task_offsets") == ("", False)
    assert form._save_btn.text() == "Save 1 change"             # vm_enabled, still on


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
