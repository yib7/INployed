"""Tests for the self-contained apply.md sheet (cycle 13: résumé-rich).

apply_data.write() renders one apply.md per tailored folder: the no-submit
fill-it-out playbook at the top, the candidate basics + structured address, the
active standard answers (work auth / EEO / how-did-you-hear), THIS JOB'S TAILORED
RÉSUMÉ translated into markdown (Work experience / Projects / Leadership /
Technical skills — built deterministically from the tailor's own selection +
surviving bullets, NO LLM call), and a hidden meta marker carrying the job
identity. The résumé sections include ONLY the blocks the tailor selected for this
job, with each block's surviving bullet text verbatim. No apply_data.json is
written. The write tests pin STORE_PATH + APPLY_CONFIG to temp paths so they never
read the developer's real files.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import apply_answers, apply_config, apply_data, output  # noqa: E402


_MASTER = {
    "basics": {"name": "Test Person", "email": "t@example.com", "phone": "555",
               "location": "NYC", "linkedin": "li", "github": "gh"},
    "education": [{"school": "Uni", "degree": "BS", "dates": "2020-2024"}],
    "experience": [
        {"org": "Acme Corp", "title": "SWE Intern", "location": "NYC",
         "dates": "2024-06 / 2024-08",
         "achievements": [{"id": "a1"}, {"id": "a2"}]},
        {"org": "Other Co", "title": "Analyst", "location": "Remote",
         "dates": "2023", "achievements": [{"id": "z1"}]},
    ],
    "projects": [
        {"name": "CoolApp", "dates": "2024", "repo": "gh/cool",
         "live_url": "http://cool", "achievements": [{"id": "p1"}]},
    ],
    "leadership": [
        {"org": "Coding Club", "title": "President", "dates": "2023-2024",
         "achievements": [{"id": "l1"}]},
    ],
}

_JOB = {"job_posting_id": "42", "company_name": "Acme", "job_title": "Engineer",
        "url": "https://example.com/job/42"}

# What the tailor would hand apply_data.write(): the selection, the bullets that
# survived one-page enforcement (keyed by group key = "+".join(atom_ids)), and the
# compressed skill lines.
_SEL = {
    "experience": [{"name": "Acme Corp", "groups": [["a1"], ["a2"]]}],
    "projects": [{"name": "CoolApp", "groups": [["p1"]]}],
    "leadership": [{"name": "Coding Club", "groups": [["l1"]]}],
}
_BULLETS = {"a1": "Built the ingestion pipeline fast.", "a2": "Cut cloud spend 40%.",
            "p1": "Shipped CoolApp end to end.", "l1": "Grew membership threefold."}
_SKILLS = [{"label": "Languages", "items": "Python, SQL"}]


@pytest.fixture(autouse=True)
def _stub_master(monkeypatch):
    monkeypatch.setattr(apply_data.assets, "load_master", lambda: _MASTER)


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    # Never read the developer's real apply config / answer store.
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")


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
    assert cfg["work_authorized"] is True
    assert cfg["requires_sponsorship"] is False


def test_load_apply_config_ignores_unreadable_file(tmp_path, monkeypatch):
    path = tmp_path / "apply_config.json"
    path.write_text("not json{", encoding="utf-8")
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", path)
    cfg = apply_config.load_apply_config()
    assert cfg["work_authorized"] is True


# --- apply_data.write produces a self-contained apply.md ---------------------

def test_write_creates_apply_md_not_json(tmp_path):
    out = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS, skill_lines=_SKILLS)

    assert out.name == "apply.md"
    assert out.exists()
    assert not (tmp_path / "apply_data.json").exists()  # JSON is gone

    text = out.read_text(encoding="utf-8")
    assert "Test Person" in text and "t@example.com" in text      # candidate
    assert "Uni" in text                                          # education


def test_write_embeds_no_submit_playbook(tmp_path):
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8").lower()
    assert "never" in text and "submit" in text          # never-submit contract
    assert "electronic signature" in text                # sign with name + date
    assert "xxxxx" in text                               # placeholder-for-blocking-required
    assert "captcha" in text and "password" in text      # safety walls preserved


def test_write_playbook_notes_optional_unknowns_for_review(tmp_path):
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    # Cycle 13 copy tweak: optional unknowns are now flagged for review too.
    assert "note them for review too" in text
    assert "Leave optional unknowns blank." not in text  # the bare old wording is gone


def test_write_marker_roundtrips_job_identity(tmp_path):
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    meta = apply_data.parse_marker(text)
    assert meta["job_posting_id"] == "42"
    assert meta["url"] == "https://example.com/job/42"
    assert meta["company"] == "Acme"


def test_write_includes_structured_address(tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    monkeypatch.setattr(apply_answers, "STORE_PATH", store)
    ans = apply_answers.seed_defaults()
    by = {e["id"]: e for e in ans}
    by["address_street"]["answer"] = "1 Main St"
    by["address_city"]["answer"] = "Boston"
    by["address_state"]["answer"] = "MA"
    by["address_zip"]["answer"] = "02100"
    apply_answers.save(ans, store)
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    for part in ("1 Main St", "Boston", "MA", "02100", "United States"):
        assert part in text


def test_write_standard_answers_render_bools_and_exclude_address(tmp_path):
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    assert "Yes" in text and "No" in text                 # work auth Yes / sponsorship No
    assert "Street address (line 1)." not in text         # address not repeated here


def test_write_has_no_documents_or_upload_language(tmp_path):
    # apply.md is for portals that DON'T auto-fill from a résumé upload, so it no
    # longer carries a Documents section or any directive to upload files.
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "## Documents" not in text                  # the upload section is gone
    assert "Upload the résumé PDF" not in text         # the upload directive is gone
    assert output.resume_filename() not in text        # no document path is listed in the sheet
    assert "## Work experience" in text                # ...but the résumé itself still renders


def test_write_explains_when_to_use_as_fallback(tmp_path):
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    assert "When to use this sheet" in text            # purpose is spelled out
    low = text.lower()
    assert "auto-fill" in low and "by hand" in low     # fallback framing: normal=auto-fill, this=by hand


# --- résumé sections mirror this job's tailored résumé (deterministic) --------

def test_write_renders_selected_experience_with_headers_and_bullets(tmp_path):
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "## Work experience" in text
    assert "Acme Corp" in text and "SWE Intern" in text          # org + title
    assert "2024-06 / 2024-08" in text                           # dates
    assert "Built the ingestion pipeline fast." in text          # bullet 1 verbatim
    assert "Cut cloud spend 40%." in text                        # bullet 2 verbatim


def test_write_includes_only_selected_blocks(tmp_path):
    # "Other Co" is in the master but NOT in the selection for this job.
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "Other Co" not in text
    assert "Analyst" not in text


def test_write_renders_projects_and_leadership(tmp_path):
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "## Projects" in text and "CoolApp" in text
    assert "Shipped CoolApp end to end." in text
    assert "## Leadership" in text and "Coding Club" in text and "President" in text
    assert "Grew membership threefold." in text


def test_write_renders_skills_from_skill_lines(tmp_path):
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "## Technical skills" in text
    assert "Languages" in text and "Python, SQL" in text


def test_write_skips_groups_whose_bullet_didnt_survive(tmp_path):
    # a2's bullet was trimmed away on one-page enforcement -> not in the dict.
    bullets = {"a1": "Built the ingestion pipeline fast.", "p1": "Shipped CoolApp.",
               "l1": "Grew membership threefold."}
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=bullets,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "Built the ingestion pipeline fast." in text
    assert "Cut cloud spend 40%." not in text   # dropped group is absent


def test_write_without_selection_renders_note_not_crash(tmp_path):
    # Backfill / CLI path: no tailoring data available.
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    assert "## Work experience" not in text
    assert "Re-tailor" in text                  # the placeholder note
    # the rest of the sheet is still valid
    assert "Test Person" in text and apply_data.parse_marker(text)["job_posting_id"] == "42"


def test_write_from_folder_backfills_without_resume_sections(tmp_path):
    out = apply_data.write_from_folder(tmp_path, _JOB)
    assert out.name == "apply.md"
    text = out.read_text(encoding="utf-8")
    assert "## Work experience" not in text and "Re-tailor" in text
