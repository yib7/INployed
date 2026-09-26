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
from answer_bank import confirmed_bank, custom, standard_bank, unconfirmed  # noqa: E402
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
    """A synthetic answer bank: the shared confirmed answers (`answer_bank`),
    plus one custom entry the named keys do not cover."""
    return standard_bank() + [custom("salary_expectation", "What is your desired salary?",
                                     "Open to discussion")]


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


def test_form_digest_from_dict_tolerates_a_missing_autocomplete_key():
    """A digest stored before `autocomplete` existed still loads."""
    raw = {"url_host": "x", "title": "t", "text": "",
           "fields": [{"n": 0, "locator": [0, "#pw"], "label": "Password", "type": "other",
                       "required": False, "id_or_name": "pw"}],
           "buttons": []}
    back = apply_form.FormDigest.from_dict(raw)
    assert back.fields[0].autocomplete == ""
    raw["fields"][0]["autocomplete"] = "current-password"
    assert apply_form.FormDigest.from_dict(raw).fields[0].autocomplete == "current-password"


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


def _v1(**answers):
    """A version 1 bank (untyped, no confirmed flag) with these answers."""
    return [{"id": e["id"], "question": e["question"], "answer": answers.get(e["id"], ""),
             "kind": "fixed", "status": "active"} for e in apply_answers.seed_defaults()]


def test_build_reads_worded_yes_no_answers_once_migrated(tmp_path):
    # the 2026-09-26 Contoso run put "No" on "Are you legally authorized" from
    # "Yes, I am a US citizen"; the store now keeps "Yes" with the words as its
    # note (`migrate_v1`), and the fact is the typed answer
    v1 = _v1(work_authorized="Yes, I am a US citizen",
             requires_sponsorship="No, I am a US citizen",
             willing_to_relocate="Yes willing to relocate and open to on-site")
    cat = apply_facts.build(tmp_path, answers=apply_answers.migrate_v1(v1)[0])
    assert cat.value("work_authorized") == "Yes"
    assert cat.value("requires_sponsorship") == "No"
    assert cat.value("willing_to_relocate") == "Yes"


def test_build_a_yes_no_answer_without_yes_or_no_gives_no_fact(tmp_path):
    # moved to the store's rule on purpose (cycle 18): the words migrate into
    # the note and the answer stays not set, so no form gets "Open to NYC"
    migrated = apply_answers.migrate_v1(_v1(willing_to_relocate="Open to NYC"))[0]
    cat = apply_facts.build(tmp_path, answers=migrated)
    assert cat.value("willing_to_relocate") == ""
    assert "willing_to_relocate" not in cat.to_criteria()


# A second bank whose every answer differs from `_bank()`'s.
_OTHER = {"work_authorized": "No", "requires_sponsorship": "Yes", "years_experience": "7",
          "willing_to_relocate": "No", "onsite_ok": "No", "gender": "Female",
          "race_ethnicity": "Asian", "veteran_status": "Decline to self-identify",
          "disability_status": "Decline to self-identify", "how_did_you_hear": "Referral",
          "authorization_statement": "A US citizen.",
          "address_street": "9 Elm Road", "address_city": "Springfield",
          "address_state": "Oregon", "address_zip": "97477", "address_country": "Canada"}


def test_build_the_store_outranks_a_stale_sheet_for_every_answer_and_the_address(folder):
    # FL-1: the sheet was written from `_bank()`; the store now holds other
    # answers, and every bank-backed fact is the store's
    sheet = (folder / "apply.md").read_text(encoding="utf-8")
    assert "- **Are you willing to relocate?** Yes" in sheet
    assert "- **Street:** 123 Main Street" in sheet
    bank = confirmed_bank(**_OTHER) + [custom("salary_expectation",
                                              "What is your desired salary?", "90,000")]
    cat = apply_facts.build(folder, answers=bank)
    for eid, answer in _OTHER.items():
        key = "answer_authorization_statement" if eid == "authorization_statement" else eid
        assert cat.value(key) == answer, eid
    assert cat.value("answer_salary_expectation") == "90,000"


def test_build_the_sheet_fills_no_answer_the_store_lacks(folder):
    # FL-1, moved on purpose: the sheet's Standard answers and Address used to
    # fill what the bank lacked; now a store with nothing set gives no answer
    bank = [dict(e, answer="", confirmed=False) for e in apply_answers.seed_defaults()]
    cat = apply_facts.build(folder, answers=bank)
    for key in (*apply_facts._NAMED_BANK_IDS, "answer_authorization_statement",
                "answer_salary_expectation"):
        assert cat.value(key) == "", key
        assert key not in cat.to_criteria(), key
    # the sheet still gives the candidate, the education and the current job
    assert cat.value("email") == "jane.doe@example.com"
    assert cat.value("education_school") == "State University"
    assert cat.value("current_company") == "Acme Corp"


def test_build_an_unconfirmed_answer_yields_no_fact(folder):
    bank = unconfirmed(_bank(), "work_authorized", "years_experience", "address_city",
                       "salary_expectation")
    cat = apply_facts.build(folder, answers=bank)
    for key in ("work_authorized", "years_experience", "address_city",
                "answer_salary_expectation"):
        assert cat.value(key) == "", key
        assert not cat.has(key) and key not in cat.to_criteria(), key
    blob = json.dumps(cat.to_criteria()) + cat.verification_excerpt()
    assert "Open to discussion" not in blob
    # the confirmed ones still read
    assert cat.value("requires_sponsorship") == "No"
    assert cat.value("address_street") == "123 Main Street"


@pytest.mark.parametrize("entry", [
    # a version 1 row: no type, no confirmed flag
    {"id": "work_authorized", "question": "Are you legally authorized to work in the US?",
     "answer": "true", "kind": "fixed", "status": "active"},
    # typed and confirmed, with an answer its type does not allow
    {"id": "work_authorized", "question": "Are you legally authorized to work in the US?",
     "type": "yes_no", "answer": "true", "note": "", "confirmed": True, "status": "active"},
    {"id": "work_authorized", "question": "Are you legally authorized to work in the US?",
     "type": "text", "answer": "Yes", "note": "", "confirmed": True, "status": "active"},
])
def test_build_reads_an_answer_only_through_fact_value(tmp_path, entry):
    cat = apply_facts.build(tmp_path, answers=[entry])
    assert cat.value("work_authorized") == ""


def test_build_a_named_answer_is_never_a_custom_fact_too(folder):
    cat = apply_facts.build(folder, answers=_bank())
    named = [k for k in cat.facts if k.startswith("answer_")
             and k[len("answer_"):] in apply_facts._NAMED_BANK_IDS]
    assert named == []


# --- FL-5: the fact descriptions and onsite_ok ------------------------------------

def test_the_years_relocate_and_onsite_descriptions_are_the_specs():
    d = apply_facts.DESCRIPTIONS
    assert d["years_experience"] == ("Total years of professional work experience across all "
                                     "jobs (not years with one skill, tool or language)")
    assert d["willing_to_relocate"] == ("Whether the candidate is willing to relocate to the "
                                        "job's location")
    assert d["onsite_ok"] == ("Whether the candidate is willing to work on-site in the "
                              "employer's office, in person")


def test_onsite_ok_is_a_named_yes_no_fact(folder):
    assert apply_facts._KIND_BY_KEY["onsite_ok"] == "bool"
    assert "onsite_ok" in apply_facts._NAMED_BANK_IDS
    # one list of the yes/no answers: the store's
    assert apply_facts._BOOL_BANK_IDS == apply_answers.BOOL_IDS
    assert all(apply_facts._KIND_BY_KEY[k] == "bool" for k in apply_answers.BOOL_IDS)
    cat = apply_facts.build(folder, answers=_bank())
    assert cat.value("onsite_ok") == "Yes"
    assert cat.facts["onsite_ok"].kind == "bool"
    assert "answer_onsite_ok" not in cat.facts
    no = apply_facts.build(folder, answers=standard_bank(onsite_ok="No"))
    assert no.value("onsite_ok") == "No"


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


def test_sheet_excerpt_includes_grounding_evidence_from_experience_and_education(folder):
    cat = apply_facts.build(folder, answers=_bank())
    ex = cat.sheet_excerpt()
    assert "## Candidate" in ex and "### Address" in ex and "## Standard answers" in ex
    assert "jane.doe@example.com" in ex and "123 Main Street" in ex
    assert "Are you legally authorized to work in the US?" in ex
    assert "## Work experience" in ex and "Built the ingestion pipeline." in ex
    assert "State University" in ex and "Computer Science" in ex
    assert "## Cover letter" not in ex
    assert len(cat.sheet_excerpt(max_chars=100)) <= 100


def test_verification_evidence_includes_bank_fallbacks_and_artifact_names(folder):
    bank = _bank() + [custom("availability", "When can you start?", "October 15")]
    cat = apply_facts.build(folder, answers=bank)
    evidence = cat.verification_excerpt()
    for expected in ("State University", "Software Engineer", "When can you start?",
                     "October 15", "Jane_Doe_Resume.pdf", "Jane_Doe_Cover_Letter.pdf"):
        assert expected in evidence
    assert str(folder) not in evidence


def test_verification_evidence_limits_facts_without_losing_their_values():
    cat = apply_facts.FactCatalog([
        apply_facts.Fact("email", "person@example.com", "Email address"),
        apply_facts.Fact("answer_late", "start after October 15", "Start date"),
        apply_facts.Fact("resume_file", r"C:\private\documents\resume.pdf", "Resume", "file"),
    ])
    evidence = cat.verification_excerpt(["answer_late", "resume_file"])
    assert "Start date" in evidence and "start after October 15" in evidence
    assert "resume.pdf" in evidence
    assert "private" not in evidence and "person@example.com" not in evidence


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
