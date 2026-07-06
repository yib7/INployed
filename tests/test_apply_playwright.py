"""Tests for the Playwright modern-board driver's PURE bits (apply_playwright).

Exercises apply.md parsing, name splitting, and folder artifact discovery — no
browser. The Playwright driving (fill_identity, upload_files, run) is validated in
live runs, not here, exactly like apply_verify's locator wrappers.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_playwright  # noqa: E402

# A representative slice of a real apply.md (Gotion/CITGO shape).
_APPLY_MD = """\
# Apply sheet — Associate Business Analyst @ CITGO
Generated 2026-07-05.

## Instructions for the form-filler (read first)
- **Never click the final Submit / Apply / Send / Finish button.** Stop at review.

## Candidate
- **Name:** Jane Doe
- **Email:** jane.doe@example.com
- **Phone:** 555-555-0100
- **Location:** Anytown, VA
- **LinkedIn:** https://linkedin.com/in/Jane
- **GitHub / Portfolio:** https://github.com/yib7

### Address
- **Full:** 123 Main Street, Anytown, Virginia ST 00000, United States
- **Street:** 123 Main Street
- **City:** Anytown
- **State / Province:** Virginia
- **ZIP / Postal:** ST 00000
- **Country:** United States

## Education
- College of William & Mary — B.S. Computer Science

## Standard answers
- **Are you legally authorized to work in the US?** Yes
- **Will you now or in the future require visa sponsorship?** No
- **Work-authorization statement (free text).** Authorized to work in the US; no sponsorship.
- **Gender (EEO self-identification).** Male

## Electronic signature (use at the end, where the form asks — do not submit)
- **Signature (type):** Jane Doe
- **Date:** use today's date (the day you apply)
"""


def test_split_name_two_parts():
    assert apply_playwright.split_name("Jane Doe") == ("Jane", "Doe")


def test_split_name_single_and_empty():
    assert apply_playwright.split_name("Cher") == ("Cher", "")
    assert apply_playwright.split_name("") == ("", "")
    assert apply_playwright.split_name("   ") == ("", "")


def test_split_name_multiword_surname():
    # Three+ tokens: first token is the first name, the rest is the surname.
    assert apply_playwright.split_name("Ana Maria de la Cruz") == ("Ana", "Maria de la Cruz")


def test_parse_candidate_block():
    p = apply_playwright.parse_apply_md(_APPLY_MD)
    c = p["candidate"]
    assert c["name"] == "Jane Doe"
    assert c["email"] == "jane.doe@example.com"
    assert c["phone"] == "555-555-0100"
    assert c["linkedin"] == "https://linkedin.com/in/Jane"
    assert c["github / portfolio"] == "https://github.com/yib7"


def test_parse_address_block():
    p = apply_playwright.parse_apply_md(_APPLY_MD)
    a = p["address"]
    assert a["country"] == "United States"
    assert a["street"] == "123 Main Street"
    assert a["zip / postal"] == "ST 00000"


def test_parse_standard_answers_keep_question_text():
    p = apply_playwright.parse_apply_md(_APPLY_MD)
    sa = dict(p["standard_answers"])
    assert sa["Are you legally authorized to work in the US?"] == "Yes"
    assert sa["Will you now or in the future require visa sponsorship?"] == "No"
    assert sa["Gender (EEO self-identification)."] == "Male"


def test_parse_signature_name():
    p = apply_playwright.parse_apply_md(_APPLY_MD)
    assert p["signature_name"] == "Jane Doe"


def test_parse_ignores_playbook_instruction_bullets():
    # The form-filler instructions are bold bullets too, but they live in the
    # Instructions section — they must NOT leak into candidate/standard answers.
    p = apply_playwright.parse_apply_md(_APPLY_MD)
    assert "Never click the final Submit / Apply / Send / Finish button." \
        not in dict(p["standard_answers"])
    assert all("Never click" not in k for k in p["candidate"])


def test_parse_empty_returns_empty_shape():
    p = apply_playwright.parse_apply_md("")
    assert p == {"candidate": {}, "address": {}, "standard_answers": [],
                 "signature_name": ""}


def test_load_folder_discovers_pdfs(tmp_path):
    (tmp_path / "apply.md").write_text(_APPLY_MD, encoding="utf-8")
    (tmp_path / "Jane_Doe_Resume.pdf").write_bytes(b"%PDF-1.4 resume")
    (tmp_path / "Jane_Doe_Cover_Letter.pdf").write_bytes(b"%PDF-1.4 cover")
    p = apply_playwright._load_folder(tmp_path)
    assert p["resume"].endswith("Jane_Doe_Resume.pdf")
    assert p["cover"].endswith("Jane_Doe_Cover_Letter.pdf")
    assert p["candidate"]["name"] == "Jane Doe"


def test_load_folder_missing_applymd_is_tolerant(tmp_path):
    p = apply_playwright._load_folder(tmp_path)
    assert p["candidate"] == {} and p["resume"] == "" and p["cover"] == ""
