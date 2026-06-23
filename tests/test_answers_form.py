"""Headless tests for local/answers_form.py — the Apply Answers table editor.

Builds the real Tk widgets over a temp apply_answers.json and verifies the
behaviours that keep the store safe: one row per stored answer, adding + saving a
new row persists it, validation blocks a bad row, and "Revert to opening state"
restores the snapshot taken when the editor opened.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

tk = pytest.importorskip("tkinter")

import answers_form  # noqa: E402
from resume_tailor import apply_answers as aa  # noqa: E402


def _store(tmp_path):
    p = tmp_path / "apply_answers.json"
    aa.save(aa.seed_defaults(), p)
    return p


def _editor(root, path):
    return answers_form.AnswersEditor(tk.Frame(root), store_path=path)


def test_builds_one_row_per_answer(root, tmp_path):
    ed = _editor(root, _store(tmp_path))
    assert len(ed.rows) == len(aa.seed_defaults())


def test_add_then_save_persists_new_row(root, tmp_path):
    p = _store(tmp_path)
    ed = _editor(root, p)
    ed.add_row({"id": "q_new", "question": "New?", "answer": "Yes",
                "kind": "open-ended", "status": "active"})
    assert ed.save() is True
    assert any(e["id"] == "q_new" for e in aa.load(p))


def test_collect_skips_blank_rows(root, tmp_path):
    p = _store(tmp_path)
    ed = _editor(root, p)
    ed.add_row()  # a wholly-blank row
    collected = ed.collect()
    assert len(collected) == len(aa.seed_defaults())  # blank row dropped


def test_invalid_kind_blocks_save(root, tmp_path, monkeypatch):
    p = _store(tmp_path)
    ed = _editor(root, p)
    ed.rows[0]["kind"].set("bogus")
    monkeypatch.setattr(answers_form.messagebox, "showerror", lambda *a, **k: None)
    assert ed.save() is False


def test_revert_restores_on_open_file(root, tmp_path):
    p = _store(tmp_path)
    ed = _editor(root, p)
    aa.append_needs_review(["Sneaky?"], p)  # external change after open
    assert any(e["question"] == "Sneaky?" for e in aa.load(p))
    ed.revert()
    assert all(e["question"] != "Sneaky?" for e in aa.load(p))


def test_needs_review_filter_keeps_rows_for_save(root, tmp_path):
    p = _store(tmp_path)
    ed = _editor(root, p)
    ed.filter_needs_review.set(True)
    ed._apply_filter()
    # filtering only hides frames; all rows remain collectable
    assert len(ed.collect()) == len(aa.seed_defaults())


def test_add_row_clears_needs_review_filter_so_new_row_is_visible(root, tmp_path):
    # A new row is "active", which the needs-review filter would hide instantly —
    # making "Add answer" look broken. Adding must drop the filter and show the row.
    p = _store(tmp_path)
    ed = _editor(root, p)
    ed.filter_needs_review.set(True)
    ed._apply_filter()
    row = ed.add_row()
    assert ed.filter_needs_review.get() is False
    assert row["frame"].winfo_manager() == "pack"  # actually shown, not hidden
