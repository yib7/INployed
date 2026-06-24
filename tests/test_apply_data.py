"""Tests for the self-contained apply.md sheet (cycle 12).

apply_data.write() must render one apply.md per tailored folder: the no-submit
fill-it-out playbook at the top, the candidate basics + structured address, the
active standard answers (built from the apply_answers store, defaulting to the
candidate's reality — US citizen / GC, no sponsorship), the tailored résumé
highlights, and a hidden meta marker carrying the job identity. No apply_data.json
is written. The write tests pin STORE_PATH to a temp path so they never read the
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
    assert cfg["address_country"] == "United States"
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


# --- apply_data.write produces a self-contained apply.md ---------------------

def test_write_creates_apply_md_not_json(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    # Hermetic: no store file -> load() seeds+migrates defaults in memory.
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    out = apply_data.write(_JOB, tmp_path, ["bullet one", "bullet two"])

    assert out.name == "apply.md"
    assert out.exists()
    assert not (tmp_path / "apply_data.json").exists()  # JSON is gone

    text = out.read_text(encoding="utf-8")
    assert "Test Person" in text and "t@example.com" in text      # candidate
    assert "Uni" in text                                          # education
    assert "bullet one" in text and "bullet two" in text         # résumé highlights


def test_write_embeds_no_submit_playbook(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    text = apply_data.write(_JOB, tmp_path, []).read_text(encoding="utf-8").lower()
    assert "never" in text and "submit" in text          # never-submit contract
    assert "electronic signature" in text                # sign with name + date
    assert "xxxxx" in text                               # placeholder-for-blocking-required
    assert "captcha" in text and "password" in text      # safety walls preserved


def test_write_marker_roundtrips_job_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    text = apply_data.write(_JOB, tmp_path, []).read_text(encoding="utf-8")
    meta = apply_data.parse_marker(text)
    assert meta["job_posting_id"] == "42"
    assert meta["url"] == "https://example.com/job/42"
    assert meta["company"] == "Acme"


def test_write_includes_structured_address(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    store = tmp_path / "apply_answers.json"
    monkeypatch.setattr(apply_answers, "STORE_PATH", store)
    ans = apply_answers.seed_defaults()
    by = {e["id"]: e for e in ans}
    by["address_street"]["answer"] = "1 Main St"
    by["address_city"]["answer"] = "Boston"
    by["address_state"]["answer"] = "MA"
    by["address_zip"]["answer"] = "02100"
    apply_answers.save(ans, store)
    text = apply_data.write(_JOB, tmp_path, []).read_text(encoding="utf-8")
    for part in ("1 Main St", "Boston", "MA", "02100", "United States"):
        assert part in text


def test_write_standard_answers_render_bools_and_exclude_address(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    text = apply_data.write(_JOB, tmp_path, []).read_text(encoding="utf-8")
    assert "Yes" in text and "No" in text                 # work auth Yes / sponsorship No
    # address questions are NOT repeated in the Standard-answers section
    assert "Street address (line 1)." not in text


def test_write_cover_line_only_when_pdf_present(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    # absent: cover_letter requested but no PDF on disk -> no cover line
    text = apply_data.write(_JOB, tmp_path, [], cover_letter=True).read_text(encoding="utf-8")
    assert "Cover letter" not in text
    # present: the cover PDF exists -> its path is listed
    (tmp_path / apply_data.output.cover_filename()).write_bytes(b"%PDF cover")
    text = apply_data.write(_JOB, tmp_path, [], cover_letter=True).read_text(encoding="utf-8")
    assert "Cover letter" in text and apply_data.output.cover_filename() in text
