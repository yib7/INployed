"""Headless tests for local/resume_data_form.py — the Résumé Data editor.

Builds the real Tk widgets over a temp master_experience.yaml (the `master_tmp`
fixtures), and verifies the safety-critical behaviours: it parses the sections,
its validator surfaces a broken file's problems, and "Revert to opening state"
restores the file to the snapshot taken when the editor opened.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

tk = pytest.importorskip("tkinter")

import resume_data_form  # noqa: E402


def _editor(root, path):
    return resume_data_form.ResumeDataEditor(tk.Frame(root), master_path=path)


def test_editor_builds_and_lists_sections(root, master_tmp):
    ed = _editor(root, master_tmp)
    assert "experience" in ed.sections
    assert "projects" in ed.sections
    # the seeded entry's atom widget is bound
    assert ("a1", "what") in ed._atom_vars


def test_validate_surfaces_errors(root, master_tmp_broken):
    ed = _editor(root, master_tmp_broken)
    errs = ed.validate()
    assert errs  # non-empty: missing basics + duplicate atom id
    assert any("basics" in e for e in errs)


def test_validate_clean_file_has_no_errors(root, master_tmp):
    ed = _editor(root, master_tmp)
    assert ed.validate() == []


def test_revert_restores_on_open_snapshot(root, master_tmp):
    from resume_tailor import master_edit
    ed = _editor(root, master_tmp)
    master_edit.update_atom("a1", {"what": "changed away"})
    assert "changed away" in master_tmp.read_text(encoding="utf-8")
    ed.revert()
    assert "changed away" not in master_tmp.read_text(encoding="utf-8")
    assert "did a thing" in master_tmp.read_text(encoding="utf-8")


def test_save_persists_field_edit(root, master_tmp):
    ed = _editor(root, master_tmp)
    ed._atom_vars[("a1", "what")].set("rebuilt the pipeline")
    assert ed.save() is True
    assert "rebuilt the pipeline" in master_tmp.read_text(encoding="utf-8")
