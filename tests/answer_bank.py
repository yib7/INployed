"""A confirmed, typed answer bank for the tests.

The run reads an answer only through `apply_answers.fact_value`, which gives ""
for an entry the user has not confirmed. A test bank that means "the user saved
these answers" confirms each entry it sets, through `confirmed_bank`, so a test
written before the store was typed keeps its meaning.
"""
from __future__ import annotations

from typing import Any

from resume_tailor import apply_answers

# The synthetic answers the fact, judge and runner tests share.
ANSWERS = {
    "work_authorized": "Yes", "requires_sponsorship": "No", "years_experience": "2",
    "willing_to_relocate": "Yes", "onsite_ok": "Yes",
    "gender": "Decline to self-identify", "race_ethnicity": "Decline to self-identify",
    "veteran_status": "I am not a protected veteran",
    "disability_status": "No, I do not have a disability and have not had one in the past",
    "how_did_you_hear": "LinkedIn",
    "address_street": "123 Main Street", "address_city": "Anytown",
    "address_state": "California", "address_zip": "12345", "address_country": "United States",
}


def confirmed_bank(**answers: str) -> list[dict[str, Any]]:
    """`apply_answers.seed_defaults()` with each keyword's answer set by id, and
    every entry that holds an answer confirmed ("" leaves it not set). An id the
    built-ins lack raises KeyError; add a custom entry with `custom`."""
    bank = apply_answers.seed_defaults()
    by_id = {e["id"]: e for e in bank}
    for eid, answer in answers.items():
        by_id[eid]["answer"] = answer
    for e in bank:
        e["confirmed"] = bool(e["answer"])
    return bank


def standard_bank(**overrides: str) -> list[dict[str, Any]]:
    """`confirmed_bank` over `ANSWERS`, each keyword replacing one answer."""
    return confirmed_bank(**{**ANSWERS, **overrides})


def custom(eid: str, question: str, answer: str, *, type: str = "text",
           confirmed: bool = True, note: str = "") -> dict[str, Any]:
    """One custom entry of the typed store."""
    return {"id": eid, "question": question, "type": type, "answer": answer, "note": note,
            "confirmed": confirmed, "status": "active"}


def unconfirmed(bank: list[dict[str, Any]], *ids: str) -> list[dict[str, Any]]:
    """`bank` with the named entries' confirmation taken back (their answers kept)."""
    for e in bank:
        if e["id"] in ids:
            e["confirmed"] = False
    return bank
