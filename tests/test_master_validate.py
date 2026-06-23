"""Tests for local/resume_tailor/master_validate.py — the setup linter.

`validate_master` catches the structural mistakes a non-technical user could make
in master_experience.yaml so the dashboard can show a clear error instead of the
pipeline failing later. `validate_answers` delegates to the answer store, and
`check_setup` runs both against the live files for the dashboard's Check setup.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import master_validate as mv  # noqa: E402


def test_valid_master_has_no_errors():
    m = {"basics": {"name": "A", "email": "a@b.c"},
         "experience": [{"org": "X", "achievements": [{"id": "a1", "what": "did a"}]}]}
    assert mv.validate_master(m) == []


def test_master_flags_missing_basics_and_dupe_atom_ids():
    m = {"experience": [{"org": "X", "achievements": [{"id": "a1", "what": "w"}]}],
         "projects": [{"name": "P", "achievements": [{"id": "a1", "what": "w"}]}]}
    errs = mv.validate_master(m)
    assert any("basics" in e for e in errs)
    assert any("duplicate" in e.lower() and "a1" in e for e in errs)


def test_master_flags_atom_missing_what():
    m = {"basics": {"name": "A", "email": "a@b.c"},
         "experience": [{"org": "X", "achievements": [{"id": "a1", "what": ""}]}]}
    assert any("what" in e for e in mv.validate_master(m))


def test_master_flags_tailor_required_unknown_block():
    m = {"basics": {"name": "A", "email": "a@b.c"}, "experience": [],
         "tailor": {"required": {"experience": ["Ghost Corp"]}}}
    assert any("Ghost Corp" in e for e in mv.validate_master(m))


def test_validate_answers_delegates():
    bad = [{"id": "x", "question": "", "answer": "", "kind": "fixed", "status": "active"}]
    assert mv.validate_answers(bad)  # non-empty


def test_check_setup_returns_two_keys():
    out = mv.check_setup()
    assert set(out) == {"master", "answers"}
    assert all(isinstance(v, list) for v in out.values())
