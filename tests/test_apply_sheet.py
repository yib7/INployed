"""Tests for the apply.md sheet parser (local/apply_sheet.py).

Exercises apply.md parsing and name splitting: split_name and parse_apply_md,
the two functions apply_facts.py's fact catalog (the Jev-judged auto-apply
run) relies on to read a job folder's apply.md into structured fields.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_sheet  # noqa: E402

# A synthetic slice of an apply.md, in the shape the generator emits.
_APPLY_MD = """\
# Apply sheet: Associate Business Analyst @ Acme Analytics
Generated 2026-07-05.

## Instructions for the form-filler (read first)
- **Never click the final Submit / Apply / Send / Finish button.** Stop at review.

## Candidate
- **Name:** Jane Doe
- **Email:** jane.doe@example.com
- **Phone:** 555-555-0100
- **Location:** Anytown, CA
- **LinkedIn:** https://linkedin.com/in/janedoe
- **GitHub / Portfolio:** https://github.com/janedoe

### Address
- **Full:** 123 Main Street, Anytown, California 12345, United States
- **Street:** 123 Main Street
- **City:** Anytown
- **State / Province:** California
- **ZIP / Postal:** 12345
- **Country:** United States

## Education
- State University — B.S. Computer Science

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
    assert apply_sheet.split_name("Jane Doe") == ("Jane", "Doe")


def test_split_name_single_and_empty():
    assert apply_sheet.split_name("Cher") == ("Cher", "")
    assert apply_sheet.split_name("") == ("", "")
    assert apply_sheet.split_name("   ") == ("", "")


def test_split_name_multiword_surname():
    # Three+ tokens: first token is the first name, the rest is the surname.
    assert apply_sheet.split_name("Ana Maria de la Cruz") == ("Ana", "Maria de la Cruz")


def test_parse_candidate_block():
    p = apply_sheet.parse_apply_md(_APPLY_MD)
    c = p["candidate"]
    assert c["name"] == "Jane Doe"
    assert c["email"] == "jane.doe@example.com"
    assert c["phone"] == "555-555-0100"
    assert c["linkedin"] == "https://linkedin.com/in/janedoe"
    assert c["github / portfolio"] == "https://github.com/janedoe"


def test_parse_address_block():
    p = apply_sheet.parse_apply_md(_APPLY_MD)
    a = p["address"]
    assert a["country"] == "United States"
    assert a["street"] == "123 Main Street"
    assert a["zip / postal"] == "12345"


def test_parse_standard_answers_keep_question_text():
    p = apply_sheet.parse_apply_md(_APPLY_MD)
    sa = dict(p["standard_answers"])
    assert sa["Are you legally authorized to work in the US?"] == "Yes"
    assert sa["Will you now or in the future require visa sponsorship?"] == "No"
    assert sa["Gender (EEO self-identification)."] == "Male"


# The sheet's escapes: a `*` in a question or an answer is
# written `\*`, and a note sits under its answer as a sub-bullet.
_ESCAPED = r"""## Standard answers
- **Rate your SQL skill from 1 to 5 \*** 4 \* strong
- **Pronouns:** she/her
- **Are you legally authorized to work in the US?** Yes
  - Note: I am a US citizen
- **Are you willing to relocate?** No
  - Note: not with **bold** words

## Candidate
- **Email:** jane.doe@example.com
"""


def test_parse_standard_answers_unescape_stars_and_keep_a_trailing_colon():
    p = apply_sheet.parse_apply_md(_ESCAPED)
    assert p["standard_answers"] == [
        ("Rate your SQL skill from 1 to 5 *", "4 * strong"),
        ("Pronouns:", "she/her"),
        ("Are you legally authorized to work in the US?", "Yes"),
        ("Are you willing to relocate?", "No")]
    # the other sections' labels still lose their colon
    assert p["candidate"] == {"email": "jane.doe@example.com"}


def test_parse_signature_name():
    p = apply_sheet.parse_apply_md(_APPLY_MD)
    assert p["signature_name"] == "Jane Doe"


def test_parse_ignores_playbook_instruction_bullets():
    # The form-filler instructions are bold bullets too, but they live in the
    # Instructions section — they must NOT leak into candidate/standard answers.
    p = apply_sheet.parse_apply_md(_APPLY_MD)
    assert "Never click the final Submit / Apply / Send / Finish button." \
        not in dict(p["standard_answers"])
    assert all("Never click" not in k for k in p["candidate"])


def test_parse_empty_returns_empty_shape():
    p = apply_sheet.parse_apply_md("")
    assert p == {"candidate": {}, "address": {}, "standard_answers": [],
                 "signature_name": ""}
