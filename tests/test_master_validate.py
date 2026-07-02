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


def test_master_flags_unanchored_skill_alias():
    m = {"basics": {"name": "A", "email": "a@b.c"},
         "skills": {"concepts_and_methodologies": ["A/B Testing"]},
         "skill_aliases": {"A/B Testing": ["Experimentation"],
                           "Made Up Concept": ["bogus"]}}
    errs = mv.validate_master(m)
    assert any("Made Up Concept" in e for e in errs)        # unanchored -> flagged
    assert not any("A/B Testing" in e for e in errs)        # anchored (paren-stripped) -> fine


def test_master_alias_anchor_matches_paren_stripped_concept():
    m = {"basics": {"name": "A", "email": "a@b.c"},
         "skills": {"concepts_and_methodologies": ["Exploratory Data Analysis (EDA)"]},
         "skill_aliases": {"Exploratory Data Analysis (EDA)": ["data analysis"]}}
    assert mv.validate_master(m) == []


def test_master_flags_unanchored_match_only_alias():
    m = {"basics": {"name": "A", "email": "a@b.c"},
         "skills": {"developer_tools": ["Docker"]},
         "skill_aliases_match_only": {"Docker": ["Containerization"],
                                      "Phantom Tool": ["nope"]}}
    errs = mv.validate_master(m)
    assert any("Phantom Tool" in e for e in errs)           # unanchored -> flagged
    assert not any("Docker" in e for e in errs)             # anchored -> fine


def test_validate_answers_delegates():
    bad = [{"id": "x", "question": "", "answer": "", "kind": "fixed", "status": "active"}]
    assert mv.validate_answers(bad)  # non-empty


def test_check_setup_returns_two_keys():
    out = mv.check_setup()
    assert set(out) == {"master", "answers"}
    assert all(isinstance(v, list) for v in out.values())
