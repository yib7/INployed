"""Tests for local/resume_tailor/apply_answers.py — the master answer store.

The store is the reusable bank of screening-question answers the apply sheet
consumes. Each entry is tagged fixed/open-ended; every entry is `active` (the
needs-review status was retired in cycle 13, but `validate()` still tolerates it
so an old store loads). These tests pin: seeding from the legacy defaults,
flattening active answers back into the exact legacy `standard_answers` shape,
validation, atomic save with `.bak`, and migration of an existing apply_config.json.
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


def test_append_needs_review_is_retired():
    # cycle 13: the auto-flagging helper (only the retired apply-to-job skill used
    # it) is gone — answers are added manually now.
    assert not hasattr(aa, "append_needs_review")


def test_validate_tolerates_legacy_needs_review():
    # the needs-review status is retired from the UI, but an old store that still
    # carries it must still load + validate (it migrates to active on next save).
    legacy = [{"id": "x", "question": "q", "answer": "a", "kind": "open-ended",
               "status": "needs-review"}]
    assert aa.validate(legacy) == []


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


# --- cycle 12: structured address fields -------------------------------------

_ADDRESS_IDS = ("address_street", "address_city", "address_state",
                "address_zip", "address_country")


def test_defaults_include_structured_address():
    for key in _ADDRESS_IDS:
        assert key in apply_config.DEFAULTS
    assert apply_config.DEFAULTS["address_country"] == "United States"
    # the rest start blank for the user to fill
    assert apply_config.DEFAULTS["address_street"] == ""


def test_seed_defaults_address_is_open_ended():
    seeded = {e["id"]: e for e in aa.seed_defaults()}
    for key in _ADDRESS_IDS:
        assert seeded[key]["kind"] == "open-ended"
        assert seeded[key]["question"]  # has a human-readable label
    assert aa.as_standard_answers(aa.seed_defaults())["address_country"] == "United States"


def test_load_stays_pure_no_merge(tmp_path):
    # plain load() returns EXACTLY the stored entries (the editor round-trip relies on this)
    old = [e for e in aa.seed_defaults() if e["id"] not in _ADDRESS_IDS]
    p = tmp_path / "apply_answers.json"
    aa.save(old, p)
    assert aa.load(p) == old


def test_load_with_defaults_merges_missing_address(tmp_path):
    # an OLD store saved before address existed (seed minus the address ids)
    old = [e for e in aa.seed_defaults() if e["id"] not in _ADDRESS_IDS]
    p = tmp_path / "apply_answers.json"
    aa.save(old, p)
    loaded = {e["id"]: e for e in aa.load_with_defaults(p)}
    for key in _ADDRESS_IDS:
        assert key in loaded, f"{key} should be merged in by load_with_defaults"
        assert loaded[key]["status"] == "active"
    # custom/existing entries are preserved
    assert "how_did_you_hear" in loaded
