"""Tests for local/apply_facts.py (the fact catalog the auto-apply loop reasons
with) and the FormDigest dataclasses in local/apply_form.py.

Everything here is synthetic: the apply.md text is written by
`apply_data.build_markdown` over a synthetic master and a synthetic answer bank,
the PDFs are empty files in a temp folder, and `build()` always receives the bank
through its `answers=` argument so the user's real store is never read.
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_facts  # noqa: E402
import apply_form  # noqa: E402
from resume_tailor import apply_answers, apply_config, apply_data  # noqa: E402

_MASTER = {
    "basics": {"name": "Jane Q Doe", "email": "jane.doe@example.com",
               "phone": "555-555-0100", "location": "Anytown, CA",
               "linkedin": "https://linkedin.com/in/janedoe",
               "github": "https://github.com/janedoe"},
    "education": [{"school": "State University", "degree": "B.S.",
                   "concentration": "Computer Science", "dates": "2020 - 2024",
                   "gpa": "3.8"}],
    "experience": [
        {"org": "Acme Corp", "title": "Software Engineer", "location": "Anytown, CA",
         "dates": "2024-06 / present", "achievements": [{"id": "a1"}]},
    ],
    "projects": [],
    "leadership": [],
}
_JOB = {"job_posting_id": "42", "company_name": "Acme", "job_title": "Engineer",
        "url": "https://example.com/job/42"}
_SEL = {"experience": [{"name": "Acme Corp", "groups": [["a1"]]}]}
_BULLETS = {"a1": "Built the ingestion pipeline."}


def _bank():
    """A synthetic answer bank: the seeded defaults with values filled in, plus
    one custom entry the named keys do not cover."""
    bank = apply_answers.seed_defaults()
    values = {"work_authorized": "true", "requires_sponsorship": "false",
              "years_experience": "2", "willing_to_relocate": "true",
              "gender": "Decline to self-identify",
              "race_ethnicity": "Decline to self-identify",
              "veteran_status": "I am not a veteran",
              "disability_status": "No, I do not have a disability",
              "how_did_you_hear": "LinkedIn",
              "address_street": "123 Main Street", "address_city": "Anytown",
              "address_state": "California", "address_zip": "12345",
              "address_country": "United States"}
    for e in bank:
        if e["id"] in values:
            e["answer"] = values[e["id"]]
    bank.append({"id": "salary_expectation", "question": "What is your desired salary?",
                 "answer": "Open to discussion", "kind": "open-ended", "status": "active"})
    return bank


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")


@pytest.fixture
def folder(tmp_path):
    md = apply_data.build_markdown(_MASTER, _JOB, _bank(), sel=_SEL, bullets=_BULLETS,
                                   cover_body="Dear hiring team,\n\nI am writing to apply.")
    (tmp_path / "apply.md").write_text(md, encoding="utf-8")
    (tmp_path / "Jane_Doe_Resume.pdf").write_bytes(b"%PDF-1.4 resume")
    (tmp_path / "Jane_Doe_Cover_Letter.pdf").write_bytes(b"%PDF-1.4 cover")
    return tmp_path


# --- FormDigest dataclasses ---------------------------------------------------

def test_form_digest_json_round_trip():
    digest = apply_form.FormDigest(
        url_host="boards.greenhouse.io", title="Apply", text="Application form",
        fields=[apply_form.Field(n=0, locator=(0, "#first_name"), label="First Name",
                                 type="text", required=True, id_or_name="first_name"),
                apply_form.Field(n=1, locator=(1, "select[name=q1]"), label="Gender",
                                 type="select", required=False,
                                 options=["Male", "Female", "Decline to self-identify"])],
        buttons=[apply_form.Button(n=0, locator=(0, "button[type=submit]"),
                                   text="Submit application", kind_hint="submit")])
    raw = json.loads(json.dumps(digest.to_dict()))
    back = apply_form.FormDigest.from_dict(raw)
    assert back == digest
    assert back.fields[1].locator == (1, "select[name=q1]")
    assert isinstance(back.fields[1].locator, tuple)


def test_field_defaults_are_independent():
    a = apply_form.Field(n=0, locator=(0, "#a"), label="A", type="text", required=False)
    b = apply_form.Field(n=1, locator=(0, "#b"), label="B", type="text", required=False)
    a.options.append("x")
    assert b.options == []
    assert a.placeholder == "" and a.help == "" and a.id_or_name == ""


def test_field_types_are_the_listed_set():
    assert set(apply_form.FIELD_TYPES) == {
        "text", "email", "tel", "url", "number", "textarea", "select", "radio",
        "checkbox", "file", "date", "listbox", "other"}


# --- build(): the checkpoint (a) ---------------------------------------------

def test_build_splits_the_name(folder):
    cat = apply_facts.build(folder, answers=_bank())
    assert cat.value("full_name") == "Jane Q Doe"
    assert cat.value("first_name") == "Jane"
    assert cat.value("last_name") == "Q Doe"
    assert cat.value("email") == "jane.doe@example.com"
    assert cat.value("phone") == "555-555-0100"
    assert cat.value("location") == "Anytown, CA"
    assert cat.value("linkedin_url") == "https://linkedin.com/in/janedoe"
    assert cat.value("github_url") == "https://github.com/janedoe"


def test_build_points_resume_and_cover_at_the_folder_pdfs(folder):
    cat = apply_facts.build(folder, answers=_bank())
    assert cat.value("resume_file") == str(folder / "Jane_Doe_Resume.pdf")
    assert cat.value("cover_letter_file") == str(folder / "Jane_Doe_Cover_Letter.pdf")
    assert cat.facts["resume_file"].kind == "file"
    assert cat.value("cover_letter_text").startswith("Dear hiring team,")


def test_build_without_pdfs_leaves_the_file_facts_empty(tmp_path):
    (tmp_path / "apply.md").write_text(
        apply_data.build_markdown(_MASTER, _JOB, _bank()), encoding="utf-8")
    cat = apply_facts.build(tmp_path, answers=_bank())
    assert cat.value("resume_file") == ""
    assert cat.value("cover_letter_file") == ""
    assert cat.value("cover_letter_text") == ""
    assert not cat.has("resume_file")
    assert "resume_file" not in cat.to_criteria()


def test_build_one_answer_fact_per_uncovered_bank_entry(folder):
    cat = apply_facts.build(folder, answers=_bank())
    # The DEFAULTS ids map onto the named keys; only the remainder become answer_<id>.
    assert cat.value("work_authorized") == "Yes"
    assert cat.value("requires_sponsorship") == "No"
    assert cat.value("willing_to_relocate") == "Yes"
    assert cat.value("years_experience") == "2"
    assert cat.value("gender") == "Decline to self-identify"
    assert cat.value("how_did_you_hear") == "LinkedIn"
    assert "answer_work_authorized" not in cat.facts
    assert cat.value("answer_authorization_statement").startswith("Authorized to work")
    assert cat.value("answer_salary_expectation") == "Open to discussion"
    assert cat.facts["answer_salary_expectation"].description == "What is your desired salary?"
    assert cat.facts["work_authorized"].kind == "bool"


def test_build_reads_address_education_and_current_job(folder):
    cat = apply_facts.build(folder, answers=_bank())
    assert cat.value("address_street") == "123 Main Street"
    assert cat.value("address_city") == "Anytown"
    assert cat.value("address_state") == "California"
    assert cat.value("address_zip") == "12345"
    assert cat.value("address_country") == "United States"
    assert cat.value("education_school") == "State University"
    assert cat.value("education_degree") == "B.S."
    assert cat.value("education_field") == "Computer Science"
    assert cat.value("education_grad_year") == "2024"
    assert cat.value("current_company") == "Acme Corp"
    assert cat.value("current_title") == "Software Engineer"


def test_build_signature_and_today(folder):
    cat = apply_facts.build(folder, answers=_bank(), today=date(2026, 9, 21))
    assert cat.value("signature_name") == "Jane Q Doe"
    assert cat.value("today") == "2026-09-21"
    assert cat.facts["today"].kind == "date"


def test_build_blank_education_when_absent(tmp_path):
    master = dict(_MASTER, education=[], experience=[])
    (tmp_path / "apply.md").write_text(
        apply_data.build_markdown(master, _JOB, _bank()), encoding="utf-8")
    cat = apply_facts.build(tmp_path, answers=_bank())
    for key in ("education_school", "education_degree", "education_field",
                "education_grad_year", "current_company", "current_title"):
        assert cat.value(key) == ""
        assert key not in cat.to_criteria()


def test_build_master_basics_fill_gaps(folder):
    cat = apply_facts.build(folder, answers=_bank(),
                            master_basics={"website": "https://janedoe.dev"})
    assert cat.value("website_url") == "https://janedoe.dev"


def test_to_criteria_carries_descriptions_only(folder):
    cat = apply_facts.build(folder, answers=_bank())
    crit = cat.to_criteria()
    assert crit["first_name"] == "The candidate's first (given) name"
    assert set(crit) <= set(cat.facts)
    blob = json.dumps(crit)
    for value in ("Jane", "jane.doe@example.com", "555-555-0100", "123 Main Street",
                  "Open to discussion", "Anytown"):
        assert value not in blob
    # every key in the criteria has a value the loop can type
    assert all(cat.has(k) for k in crit)


def test_sheet_excerpt_is_candidate_address_and_standard_answers(folder):
    cat = apply_facts.build(folder, answers=_bank())
    ex = cat.sheet_excerpt()
    assert "## Candidate" in ex and "### Address" in ex and "## Standard answers" in ex
    assert "jane.doe@example.com" in ex and "123 Main Street" in ex
    assert "Are you legally authorized to work in the US?" in ex
    assert "## Work experience" not in ex and "Built the ingestion" not in ex
    assert "## Cover letter" not in ex
    assert len(cat.sheet_excerpt(max_chars=100)) <= 100


def test_sheet_excerpt_cuts_on_a_line_boundary(folder):
    cat = apply_facts.build(folder, answers=_bank())
    full = cat.sheet_excerpt()
    assert "\n" in full[:100]
    for cap in (100, 250, 777):
        cut = cat.sheet_excerpt(max_chars=cap)
        assert len(cut) <= cap
        assert full.startswith(cut)
        assert full[len(cut)] == "\n"          # the cut lands at a line end, never inside one
    assert cat.sheet_excerpt(max_chars=len(full) + 10) == full
    # a first line longer than the cap is the one case where mid-line is unavoidable
    assert apply_facts.FactCatalog(sheet_text="## Candidate\n" + "x" * 50,
                                   ).sheet_excerpt(max_chars=5) == "## Ca"


def test_build_with_no_apply_md_gives_an_empty_catalog(tmp_path):
    cat = apply_facts.build(tmp_path, answers=[])
    assert cat.value("first_name") == ""
    assert cat.value("today")
    assert cat.sheet_excerpt() == ""


def test_descriptions_carry_no_em_dash_or_contrast_framing():
    import re
    blob = " ".join(apply_facts.DESCRIPTIONS.values())
    assert chr(0x2014) not in blob
    assert not re.search(r",\s*not\s|\brather than\b|\binstead of\b|\bnot just\b", blob)


# --- quick_map -----------------------------------------------------------------

@pytest.mark.parametrize("label,ident,type_,expected", [
    ("First Name", "first_name", "text", "first_name"),
    ("First name *", "", "text", "first_name"),
    ("Given name", "", "text", "first_name"),
    ("", "job_application[first_name]", "text", "first_name"),
    ("Last Name", "last_name", "text", "last_name"),
    ("Surname", "", "text", "last_name"),
    ("Family name", "", "text", "last_name"),
    ("Email", "email", "email", "email"),
    ("Email address", "", "text", "email"),
    ("Phone", "phone", "tel", "phone"),
    ("Mobile", "", "text", "phone"),
    ("Telephone number", "", "text", "phone"),
    ("LinkedIn Profile", "", "text", "linkedin_url"),
    ("GitHub", "", "url", "github_url"),
    ("Website", "", "url", "website_url"),
    ("Portfolio", "", "text", "website_url"),
    ("Resume/CV", "resume", "file", "resume_file"),
    ("CV", "", "file", "resume_file"),
    ("Resume", "", "text", None),
    ("Cover Letter", "cover_letter", "file", "cover_letter_file"),
    ("Cover Letter", "cover_letter_text", "textarea", None),
    ("City", "", "text", "address_city"),
    ("State", "", "select", "address_state"),
    ("Province", "", "text", "address_state"),
    ("Zip", "", "text", "address_zip"),
    ("Postal code", "", "text", "address_zip"),
    ("Country", "", "listbox", "address_country"),
    # address tokens: at most four label tokens and no other content noun
    ("State / Province", "", "select", "address_state"),
    ("Zip / Postal code", "", "text", "address_zip"),
    ("Home address city", "", "text", "address_city"),
    ("Country of residence", "", "select", "address_country"),
    ("", "job_application[country]", "select", "address_country"),
    ("State your desired salary", "", "text", None),
    ("Country of work authorization", "", "select", None),
    ("Which state are you currently located in?", "", "select", None),
    ("Employer city", "", "text", None),
    ("Company state", "", "text", None),
    ("Country of citizenship", "", "select", None),
    ("Preferred state", "", "select", None),
    ("Username", "", "text", None),
    ("User city", "", "text", None),
    # `name` alone: exactly name / full name / your name
    ("Full name", "", "text", "full_name"),
    ("Name", "name", "text", "full_name"),
    ("Your name", "", "text", "full_name"),
    ("Preferred name", "", "text", None),
    ("Company name", "", "text", None),
    ("Referrer name", "", "text", None),
    ("Employer name", "", "text", None),
    ("Phone type", "", "select", None),
    ("Why do you want to work here?", "question_1", "textarea", None),
    ("", "", "text", None),
])
def test_quick_map_table(label, ident, type_, expected):
    assert apply_facts.quick_map(label, ident, type_) == expected
