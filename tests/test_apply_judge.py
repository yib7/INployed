"""Tests for local/apply_judge.py: every Jev question and threshold the
auto-apply loop uses, built over synthetic digests and a synthetic catalog.

The readers are exercised with hand-built `jev.Answer`s (so a threshold test
pins one number) and, once, end to end through `FakeJev` over a 12-field
Greenhouse-shaped digest. The FakeJev rules (word overlap; `none`/`other`/
`leave_blank`/`no_match` on zero overlap; noul 0.9 on two content words) are
what the fixture labels are written against. No browser, no network.
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_facts  # noqa: E402
import apply_judge  # noqa: E402
import jev  # noqa: E402
from apply_form import Button, Field, FormDigest  # noqa: E402
from resume_tailor import apply_answers, apply_config, apply_data  # noqa: E402

_MASTER = {
    "basics": {"name": "Jane Doe", "email": "jane.doe@example.com",
               "phone": "555-555-0100", "location": "Anytown, CA",
               "linkedin": "https://linkedin.com/in/janedoe",
               "github": "https://github.com/janedoe"},
    "education": [{"school": "State University", "degree": "B.S.",
                   "concentration": "Computer Science", "dates": "2020 - 2024"}],
    "experience": [{"org": "Acme Corp", "title": "Software Engineer", "location": "Anytown",
                    "dates": "2024 / present", "achievements": [{"id": "a1"}]}],
    "projects": [], "leadership": [],
}
_JOB = {"job_posting_id": "42", "company_name": "Acme", "job_title": "Software Engineer",
        "url": "https://example.com/job/42"}


def _bank():
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
def catalog(tmp_path):
    md = apply_data.build_markdown(_MASTER, _JOB, _bank(),
                                   sel={"experience": [{"name": "Acme Corp", "groups": [["a1"]]}]},
                                   bullets={"a1": "Built the ingestion pipeline."},
                                   cover_body="Dear hiring team,\n\nI am writing to apply.")
    (tmp_path / "apply.md").write_text(md, encoding="utf-8")
    (tmp_path / "Jane_Doe_Resume.pdf").write_bytes(b"%PDF-1.4 resume")
    (tmp_path / "Jane_Doe_Cover_Letter.pdf").write_bytes(b"%PDF-1.4 cover")
    return apply_facts.build(tmp_path, answers=_bank(),
                             master_basics={"website": "https://janedoe.dev"},
                             today=date(2026, 9, 21))


def _f(n, label, type_="text", required=False, options=(), ident="", help=""):
    # an `other` control here stands for an `<input type=password>` (the
    # extractor's type for one, with `secret` set)
    return Field(n=n, locator=(0, f"#f{n}"), label=label, type=type_, required=required,
                 options=list(options), id_or_name=ident, help=help, secret=type_ == "other")


def _greenhouse(consent=""):
    """A 12-field Greenhouse-shaped page; `consent` adds a required checkbox
    with that label as field 12."""
    fields = [
        _f(0, "First Name", required=True, ident="first_name"),
        _f(1, "Last Name", required=True, ident="last_name"),
        _f(2, "Email", "email", required=True, ident="email"),
        _f(3, "Phone", "tel", ident="phone"),
        _f(4, "Resume/CV", "file", required=True, ident="resume"),
        _f(5, "Cover Letter", "file", ident="cover_letter"),
        _f(6, "LinkedIn Profile", ident="job_application[answers_attributes][0][text_value]"),
        _f(7, "Website", "url", ident="job_application[answers_attributes][1][text_value]"),
        _f(8, "Are you legally authorized to work in the United States?", "select",
           required=True, options=("Yes", "No")),
        _f(9, "Will you now or in the future require sponsorship for employment visa status?",
           "select", required=True, options=("Yes", "No")),
        _f(10, "Gender", "select", options=("Male", "Female", "Decline to self-identify")),
        _f(11, "Why are you interested in this role? Please write a short essay about "
               "your motivation.", "textarea"),
    ]
    if consent:
        fields.append(_f(12, consent, "checkbox", required=True))
    buttons = [Button(n=0, locator=(0, "#submit_app"), text="Submit application",
                      kind_hint="submit"),
               Button(n=1, locator=(0, "#back"), text="Back", kind_hint="link")]
    return FormDigest(url_host="boards.greenhouse.io",
                      title="Apply for Software Engineer at Acme",
                      text="Application form for Software Engineer. Fill in the form below "
                           "and submit your application.",
                      fields=fields, buttons=buttons)


def _choice(name, conf=1.0, names=()):
    names = list(names) or [name]
    probs = {n: (conf if n == name else round((1 - conf) / max(1, len(names) - 1), 4))
             for n in names}
    return jev.Answer(kind="choice", choice=name, probabilities=probs, confidence=conf)


def _noul(p):
    return jev.Answer(kind="noul", noul=p)


def _page_answers(digest, mapping, options=None, roles=None, *, prohibited=0.05,
                  account=0.05, captcha=0.05, state="application_form", state_conf=0.95):
    """Hand-built answers for `page_questions` over `digest`: `mapping` is
    n -> (key, confidence), `options` n -> (option, confidence), `roles`
    n -> (role, confidence)."""
    answers = {"page_state": _choice(state, state_conf),
               "asks_for_prohibited": _noul(prohibited),
               "requires_account": _noul(account),
               "has_captcha": _noul(captcha)}
    for f in digest.fields:
        key, conf = mapping.get(f.n, ("leave_blank", 1.0))
        answers[f"field_{f.n}_source"] = _choice(key, conf)
        if f.options:
            opt, oconf = (options or {}).get(f.n, ("no_match", 1.0))
            answers[f"field_{f.n}_option"] = _choice(opt, oconf)
            answers[f"field_{f.n}_pick"] = _choice(opt, oconf)
    for b in digest.buttons:
        role, conf = (roles or {}).get(b.n, ("other", 1.0))
        answers[f"button_{b.n}_role"] = _choice(role, conf)
    return answers


_CONTRAST = re.compile(r",\s*not\s|,\s*never\s|\bnot just\b|\brather than\b|\binstead of\b"
                       r"|\bnot\b[^.]{0,60}\bbut\b", re.I)


def _assert_clean_text(obj):
    blob = json.dumps(obj, ensure_ascii=False)
    assert chr(0x2014) not in blob
    assert not _CONTRAST.search(blob), _CONTRAST.search(blob).group(0)


# --- constants --------------------------------------------------------------------

def test_threshold_constants_match_the_spec_table():
    assert apply_judge.PAGE_STATE_MIN_CONF == 0.40
    assert apply_judge.FIELD_MAP_MIN_CONF == 0.70
    assert apply_judge.OPTION_MIN_CONF == 0.70
    assert apply_judge.BUTTON_SUBMIT_MIN_CONF == 0.50
    assert apply_judge.BUTTON_ADVANCE_MIN_CONF == 0.50
    assert apply_judge.VERIFY_MIN == 0.80
    # the flags park nothing on their own since 2026-09-22, so they have no gate
    assert not hasattr(apply_judge, "PROHIBITED_MAX")
    assert not hasattr(apply_judge, "CAPTCHA_MAX")
    assert apply_judge.GROUNDING_MIN == 0.70
    assert apply_judge.MAX_PAGES == 20
    assert apply_judge.PAGE_TEXT_CAP == 4000
    doc = " ".join(apply_judge.__doc__.split())
    assert "tuned 2026-09-25 (SP8b)" in doc and "UNTUNED" not in doc
    assert "cache.json" in doc and "matrix_cache.json" in doc


def test_the_option_tuples():
    assert apply_judge.PAGE_STATES == (
        "application_form", "login_wall", "signup_form", "review_page", "confirmation",
        "code_gate", "captcha_or_bot_check", "payment_request", "error_or_dead",
        "job_posting", "other")
    assert apply_judge.BUTTON_ROLES == ("advance", "submit", "back", "apply_entry",
                                        "upload", "other")
    assert apply_judge.SPECIAL_SOURCES == ("resume_file", "cover_letter_file",
                                           "cover_letter_text", "signature_today",
                                           "consent_attest", "needs_generation",
                                           "leave_blank")


# --- page_questions: checkpoint (b) ---------------------------------------------

def test_page_questions_state_shape(catalog):
    digest = _greenhouse()
    state, questions = apply_judge.page_questions(digest, catalog, _JOB)
    assert state["job"] == {"company": "Acme", "title": "Software Engineer"}
    assert state["page"]["url_host"] == "boards.greenhouse.io"
    assert state["page"]["title"] == digest.title
    assert state["page"]["headline_text"] == digest.text
    assert [f["n"] for f in state["fields"]] == list(range(12))
    assert state["fields"][8]["options"] == ["Yes", "No"]
    assert state["fields"][8]["required"] is True
    assert "placeholder" not in state["fields"][0]      # empty strings are dropped
    # no kind_hint; the DOM flags (READ-10)
    assert state["buttons"][0] == {"n": 0, "text": "Submit application", "in_form": False,
                                   "disabled": False, "primary": False}
    # `facts` is the one description map: the catalog's facts plus the special
    # sources, so every key a field-source Choice offers is described there once
    assert state["facts"] == {**apply_judge.SPECIAL_DESCRIPTIONS, **catalog.to_criteria()}
    assert state["facts"]["first_name"] == "The candidate's first (given) name"
    assert state["facts"]["leave_blank"] == apply_judge.SPECIAL_DESCRIPTIONS["leave_blank"]
    assert "Jane" not in json.dumps(state)


def test_page_questions_emit_every_question(catalog):
    digest = _greenhouse()
    state, q = apply_judge.page_questions(digest, catalog, _JOB)
    # SP4: the page state and the page's signals are the read's own request
    # (`read_questions`); the mapping asks the fields and the buttons
    assert "page_state" not in q and "has_captcha" not in q and "requires_account" not in q
    for i, f in enumerate(digest.fields):
        src = q[f"field_{f.n}_source"]
        assert src["type"] == "choice"
        assert "leave_blank" in src["criteria"] and "needs_generation" in src["criteria"]
        # the instruction names the description map and the field by position
        assert "`facts`" in src["instructions"] and f"`fields[{i}]`" in src["instructions"]
        assert set(src["criteria"]) <= set(state["facts"])
        assert all(v is None for v in src["criteria"].values())
        keys = set(src["criteria"])
        if f.type == "file":
            assert keys == {"resume_file", "cover_letter_file", "needs_generation",
                            "leave_blank"}
        elif f.type == "email":
            assert keys == {"email", "needs_generation", "leave_blank"}
        elif f.type == "tel":
            assert keys == {"phone", "needs_generation", "leave_blank"}
        elif f.type == "url":
            assert keys == {"linkedin_url", "github_url", "website_url",
                            "needs_generation", "leave_blank"}
        elif f.options:
            # a select never takes a name, an email, a URL, a file or prose
            assert {"work_authorized", "gender", "address_country",
                    "answer_salary_expectation"} <= keys
            assert not keys & {"first_name", "email", "linkedin_url", "resume_file",
                               "cover_letter_text", "signature_today", "consent_attest"}
        else:
            assert set(catalog.to_criteria()) - {"resume_file", "cover_letter_file",
                                                 "signature_name", "today"} <= keys
            assert "signature_today" in keys and "cover_letter_text" in keys
            assert "resume_file" not in keys and "consent_attest" not in keys
        key = apply_facts.quick_map(f.label, f.id_or_name, f.type)
        if f.options and key and catalog.has(key):
            opt = q[f"field_{f.n}_option"]
            assert list(opt["criteria"]) == f.options + ["no_match"]
            assert catalog.value(key) in json.dumps(opt["instructions"])
        else:
            # a model-mapped select's pick waits for the second request
            assert f"field_{f.n}_option" not in q
    for i, b in enumerate(digest.buttons):
        role = q[f"button_{b.n}_role"]
        assert set(role["criteria"]) == set(apply_judge.BUTTON_ROLES)
        assert f"buttons[{i}]" in json.dumps(role["instructions"])
    assert q["asks_for_prohibited"]["type"] == "noul"
    # every id is a real question the model sees in full (ids are never sent)
    assert all("instructions" in v for v in q.values())


def test_consent_attest_is_offered_to_checkboxes_only(catalog):
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "I certify that the information provided is accurate", "checkbox",
           required=True),
        _f(1, "Gender", "radio", options=("Male", "Female")),
        _f(2, "Anything else?", "textarea"),
        _f(3, "Resume", "file"),
    ])
    state, q = apply_judge.page_questions(digest, catalog, _JOB)
    offered = {f.n: "consent_attest" in q[f"field_{f.n}_source"]["criteria"]
               for f in digest.fields}
    assert offered == {0: True, 1: False, 2: False, 3: False}
    desc = state["facts"]["consent_attest"]
    assert set(desc) == {"what", "not_for"}
    assert "accuracy of the application" in desc["what"]
    assert "consent to be contacted" in desc["what"]
    assert "background-check" in desc["not_for"] and "non-compete" in desc["not_for"]
    _assert_clean_text(state["facts"])


def test_plan_consent_attest_checks_the_box(catalog):
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "I agree to the privacy notice", "checkbox", required=True),
        _f(1, "I consent to a background check", "checkbox", required=True),
        _f(2, "Subscribe to job alerts", "checkbox"),
    ])
    answers = _page_answers(digest, {0: ("consent_attest", 0.9),
                                     1: ("consent_attest", 0.65),      # below CONSENT_MIN_CONF
                                     2: ("leave_blank", 0.9)})
    p = apply_judge.plan(digest, catalog, answers)
    by_n = {f.n: f for f in p.fields}
    assert (by_n[0].action, by_n[0].option, by_n[0].value) == ("select", "checked", "yes")
    assert by_n[0].fact_key == "consent_attest"
    assert by_n[1].action == "skip" and by_n[1].fact_key is None
    assert by_n[2].action == "skip"
    assert p.park_reason == "required field without an answer: I consent to a background check"
    assert [m[0] for m in p.missing] == ["I consent to a background check",
                                         "Subscribe to job alerts"]


_ARBITRATION = "I agree to the terms, including the arbitration agreement"


@pytest.mark.parametrize("conf,expected", [(0.8, "skip"), (0.9, "select"), (0.85, "select")])
def test_plan_consent_attest_needs_its_own_higher_floor(catalog, conf, expected):
    """Ticking a box the loop cannot take back needs more than the ordinary
    mapping floor: a consent that names a commitment, below `CONSENT_MIN_CONF`
    (0.85, above `FIELD_MAP_MIN_CONF`), follows the ordinary unanswerable
    rule."""
    assert apply_judge.CONSENT_MIN_CONF == 0.85 > apply_judge.FIELD_MAP_MIN_CONF
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, _ARBITRATION, "checkbox", required=True)])
    p = apply_judge.plan(digest, catalog, _page_answers(digest, {0: ("consent_attest", conf)}))
    box = p.fields[0]
    assert box.action == expected and box.confidence == conf
    if expected == "select":
        assert (box.fact_key, box.option, box.value) == ("consent_attest", "checked", "yes")
        assert p.park_reason == "" and p.missing == []
    else:
        assert box.fact_key is None and box.option is None and box.value == ""
        assert p.park_reason == f"required field without an answer: {_ARBITRATION}"
        assert p.missing == [(_ARBITRATION, "checkbox")]


# --- routine consents tick at the mapping floor (controller decision, SP5 round 2) --------------

@pytest.mark.parametrize("label, conf, ticked", [
    ("I agree to the privacy notice for candidates", 0.72, True),
    ("I consent to a background check", 0.80, False),
    (_ARBITRATION, 0.80, False),
    ("I agree to receive SMS messages about my application", 0.80, False),
    ("I agree to the privacy notice for candidates", 0.68, False),     # under the mapping floor
])
def test_a_routine_consent_ticks_at_the_mapping_floor_and_a_commitment_does_not(
        catalog, label, conf, ticked):
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, label, "checkbox", required=True)])
    p = apply_judge.plan(digest, catalog, _page_answers(digest, {0: ("consent_attest", conf)}))
    box = p.fields[0]
    assert (box.action == "select" and box.option == "checked") is ticked, (label, conf)
    assert (p.park_reason == "") is ticked
    # a routine tick under CONSENT_MIN_CONF is no target of the second look
    targets = apply_judge.reask_targets(digest, catalog, _page_answers(
        digest, {0: ("consent_attest", conf)}), p, what="source")
    assert (targets == []) is ticked


# the probes of review R3-I1 (all keep CONSENT_MIN_CONF)
_CUT_300 = ("I have read the privacy notice for candidates and I understand how my personal "
            "information is collected, processed, stored and retained for this application, "
            "and I certify that the information provided in this application is true, "
            "accurate and complete to the best of my knowledge, and I agree to the terms and "
            "conditions of this application and to be contacted about")[:300]
_NAMED_COMMITMENTS = [
    "I consent to the Biometric Privacy Policy",
    "I agree to the Binding Dispute Resolution Terms",
    "I accept the Non-Disclosure Agreement Terms",
    "I consent to the Video Interview Recording Terms",
    "I agree to the Pre-Employment Assessment Terms",
]
_OPTIONAL_MARKETING = [
    "Contact me about any position",
    "I agree to be contacted about any role",
    "Contact me by email",
    "I accept the Talent Network Terms",
    "I accept the Job Alerts Terms",
    "I agree to the Future Opportunities Privacy Notice",
]


@pytest.mark.parametrize("label, company", [
    ("I agree to the privacy notice for candidates", ""),
    ("I certify that the information provided is accurate", ""),
    ("I agree with the terms and conditions of the application", ""),
    ("I confirm that the information in this application is accurate", ""),
    # the job's company before a possessive (review round 4, M2: no other name)
    ("I have read and accept Fabrikam's Privacy Policy", "Fabrikam"),
    ("I accept Acme's Privacy Policy", "Acme"),
    ("I have read and agree to Checkr's Privacy Policy", "Checkr"),
    ("I accept HireRight's Terms of Use", "HireRight Inc."),
    # the job's company before a routine noun
    ("I have read the Adatum Privacy Notice for Candidates", "Adatum"),
    ("I have read the Adatum Corporation Privacy Notice", "Adatum Corporation, Inc."),
    ("I consent to the processing and storage of my personal data for this application", ""),
    ("I agree to be contacted about this role", ""),
    ("I agree to be contacted regarding this position", ""),
    ("By checking this box, I acknowledge that I have read the Terms of Use", ""),
    ("I certify that the information in this application is true and complete to the best "
     "of my knowledge", ""),
])
def test_a_label_that_names_only_routine_things_is_a_routine_consent(label, company):
    assert apply_judge.routine_consent(label, company=company), label


@pytest.mark.parametrize("label, company", [(label, "Fabrikam") for label in [
    "I consent to a background check",
    "I consent to a criminal record check",
    "I consent to a credit check",
    "I agree to a drug test",
    "I am 18 years of age or older",
    "I confirm I am of legal working age",
    "I agree to the non-compete agreement",
    "I am willing to relocate",
    _ARBITRATION,
    "I agree to receive SMS messages about my application",
    "I agree to receive text messages about my application",
    "I agree to receive marketing emails",
    "Subscribe me to the newsletter",
    "I agree to share my data with partners",
    "I agree to be contacted about future opportunities",
    "I agree to the privacy policy and to share my data with Partners",
    "I agree",
    "I Agree To Share My Data With Partners",
    "I agree to be contacted about the role",
    # review R3-I1: other scripts, a label at the extractor's cap, commitments
    # written as names, the contact family without this application or role
    "Я согласен на проверку судимости и Privacy Policy",
    "我同意背景调查 Privacy Policy",
    _CUT_300,
    *_NAMED_COMMITMENTS,
    *_OPTIONAL_MARKETING,
]] + [
    # a name before a routine noun that is no job's company (none given, or another)
    ("I have read the Adatum Privacy Notice for Candidates", ""),
    ("I have read the Adatum Privacy Notice for Candidates", "Fabrikam"),
    # two capitalised words before a possessive are no single name
    ("I accept Talent Network's Privacy Policy", ""),
    # review round 4, M2: a possessive name that is no job's company (two
    # background-screening vendors)
    ("I have read and agree to Checkr's Privacy Policy", "Fabrikam"),
    ("I accept HireRight's Terms of Use", "Fabrikam"),
    ("I accept Acme's Privacy Policy", ""),
    # review round 4, M1: a label that ends on a function word was cut
    ("I agree to the Privacy Policy and the", ""),
    ("I certify that the information provided is accurate and", ""),
    ("I agree with the terms and conditions of the", ""),
    ("I have read the privacy notice of", ""),
    ("I agree to the terms of a", ""),
    ("I accept the privacy policy, and", ""),
])
def test_a_label_that_names_a_commitment_or_anything_else_is_no_routine_consent(label, company):
    assert len(_CUT_300) == 300
    assert not apply_judge.routine_consent(label, company=company), label


def test_a_consent_the_extractor_cut_or_an_optional_one_keeps_the_commitment_floor():
    routine = "I agree to the privacy notice for candidates"
    floor = apply_judge.consent_floor
    assert floor(routine, required=True) == apply_judge.FIELD_MAP_MIN_CONF
    # a label the extractor cut, or whose button or widget words it skipped
    assert floor(routine, required=True, partial=True) == apply_judge.CONSENT_MIN_CONF
    # an optional box
    assert floor(routine, required=False) == apply_judge.CONSENT_MIN_CONF
    for label in _OPTIONAL_MARKETING:
        assert floor(label, required=False) == apply_judge.CONSENT_MIN_CONF, label


@pytest.mark.parametrize("label, required, partial, ticked", [
    ("I agree to the privacy notice for candidates", True, False, True),
    ("I agree to the privacy notice for candidates", False, False, False),
    ("I agree to the privacy notice for candidates", True, True, False),
    ("I consent to the Biometric Privacy Policy", True, False, False),
    *[(label, False, False, False) for label in _OPTIONAL_MARKETING],
])
def test_plan_ticks_at_074_only_a_required_whole_routine_consent(catalog, label, required,
                                                                 partial, ticked):
    box = _f(0, label, "checkbox", required=required)
    box.label_partial = partial
    digest = FormDigest(url_host="x", title="t", text="", fields=[box])
    p = apply_judge.plan(digest, catalog, _page_answers(digest, {0: ("consent_attest", 0.74)}),
                         company="Fabrikam")
    assert (p.fields[0].action == "select") is ticked, label


def test_the_jobs_company_is_the_only_name_a_routine_consent_takes(catalog):
    label = "I have read the Fabrikam Privacy Notice"
    box = _f(0, label, "checkbox", required=True)
    digest = FormDigest(url_host="x", title="t", text="", fields=[box])
    answers = _page_answers(digest, {0: ("consent_attest", 0.74)})
    assert apply_judge.plan(digest, catalog, answers, company="Fabrikam").fields[0].action == \
        "select"
    assert apply_judge.plan(digest, catalog, answers).fields[0].action == "skip"
    assert apply_judge.plan(digest, catalog, answers, company="Adatum").fields[0].action == \
        "skip"


def test_unbacked_file_specials_are_not_offered(catalog, tmp_path):
    """A folder without PDFs (and a sheet without a cover letter) has no
    `resume_file`, `cover_letter_file` or `cover_letter_text` value: none of
    them is in `facts` or in any field's criteria, so the model cannot pick a
    file that does not exist."""
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "apply.md").write_text(apply_data.build_markdown(_MASTER, _JOB, _bank()),
                                   encoding="utf-8")
    cat = apply_facts.build(bare, answers=_bank())
    for key in ("resume_file", "cover_letter_file", "cover_letter_text"):
        assert not cat.has(key)
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Resume", "file", required=True, ident="resume"),
        _f(1, "Cover letter", "textarea"),
        _f(2, "Gender", "select", options=("Male", "Female")),
    ])
    state, q = apply_judge.page_questions(digest, cat, _JOB)
    absent = {"resume_file", "cover_letter_file", "cover_letter_text"}
    assert not absent & set(state["facts"])
    for f in digest.fields:
        assert not absent & set(q[f"field_{f.n}_source"]["criteria"]), f.label
    assert set(q["field_0_source"]["criteria"]) == {"needs_generation", "leave_blank"}
    assert "signature_today" in q["field_1_source"]["criteria"]
    # with the PDFs and the cover letter present they are offered as before
    state, q = apply_judge.page_questions(digest, catalog, _JOB)
    assert absent <= set(state["facts"])
    assert set(q["field_0_source"]["criteria"]) == {"resume_file", "cover_letter_file",
                                                    "needs_generation", "leave_blank"}
    assert "cover_letter_text" in q["field_1_source"]["criteria"]


def test_page_questions_name_fields_by_position_and_carry_n(catalog):
    """`n` is a field's identity across `plan`; the instruction path is the
    position in `state.fields`, and the state entry carries `n` explicitly."""
    digest = FormDigest(url_host="x", title="t", text="",
                        fields=[_f(3, "First Name", ident="first_name"),
                                _f(7, "Country", "select", options=("United States", "Canada"))],
                        buttons=[Button(n=5, locator=(0, "#b"), text="Next")])
    state, q = apply_judge.page_questions(digest, catalog, _JOB)
    assert [f["n"] for f in state["fields"]] == [3, 7]
    assert set(k for k in q if k.startswith("field_")) ==         {"field_3_source", "field_7_source", "field_7_option"}
    assert "`fields[0]`" in q["field_3_source"]["instructions"]
    assert "`fields[1]`" in q["field_7_source"]["instructions"]
    assert "fields[1].label" in json.dumps(q["field_7_option"]["instructions"])
    assert "`buttons[0]`" in q["button_5_role"]["instructions"]
    assert not any(f"fields[{n}]" in json.dumps(q) for n in (3, 7))


def test_button_roles_separate_a_wizard_continue_from_the_final_submit(catalog):
    """A multi-step form's Continue is often an HTML `type=submit` control and
    the extractor's `kind_hint` says `submit` for it (and for "Apply now": the
    hint is a regex over the text). The live judge took the word literally
    (advance 0.72 / submit 0.28, confidence 0.66, below the advance gate;
    apply_entry 0.55 / submit 0.45 on a posting). The state now carries the
    button text alone and the criteria say what each role is not for. The
    fake reads the same roles."""
    for role in ("advance", "submit"):
        assert apply_judge._BUTTON_CRITERIA[role]["not_for"]
    digest = FormDigest(url_host="x", title="t", text="",
                        fields=[_f(0, "First name", ident="first_name")],
                        buttons=[Button(n=0, locator=(0, "#c"), text="Continue",
                                        kind_hint="submit"),
                                 Button(n=1, locator=(0, "#s"), text="Submit application",
                                        kind_hint="submit"),
                                 Button(n=2, locator=(0, "#b"), text="Back", kind_hint="button"),
                                 Button(n=3, locator=(0, "#l"), text="Sign in",
                                        kind_hint="submit")])
    state, q = apply_judge.page_questions(digest, catalog, _JOB)
    assert all(set(b) == {"n", "text", "in_form", "disabled", "primary"}
               for b in state["buttons"])
    answers = jev.FakeJev().judge(state, q)
    assert [answers[f"button_{n}_role"].choice for n in range(4)] ==         ["advance", "submit", "back", "advance"]


def test_source_instruction_relates_the_escapes_to_facts(catalog):
    _, q = apply_judge.page_questions(_greenhouse(), catalog, _JOB)
    text = q["field_11_source"]["instructions"]
    assert text.startswith("Which key of `facts` describes what")
    assert "`leave_blank`" in text and "`needs_generation`" in text
    assert "for an essay question no fact answers, `needs_generation`" in text
    assert "written" in apply_judge.SPECIAL_DESCRIPTIONS["needs_generation"]
    assert "blank" in apply_judge.SPECIAL_DESCRIPTIONS["leave_blank"]


def test_button_role_criteria_are_structured_and_advance_covers_sign_in(catalog):
    """Spec 3.5 reads a login wall's Sign in button as `advance`: the criteria
    say so in examples, in the same `what` / `examples` shape for every role."""
    digest = FormDigest(url_host="x", title="t", text="",
                        buttons=[Button(n=0, locator=(0, "#s"), text="Sign in")])
    _, q = apply_judge.page_questions(digest, catalog, _JOB)
    crit = q["button_0_role"]["criteria"]
    for role in apply_judge.BUTTON_ROLES:
        keys = {"what", "examples"} | ({"not_for"} if role in ("advance", "submit") else set())
        assert set(crit[role]) == keys, role
    advance = " | ".join(crit["advance"]["examples"]).lower()
    for text in ("sign in", "log in", "create account", "continue with email", "next"):
        assert text in advance, text
    assert "submit application" in " | ".join(crit["submit"]["examples"]).lower()
    # the fake reads Sign in as advance too
    answers = jev.FakeJev().judge(*apply_judge.page_questions(digest, catalog, _JOB))
    assert answers["button_0_role"].choice == "advance"


def test_page_questions_option_instruction_carries_the_quick_map_value(catalog):
    digest = FormDigest(url_host="x", title="t", text="",
                        fields=[_f(0, "Country", "select", options=("United States", "Canada")),
                                _f(1, "Gender", "select", options=("Male", "Female"))])
    _, q = apply_judge.page_questions(digest, catalog, _JOB)
    blob = json.dumps(q["field_0_option"]["instructions"])
    assert "United States" in blob and "fields[0].label" in blob
    # no quick_map hit: no pick until the fact is known (`option_questions`)
    assert "field_1_option" not in q


def test_page_questions_cap_help_options_and_text(catalog):
    long_help = "h" * 500
    digest = FormDigest(url_host="x", title="t", text="z" * 10_000,
                        fields=[_f(0, "Country", "select", help=long_help,
                                   options=[f"opt{i}" for i in range(100)])])
    state, q = apply_judge.page_questions(digest, catalog, _JOB)
    # 1,200 since 2026-09-22: LinkedIn's posting text began past character 600
    assert len(state["page"]["headline_text"]) == apply_judge.HEADLINE_CHARS == 1200
    assert len(state["fields"][0]["help"]) == 200
    assert len(state["fields"][0]["options"]) == 40
    assert len(q["field_0_option"]["criteria"]) == 41


def test_page_request_serialises_under_60k_for_40_fields(catalog):
    fields = []
    for i in range(40):
        opts = ("Yes", "No", "Prefer not to say") if i % 3 == 0 else ()
        fields.append(_f(i, f"Question {i}: tell us about requirement number {i} "
                            "and how you meet it", "select" if opts else "text",
                         required=i % 2 == 0, options=opts, ident=f"question_{i}",
                         help="Optional help text for this question." if i % 4 == 0 else ""))
    digest = FormDigest(url_host="jobs.example.com", title="Apply", text="x" * 4000,
                        fields=fields,
                        buttons=[Button(n=i, locator=(0, f"#b{i}"), text=f"Button {i}")
                                 for i in range(6)])
    state, q = apply_judge.page_questions(digest, catalog, _JOB)
    # the SDK serialises with pydantic-core's to_json: compact separators
    blob = json.dumps({"state": state, "questions": q}, separators=(",", ":"))
    assert len(blob) < 60_000, len(blob)
    _assert_clean_text(q)


def test_every_choice_carries_an_escape_option(catalog):
    _, q = apply_judge.page_questions(_greenhouse(), catalog, _JOB)
    _, q2 = apply_judge.code_pick_questions(["123456", "654321"], "Your code is 123456")
    escapes = {"other", "none", "leave_blank", "no_match"}
    for qid, spec in {**q, **q2}.items():
        if spec["type"] == "choice":
            assert escapes & set(spec["criteria"]), qid


def test_question_text_is_clean(catalog):
    _, q = apply_judge.page_questions(_greenhouse(), catalog, _JOB)
    _assert_clean_text(q)
    _, q = apply_judge.verify_questions([{"n": 0, "label": "A", "value": "b"}], "sheet")
    _assert_clean_text(q)
    _, q = apply_judge.inbox_questions([{"n": 0, "sender": "a", "subject": "b",
                                         "preview": "c"}], "greenhouse.io")
    _assert_clean_text(q)
    _, q = apply_judge.grounding_questions(["A sentence."], "sheet")
    _assert_clean_text(q)


# --- read_page_state -------------------------------------------------------------

def test_read_page_state():
    assert apply_judge.read_page_state({"page_state": _choice("login_wall", 0.8)}) == \
        ("login_wall", 0.8)
    assert apply_judge.read_page_state({}) == ("other", 0.0)


# --- plan: checkpoint (c) -----------------------------------------------------------

def test_plan_parks_a_low_confidence_required_field_and_flags_an_optional_one(catalog):
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Desired salary", required=True),
        _f(1, "Referral name"),
        _f(2, "Email", "email", required=True, ident="email"),
    ])
    answers = _page_answers(digest, {0: ("answer_salary_expectation", 0.5),
                                     1: ("full_name", 0.4), 2: ("email", 0.99)})
    p = apply_judge.plan(digest, catalog, answers)
    by_n = {f.n: f for f in p.fields}
    assert by_n[0].action == "skip" and by_n[0].fact_key is None
    assert by_n[1].action == "skip" and by_n[1].value == ""
    assert by_n[2].action == "fill" and by_n[2].value == "jane.doe@example.com"
    assert p.park_reason == "required field without an answer: Desired salary"
    assert [m[0] for m in p.missing] == ["Desired salary", "Referral name"]
    assert p.flags == {"asks_for_prohibited": 0.05, "requires_account": 0.05,
                       "has_captcha": 0.05}


def test_plan_quick_map_wins_over_the_model(catalog, caplog):
    import logging
    digest = FormDigest(url_host="x", title="t", text="",
                        fields=[_f(0, "First Name", required=True, ident="first_name")])
    answers = _page_answers(digest, {0: ("last_name", 0.95)})
    with caplog.at_level(logging.DEBUG, logger="apply_judge"):
        p = apply_judge.plan(digest, catalog, answers)
    assert p.fields[0].fact_key == "first_name"
    assert p.fields[0].value == "Jane"
    assert p.fields[0].action == "fill"
    assert any("first_name" in r.getMessage() and "last_name" in r.getMessage()
               for r in caplog.records)


def test_plan_quick_map_hit_with_an_empty_fact_falls_through_to_the_model(tmp_path):
    """No address in the sheet or the bank: `City` quick-maps to `address_city`,
    which is empty, so the model's `location` is used; a select in the same
    spot rides in the second request like any model-mapped field."""
    (tmp_path / "apply.md").write_text(apply_data.build_markdown(_MASTER, _JOB, []),
                                       encoding="utf-8")
    cat = apply_facts.build(tmp_path, answers=[])
    for key in ("address_city", "address_state", "address_zip"):
        assert not cat.has(key)
    cat.facts["answer_state"] = apply_facts.Fact("answer_state", "California",
                                                 "State of residence")
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "City", required=True),
        _f(1, "State", "select", required=True, options=("California", "Nevada")),
        _f(2, "Zip", required=True),
    ])
    answers = _page_answers(digest, {0: ("location", 0.9), 1: ("answer_state", 0.9),
                                     2: ("leave_blank", 0.9)})
    p = apply_judge.plan(digest, cat, answers)
    by_n = {f.n: f for f in p.fields}
    assert by_n[0].fact_key == "location" and by_n[0].value == "Anytown, CA"
    assert by_n[0].action == "fill" and by_n[0].confidence == 0.9
    assert by_n[1].fact_key == "answer_state" and by_n[1].action == "skip"   # waits for _pick
    assert by_n[2].fact_key is None and by_n[2].action == "skip"
    assert p.park_reason == "required field without an answer: State"
    state2, q2 = apply_judge.option_questions(digest, p)
    assert set(q2) == {"field_1_pick"} and state2["fields"][0]["n"] == 1
    answers["field_1_pick"] = _choice("California", 0.95)
    p2 = apply_judge.plan(digest, cat, answers)
    assert {f.n: f for f in p2.fields}[1].option == "California"
    assert p2.park_reason == "required field without an answer: Zip"


def test_plan_leave_blank_and_needs_generation(catalog):
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Anything else?", "textarea"),
        _f(1, "Why us?", "textarea", required=True),
        _f(2, "Why this role?", "textarea"),
    ])
    answers = _page_answers(digest, {0: ("leave_blank", 0.9), 1: ("needs_generation", 0.9),
                                     2: ("needs_generation", 0.9)})
    p = apply_judge.plan(digest, catalog, answers)
    assert [f.action for f in p.fields] == ["skip", "generate", "generate"]
    assert p.park_reason == ""
    p2 = apply_judge.plan(digest, catalog, answers, generation_enabled=False)
    assert [f.action for f in p2.fields] == ["skip", "skip", "skip"]
    assert p2.park_reason == "required field without an answer: Why us?"


def test_plan_options_and_uploads(catalog):
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Are you authorized to work in the US?", "select", required=True,
           options=("Yes", "No")),
        _f(1, "Gender", "select", options=("Male", "Female", "Decline to self-identify")),
        _f(2, "Resume", "file", required=True, ident="resume"),
        _f(3, "Country", "listbox", options=("Canada", "United States")),
        _f(4, "Veteran status", "radio", required=True, options=("Yes", "No")),
    ])
    answers = _page_answers(
        digest, {0: ("work_authorized", 0.9), 1: ("gender", 0.9), 2: ("resume_file", 0.9),
                 3: ("address_country", 0.9), 4: ("veteran_status", 0.9)},
        options={0: ("Yes", 0.95), 1: ("Decline to self-identify", 0.5),
                 3: ("United States", 0.9), 4: ("no_match", 1.0)})
    p = apply_judge.plan(digest, catalog, answers)
    by_n = {f.n: f for f in p.fields}
    assert by_n[0].action == "select" and by_n[0].option == "Yes" and by_n[0].value == "Yes"
    assert by_n[1].action == "skip" and by_n[1].option is None      # below OPTION_MIN_CONF
    assert by_n[2].action == "upload" and by_n[2].value.endswith("Jane_Doe_Resume.pdf")
    assert by_n[3].action == "select" and by_n[3].option == "United States"
    assert by_n[4].action == "skip"
    assert p.park_reason == "required field without an answer: Veteran status"
    assert [m[0] for m in p.missing] == ["Gender", "Veteran status"]


def test_plan_signature_today_by_label(catalog):
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Signature (type your full name)", required=True),
        _f(1, "Date", "date", required=True),
    ])
    answers = _page_answers(digest, {0: ("signature_today", 0.9), 1: ("signature_today", 0.9)})
    p = apply_judge.plan(digest, catalog, answers)
    assert p.fields[0].value == "Jane Doe" and p.fields[0].action == "fill"
    assert p.fields[1].value == "2026-09-21" and p.fields[1].action == "fill"


@pytest.mark.parametrize("label,type_,expected", [
    ("Candidate Signature", "text", "Jane Doe"),        # "date" inside a word is no date
    ("Signature (type your full name)", "text", "Jane Doe"),
    ("Update your signature", "text", "Jane Doe"),
    ("Date", "text", "2026-09-21"),
    ("Date signed", "text", "2026-09-21"),
    ("Dated", "text", "2026-09-21"),
    ("Today's date", "text", "2026-09-21"),
    ("Signature date", "text", "2026-09-21"),
    ("Signature", "date", "2026-09-21"),                # the control type decides too
])
def test_plan_signature_today_decides_by_whole_token_or_control_type(catalog, label, type_,
                                                                     expected):
    digest = FormDigest(url_host="x", title="t", text="", fields=[_f(0, label, type_)])
    p = apply_judge.plan(digest, catalog, _page_answers(digest, {0: ("signature_today", 0.9)}))
    assert p.fields[0].value == expected and p.fields[0].action == "fill"


def _split(pick, conf, probs):
    return jev.Answer(kind="choice", choice=pick, probabilities=dict(probs), confidence=conf)


def test_plan_pools_the_sources_that_type_the_same_words(catalog):
    # SP8b, live 2026-09-25 (date_mmddyyyy.html): the judge split a signature
    # box between the full name (0.61, confidence 0.59) and the typed
    # signature (0.29); both type the name, so the mapping stands at 0.90
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Signature (type your full name)", required=True)])
    answers = _page_answers(digest, {})
    answers["field_0_source"] = _split("full_name", 0.59, {
        "full_name": 0.61, "signature_today": 0.29, "leave_blank": 0.09, "email": 0.01})
    p = apply_judge.plan(digest, catalog, answers)
    assert (p.fields[0].action, p.fields[0].value) == ("fill", "Jane Doe")
    assert p.fields[0].fact_key == "full_name"
    assert p.fields[0].confidence == pytest.approx(0.90)
    assert p.park_reason == "" and p.missing == []


def test_plan_pools_neither_other_words_nor_a_shared_yes(catalog):
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Signature (type your full name)", required=True),
        _f(1, "Are you willing to relocate?", "text", required=True)])
    answers = _page_answers(digest, {})
    # the first name types other words than the full name: no pooling
    answers["field_0_source"] = _split("full_name", 0.59, {
        "full_name": 0.61, "first_name": 0.30, "leave_blank": 0.09})
    # two Yes/No facts that both hold "Yes" never pool: a Yes to one
    # question is no answer to another
    assert catalog.value("work_authorized") == catalog.value("willing_to_relocate") == "Yes"
    answers["field_1_source"] = _split("willing_to_relocate", 0.45, {
        "willing_to_relocate": 0.45, "work_authorized": 0.40, "leave_blank": 0.15})
    p = apply_judge.plan(digest, catalog, answers)
    assert [pf.action for pf in p.fields] == ["skip", "skip"]
    assert p.park_reason == ("required field without an answer: "
                             "Signature (type your full name)")


def _bank_with(*entries):
    bank = _bank()
    bank.extend({"id": eid, "question": q, "answer": a, "kind": "fixed", "status": "active"}
                for eid, q, a in entries)
    return bank


@pytest.mark.parametrize("label,split,value", [
    # SP8b review I2: two answer-bank entries that both hold "Yes"; the judge
    # was unsure which applied, and the shared Yes is no agreement
    ("Do you hold a current driver's license?",
     {"answer_over_18": 0.45, "answer_background_check": 0.30, "leave_blank": 0.25}, "Yes"),
    # and a number: the years of experience and an answer-bank entry both "2"
    ("Years of Python experience",
     {"years_experience": 0.45, "answer_sql_years": 0.30, "leave_blank": 0.25}, "2"),
])
def test_plan_pools_no_answer_bank_yes_and_no_shared_number(tmp_path, label, split, value):
    (tmp_path / "apply.md").write_text(apply_data.build_markdown(_MASTER, _JOB, _bank()),
                                       encoding="utf-8")
    cat = apply_facts.build(tmp_path, today=date(2026, 9, 21), answers=_bank_with(
        ("over_18", "Are you at least 18 years old?", "Yes"),
        ("background_check", "Will you consent to a background check?", "Yes"),
        ("sql_years", "How many years of SQL experience do you have?", "2")))
    keys = [k for k in split if k != "leave_blank"]
    assert [cat.value(k) for k in keys] == [value, value]
    options = ("Yes", "No") if value == "Yes" else ()
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, label, "radio" if options else "text", required=True, options=options)])
    # the option pick is sure: only the source split decides
    answers = _page_answers(digest, {}, options={0: ("Yes", 1.0)})
    answers["field_0_source"] = _split(keys[0], 0.45, split)
    p = apply_judge.plan(digest, cat, answers)
    assert p.fields[0].action == "skip"
    assert p.park_reason == f"required field without an answer: {label}"


def test_a_portfolio_box_takes_the_github_fact_when_no_website_is_stored(tmp_path):
    # SP8b, live 2026-09-25 (validation_errors.html's required Portfolio URL):
    # with no website stored, `quick_map`'s website_url has no value and the
    # box goes to the judge, which read it leave_blank at 0.54 to 0.65 while
    # the GitHub fact said "GitHub profile URL"; the sheet's row is "GitHub /
    # Portfolio", and its description now says so
    (tmp_path / "apply.md").write_text(apply_data.build_markdown(_MASTER, _JOB, _bank()),
                                       encoding="utf-8")
    cat = apply_facts.build(tmp_path, answers=_bank(), today=date(2026, 9, 21))
    assert not cat.has("website_url")
    assert "portfolio" in cat.to_criteria()["github_url"].lower()
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Portfolio URL", "url", required=True, ident="portfolio")])
    state, questions = apply_judge.page_questions(digest, cat, _JOB)
    p = apply_judge.plan(digest, cat, jev.FakeJev().judge(state, questions))
    assert (p.fields[0].action, p.fields[0].fact_key, p.fields[0].value) == (
        "fill", "github_url", "https://github.com/janedoe")
    assert p.park_reason == ""


def test_plan_special_source_without_a_file_follows_the_blank_rule(catalog, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "apply.md").write_text(apply_data.build_markdown(_MASTER, _JOB, _bank()),
                                   encoding="utf-8")
    cat = apply_facts.build(bare, answers=_bank())
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Cover Letter", "file", ident="cover_letter"),
        _f(1, "Resume", "file", required=True, ident="resume"),
    ])
    answers = _page_answers(digest, {0: ("cover_letter_file", 0.9), 1: ("resume_file", 0.9)})
    p = apply_judge.plan(digest, cat, answers)
    assert [f.action for f in p.fields] == ["skip", "skip"]
    assert p.park_reason == "required field without an answer: Resume"


def test_plan_buttons_keep_the_best_n_per_role(catalog):
    digest = FormDigest(url_host="x", title="t", text="", buttons=[
        Button(n=0, locator=(0, "#a"), text="Next"),
        Button(n=1, locator=(0, "#b"), text="Continue"),
        Button(n=2, locator=(0, "#c"), text="Submit"),
    ])
    answers = _page_answers(digest, {}, roles={0: ("advance", 0.7), 1: ("advance", 0.9),
                                              2: ("submit", 0.95)})
    p = apply_judge.plan(digest, catalog, answers)
    assert p.buttons == {"advance": (1, 0.9), "submit": (2, 0.95)}


@pytest.mark.parametrize("label,sensitive", [
    ("Social Security Number", True), ("SSN (last 4)", True), ("Date of Birth", True),
    ("Birthdate", True), ("Bank account number", True), ("Routing number", True),
    ("Credit card number", True), ("Passport number", True), ("Driver's license number", True),
    ("Mother's maiden name", True), ("Birthday", True), ("National Insurance number", True),
    ("SIN", True), ("Tax file number", True), ("Tax ID", True),
    ("Do you have a valid passport?", False), ("Do you have a valid driver's license?", False),
    ("Phone number", False), ("Start date", False), ("Destination", False)])
def test_sensitive_labels(label, sensitive):
    assert apply_judge.is_sensitive_field(label) is sensitive


def test_a_sensitive_box_is_never_filled_whatever_the_model_maps_it_to(catalog):
    # the fake judge mapped "Social Security Number" to signature_today (the
    # typed name) in the runner's signup test; the guard does not ask
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Social Security Number", required=True), _f(1, "Date of birth")])
    answers = _page_answers(digest, {0: ("signature_today", 1.0), 1: ("needs_generation", 1.0)})
    p = apply_judge.plan(digest, catalog, answers)
    assert [(pf.fact_key, pf.action) for pf in p.fields] == [(None, "skip"), (None, "skip")]
    # its own reason, and no missing answer: a stored SSN would never be typed
    assert p.park_reason == apply_judge.sensitive_reason("Social Security Number")
    assert p.missing == []


def test_plan_flags_never_park_and_an_unanswerable_required_question_does(catalog):
    # the user's rule: the facts hold only what they chose to share, so a
    # question they cannot answer (an SSN) is the required-field park; the
    # prohibited and captcha flags are recorded and park nothing
    ssn = FormDigest(url_host="x", title="t", text="",
                     fields=[_f(0, "Social Security Number", required=True)])
    p = apply_judge.plan(ssn, catalog, _page_answers(ssn, {}, prohibited=0.8, captcha=0.9))
    assert p.park_reason == apply_judge.sensitive_reason("Social Security Number")
    assert p.flags["asks_for_prohibited"] == 0.8 and p.flags["has_captcha"] == 0.9
    optional = FormDigest(url_host="x", title="t", text="",
                          fields=[_f(0, "Social Security Number", required=False)])
    p = apply_judge.plan(optional, catalog,
                         _page_answers(optional, {}, prohibited=0.8, captcha=0.9))
    assert p.park_reason == ""
    assert [pf.action for pf in p.fields] == ["skip"]


def test_plan_to_dict_is_json(catalog):
    digest = _greenhouse()
    answers = _page_answers(digest, {0: ("first_name", 0.9)})
    p = apply_judge.plan(digest, catalog, answers)
    raw = json.loads(json.dumps(p.to_dict()))
    assert raw["fields"][0]["fact_key"] == "first_name"
    assert raw["park_reason"].startswith("required field")


# --- option_questions: the second request ----------------------------------------

def test_option_questions_cover_only_model_mapped_fields_with_options(catalog):
    digest = _greenhouse()
    mapping = {0: ("first_name", 0.9), 8: ("work_authorized", 0.9),
               9: ("requires_sponsorship", 0.9), 10: ("gender", 0.9)}
    answers = _page_answers(digest, mapping)
    p = apply_judge.plan(digest, catalog, answers)
    state, q = apply_judge.option_questions(digest, p)
    assert set(q) == {"field_8_pick", "field_9_pick", "field_10_pick"}
    assert [f["n"] for f in state["fields"]] == [8, 9, 10]
    blob = json.dumps(q["field_9_pick"]["instructions"])
    assert '"No"' in blob and "fields[1]" in blob
    assert list(q["field_10_pick"]["criteria"]) == \
        ["Male", "Female", "Decline to self-identify", "no_match"]
    # a quick_map field with options never rides in the second request
    digest2 = FormDigest(url_host="x", title="t", text="",
                         fields=[_f(0, "Country", "select", options=("Canada", "United States"))])
    p2 = apply_judge.plan(digest2, catalog, _page_answers(digest2, {}, {0: ("United States", 1.0)}))
    assert apply_judge.option_questions(digest2, p2) == ({"fields": []}, {})


# --- verify: checkpoint (d) --------------------------------------------------------

def test_verify_questions_shape():
    filled = [{"n": 0, "label": "First Name", "value": "Jane"},
              {"n": 3, "label": "Phone", "value": "XXXXX"}]
    state, q = apply_judge.verify_questions(filled, "## Candidate\n- **Name:** Jane Doe")
    assert state["sheet_excerpt"].startswith("## Candidate")
    assert state["filled"] == filled
    assert set(q) == {"verify_0", "placeholder_0", "verify_3", "placeholder_3"}
    assert q["verify_3"]["type"] == "noul" and q["placeholder_3"]["type"] == "noul"
    assert "sheet_excerpt" in json.dumps(q["verify_3"]["instructions"])
    assert "XXXXX" in json.dumps(q["verify_3"]["instructions"])


def test_read_verification_marks_a_field_below_verify_min_as_failed():
    filled = [{"n": 0, "label": "First Name", "value": "Jane"},
              {"n": 3, "label": "Phone", "value": "XXXXX"},
              {"n": 5, "label": "City", "value": "Anytown"}]
    answers = {"verify_0": _noul(0.95), "placeholder_0": _noul(0.02),
               "verify_3": _noul(0.79), "placeholder_3": _noul(0.10),
               "verify_5": _noul(0.90), "placeholder_5": _noul(0.90)}
    results = apply_judge.read_verification(filled, answers)
    assert [(r.n, r.label, r.ok) for r in results] == \
        [(0, "First Name", True), (3, "Phone", False), (5, "City", False)]
    assert results[1].p_correct == 0.79 and results[1].p_placeholder == 0.10
    assert results[0].ok is True
    assert apply_judge.read_verification(filled[:1], {})[0].ok is False


# --- inbox, code pick, grounding -------------------------------------------------

def test_inbox_questions_and_read_inbox():
    """Two Nouls per message (one yes/no each): sent by the site, carries a
    code. `read_inbox` takes the message maximising their product with both
    above `INBOX_MIN`."""
    messages = [{"n": 0, "sender": "news@example.com", "subject": "Weekly digest",
                 "preview": "Top stories this week"},
                {"n": 1, "sender": "no-reply@greenhouse.io",
                 "subject": "Your Greenhouse verification code",
                 "preview": "Enter the code 482913 to continue"},
                {"n": 2, "sender": "no-reply@greenhouse.io",
                 "subject": "Welcome to Greenhouse",
                 "preview": "Thanks for creating your account"}]
    state, q = apply_judge.inbox_questions(messages, "greenhouse.io")
    assert state["site"] == "greenhouse.io" and state["messages"] == messages
    assert set(q) == {"msg_0_from_site", "msg_0_has_code", "msg_1_from_site",
                      "msg_1_has_code", "msg_2_from_site", "msg_2_has_code"}
    assert all(spec["type"] == "noul" for spec in q.values())
    assert "greenhouse.io" in json.dumps(q["msg_1_from_site"]["instructions"])
    assert "greenhouse.io" not in json.dumps(q["msg_1_has_code"]["instructions"])
    assert "code" in json.dumps(q["msg_1_has_code"]["instructions"])
    assert "code" not in json.dumps(q["msg_1_from_site"]["instructions"])
    answers = jev.FakeJev().judge(state, q)
    assert answers["msg_1_from_site"].noul == 0.9 and answers["msg_1_has_code"].noul == 0.9
    assert answers["msg_2_from_site"].noul == 0.9 and answers["msg_2_has_code"].noul == 0.1
    assert answers["msg_0_from_site"].noul == 0.1 and answers["msg_0_has_code"].noul == 0.1
    assert apply_judge.read_inbox(answers, messages) == 1
    assert apply_judge.INBOX_MIN == 0.5


def test_inbox_questions_name_the_ats_and_the_company():
    """The runner's `site` is the form's host, which is not who sends the
    code mail: the ATS does. The from-site question names the ATS (its
    display name, from the queue entry's system) and the company, so the
    judge can say yes to the ATS's mail and no to another ATS's decoy. An
    unknown system asks the site-only question."""
    messages = [{"n": 0, "sender": "no-reply@greenhouse.io",
                 "subject": "Your Greenhouse security code",
                 "preview": "Message sent by Greenhouse. Your security code is MKPZ3QRA."},
                {"n": 1, "sender": "no-reply@ashbyhq.com",
                 "subject": "Verify your email for Ashby",
                 "preview": "Your Ashby verification code is Q7R2XK."}]
    state, q = apply_judge.inbox_questions(messages, "127.0.0.1", ats="greenhouse",
                                           company="Fabrikam")
    blob = json.dumps(q["msg_0_from_site"]["instructions"])
    assert "127.0.0.1" in blob and "Greenhouse" in blob and "Fabrikam" in blob
    assert "code" not in blob.lower()
    assert state["site"] == "127.0.0.1" and state["messages"] == messages
    for spec in q.values():
        _assert_clean_text({"x": spec})
    answers = jev.FakeJev().judge(state, q)
    assert answers["msg_0_from_site"].noul == 0.9 and answers["msg_0_has_code"].noul == 0.9
    assert answers["msg_1_from_site"].noul == 0.1
    assert apply_judge.read_inbox(answers, messages) == 0
    # every known system has a display name; `other` and "" ask about the site alone
    for system in ("greenhouse", "lever", "ashby", "workday", "icims", "smartrecruiters",
                   "jobvite", "workable", "successfactors", "taleo", "oracle", "brassring",
                   "adp", "ukg", "dayforce", "linkedin"):
        assert apply_judge.ATS_NAMES[system]
    for system in ("other", "", "unheard-of"):
        _, q = apply_judge.inbox_questions(messages, "127.0.0.1", ats=system, company="Fabrikam")
        blob = json.dumps(q["msg_0_from_site"]["instructions"])
        assert "Fabrikam" not in blob and "applicant" not in blob
        assert "127.0.0.1" in blob


def _inbox(**p):
    return {qid: _noul(v) for qid, v in p.items()}


def test_read_inbox_needs_both_above_the_floor_and_picks_the_best_product():
    messages = [{"n": 0}, {"n": 1}, {"n": 2}]
    # a site email without a code, and a code email from elsewhere: nothing
    assert apply_judge.read_inbox(_inbox(msg_0_from_site=0.95, msg_0_has_code=0.4,
                                         msg_1_from_site=0.3, msg_1_has_code=0.99),
                                  messages) is None
    # exactly at the floor is out
    assert apply_judge.read_inbox(_inbox(msg_0_from_site=0.5, msg_0_has_code=0.9),
                                  messages) is None
    # the best product wins, and a strong single factor cannot beat it
    assert apply_judge.read_inbox(_inbox(msg_0_from_site=0.6, msg_0_has_code=0.6,
                                         msg_1_from_site=0.9, msg_1_has_code=0.8,
                                         msg_2_from_site=0.99, msg_2_has_code=0.51),
                                  messages) == 1
    assert apply_judge.read_inbox({}, messages) is None


def test_code_pick_questions_and_read_code_pick():
    state, q = apply_judge.code_pick_questions(["482913", "2024"], "Your code is 482913.")
    assert list(q["code_pick"]["criteria"]) == ["482913", "2024", "none"]
    assert state == {"body": "Your code is 482913.", "candidates": ["482913", "2024"]}
    assert apply_judge.read_code_pick({"code_pick": _choice("482913", 0.9)}) == "482913"
    assert apply_judge.read_code_pick({"code_pick": _choice("none", 0.9)}) is None
    assert apply_judge.read_code_pick({}) is None
    answers = jev.FakeJev().judge(state, q)
    assert apply_judge.read_code_pick(answers) == "482913"


def test_grounding_questions_and_read_grounding():
    sheet = ("## Candidate\n- **Name:** Jane Doe\n\n## Standard answers\n"
             "- **How many years of relevant experience do you have?** 2\n"
             "- **Technical skills:** Python, SQL, ingestion pipelines")
    sentences = ["I have two years of experience building ingestion pipelines in Python.",
                 "I led a team of forty engineers at a unicorn startup."]
    state, q = apply_judge.grounding_questions(sentences, sheet)
    assert state["sheet_excerpt"] == sheet and state["sentences"] == sentences
    assert set(q) == {"grounded_0", "grounded_1"}
    assert sentences[1] in json.dumps(q["grounded_1"]["instructions"])
    answers = jev.FakeJev().judge(state, q)
    ok, weakest = apply_judge.read_grounding(answers, sentences)
    assert ok is False and weakest == 0.1
    assert answers["grounded_0"].noul == 0.9
    assert apply_judge.read_grounding({"grounded_0": _noul(0.71), "grounded_1": _noul(0.9)},
                                      sentences) == (True, 0.71)
    assert apply_judge.read_grounding({"grounded_0": _noul(0.9)}, sentences) == (False, 0.0)


# --- end to end through FakeJev over the Greenhouse-shaped digest --------------------

def test_fake_jev_end_to_end_over_the_greenhouse_digest(catalog):
    digest = _greenhouse()
    fake = jev.FakeJev()
    rs, rq = apply_judge.read_questions(digest, "https://boards.greenhouse.io/acme/jobs/1")
    read = apply_judge.read_page(fake.judge(rs, rq),
                                 apply_judge.page_facts(digest,
                                                        "https://boards.greenhouse.io/acme/jobs/1"))
    assert (read.state, read.conf) == ("application_form", 1.0)
    state, q = apply_judge.page_questions(digest, catalog, _JOB)
    answers = fake.judge(state, q)
    answers.update(fake.judge(rs, rq))

    first = apply_judge.plan(digest, catalog, answers)
    # the option picks for the model-mapped selects are only known after the
    # mapping: the first plan parks on them, the second request resolves them
    assert first.park_reason.startswith("required field without an answer")
    state2, q2 = apply_judge.option_questions(digest, first)
    assert set(q2) == {"field_8_pick", "field_9_pick", "field_10_pick"}
    answers.update(fake.judge(state2, q2))

    p = apply_judge.plan(digest, catalog, answers)
    got = {f.n: (f.fact_key, f.action, f.option) for f in p.fields}
    assert got == {
        0: ("first_name", "fill", None),
        1: ("last_name", "fill", None),
        2: ("email", "fill", None),
        3: ("phone", "fill", None),
        4: ("resume_file", "upload", None),
        5: ("cover_letter_file", "upload", None),
        6: ("linkedin_url", "fill", None),
        7: ("website_url", "fill", None),
        8: ("work_authorized", "select", "Yes"),
        9: ("requires_sponsorship", "select", "No"),
        10: ("gender", "select", "Decline to self-identify"),
        11: ("needs_generation", "generate", None),
    }
    values = {f.n: f.value for f in p.fields}
    assert values[0] == "Jane" and values[1] == "Doe"
    assert values[4].endswith("Jane_Doe_Resume.pdf")
    assert values[7] == "https://janedoe.dev"
    assert p.park_reason == "" and p.missing == []
    assert p.buttons["submit"] == (0, 1.0) and p.buttons["back"] == (1, 1.0)
    assert p.flags == {"asks_for_prohibited": 0.1, "requires_account": 0.1,
                       "has_captcha": 0.1}
    assert all(f.confidence >= apply_judge.FIELD_MAP_MIN_CONF for f in p.fields)


def _fake_run(digest, catalog):
    """The two-request loop through FakeJev, returning the settled plan."""
    fake = jev.FakeJev()
    state, q = apply_judge.page_questions(digest, catalog, _JOB)
    answers = fake.judge(state, q)
    first = apply_judge.plan(digest, catalog, answers)
    state2, q2 = apply_judge.option_questions(digest, first)
    if q2:
        answers.update(fake.judge(state2, q2))
    return apply_judge.plan(digest, catalog, answers)


def test_fake_jev_end_to_end_checks_an_attestation_box_and_parks_a_background_check(catalog):
    p = _fake_run(_greenhouse("I certify that the information provided in this "
                              "application is accurate"), catalog)
    box = {f.n: f for f in p.fields}[12]
    assert (box.fact_key, box.action, box.option, box.value) ==         ("consent_attest", "select", "checked", "yes")
    assert p.park_reason == "" and p.missing == []

    p = _fake_run(_greenhouse("I consent to a background check and drug test"), catalog)
    box = {f.n: f for f in p.fields}[12]
    assert box.fact_key is None and box.action == "skip"
    assert p.park_reason == ("required field without an answer: "
                             "I consent to a background check and drug test")
    assert [m[0] for m in p.missing] == ["I consent to a background check and drug test"]


def test_fake_jev_flags_a_captcha_page_and_a_login_wall(catalog):
    captcha = FormDigest(url_host="jobs.example.com", title="Verify you are human",
                         text="reCAPTCHA: please complete the robot check to continue.",
                         fields=[], buttons=[])
    state, q = apply_judge.read_questions(captcha)
    answers = jev.FakeJev().judge(state, q)
    p = apply_judge.plan(captcha, catalog, answers)
    assert p.flags["has_captcha"] == 0.9
    assert p.park_reason == ""          # the flag is recorded; the page state parks
    read = apply_judge.read_page(answers, apply_judge.page_facts(captcha))
    assert read.state == "captcha_or_bot_check"

    login = FormDigest(url_host="jobs.example.com", title="Sign in to your account",
                       text="Sign in with your existing account email and password.",
                       fields=[_f(0, "Email", "email", required=True),
                               _f(1, "Password", "other", required=True)],
                       buttons=[Button(n=0, locator=(0, "#s"), text="Sign in")])
    state, q = apply_judge.read_questions(login)
    answers = jev.FakeJev().judge(state, q)
    assert answers["page_sign_in"].noul == 0.9
    assert apply_judge.plan(login, catalog, answers).flags["requires_account"] == 0.9
    read = apply_judge.read_page(answers, apply_judge.page_facts(login))
    assert read.state == "login_wall"


def test_plan_never_puts_a_fact_in_a_password_field(catalog):
    """Only `ats_accounts.fill_password` ever writes a password field, so the
    plan carries no value for one whatever the mapping says: its action tells
    the runner to type the master password there."""
    fields = [_f(0, "Email", "email", required=True, ident="email"),
              _f(1, "Password", "other", required=True, ident="signup_password"),
              _f(2, "Confirm password", "other", required=True, ident="password_confirmation")]
    digest = FormDigest(url_host="jobs.example.com", title="Create an account",
                        text="Create an account to apply.", fields=fields,
                        buttons=[Button(n=0, locator=(0, "#go"), text="Create account",
                                        kind_hint="submit")])
    answers = _page_answers(digest, {0: ("email", 0.99), 1: ("phone", 0.99),
                                     2: ("phone", 0.99)})
    plan = apply_judge.plan(digest, catalog, answers)
    by_n = {pf.n: pf for pf in plan.fields}
    assert by_n[0].action == "fill"
    assert [by_n[1].action, by_n[2].action] == [apply_judge.PASSWORD_ACTION] * 2
    assert [by_n[1].value, by_n[2].value] == ["", ""]
    assert [by_n[1].fact_key, by_n[2].fact_key] == [None, None]
    # no answer is asked for (the user is never nudged to store a password)
    # and the plan does not park: the runner types the master password
    assert plan.park_reason == ""
    assert plan.missing == []


def test_plan_reads_a_masked_sensitive_box_as_sensitive_before_a_password(catalog):
    """A masked "Passport number" matches the password shape (`pass`); it is
    a sensitive question, so the master password never goes into it."""
    fields = [_f(0, "Passport number", "other", required=True, ident="passport_no")]
    digest = FormDigest(url_host="jobs.example.com", title="Apply", text="Apply.",
                        fields=fields, buttons=[])
    plan = apply_judge.plan(digest, catalog, _page_answers(digest, {0: ("phone", 0.99)}))
    assert plan.fields[0].action == "skip"
    assert plan.park_reason == apply_judge.sensitive_reason("Passport number")
