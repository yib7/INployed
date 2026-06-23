"""Tests for local/resume_tailor/apply_answers.py — the master answer store.

The store is the reusable bank of screening-question answers the apply skill
consumes. Each entry is tagged fixed/open-ended and active/needs-review. These
tests pin: seeding from the legacy defaults, flattening active answers back into
the exact legacy `standard_answers` shape, validation, atomic save with `.bak`,
migration of an existing apply_config.json, and dedupe of captured questions.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import apply_answers as aa  # noqa: E402
from resume_tailor import apply_config  # noqa: E402


def test_seed_defaults_one_entry_per_default():
    seeded = aa.seed_defaults()
    ids = {e["id"] for e in seeded}
    assert ids == set(apply_config.DEFAULTS)
    for e in seeded:
        assert e["question"] and e["kind"] in aa.KINDS and e["status"] == "active"


def test_as_standard_answers_reproduces_defaults():
    assert aa.as_standard_answers(aa.seed_defaults()) == apply_config.DEFAULTS


def test_save_then_load_roundtrips_and_makes_bak(tmp_path):
    p = tmp_path / "apply_answers.json"
    ans = aa.seed_defaults()
    aa.save(ans, p)
    aa.save(ans, p)  # second write makes a .bak
    assert (p.with_name(p.name + ".bak")).exists()
    assert aa.load(p) == ans


def test_validate_flags_bad_kind_status_and_dupe_ids():
    bad = [{"id": "x", "question": "q", "answer": "a", "kind": "weird", "status": "active"},
           {"id": "x", "question": "q2", "answer": "a", "kind": "fixed", "status": "nope"}]
    errs = aa.validate(bad)
    assert any("kind" in e for e in errs)
    assert any("status" in e for e in errs)
    assert any("duplicate" in e.lower() for e in errs)


def test_validate_passes_seed():
    assert aa.validate(aa.seed_defaults()) == []


def test_save_rejects_invalid(tmp_path):
    p = tmp_path / "apply_answers.json"
    bad = [{"id": "", "question": "", "answer": "", "kind": "fixed", "status": "active"}]
    try:
        aa.save(bad, p)
    except ValueError:
        pass
    else:
        raise AssertionError("save should reject an invalid store")


def test_append_needs_review_dedupes(tmp_path):
    p = tmp_path / "apply_answers.json"
    aa.save(aa.seed_defaults(), p)
    aa.append_needs_review(["Why do you want to work here?"], p)
    aa.append_needs_review(["why do you WANT to work here?  "], p)  # same, normalized
    nr = [e for e in aa.load(p) if e["status"] == "needs-review"]
    assert len(nr) == 1
    assert nr[0]["kind"] == "open-ended"
    assert nr[0]["answer"] == ""


def test_migrate_applies_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / "apply_config.json"
    cfg.write_text(json.dumps({"how_did_you_hear": "Referral"}), encoding="utf-8")
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", cfg)
    merged = aa.migrate_from_apply_config(aa.seed_defaults())
    assert aa.as_standard_answers(merged)["how_did_you_hear"] == "Referral"


def test_load_absent_file_seeds_and_migrates(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    loaded = aa.load(tmp_path / "missing_answers.json")
    assert {e["id"] for e in loaded} == set(apply_config.DEFAULTS)
