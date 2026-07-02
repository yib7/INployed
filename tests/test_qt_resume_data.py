"""SP7: the Qt Resume Data editor (YAML round-trip) + resume.md generator (mocked LLM)."""
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


def test_edit_basics_round_trips(qtbot, master_tmp):
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
    assert "Example Corp" in ed._verbatim_edits         # experience block has the toggle
    cb, edit = ed._verbatim_edits["Example Corp"]
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
    cb, edit = ed._verbatim_edits["Example Corp"]
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
