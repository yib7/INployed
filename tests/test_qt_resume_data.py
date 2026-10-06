"""The Qt Resume Data editor (YAML round-trip) + resume.md generator (mocked LLM)."""
import yaml
from PySide6 import QtCore

import resume_md
from qt import resume_data_tab as rdt
from qt.resume_data_tab import ResumeDataEditor


def _editor(qtbot, master_path):
    ed = ResumeDataEditor(master_path=master_path)
    qtbot.addWidget(ed)
    return ed


def test_no_horizontal_overflow(qtbot, master_tmp):
    # text bars (and the Delete buttons) must stay within the visible width
    ed = _editor(qtbot, master_tmp)
    off = QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert ed.scroll.horizontalScrollBarPolicy() == off
    ed.reload()  # survives a rebuild
    assert ed.scroll.horizontalScrollBarPolicy() == off


def test_edit_basics_round_trips(qtbot, master_tmp, tmp_path, monkeypatch):
    # `save()` also persists the verbatim blocks through jobsdata, which resolves
    # config.json off `jobsdata.HERE`. Without this redirect the test rewrote the
    # developer's REAL local/config.json -- the only file in the whole worktree a
    # full suite run still touched. Every other save()-calling test here already
    # patches HERE; this one was the hole.
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    ed = _editor(qtbot, master_tmp)
    ed._basics_edits["name"].setText("New Name")
    assert ed.save() is True
    data = yaml.safe_load(master_tmp.read_text(encoding="utf-8"))
    assert data["basics"]["name"] == "New Name"


def test_delete_atom_removes_it(qtbot, master_tmp):
    ed = _editor(qtbot, master_tmp)
    # the fixture has experience atom id "a1"
    assert ("a1", "what") in ed._atom_edits
    import resume_tailor.master_edit as me
    me.delete_atom("a1", master_tmp)   # exercise the same mutation the Delete button calls
    ed.reload()
    assert ("a1", "what") not in ed._atom_edits


def test_validate_reports_problems(qtbot, master_tmp_broken):
    ed = _editor(qtbot, master_tmp_broken)
    assert ed.validate()  # broken fixture: missing basics + duplicate atom id


def test_generate_uses_injected_call_not_real_gemini(qtbot, master_tmp, monkeypatch):
    ed = _editor(qtbot, master_tmp)
    monkeypatch.setattr(resume_md, "MASTER_YAML_PATH", master_tmp)
    monkeypatch.setattr(rdt.QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: rdt.QtWidgets.QMessageBox.StandardButton.Yes))
    captured = {}
    monkeypatch.setattr(rdt.workers, "run_async",
                        lambda owner, fn, on_done=None, on_error=None: captured.setdefault("fn", fn))
    # generate_resume_md must run with an injected/faked transport, never real Gemini
    monkeypatch.setattr(resume_md, "generate_resume_md",
                        lambda yaml_text, model, **k: "# Resume\n")
    ed._generate()
    assert "fn" in captured
    assert captured["fn"]() == "# Resume\n"


def test_resume_md_write_backs_up(qtbot, master_tmp, tmp_path, monkeypatch):
    ed = _editor(qtbot, master_tmp)
    target = tmp_path / "resume.md"
    target.write_text("OLD\n", encoding="utf-8")
    monkeypatch.setattr(resume_md, "RESUME_MD_PATH", target)
    ed._resume_md_write("# New resume\n")
    assert target.read_text(encoding="utf-8") == "# New resume\n"
    assert (tmp_path / "resume.md.bak").read_text(encoding="utf-8") == "OLD\n"


def test_stale_banner_follows_staleness(qtbot, master_tmp, monkeypatch):
    ed = _editor(qtbot, master_tmp)
    monkeypatch.setattr(resume_md, "resume_md_stale", lambda **k: True)
    ed._refresh_stale_banner()
    assert not ed.stale_banner.isHidden()   # visible when resume.md has drifted
    monkeypatch.setattr(resume_md, "resume_md_stale", lambda **k: False)
    ed._refresh_stale_banner()
    assert ed.stale_banner.isHidden()       # hidden once in sync


def test_stale_banner_regenerate_calls_generate(qtbot, master_tmp, monkeypatch):
    ed = _editor(qtbot, master_tmp)
    called = []
    monkeypatch.setattr(ed, "_generate", lambda: called.append(True))
    ed.stale_regen_btn.click()
    assert called == [True]


def test_resume_layout_section_lists_master_entries(qtbot, master_tmp, tmp_path, monkeypatch):
    # Rows are derived from the master so their names match what the engine looks up:
    # experience/leadership by org, projects by name (fixture: "Example Corp" / "ProjX").
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    ed = _editor(qtbot, master_tmp)
    assert "Example Corp" in ed._layout_section_edits
    assert "ProjX" in ed._layout_project_edits


def test_resume_layout_toggle_persists(qtbot, master_tmp, tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    ed = _editor(qtbot, master_tmp)
    assert ed._layout_enabled_cb.isChecked() is True          # default on
    ed._layout_enabled_cb.setChecked(False)                   # toggling saves immediately
    assert jobsdata.load_resume_layout_enabled() is False


def test_resume_layout_save_writes_both_maps(qtbot, master_tmp, tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    ed = _editor(qtbot, master_tmp)
    ed._layout_section_edits["Example Corp"].setText("2, 1")
    ed._layout_project_edits["ProjX"].setText("3, 2, 1")
    ed._save_layout()
    assert jobsdata.load_resume_layout() == {"Example Corp": {"line_targets": [2, 1]}}
    assert jobsdata.load_project_layout() == {"ProjX": {"line_targets": [3, 2, 1]}}


def test_resume_layout_prefills_saved_targets(qtbot, master_tmp, tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_project_layout({"ProjX": {"line_targets": [3, 1]}})
    ed = _editor(qtbot, master_tmp)
    assert ed._layout_project_edits["ProjX"].text().replace(" ", "") == "3,1"


def test_projects_count_control_loads_and_saves(qtbot, master_tmp, tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_projects_count(5, "exact")
    ed = _editor(qtbot, master_tmp)
    assert ed._projects_count_spin.value() == 5
    assert ed._projects_mode_exact.isChecked()
    ed._projects_count_spin.setValue(2)
    ed._projects_mode_max.setChecked(True)
    ed._save_layout()
    assert jobsdata.load_projects_count() == (2, "max")


def test_project_tiers_control_loads_and_saves(qtbot, master_tmp, tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_project_bullet_tiers([{"projects": 2, "bullets": 3}])
    ed = _editor(qtbot, master_tmp)
    assert ed._project_tiers_edit.text().replace(" ", "") == "2:3"   # prefilled from config
    ed._project_tiers_edit.setText("2:3, 2:2, 1:1")
    ed._save_layout()
    assert jobsdata.load_project_bullet_tiers() == [
        {"projects": 2, "bullets": 3}, {"projects": 2, "bullets": 2}, {"projects": 1, "bullets": 1}]


def test_project_tiers_blank_clears(qtbot, master_tmp, tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_project_bullet_tiers([{"projects": 1, "bullets": 3}])
    ed = _editor(qtbot, master_tmp)
    ed._project_tiers_edit.setText("")        # clearing the box disables tiering
    ed._save_layout()
    assert jobsdata.load_project_bullet_tiers() == []


def test_parse_tiers_drops_malformed_tokens():
    assert rdt._parse_tiers("2:3, junk, 1:1, 5") == [
        {"projects": 2, "bullets": 3}, {"projects": 1, "bullets": 1}]


def test_projects_count_warning_shows_above_four(qtbot, master_tmp, tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    ed = _editor(qtbot, master_tmp)
    ed._projects_count_spin.setValue(3)
    assert ed._projects_warn.isHidden()       # safe value -> no one-page warning
    ed._projects_count_spin.setValue(6)
    assert not ed._projects_warn.isHidden()    # cranked up -> warning appears


def test_stale_layout_entry_can_be_removed(qtbot, master_tmp, tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    # "Ghost Project" is not in the master -> it is a stale custom-layout row.
    jobsdata.save_project_layout({"ProjX": {"line_targets": [2]},
                                  "Ghost Project": {"line_targets": [1]}})
    ed = _editor(qtbot, master_tmp)
    assert any(r[1] == "Ghost Project" for r in ed._stale_layout_rows)
    assert ed._remove_stale_btn.isEnabled()
    ed._remove_stale_layout()
    assert jobsdata.load_project_layout() == {"ProjX": {"line_targets": [2]}}
    assert ed._stale_layout_rows == []


def test_verbatim_block_toggle_and_save(qtbot, master_tmp, tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    ed = _editor(qtbot, master_tmp)
    # editors are keyed by (section, idx) so same-named entries can't
    # clobber each other; the persisted-store name rides in the value.
    by_name = {nm: (cb, edit) for (nm, cb, edit) in ed._verbatim_edits.values()}
    assert "Example Corp" in by_name                     # experience block has the toggle
    cb, edit = by_name["Example Corp"]
    assert cb.isChecked() is False                       # default: tailored
    cb.setChecked(True)
    edit.setPlainText("My exact bullet one\n   \nMy exact bullet two")  # blank line dropped
    ed.save()
    assert jobsdata.load_verbatim_blocks() == {
        "Example Corp": ["My exact bullet one", "My exact bullet two"]}


def test_verbatim_block_prefills_and_unchecking_reverts(qtbot, master_tmp, tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_verbatim_blocks({"Example Corp": ["Saved bullet"]})
    ed = _editor(qtbot, master_tmp)
    cb, edit = {nm: (c, e) for (nm, c, e) in ed._verbatim_edits.values()}["Example Corp"]
    assert cb.isChecked() is True                        # prefilled from saved verbatim
    assert edit.toPlainText() == "Saved bullet"
    cb.setChecked(False)                                 # off -> revert to normal tailoring
    ed.save()
    assert "Example Corp" not in jobsdata.load_verbatim_blocks()


def test_push_button_disabled_unless_vm_on(qtbot, master_tmp, monkeypatch):
    ed = _editor(qtbot, master_tmp)
    monkeypatch.setattr(rdt.settings, "load", lambda *a, **k: {"vm_enabled": False})
    ed._refresh_push_state()
    assert not ed.btn_push_md.isEnabled()

    class _T:
        def configured(self):
            return True

    import vm_sync
    monkeypatch.setattr(rdt.settings, "load", lambda *a, **k: {"vm_enabled": True})
    monkeypatch.setattr(vm_sync.VMTarget, "from_env", staticmethod(lambda *a, **k: _T()))
    ed._refresh_push_state()
    assert ed.btn_push_md.isEnabled()


def test_push_outcome_distinguishes_success_and_failure():
    # scp returns a CompletedProcess (it doesn't raise on failure), so a non-zero
    # return code must be reported as a failure — not silently treated as success.
    import types
    ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
    bad = types.SimpleNamespace(returncode=1, stdout="",
                                stderr="pscp: unable to open ~/resume.md")
    assert ResumeDataEditor._push_outcome(ok) == (True, "resume.md pushed to the VM.")
    failed, msg = ResumeDataEditor._push_outcome(bad)
    assert failed is False and "unable to open" in msg


# --- Save validates every entry/basics it is about to write BEFORE writing
# anything, with entry_problems: the same rules append_entry and the add-entry
# dialog enforce. ------------------------------------------------------------

def test_save_blocks_and_writes_nothing_when_an_entry_field_is_blanked(
        qtbot, master_tmp, monkeypatch):
    monkeypatch.setattr(rdt.QtWidgets.QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    ed = _editor(qtbot, master_tmp)
    before = master_tmp.read_bytes()
    ed._entry_edits[("experience", 0, "dates")].setText("")
    ed._basics_edits["name"].setText("New Name")   # a would-be VALID change too
    assert ed.save() is False
    assert master_tmp.read_bytes() == before       # nothing written -- not even basics


def test_save_blocks_and_writes_nothing_when_a_changed_atoms_what_is_blanked(
        qtbot, master_tmp, monkeypatch):
    monkeypatch.setattr(rdt.QtWidgets.QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    ed = _editor(qtbot, master_tmp)
    before = master_tmp.read_bytes()
    ed._atom_edits[("a1", "what")].setText("")     # fixture atom under experience[0]
    assert ed.save() is False
    assert master_tmp.read_bytes() == before


def test_save_blocks_and_writes_nothing_when_basics_name_is_blanked(
        qtbot, master_tmp, monkeypatch):
    monkeypatch.setattr(rdt.QtWidgets.QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    ed = _editor(qtbot, master_tmp)
    before = master_tmp.read_bytes()
    ed._basics_edits["name"].setText("")
    assert ed.save() is False
    assert master_tmp.read_bytes() == before


def test_save_still_writes_a_valid_change(qtbot, master_tmp, tmp_path, monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    ed = _editor(qtbot, master_tmp)
    ed._entry_edits[("experience", 0, "dates")].setText("2024-06 / 2024-09")
    assert ed.save() is True
    data = yaml.safe_load(master_tmp.read_text(encoding="utf-8"))
    assert data["experience"][0]["dates"] == "2024-06 / 2024-09"


def test_save_blocks_when_basics_email_is_blanked(qtbot, master_tmp_broken, monkeypatch):
    # master_tmp_broken has no `basics` at all yet; filling in a name but leaving
    # email blank must still be blocked by the same name/email check Save
    # runs on the projected basics before writing anything.
    monkeypatch.setattr(rdt.QtWidgets.QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    ed = _editor(qtbot, master_tmp_broken)
    before = master_tmp_broken.read_bytes()
    ed._basics_edits["name"].setText("Someone")
    assert ed.save() is False
    assert master_tmp_broken.read_bytes() == before


# --- the add-entry dialog validates inline and disables OK until
# valid, and keeps every field intact when the write itself fails. `QDialog.exec`
# is faked (never a real modal loop) so the test drives the widgets directly and
# never blocks headless. -----------------------------------------------------

def test_add_entry_dialog_ok_disabled_until_every_rule_passes(qtbot, master_tmp, monkeypatch):
    ed = _editor(qtbot, master_tmp)
    seen = {}

    def fake_exec(self):
        ok_btn = self.findChild(rdt.QtWidgets.QDialogButtonBox).button(
            rdt.QtWidgets.QDialogButtonBox.StandardButton.Ok)
        seen["initially_disabled"] = not ok_btn.isEnabled()
        self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_name").setText("New Proj")
        self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_dates").setText("2025")
        self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_what").setText("built it")
        seen["still_disabled_without_an_angle"] = not ok_btn.isEnabled()
        self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_angles").setText("backend")
        seen["enabled_once_valid"] = ok_btn.isEnabled()
        return rdt.QtWidgets.QDialog.DialogCode.Rejected   # cancel; nothing written

    monkeypatch.setattr(rdt.QtWidgets.QDialog, "exec", fake_exec)
    ed._add_entry_dialog("projects")
    assert seen == {"initially_disabled": True, "still_disabled_without_an_angle": True,
                    "enabled_once_valid": True}


def test_add_entry_dialog_keeps_input_on_a_failed_write(qtbot, master_tmp, monkeypatch):
    ed = _editor(qtbot, master_tmp)
    monkeypatch.setattr(rdt.QtWidgets.QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(rdt.master_edit, "append_entry",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    calls = {"n": 0}

    def fake_exec(self):
        calls["n"] += 1
        name_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_name")
        dates_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_dates")
        what_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_what")
        angles_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_angles")
        if calls["n"] == 1:
            name_edit.setText("New Proj")
            dates_edit.setText("2025")
            what_edit.setText("built it")
            angles_edit.setText("backend")
            return rdt.QtWidgets.QDialog.DialogCode.Accepted
        # second round, after the failed write: every field must still hold what
        # the user typed; nothing here re-created the dialog or cleared it.
        assert name_edit.text() == "New Proj"
        assert dates_edit.text() == "2025"
        assert what_edit.text() == "built it"
        assert angles_edit.text() == "backend"
        return rdt.QtWidgets.QDialog.DialogCode.Rejected

    monkeypatch.setattr(rdt.QtWidgets.QDialog, "exec", fake_exec)
    ed._add_entry_dialog("projects")
    assert calls["n"] == 2   # the SAME dialog re-opened after the failed write


# --- the add-entry dialog's Impact field must split one
# achievement per LINE, like the add-achievement dialog and the in-place edits,
# since "$1,200" holds a comma inside a single number. ------------------------

def test_add_entry_dialog_impact_splits_on_newlines_not_commas(qtbot, master_tmp, monkeypatch):
    ed = _editor(qtbot, master_tmp)

    def fake_exec(self):
        self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_name").setText("New Proj")
        self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_dates").setText("2025")
        self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_what").setText("built it")
        self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_angles").setText("backend")
        imp = self.findChild(rdt.QtWidgets.QPlainTextEdit, "add_entry_impact")
        assert imp is not None, "Impact must be a QPlainTextEdit, one line per impact"
        imp.setPlainText("Cut costs by $1,200 per month\nsaving 40%")
        return rdt.QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(rdt.QtWidgets.QDialog, "exec", fake_exec)
    ed._add_entry_dialog("projects")
    data = yaml.safe_load(master_tmp.read_text(encoding="utf-8"))
    impact = data["projects"][-1]["achievements"][0]["impact"]
    assert impact == ["Cut costs by $1,200 per month", "saving 40%"]


# --- a YAML parse error in the master must not escape the
# write handler: master_edit wraps ruamel's YAMLError in a ValueError, so it
# is caught by the SAME `except (ValueError, OSError)` as any other failed
# write, shown to the user, and the dialog reopens with every field intact. --

def test_add_entry_dialog_keeps_input_when_the_master_is_broken_yaml(
        qtbot, master_tmp, monkeypatch):
    ed = _editor(qtbot, master_tmp)
    shown = []
    monkeypatch.setattr(rdt.QtWidgets.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: shown.append(a[2])))
    calls = {"n": 0}

    def fake_exec(self):
        calls["n"] += 1
        name_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_name")
        dates_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_dates")
        what_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_what")
        angles_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_entry_angles")
        if calls["n"] == 1:
            name_edit.setText("New Proj")
            dates_edit.setText("2025")
            what_edit.setText("built it")
            angles_edit.setText("backend")
            # The file becomes invalid YAML between opening the dialog and
            # clicking OK (a hand edit elsewhere); append_entry's own
            # `_load_doc` call is what discovers this.
            master_tmp.write_text("basics:\n  name: b: c\n", encoding="utf-8")
            return rdt.QtWidgets.QDialog.DialogCode.Accepted
        # second round, after the failed write: every field must still hold
        # what the user typed; nothing here re-created the dialog or cleared it.
        assert name_edit.text() == "New Proj"
        assert dates_edit.text() == "2025"
        assert what_edit.text() == "built it"
        assert angles_edit.text() == "backend"
        return rdt.QtWidgets.QDialog.DialogCode.Rejected

    monkeypatch.setattr(rdt.QtWidgets.QDialog, "exec", fake_exec)
    ed._add_entry_dialog("projects")
    assert calls["n"] == 2   # the SAME dialog re-opened after the failed write
    assert shown and "line" in shown[0] and "column" in shown[0]


# --- the add-atom ("Add achievement") dialog gets the same
# treatment as the add-entry dialog above -- live validation with OK disabled
# until every rule passes, and the input kept intact when the write fails. -----

def test_add_atom_dialog_ok_disabled_until_every_rule_passes(qtbot, master_tmp, monkeypatch):
    ed = _editor(qtbot, master_tmp)
    seen = {}

    def fake_exec(self):
        ok_btn = self.findChild(rdt.QtWidgets.QDialogButtonBox).button(
            rdt.QtWidgets.QDialogButtonBox.StandardButton.Ok)
        seen["initially_disabled"] = not ok_btn.isEnabled()
        self.findChild(rdt.QtWidgets.QLineEdit, "add_atom_what").setText("built it")
        seen["still_disabled_without_an_angle"] = not ok_btn.isEnabled()
        self.findChild(rdt.QtWidgets.QLineEdit, "add_atom_angles").setText("backend")
        seen["enabled_once_valid"] = ok_btn.isEnabled()
        return rdt.QtWidgets.QDialog.DialogCode.Rejected   # cancel; nothing written

    monkeypatch.setattr(rdt.QtWidgets.QDialog, "exec", fake_exec)
    ed._add_atom_dialog("projects", 0)
    assert seen == {"initially_disabled": True, "still_disabled_without_an_angle": True,
                    "enabled_once_valid": True}


def test_add_atom_dialog_flags_an_em_dash(qtbot, master_tmp, monkeypatch):
    ed = _editor(qtbot, master_tmp)
    seen = {}

    def fake_exec(self):
        ok_btn = self.findChild(rdt.QtWidgets.QDialogButtonBox).button(
            rdt.QtWidgets.QDialogButtonBox.StandardButton.Ok)
        problems = self.findChild(rdt.QtWidgets.QLabel, "add_atom_problems")
        what_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_atom_what")
        angles_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_atom_angles")
        what_edit.setText("shipped it—fast")
        angles_edit.setText("backend")
        seen["disabled_with_an_em_dash"] = not ok_btn.isEnabled()
        seen["problem_names_the_em_dash"] = "em dash" in problems.text()
        what_edit.setText("shipped it fast")
        seen["enabled_once_the_em_dash_is_gone"] = ok_btn.isEnabled()
        return rdt.QtWidgets.QDialog.DialogCode.Rejected

    monkeypatch.setattr(rdt.QtWidgets.QDialog, "exec", fake_exec)
    ed._add_atom_dialog("projects", 0)
    assert seen == {"disabled_with_an_em_dash": True, "problem_names_the_em_dash": True,
                    "enabled_once_the_em_dash_is_gone": True}


def test_add_atom_dialog_keeps_input_on_a_failed_write(qtbot, master_tmp, monkeypatch):
    ed = _editor(qtbot, master_tmp)
    monkeypatch.setattr(rdt.QtWidgets.QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(rdt.master_edit, "add_atom",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    calls = {"n": 0}

    def fake_exec(self):
        calls["n"] += 1
        what_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_atom_what")
        angles_edit = self.findChild(rdt.QtWidgets.QLineEdit, "add_atom_angles")
        if calls["n"] == 1:
            what_edit.setText("built it")
            angles_edit.setText("backend")
            return rdt.QtWidgets.QDialog.DialogCode.Accepted
        # second round, after the failed write: every field must still hold what
        # the user typed; nothing here re-created the dialog or cleared it.
        assert what_edit.text() == "built it"
        assert angles_edit.text() == "backend"
        return rdt.QtWidgets.QDialog.DialogCode.Rejected

    monkeypatch.setattr(rdt.QtWidgets.QDialog, "exec", fake_exec)
    ed._add_atom_dialog("projects", 0)
    assert calls["n"] == 2   # the SAME dialog re-opened after the failed write
