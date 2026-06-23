"""Tests for the apply_data.json form-prefill profile (SP4 T4.1).

apply_data.write() must embed a top-level "standard_answers" object built from the
master answer store (apply_answers), defaulting to the candidate's reality (US
citizen / GC, no sponsorship). On first run the store seeds from apply_config
DEFAULTS and migrates a repo-root apply_config.json's overrides in once; absent
both, the hardcoded defaults apply. The review-before-submit "instructions" wording
stays. The write tests pin STORE_PATH to a temp path so they never read the
developer's real apply_answers.json.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import apply_answers, apply_config, apply_data  # noqa: E402


_MASTER = {
    "basics": {"name": "Test Person", "email": "t@example.com", "phone": "555",
               "location": "NYC", "linkedin": "li", "github": "gh"},
    "education": [{"school": "Uni", "degree": "BS", "dates": "2020-2024"}],
}

_JOB = {"job_posting_id": "42", "company_name": "Acme", "job_title": "Engineer",
        "url": "https://example.com/job/42"}


@pytest.fixture(autouse=True)
def _stub_master(monkeypatch):
    monkeypatch.setattr(apply_data.assets, "load_master", lambda: _MASTER)


# --- load_apply_config -------------------------------------------------------

def test_load_apply_config_defaults_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    cfg = apply_config.load_apply_config()
    assert cfg["work_authorized"] is True
    assert cfg["requires_sponsorship"] is False
    assert cfg["willing_to_relocate"] is True
    assert cfg["years_experience"] == "0"
    assert cfg["how_did_you_hear"] == "LinkedIn"
    assert cfg["gender"] == "Decline to self-identify"
    assert cfg["disability_status"] == "Decline to self-identify"
    assert "no visa sponsorship" in cfg["authorization_statement"].lower()


def test_load_apply_config_override_is_honored(tmp_path, monkeypatch):
    path = tmp_path / "apply_config.json"
    path.write_text(json.dumps({"willing_to_relocate": False,
                                "how_did_you_hear": "Referral"}), encoding="utf-8")
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", path)
    cfg = apply_config.load_apply_config()
    assert cfg["willing_to_relocate"] is False
    assert cfg["how_did_you_hear"] == "Referral"
    # unspecified keys still fall back to defaults
    assert cfg["work_authorized"] is True
    assert cfg["requires_sponsorship"] is False


def test_load_apply_config_ignores_unreadable_file(tmp_path, monkeypatch):
    path = tmp_path / "apply_config.json"
    path.write_text("not json{", encoding="utf-8")
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", path)
    cfg = apply_config.load_apply_config()
    assert cfg["work_authorized"] is True


# --- apply_data.write embeds standard_answers --------------------------------

def test_write_embeds_standard_answers_with_citizen_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    # Hermetic: no store file -> load() seeds+migrates defaults in memory.
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    out = apply_data.write(_JOB, tmp_path, ["bullet one", "bullet two"])
    data = json.loads(out.read_text(encoding="utf-8"))

    assert "standard_answers" in data
    sa = data["standard_answers"]
    assert sa["work_authorized"] is True
    assert sa["requires_sponsorship"] is False
    assert sa["willing_to_relocate"] is True
    # review-before-submit wording preserved
    assert "review-before-submit" in data["instructions"].lower()
    assert data["job"]["url"] == "https://example.com/job/42"
    assert data["resume_bullets"] == ["bullet one", "bullet two"]


def test_write_honors_apply_config_override(tmp_path, monkeypatch):
    path = tmp_path / "apply_config.json"
    path.write_text(json.dumps({"willing_to_relocate": False}), encoding="utf-8")
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", path)
    # Hermetic: no store file -> load() migrates the apply_config override in memory.
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    out = apply_data.write(_JOB, tmp_path, [])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["standard_answers"]["willing_to_relocate"] is False
    assert data["standard_answers"]["requires_sponsorship"] is False


def test_write_embeds_answer_bank_matching_standard_answers(tmp_path, monkeypatch):
    # Hermetic: point the store at a temp file seeded from the (absent) config.
    store = tmp_path / "apply_answers.json"
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(apply_answers, "STORE_PATH", store)
    apply_answers.save(apply_answers.seed_defaults(), store)
    out = apply_data.write(_JOB, tmp_path, ["b1"])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(data["answer_bank"], list) and data["answer_bank"]
    assert data["standard_answers"] == apply_answers.as_standard_answers()
    # the rich entries carry kind/status metadata the flat dict lacks
    assert all({"kind", "status"} <= set(e) for e in data["answer_bank"])
