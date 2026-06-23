"""SP7: the Qt Resume Data editor (YAML round-trip) + resume.md generator (mocked LLM)."""
import yaml

import resume_md
from qt import resume_data_tab as rdt
from qt.resume_data_tab import ResumeDataEditor


def _editor(qtbot, master_path):
    ed = ResumeDataEditor(master_path=master_path)
    qtbot.addWidget(ed)
    return ed


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
