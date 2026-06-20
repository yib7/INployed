"""Tests for the apply_data.json form-prefill profile (SP4 T4.1).

apply_data.write() must embed a top-level "standard_answers" object built from
load_apply_config(), defaulting to the candidate's reality (US citizen / GC, no
sponsorship). A repo-root apply_config.json overrides those defaults; absent, the
hardcoded defaults apply. The review-before-submit "instructions" wording stays.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import apply_config, apply_data  # noqa: E402


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
    out = apply_data.write(_JOB, tmp_path, [])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["standard_answers"]["willing_to_relocate"] is False
    assert data["standard_answers"]["requires_sponsorship"] is False
