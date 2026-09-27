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
from answer_bank import custom, standard_bank, unconfirmed  # noqa: E402
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
    """The shared confirmed answers (`answer_bank`), plus one custom entry."""
    return standard_bank() + [custom("salary_expectation", "What is your desired salary?",
                                     "Open to discussion")]


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
    return _bank() + [custom(eid, q, a) for eid, q, a in entries]


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


_SPONSORSHIP = "Will you now or in the future require visa sponsorship?"


@pytest.mark.parametrize("confirmed", [True, False])
def test_a_required_question_whose_answer_is_unconfirmed_parks(tmp_path, confirmed):
    # cycle 18 (FL-1): the sheet says No, but the store's answers on
    # sponsorship (the yes/no and the authorization statement that names it)
    # are not confirmed, so the catalog has no fact for the question and the
    # required field parks; confirmed, the same page fills No
    (tmp_path / "apply.md").write_text(apply_data.build_markdown(_MASTER, _JOB, _bank()),
                                       encoding="utf-8")
    assert f"- **{_SPONSORSHIP}** No" in (tmp_path / "apply.md").read_text(encoding="utf-8")
    bank = _bank() if confirmed else unconfirmed(_bank(), "requires_sponsorship",
                                                 "authorization_statement")
    cat = apply_facts.build(tmp_path, answers=bank, today=date(2026, 9, 21))
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, _SPONSORSHIP, required=True)])
    state, questions = apply_judge.page_questions(digest, cat, _JOB)
    p = apply_judge.plan(digest, cat, jev.FakeJev().judge(state, questions))
    if confirmed:
        assert (p.fields[0].action, p.fields[0].fact_key, p.fields[0].value) == (
            "fill", "requires_sponsorship", "No")
        assert p.park_reason == ""
    else:
        criteria = questions["field_0_source"]["criteria"]
        assert "requires_sponsorship" not in criteria
        assert "answer_authorization_statement" not in criteria
        assert p.fields[0].action == "skip"
        assert p.park_reason == f"required field without an answer: {_SPONSORSHIP}"


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
    state, q = apply_judge.option_questions(digest, p, catalog=catalog)
    # moved on purpose (cycle 18, FM-1): the two Yes / No selects (8, 9) are
    # settled in code by the alias set and ride in no second request
    assert [(p.fields[n].action, p.fields[n].option) for n in (8, 9)] == [
        ("select", "Yes"), ("select", "No")]
    assert set(q) == {"field_10_pick"}
    assert [f["n"] for f in state["fields"]] == [10]
    blob = json.dumps(q["field_10_pick"]["instructions"])
    assert '"Decline to self-identify"' in blob and "fields[0]" in blob
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
    # mapping, and the second request resolves them; the Yes / No selects
    # (8, 9) are settled in code by the alias set (moved on purpose, cycle
    # 18, FM-1), so the first plan waits on the optional Gender alone
    assert first.park_reason == "" and first.missing == [("Gender", "select")]
    state2, q2 = apply_judge.option_questions(digest, first, catalog=catalog)
    assert set(q2) == {"field_10_pick"}
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


# --- cycle 18 FM-1: a yes or no matches only an option in its own alias set ----------

@pytest.mark.parametrize("options, value, want", [
    (["Yes - on a work visa (OPT/H-1B)", "U.S. citizen or permanent resident"], "Yes", None),
    (["Yes, with sponsorship", "Yes, without sponsorship"], "Yes", None),
    (["No, but I will need sponsorship in the future", "Yes"], "No", None),
    (["Yes", "No"], "yes", "Yes"),
    (["Y", "N"], "Yes", "Y"),
])
def test_a_yes_or_no_matches_only_an_option_in_its_own_alias_set(options, value, want):
    assert apply_judge.match_option(value, options) == want


def _yes_no_digest(options):
    return FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Will you now or in the future require sponsorship?", "select", required=True,
           options=options)])


def test_a_plain_yes_no_list_is_settled_in_code_and_asks_the_judge_nothing(catalog):
    # FM-1: a yes / no fact's value names one option of a plain Yes / No list
    # by its alias set; the plan takes it with no pick answer at all, and the
    # second request asks nothing for it
    digest = _yes_no_digest(("Yes", "No"))
    answers = _page_answers(digest, {0: ("requires_sponsorship", 0.9)})
    del answers["field_0_pick"], answers["field_0_option"]
    p = apply_judge.plan(digest, catalog, answers)
    assert (p.fields[0].action, p.fields[0].option) == ("select", "No")
    assert p.park_reason == ""
    assert apply_judge.option_questions(digest, p, catalog=catalog) == ({"fields": []}, {})


def test_a_qualified_yes_no_list_waits_for_the_judges_pick(catalog):
    # FM-1: a qualified option is the judge's pick; no pick yet is no answer
    options = ("Yes, I will require sponsorship", "No, I do not require sponsorship")
    digest = _yes_no_digest(options)
    answers = _page_answers(digest, {0: ("requires_sponsorship", 0.9)})
    del answers["field_0_pick"], answers["field_0_option"]
    p = apply_judge.plan(digest, catalog, answers)
    assert p.fields[0].action == "skip"
    assert p.park_reason == ("required field without an answer: Will you now or in the "
                             "future require sponsorship?")
    _, q = apply_judge.option_questions(digest, p, catalog=catalog)
    assert set(q) == {"field_0_pick"}
    answers["field_0_pick"] = _choice(options[1], 0.95)
    p = apply_judge.plan(digest, catalog, answers)
    assert (p.fields[0].action, p.fields[0].option) == ("select", options[1])


def _bool_catalog(**values):
    return apply_facts.FactCatalog([apply_facts.Fact(k, v, apply_facts.DESCRIPTIONS[k], "bool")
                                    for k, v in values.items()])


def test_the_pick_for_a_yes_no_fact_carries_every_yes_no_fact_the_catalog_holds(catalog):
    # orchestrator decision 1: the mapped fact first, then the other yes / no
    # facts with a value, each as "<description>: <Yes|No>"
    d = apply_facts.DESCRIPTIONS
    assert apply_judge.candidate_answer(catalog, "requires_sponsorship", "No") == "\n".join([
        f"{d['requires_sponsorship']}: No", f"{d['work_authorized']}: Yes",
        f"{d['willing_to_relocate']}: Yes", f"{d['onsite_ok']}: Yes"])
    # a derived fact (cycle 18, SP6c) leads its own pick over the stored
    # lines and adds no line to another's: the stored lines already say it
    assert catalog.value("authorized_without_sponsorship") == "Yes"
    assert apply_judge.candidate_answer(
        catalog, "authorized_without_sponsorship", "Yes") == "\n".join([
            f"{d['authorized_without_sponsorship']}: Yes", f"{d['work_authorized']}: Yes",
            f"{d['requires_sponsorship']}: No", f"{d['willing_to_relocate']}: Yes",
            f"{d['onsite_ok']}: Yes"])
    # any other fact's pick carries its value alone
    assert apply_judge.candidate_answer(catalog, "gender", "Decline to self-identify") == \
        "Decline to self-identify"
    # a yes / no fact the catalog leaves unset is left out
    cat = _bool_catalog(work_authorized="Yes", requires_sponsorship="No",
                        willing_to_relocate="Yes", onsite_ok="")
    assert apply_judge.candidate_answer(cat, "willing_to_relocate", "Yes").split("\n") == [
        f"{d['willing_to_relocate']}: Yes", f"{d['work_authorized']}: Yes",
        f"{d['requires_sponsorship']}: No"]
    # with no catalog the value stands alone
    assert apply_judge.candidate_answer(None, "willing_to_relocate", "Yes") == "Yes"


def test_the_second_request_carries_the_combined_answer_for_a_qualified_list(catalog):
    options = ("I do not want to work in office", "Yes, I am willing to relocate",
               "No, I would prefer to stay where I am")
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Are you willing to relocate to the job location?", "radio", required=True,
           options=options)])
    answers = _page_answers(digest, {0: ("willing_to_relocate", 0.9)})
    del answers["field_0_pick"], answers["field_0_option"]
    p = apply_judge.plan(digest, catalog, answers)
    combined = apply_judge.candidate_answer(catalog, "willing_to_relocate", "Yes")
    s, q = apply_judge.option_questions(digest, p, catalog=catalog)
    assert q["field_0_pick"]["instructions"]["candidate_answer"] == combined
    assert list(q["field_0_pick"]["criteria"]) == [*options, "no_match"]
    # the fake judge reads the combined answer to the relocation option
    assert jev.FakeJev().judge(s, q)["field_0_pick"].choice == options[1]
    # the second look asks the same question
    _, q = apply_judge.reask_questions(digest, catalog, p, [0], what="pick")
    assert q["field_0_pick"]["instructions"]["candidate_answer"] == combined


# --- cycle 18 FM-2: past OPTIONS_CAP code decides only on the same words or an alias -------

def test_a_long_list_is_settled_in_code_only_by_an_exact_or_alias_match(catalog):
    filler = [f"Option {i}" for i in range(apply_judge.OPTIONS_CAP + 5)]
    starts = [*filler, "California (CA)", "Colorado (CO)"]
    assert apply_judge.match_option("California", starts, exact=True) is None
    assert apply_judge.code_pick("California", starts) is None
    assert apply_judge.code_pick("CA", [*filler, "California"]) == "California"
    assert apply_judge.code_pick("California", [*filler, "california"]) == "california"
    # a long list's starts-with option is no code pick: the judge's pick decides
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "State", "select", required=True, options=starts)])
    p = apply_judge.plan(digest, catalog, _page_answers(digest, {}, {0: ("no_match", 1.0)}))
    assert p.fields[0].fact_key == "address_state" and p.fields[0].action == "skip"
    p = apply_judge.plan(digest, catalog,
                         _page_answers(digest, {}, {0: ("California (CA)", 0.95)}))
    assert (p.fields[0].action, p.fields[0].option) == ("select", "California (CA)")


# --- cycle 18 FM-5: a number box takes a plain number; the phone only a phone's box -------

@pytest.mark.parametrize("label, ident, action", [
    ("Years of experience", "years", "skip"),
    ("Phone number", "contact", "fill"),
    ("Contact", "mobile_no", "fill"),
    ("Contact", "tel", "fill"),
    ("Contact", "telNumber", "fill"),
    ("Hotel nights", "stay", "skip"),
])
def test_the_phone_goes_into_a_number_box_only_when_it_names_a_phone(catalog, label, ident,
                                                                     action):
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, label, "number", required=True, ident=ident)])
    p = apply_judge.plan(digest, catalog, _page_answers(digest, {0: ("phone", 0.95)}))
    assert p.fields[0].action == action
    assert p.park_reason == ("" if action == "fill"
                             else f"required field without an answer: {label}")


@pytest.mark.parametrize("value, action", [
    ("120k", "skip"), ("3-5", "skip"), ("(555) 123-4567", "skip"),
    ("Less than 1 year", "skip"), ("1.5", "fill"), ("3", "fill")])
@pytest.mark.parametrize("required", [True, False])
def test_a_number_box_whose_answer_is_no_plain_number_is_left_blank(value, action, required):
    cat = apply_facts.FactCatalog([apply_facts.Fact("answer_years", value, "Years with SQL")])
    digest = FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, "Years with SQL", "number", required=required)])
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("answer_years", 0.95)}))
    assert p.fields[0].action == action
    if action == "skip":
        assert [m[0] for m in p.missing] == ["Years with SQL"]
        assert p.park_reason == ("required field without an answer: Years with SQL"
                                 if required else "")


# --- cycle 18 FM-7: a decline is a refusal to answer ------------------------------------

@pytest.mark.parametrize("text, declined", [
    ("I do not identify as a protected veteran", False),
    ("I do not wish to self-identify", True),
    ("Prefer not to say", True),
    ("I don't wish to answer", True),
    ("Decline to self-identify", True),
])
def test_declines_reads_a_refusal_and_never_a_statement(text, declined):
    assert apply_judge.declines(text) is declined


# --- cycle 18 SP6c: code settles an answer only for its own question ------------------------

_OWN_QUESTIONS = [
    # work_authorized: the plain forms (the screening set's, the fixtures', the brief's)
    ("work_authorized", "Are you legally authorized to work in the United States?", True),
    ("work_authorized", "Are you authorized to work in the US?", True),
    ("work_authorized", "Are you authorized to work in the US? *", True),
    ("work_authorized", "Are you currently authorized to work in the United States?", True),
    ("work_authorized", "Are you legally eligible to work in the U.S.?", True),
    ("work_authorized", "Will you be legally able to work in the United States on your start "
                        "date?", True),
    ("work_authorized", "I am legally authorized to work in the United States.", True),
    ("work_authorized", "Do you have the legal right to work in the United States?", True),
    # round 3: "what", "current" and "status" are no words of its own question
    ("work_authorized", "What is your current work authorization status in the U.S.?",
     False),
    ("work_authorized", "Are you legally allowed to take up employment in the United States?",
     True),
    ("work_authorized", "Are you legally authorized to work in the United States? (If not, "
                        "please explain.)", True),
    # another question: a scope or polarity word, a visa type, sponsorship, no topic
    ("work_authorized", "Are you able to work in the U.S. without employer sponsorship, now "
                        "and in the future?", False),
    ("work_authorized", "Are you authorized to work in the United States without "
                        "sponsorship?", False),
    ("work_authorized", "Are you authorized to work in the United States on an H-1B visa?",
     False),
    ("work_authorized", "Are you authorized to work in any country other than the United "
                        "States?", False),
    ("work_authorized", "Are you no longer authorized to work in the US?", False),
    ("work_authorized", "Have you ever been authorized to work in the US on a visa?", False),
    ("work_authorized", "Will you require visa sponsorship to legally work in the US?", False),
    ("work_authorized", "Are you a U.S. citizen?", False),
    ("work_authorized", "Are you currently on F-1 OPT or STEM OPT?", False),
    ("work_authorized", "Are you willing to work on-site?", False),
    # requires_sponsorship: the plain forms, a visa type named as an example
    ("requires_sponsorship", "Will you now or in the future require sponsorship for employment "
                             "visa status (e.g., H-1B visa status)?", True),
    ("requires_sponsorship", "Do you require visa sponsorship?", True),
    ("requires_sponsorship", "Will you need the company to sponsor an employment-based visa "
                             "(such as an H-1B) for you?", True),
    # "continue" only before working (round 7, rule 8)
    ("requires_sponsorship", "Will you require sponsorship in the future to continue working "
                             "in the United States?", True),
    ("requires_sponsorship", "Do you require sponsorship (e.g., H-1B, TN, O-1) to work for "
                             "us?", True),
    ("requires_sponsorship", "Will you now or in the future require sponsorship? *", True),
    ("requires_sponsorship", "Will you now or in the future require visa sponsorship?", True),
    ("requires_sponsorship", "Do you require sponsorship to work in the US?", True),
    ("requires_sponsorship", "Will you now, or in the future, require sponsorship for "
                             "employment visa status?", True),
    ("requires_sponsorship", "Do you currently or will you in the future require visa "
                             "sponsorship?", True),
    ("requires_sponsorship", "Will you require visa sponsorship to legally work in the US?",
     True),
    # another question: the inverse, now only, a visa held, a named visa type
    ("requires_sponsorship", "Are you able to work in the U.S. without employer sponsorship, "
                             "now and in the future?", False),
    ("requires_sponsorship", "Do you currently require visa sponsorship to work in the U.S.?",
     False),
    ("requires_sponsorship", "Are you currently sponsored by an employer?", False),
    ("requires_sponsorship", "Is your current visa sponsored by your employer?", False),
    ("requires_sponsorship", "Do you currently hold an H-1B visa that would need to be "
                             "transferred?", False),
    ("requires_sponsorship", "Will you require H-1B sponsorship?", False),
    ("requires_sponsorship", "I will require H-1B visa sponsorship now or in the future.",
     False),
    ("requires_sponsorship", "Will you require sponsorship for a visa other than a TN?", False),
    ("requires_sponsorship", "Will you never require sponsorship?", False),
    ("requires_sponsorship", "Have you ever been sponsored for a work visa?", False),
    ("requires_sponsorship", "Do you no longer require sponsorship?", False),
    ("requires_sponsorship", "Will you require a sponsorship transfer?", False),
    ("requires_sponsorship", "Are you willing to relocate?", False),
    # authorized_without_sponsorship (derived): its own question says without
    ("authorized_without_sponsorship", "Are you able to work in the U.S. without employer "
                                       "sponsorship, now and in the future?", True),
    ("authorized_without_sponsorship", "Are you authorized to work in the United States "
                                       "without sponsorship?", True),
    ("authorized_without_sponsorship", "Can you work in the US without requiring visa "
                                       "sponsorship now or in the future?", True),
    ("authorized_without_sponsorship", "Are you authorized to work in the United States?",
     False),
    ("authorized_without_sponsorship", "Do you require visa sponsorship?", False),
    ("authorized_without_sponsorship", "Are you authorized to work without sponsorship while "
                                       "on OPT?", False),
    ("authorized_without_sponsorship", "Can you work in the US without an H-1B sponsorship "
                                       "transfer?", False),
    # willing_to_relocate
    ("willing_to_relocate", "Are you willing to relocate?", True),
    ("willing_to_relocate", "Would you be open to relocating for this role?", True),
    # round 3: a sentence on assistance, a preamble on the office, a city: other words
    ("willing_to_relocate", "Are you willing to relocate for this position? Relocation "
                            "assistance is not provided.", False),
    ("willing_to_relocate", "I am willing to relocate to the job's location.", True),
    # round 5: the job's location is out of the plan's reach, so a place is
    # another word
    ("willing_to_relocate", "Are you willing to relocate to the job location (New York)?",
     False),
    ("willing_to_relocate", "Open to relocation", True),
    ("willing_to_relocate", "This role is on-site in our San Francisco office. Are you willing "
                            "to relocate?", False),
    ("willing_to_relocate", "Are you willing to relocate only within California?", False),
    ("willing_to_relocate", "Are you willing to relocate without relocation assistance?",
     False),
    ("willing_to_relocate", "Are you not willing to relocate?", False),
    ("willing_to_relocate", "Have you ever relocated for a job?", False),
    ("willing_to_relocate", "Are you willing to travel up to 25% of the time?", False),
    ("willing_to_relocate", "Are you willing to undergo a background check?", False),
    ("willing_to_relocate", "Are you willing to work on-site?", False),
    # onsite_ok
    ("onsite_ok", "Are you willing to work on-site?", True),
    ("onsite_ok", "Are you willing to work on-site (in the office)?", True),
    # final review I2: a count of days a week is a narrower form (the hybrid
    # question), which a Yes alone settles
    ("onsite_ok", "Are you able to work in the office 3 days a week?", False),
    ("onsite_ok", "Are you able to work on-site / in the office (3 days a week)?", False),
    ("onsite_ok", "Are you able to work in person at our office five days a week?", False),
    # round 4: "comfortable" with on-site work is its own question
    ("onsite_ok", "This role requires working in the office. Are you comfortable with this?",
     True),
    ("onsite_ok", "This role requires working in the office 3 days a week. Are you "
                  "comfortable with this?", False),
    # round 3: an acknowledgement, a team: other words
    ("onsite_ok", "I understand this position is fully on-site and I am able to work in the "
                  "office.", False),
    ("onsite_ok", "Our team works from the office. Which describes you?", False),
    # hybrid work is a narrower question: a Yes to on-site work settles it
    ("onsite_ok", "Are you comfortable with a hybrid schedule?", False),
    ("onsite_ok", "Are you looking for a fully remote position only?", False),
    ("onsite_ok", "What is your preferred work arrangement?", False),
    ("onsite_ok", "This role is on-site in our San Francisco office. Are you willing to "
                  "relocate?", False),
    ("onsite_ok", "Are you able to work on-site without accommodation?", False),
    ("onsite_ok", "Have you ever worked in an office?", False),
    ("onsite_ok", "What is your current work authorization status in the U.S.?", False),
    # remote_only (derived): its own question says only
    ("remote_only", "Are you looking for a fully remote position only?", True),
    # round 5: "open" and "willing" ask about accepting remote work
    ("remote_only", "Are you only open to remote work?", False),
    ("remote_only", "Are you seeking remote-only roles?", True),
    ("remote_only", "Are you open to remote work?", False),
    ("remote_only", "Are you willing to work on-site?", False),
    ("remote_only", "What is your preferred work arrangement?", False),
    # final review I1: "can you" and "would you" ask about accepting remote work
    ("remote_only", "Can you work remotely only?", False),
    ("remote_only", "Can you work remote only?", False),
    ("remote_only", "Would you work remote only?", False),
    ("remote_only", "Would you work fully remotely only?", False),
    # years_experience: generic qualifiers pass, a named skill, tool or field does not
    ("years_experience", "How many years of professional experience do you have?", True),
    ("years_experience", "How many years of relevant experience do you have?", True),
    ("years_experience", "Years of relevant work experience", True),
    ("years_experience", "How many years of experience do you have in total?", True),
    # round 7 (rule 6): a number in a years question asks another question
    ("years_experience", "Do you have at least 2 years of professional work experience?", False),
    ("years_experience", "Years of experience", True),
    ("years_experience", "Years of Experience *", True),
    ("years_experience", "Total years of experience", True),
    ("years_experience", "How many years of full-time experience do you have?", True),
    ("years_experience", "Years of industry experience", True),
    ("years_experience", "How many years of relevant experience do you have in this field?",
     True),
    ("years_experience", "How much experience (in years) do you have?", True),
    ("years_experience", "How many years of experience do you have with Python?", False),
    ("years_experience", "Years of Python experience", False),
    ("years_experience", "Years of Python experience?", False),
    ("years_experience", "Python experience (years)", False),
    ("years_experience", "How many years of experience do you have working with React?", False),
    ("years_experience", "How many years of experience do you have in software engineering?",
     False),
    # a role qualifier asks the total (round 2, decision 3); a number asks
    # another question (round 7, rule 6)
    ("years_experience", "Do you have 3 or more years of experience in a similar role?", False),
    ("years_experience", "Years of experience, not counting internships", False),
    ("years_experience", "Years with SQL", False),
    ("years_experience", "Years in the role", False),
    ("years_experience", "Are you at least 18 years of age?", False),
    ("years_experience", "Do you have experience with Python?", False),
]


@pytest.mark.parametrize("key, label, own", _OWN_QUESTIONS)
def test_asks_own_question_reads_the_facts_own_question_and_no_other(key, label, own):
    assert apply_judge.asks_own_question(key, label) is own


def test_asks_own_question_reads_the_help_text_and_passes_any_other_fact():
    assert apply_judge.asks_own_question(
        "work_authorized", "Work authorization",
        "Are you authorized to work in the US without sponsorship?") is False
    assert apply_judge.asks_own_question(
        "authorized_without_sponsorship", "Work authorization",
        "Are you authorized to work in the US without sponsorship?") is True
    # a fact with no own-question table (a name, an EEO answer) is never refused
    assert apply_judge.asks_own_question("email", "Referrer email only") is True
    assert apply_judge.asks_own_question("gender", "Gender") is True
    # every yes / no fact has its table, and so do the years
    assert set(apply_facts.YES_NO_KEYS) | {"years_experience"} == set(
        apply_facts.OWN_QUESTION_KEYS)


_WITHOUT = "Are you authorized to work in the United States without sponsorship?"
_REMOTE_ONLY = "Are you looking for a fully remote position only?"


def _profile_catalog(tmp_path, **answers):
    """A catalog over the standard answers with `answers` in place (the
    screening profiles' shape: every answer confirmed)."""
    return apply_facts.build(tmp_path, answers=standard_bank(**answers),
                             today=date(2026, 9, 21))


def _one_field(label, type_="radio", required=True, options=("Yes", "No")):
    return FormDigest(url_host="x", title="t", text="", fields=[
        _f(0, label, type_, required=required, options=options)])


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("label, key, answers", [
    # the sponsor profile: authorized Yes, needs sponsorship; a Yes here says otherwise
    (_WITHOUT, "work_authorized", {"requires_sponsorship": "Yes"}),
    # the citizen: needs no sponsorship; a No here reads as not authorized
    (_WITHOUT, "requires_sponsorship", {}),
    # willing to work on-site; a Yes here says remote only
    (_REMOTE_ONLY, "onsite_ok", {}),
])
def test_a_yes_no_list_mapped_to_a_fact_that_answers_another_question_gets_no_value(
        tmp_path, required, label, key, answers):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _one_field(label, required=required)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: (key, 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.value, pf.fact_key) == ("skip", None, "", None)
    assert [m[0] for m in p.missing] == [label]
    assert p.park_reason == (f"required field without an answer: {label}" if required else "")
    # nothing is asked of the judge for it: no pick, no second look at a pick
    assert apply_judge.option_questions(digest, p, catalog=cat)[1] == {}
    assert apply_judge.reask_targets(digest, cat, {}, p, what="pick") == []


@pytest.mark.parametrize("label, key, answers, option", [
    (_WITHOUT, "authorized_without_sponsorship", {}, "Yes"),
    (_WITHOUT, "authorized_without_sponsorship", {"requires_sponsorship": "Yes"}, "No"),
    (_WITHOUT, "authorized_without_sponsorship", {"work_authorized": "No"}, "No"),
    (_REMOTE_ONLY, "remote_only", {}, "No"),
    (_REMOTE_ONLY, "remote_only", {"onsite_ok": "No"}, "Yes"),
])
def test_the_derived_fact_settles_its_own_question_for_either_profile(tmp_path, label, key,
                                                                      answers, option):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _one_field(label)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: (key, 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.fact_key) == ("select", option, key)
    assert p.park_reason == ""


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("label, action", [
    ("Years of Python experience", "skip"),
    ("How many years of experience do you have with Python?", "skip"),
    ("Years of experience", "fill"),
    ("How many years of relevant experience do you have?", "fill"),
])
def test_a_number_box_takes_the_total_years_only_for_the_total_years_question(
        tmp_path, required, label, action):
    cat = _profile_catalog(tmp_path)
    digest = _one_field(label, "number", required=required, options=())
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("years_experience", 1.0)}))
    pf = p.fields[0]
    assert pf.action == action
    if action == "fill":
        assert pf.value == "2" and p.park_reason == ""
    else:
        assert (pf.value, pf.fact_key) == ("", None)
        assert [m[0] for m in p.missing] == [label]
        assert p.park_reason == (f"required field without an answer: {label}" if required
                                 else "")


def test_a_worded_list_whose_question_is_another_facts_asks_the_judge_nothing(tmp_path):
    # the judge's pick never overrides the gate: a qualified list mapped to
    # work_authorized under a without-sponsorship question is no pick question
    cat = _profile_catalog(tmp_path, requires_sponsorship="Yes")
    options = ("Yes, I am authorized and do not require sponsorship", "No")
    digest = _one_field(_WITHOUT, options=options)
    answers = _page_answers(digest, {0: ("work_authorized", 1.0)}, options={0: (options[0], 1.0)})
    p = apply_judge.plan(digest, cat, answers)
    assert (p.fields[0].action, p.fields[0].option) == ("skip", None)
    # a plan made by hand with the mapping kept still asks nothing and re-asks nothing
    kept = apply_judge.FillPlan(fields=[apply_judge.PlannedField(
        n=0, locator=(0, "#f0"), label=_WITHOUT, required=True, fact_key="work_authorized",
        value="Yes", option=None, confidence=1.0, action="skip", options=list(options))])
    assert apply_judge.option_questions(digest, kept, catalog=cat)[1] == {}
    assert apply_judge.reask_targets(digest, cat, {}, kept, what="pick") == []
    # its own question is asked as before
    own = _one_field("Are you legally authorized to work in the United States?",
                     options=options)
    kept.fields[0].label = own.fields[0].label
    assert list(apply_judge.option_questions(own, kept, catalog=cat)[1]) == ["field_0_pick"]
    assert apply_judge.reask_targets(own, cat, {}, kept, what="pick") == [0]


# --- cycle 18 SP6c round 2: narrower forms, unrestricted work, role qualifiers ----------------

_H1B = "Will you require H-1B sponsorship?"
_NOW = "Do you currently require visa sponsorship to work in the U.S.?"
_UNRESTRICTED = "Do you have unrestricted work authorization in the US?"

_FITS = [
    # requires_sponsorship: a named visa type or "now only" is a narrower form
    # of needing sponsorship; a status held now or a transfer is another question
    ("requires_sponsorship", "Will you now or in the future require visa sponsorship?", "own"),
    ("requires_sponsorship", "Do you require sponsorship (e.g., H-1B, TN, O-1) to work for us?",
     "own"),
    ("requires_sponsorship", "Will you require sponsorship now or later?", "own"),
    ("requires_sponsorship", _H1B, "narrower"),
    ("requires_sponsorship", "I will require H-1B visa sponsorship now or in the future.",
     "narrower"),
    ("requires_sponsorship", "Will you require OPT or CPT sponsorship?", "narrower"),
    ("requires_sponsorship", "Will you require TN visa sponsorship?", "narrower"),
    ("requires_sponsorship", _NOW, "narrower"),
    ("requires_sponsorship", "Do you require sponsorship now?", "narrower"),
    ("requires_sponsorship", "Do you require visa sponsorship at this time?", "narrower"),
    ("requires_sponsorship", "Are you currently sponsored by an employer?", "other"),
    ("requires_sponsorship", "Are you currently on a visa that requires sponsorship?", "other"),
    ("requires_sponsorship", "Is your current visa sponsored by your employer?", "other"),
    ("requires_sponsorship", "Will you require a sponsorship transfer?", "other"),
    ("requires_sponsorship", "Will you require sponsorship for an H-1B transfer?", "other"),
    ("requires_sponsorship", "Do you currently hold an H-1B visa that would need to be "
                             "transferred?", "other"),
    ("requires_sponsorship", "Are you able to work in the U.S. without employer sponsorship, "
                             "now and in the future?", "other"),
    # work_authorized: unrestricted work is the derived fact's question
    ("work_authorized", "Are you legally authorized to work in the United States?", "own"),
    ("work_authorized", _UNRESTRICTED, "other"),
    ("work_authorized", "Are you authorized to work in the US without any restrictions?",
     "other"),
    ("work_authorized", "Are you legally authorized to work in the United States without "
                        "restriction?", "other"),
    ("work_authorized", "Are there any restrictions on your authorization to work in the US?",
     "other"),
    ("authorized_without_sponsorship", _UNRESTRICTED, "own"),
    ("authorized_without_sponsorship", "Are you authorized to work in the US without any "
                                       "restrictions?", "own"),
    ("authorized_without_sponsorship", "Are you legally authorized to work in the United "
                                       "States without restriction?", "own"),
    ("authorized_without_sponsorship", "Are you authorized to work in the U.S. without "
                                       "restrictions?", "own"),
    # "any restrictions" asks the inverse (a Yes there says restricted), and
    # a visa type narrows it
    ("authorized_without_sponsorship", "Are there any restrictions on your authorization to "
                                       "work in the US?", "other"),
    ("authorized_without_sponsorship", "Is your work authorization restricted to one "
                                       "employer?", "other"),
    ("authorized_without_sponsorship", "Do you have unrestricted work authorization on an "
                                       "H-1B?", "other"),
    # years_experience: a role qualifier asks the total, a named field or skill does not
    ("years_experience", "Do you have 3 or more years of experience in a similar role?",
     "other"),                                  # a number (round 7, rule 6)
    ("years_experience", "How many years of experience do you have in a similar position?",
     "own"),
    ("years_experience", "Years of experience in a similar capacity", "own"),
    # one job's years (round 7, rule 6)
    ("years_experience", "How many years of experience do you have in this role?", "other"),
    ("years_experience", "Years of experience in a related role", "own"),
    ("years_experience", "How many years of experience relevant to this role do you have?",
     "own"),
    ("years_experience", "Years of experience relevant to this role", "own"),
    ("years_experience", "How many years of experience do you have in software engineering?",
     "other"),
    ("years_experience", "How many years of experience do you have with Python?", "other"),
    ("years_experience", "How many years of sales experience do you have?", "other"),
    ("years_experience", "How many years of experience do you have in sales?", "other"),
    ("years_experience", "Years of experience in a sales role", "other"),
]


@pytest.mark.parametrize("key, label, fit", _FITS)
def test_question_fit_reads_the_own_question_a_narrower_form_and_another(key, label, fit):
    assert apply_judge.question_fit(key, label) == fit
    assert apply_judge.asks_own_question(key, label) is (fit == "own")


@pytest.mark.parametrize("label", [_H1B, _NOW, "Do you require sponsorship now?",
                                   "Do you require visa sponsorship at this time?"])
def test_a_narrower_sponsorship_question_is_answered_by_no_alone(label):
    # no sponsorship now or in the future is No to every narrower form; a
    # Yes now or later says nothing of an H-1B or of now
    assert apply_judge.answers_question("requires_sponsorship", "No", label) is True
    assert apply_judge.answers_question("requires_sponsorship", "no", label) is True
    assert apply_judge.answers_question("requires_sponsorship", "Yes", label) is False
    assert apply_judge.answers_question("requires_sponsorship", "", label) is False


def test_answers_question_takes_any_value_for_the_own_question_and_none_for_another():
    for value in ("Yes", "No"):
        assert apply_judge.answers_question("requires_sponsorship", value,
                                            "Do you require visa sponsorship?") is True
        for other in ("Are you currently sponsored by an employer?",
                      "Are you currently on a visa that requires sponsorship?",
                      "Will you require a sponsorship transfer?",
                      "Do you currently hold an H-1B visa that would need to be transferred?"):
            assert apply_judge.answers_question("requires_sponsorship", value, other) is False
        # only sponsorship has a narrower form: a visa type stays another
        # question for the work facts
        assert apply_judge.answers_question(
            "work_authorized", value,
            "Are you authorized to work in the United States on an H-1B visa?") is False
        assert apply_judge.answers_question("work_authorized", value, _UNRESTRICTED) is False
    # the help is read, and a fact with no table is never refused
    assert apply_judge.answers_question("requires_sponsorship", "No", "Sponsorship",
                                        _H1B) is True
    assert apply_judge.answers_question("requires_sponsorship", "Yes", "Sponsorship",
                                        _H1B) is False
    assert apply_judge.answers_question("email", "a@example.com", "Email") is True


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("label", [_H1B, _NOW])
def test_a_narrower_sponsorship_list_takes_no_and_leaves_yes_unanswered(tmp_path, required,
                                                                         label):
    digest = _one_field(label, required=required)
    # the citizen: no sponsorship now or in the future answers it
    cat = _profile_catalog(tmp_path)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("requires_sponsorship", 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.fact_key) == ("select", "No", "requires_sponsorship")
    assert p.park_reason == ""
    # the sponsor: a Yes now or later is no answer, and the judge's Yes never lands
    cat = _profile_catalog(tmp_path, requires_sponsorship="Yes")
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("requires_sponsorship", 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.value, pf.fact_key) == ("skip", None, "", None)
    assert [m[0] for m in p.missing] == [label]
    assert p.park_reason == (f"required field without an answer: {label}" if required else "")
    assert apply_judge.option_questions(digest, p, catalog=cat)[1] == {}
    assert apply_judge.reask_targets(digest, cat, {}, p, what="pick") == []


def test_a_worded_narrower_list_asks_the_judge_for_no_and_nothing_for_yes(tmp_path):
    options = ("Yes, I will require H-1B sponsorship", "No, I will not require H-1B sponsorship")
    digest = _one_field(_H1B, options=options)
    for value, asked in (("No", ["field_0_pick"]), ("Yes", [])):
        cat = _profile_catalog(tmp_path, requires_sponsorship=value)
        kept = apply_judge.FillPlan(fields=[apply_judge.PlannedField(
            n=0, locator=(0, "#f0"), label=_H1B, required=True,
            fact_key="requires_sponsorship", value=value, option=None, confidence=1.0,
            action="skip", options=list(options))])
        assert list(apply_judge.option_questions(digest, kept, catalog=cat)[1]) == asked, value
        assert apply_judge.reask_targets(digest, cat, {}, kept, what="pick") == (
            [0] if asked else []), value


@pytest.mark.parametrize("answers, option", [
    ({}, "Yes"),                                       # the citizen
    ({"requires_sponsorship": "Yes"}, "No"),           # the sponsor
    ({"work_authorized": "No"}, "No"),
])
def test_unrestricted_work_authorization_is_the_derived_facts_question(tmp_path, answers,
                                                                        option):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _one_field(_UNRESTRICTED)
    p = apply_judge.plan(digest, cat, _page_answers(
        digest, {0: ("authorized_without_sponsorship", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("select", option)
    # mapped to work_authorized it gets no value: authorized is no answer to unrestricted
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("work_authorized", 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option, p.fields[0].fact_key) == (
        "skip", None, None)


@pytest.mark.parametrize("label, action", [
    ("How many years of experience do you have in a similar role?", "fill"),
    ("Years of experience in a similar capacity", "fill"),
    ("How many years of experience relevant to this role do you have?", "fill"),
    ("How many years of experience do you have in sales?", "skip"),
])
def test_a_number_box_takes_the_total_years_for_a_role_qualifier_and_never_a_field(
        tmp_path, label, action):
    cat = _profile_catalog(tmp_path)
    digest = _one_field(label, "number", options=())
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("years_experience", 1.0)}))
    assert (p.fields[0].action, p.fields[0].value) == (action, "2" if action == "fill" else "")


# --- cycle 18 SP6c round 3: every content word is the fact's own ------------------------------

_CRIT_WITHOUT = "Are you legally authorized to work in the United States? (Without sponsorship)"
_CRIT_SELECT_NO = ("Are you legally authorized to work in the United States? (If you will "
                   "require sponsorship, please select No.)")
_AUTH = "Are you legally authorized to work in the United States?"
_CRIT_HELP = "Answer No if you need sponsorship."
_REMOTE_PREAMBLE = "This role is remote only. Are you comfortable with that?"

# (fact, label, help, fit): the review's wrong answers, each refused or read
# as the fact whose own question it is, and the plain forms in each
# vocabulary, each passing
_WORDS = [
    # Critical: a parenthetical, an instruction or a help sentence names sponsorship
    ("work_authorized", _CRIT_WITHOUT, "", "other"),
    ("authorized_without_sponsorship", _CRIT_WITHOUT, "", "own"),
    ("requires_sponsorship", _CRIT_WITHOUT, "", "other"),
    ("work_authorized", _CRIT_SELECT_NO, "", "other"),
    ("authorized_without_sponsorship", _CRIT_SELECT_NO, "", "other"),
    ("requires_sponsorship", _CRIT_SELECT_NO, "", "other"),
    ("work_authorized", _AUTH, _CRIT_HELP, "other"),
    ("authorized_without_sponsorship", _AUTH, _CRIT_HELP, "other"),
    ("requires_sponsorship", _AUTH, _CRIT_HELP, "other"),
    ("work_authorized", "Are you authorized to work in the United States (for any employer)?",
     "", "other"),
    # I1: a statement before the question is read too
    ("remote_only", _REMOTE_PREAMBLE, "", "other"),
    ("onsite_ok", _REMOTE_PREAMBLE, "", "other"),
    ("remote_only", "This role is remote only.", "", "other"),
    # I2: another country, a clearance
    ("work_authorized", "Are you authorized to work in Canada?", "", "other"),
    ("work_authorized", "Are you legally eligible to work in the United Kingdom?", "", "other"),
    ("requires_sponsorship", "Will you require sponsorship to work in the UK?", "", "other"),
    ("requires_sponsorship", "Will you now or in the future require visa sponsorship to work "
                             "in the UK?", "", "other"),
    ("work_authorized", "Are you eligible to obtain a U.S. security clearance?", "", "other"),
    ("work_authorized", "Are you eligible for a security clearance?", "", "other"),
    # I3: a skill, a tool or a duty, wherever it stands
    ("years_experience", "Python - years of experience", "", "other"),
    ("years_experience", "Java (years of experience)", "", "other"),
    ("years_experience", "Do you have experience with Python? How many years?", "", "other"),
    ("years_experience", "How many years of experience do you have managing people?", "",
     "other"),
    ("years_experience", "Years of experience building APIs", "", "other"),
    ("years_experience", "Years of experience", "Years of experience with Kubernetes.", "other"),
    # I4: relocation assistance, a place already lived in
    ("willing_to_relocate", "Do you require relocation assistance?", "", "other"),
    ("willing_to_relocate", "Will you need relocation assistance?", "", "other"),
    ("willing_to_relocate", "Are you located in or willing to relocate to Austin, TX?", "",
     "other"),
    ("willing_to_relocate", "Are you located in or willing to relocate to the job's location?",
     "", "other"),
    ("willing_to_relocate", "Are you willing to relocate to Austin?", "", "other"),
    ("willing_to_relocate", "Are you willing to relocate to the job location (must live "
                            "within 50 miles)?", "", "other"),
    # I5: a commute, a place, a preference
    ("onsite_ok", "Are you able to commute to our office?", "", "other"),
    ("onsite_ok", "Are you able to commute to our Austin office three days a week?", "",
     "other"),
    ("onsite_ok", "Are you located within commuting distance of our office?", "", "other"),
    ("onsite_ok", "Would you rather work remotely than in the office?", "", "other"),
    ("remote_only", "Would you rather work remotely than in the office?", "", "other"),
    ("onsite_ok", "Are you willing to work on-site in New York?", "", "other"),
    # round 4: "comfortable" with on-site work is its own question
    ("onsite_ok", "Are you comfortable working on-site?", "", "own"),
    # Minor 1: plain forms that pass
    ("work_authorized", "Are you able to work in the US?", "", "own"),
    ("authorized_without_sponsorship", "Are you authorized to work in the US and will not "
                                       "require sponsorship?", "", "own"),
    ("work_authorized", "Are you authorized to work in the US and will not require "
                        "sponsorship?", "", "other"),
    ("requires_sponsorship", "Are you authorized to work in the US and will not require "
                             "sponsorship?", "", "other"),
    ("remote_only", "Do you require a fully remote role?", "", "own"),
    ("onsite_ok", "Do you require a fully remote role?", "", "other"),
    # every vocabulary's plain forms
    ("work_authorized", "Are you authorised to work in the United States?", "", "own"),
    ("work_authorized", "Are you permitted to work in America?", "", "own"),
    ("work_authorized", "Are you legally authorized to work in the USA?", "", "own"),
    ("work_authorized", _AUTH, "Please select one.", "own"),
    ("work_authorized", "Are you legally authorized to work in the United States? * Required",
     "", "own"),
    ("requires_sponsorship", "Do you need visa sponsorship?", "", "own"),
    # an employer's sponsorship (round 7, rule 8)
    ("requires_sponsorship", "Will you now or in the future need an employer to sponsor your "
                             "visa?", "", "other"),
    ("requires_sponsorship", "Will you now or at any time in the future require sponsorship?",
     "", "own"),
    # round 6: "If yes, please explain" is a neutral tail of the fixed list
    ("requires_sponsorship", "Do you require sponsorship to work in the US? (If yes, please "
                             "explain.)", "", "own"),
    ("authorized_without_sponsorship", "Can you work in the United States without the need "
                                       "for sponsorship?", "", "own"),
    ("authorized_without_sponsorship", "Are you authorized to work in the US without visa "
                                       "sponsorship?", "", "own"),
    ("authorized_without_sponsorship", "Are you authorized to work in the US and do not "
                                       "require sponsorship?", "", "own"),
    ("willing_to_relocate", "Are you willing to relocate for this role?", "", "own"),
    ("willing_to_relocate", "Are you open to relocation?", "", "own"),
    ("willing_to_relocate", "Would you move for this position?", "", "own"),
    # round 5: a place name beside "the job location" is another word (the
    # plan cannot check it is the job's location)
    ("willing_to_relocate", "Are you willing to relocate to the job location (New York)?", "",
     "other"),
    ("onsite_ok", "Are you willing to come into the office 3 days per week?", "", "narrower"),
    ("onsite_ok", "Are you able to report to our office in person?", "", "own"),
    ("onsite_ok", "Can you work onsite?", "", "own"),
    # hybrid is a narrower form of on-site work: a Yes to on-site settles it
    ("onsite_ok", "Are you willing to work a hybrid schedule?", "", "narrower"),
    ("onsite_ok", "Are you open to hybrid work?", "", "narrower"),
    # round 5: "open" and "willing" ask about accepting remote work
    ("remote_only", "Are you only open to remote roles?", "", "other"),
    ("remote_only", "Are you only willing to work remotely?", "", "other"),
    ("remote_only", "Are you looking for a remote role?", "", "other"),
    ("years_experience", "How many years of relevant professional experience do you have?", "",
     "own"),
    ("years_experience", "Years of overall experience", "", "own"),
    # a number (round 7, rule 6)
    ("years_experience", "Do you have 5+ years of related industry experience?", "", "other"),
    # round 6: "Please enter a number" is a neutral tail of the fixed list
    ("years_experience", "Years of experience", "Please enter a number.", "own"),
    ("years_experience", "How many years of professional software development experience do "
                         "you have?", "", "other"),
    # a common long form, a neutral lead or tail, a synonym; an unnamed country
    ("requires_sponsorship", "Will you now, or in the future, require the Company to commence "
                             "(\"sponsor\") an immigration case in order to employ you (for "
                             "example, H-1B or other employment-based immigration case)?", "",
     "own"),
    ("work_authorized", "Are you authorized to work in the US? (Yes/No)", "", "own"),
    ("work_authorized", "Are you authorized to work lawfully in the United States?", "", "own"),
    # round 5: a lead such as "Please enter" is read; round 6: the years hold it
    ("years_experience", "Please enter your total years of experience", "", "own"),
    ("work_authorized", "Are you legally authorized to work in the country in which this job is "
                        "located?", "", "other"),
]


@pytest.mark.parametrize("key, label, help_text, fit", _WORDS)
def test_question_fit_reads_every_content_word_of_the_label_and_the_help(key, label,
                                                                         help_text, fit):
    assert apply_judge.question_fit(key, label, help_text) == fit
    assert apply_judge.asks_own_question(key, label, help_text) is (fit == "own")


def test_a_hybrid_question_is_answered_by_yes_to_on_site_alone():
    label = "Are you willing to work a hybrid schedule?"
    assert apply_judge.answers_question("onsite_ok", "Yes", label) is True
    assert apply_judge.answers_question("onsite_ok", "No", label) is False
    assert apply_judge.answers_question("onsite_ok", "", label) is False


def test_a_cut_label_answers_no_fact_with_a_table():
    for key, label in (("work_authorized", _AUTH),
                       ("requires_sponsorship", "Do you require visa sponsorship?"),
                       ("years_experience", "Years of experience")):
        assert apply_judge.question_fit(key, label) == "own"
        assert apply_judge.question_fit(key, label, partial=True) == "other"
        assert apply_judge.asks_own_question(key, label, partial=True) is False
        assert apply_judge.answers_question(key, "Yes", label, partial=True) is False
    # a fact with no table keeps its answer
    assert apply_judge.answers_question("email", "a@example.com", "Email", partial=True) is True


@pytest.mark.parametrize("text, words", [
    # the phrase map
    ("Will you now or in the future require sponsorship?",
     {"NOWFUTURE", "require", "sponsorship"}),
    ("Will you now, or in the future, require sponsorship?",
     {"NOWFUTURE", "require", "sponsorship"}),
    ("Do you currently or will you in the future require visa sponsorship?",
     {"NOWFUTURE", "require", "visa", "sponsorship"}),
    ("Will you require sponsorship in the future?", {"FUTURE", "require", "sponsorship"}),
    ("Do you currently require sponsorship?", {"NOW", "require", "sponsorship"}),
    ("Are you authorized to work in the United States?", {"authorized", "work", "US"}),
    ("Are you authorized to work in the U.S.?", {"authorized", "work", "US"}),
    ("Are you authorized to work in the US?", {"authorized", "work", "US"}),
    ("Are you authorized to work in the USA?", {"authorized", "work", "US"}),
    ("Are you authorized to work in America?", {"authorized", "work", "US"}),
    ("Are you authorized to work without sponsorship?", {"authorized", "work", "NOSPONSOR"}),
    ("Are you authorized to work without visa sponsorship?",
     {"authorized", "work", "NOSPONSOR"}),
    ("Are you authorized to work without the need for sponsorship?",
     {"authorized", "work", "NOSPONSOR"}),
    ("Are you authorized to work and will not require sponsorship?",
     {"authorized", "work", "and", "NOSPONSOR"}),
    ("Are you authorized to work and do not require sponsorship?",
     {"authorized", "work", "and", "NOSPONSOR"}),
    ("Are you authorized to work without restriction?", {"authorized", "work", "UNRESTRICTED"}),
    ("Are you authorized to work without restrictions?",
     {"authorized", "work", "UNRESTRICTED"}),
    ("Do you have unrestricted work authorization?",
     {"have", "UNRESTRICTED", "work", "authorization"}),
    ("Can you work on-site?", {"work", "ONSITE"}),
    ("Can you work on site?", {"work", "ONSITE"}),
    ("Can you work onsite?", {"work", "ONSITE"}),
    ("Can you work in person?", {"work", "ONSITE"}),
    ("Can you work in-person?", {"work", "ONSITE"}),
    ("Can you work in the office?", {"work", "ONSITE"}),
    ("Can you work in our office?", {"work", "ONSITE"}),
    ("Can you work in office?", {"work", "ONSITE"}),
    ("Can you work on-site 3 days a week?", {"work", "ONSITE", "DAYSWEEK"}),
    ("Can you work on-site three days per week?", {"work", "ONSITE", "DAYSWEEK"}),
    ("Will you require H-1B sponsorship?", {"require", "VISATYPE", "sponsorship"}),
    # an example made of visa types only is one token (round 5: it is read,
    # never dropped); a neutral tail of the fixed list is no word of the
    # question
    ("Do you require sponsorship (e.g., H-1B, TN, O-1)?",
     {"require", "sponsorship", "VISAEXAMPLE"}),
    ("Will you need the company to sponsor a visa (such as an H-1B)?",
     {"need", "company", "sponsor", "visa", "VISAEXAMPLE"}),
    ("Do you require sponsorship, for example an H-1B?",
     {"require", "sponsorship", "VISAEXAMPLE"}),
    ("Are you authorized to work in the US? (If not, please explain.)",
     {"authorized", "work", "US"}),
    ("Are you authorized to work in the US? Please select one.", {"authorized", "work", "US"}),
    ("Are you authorized to work in the US? Select one", {"authorized", "work", "US"}),
    ("Are you authorized to work in the US? (Required)", {"authorized", "work", "US"}),
    ("Are you authorized to work in the US? *", {"authorized", "work", "US"}),
    # every other word is kept
    ("Are you authorized to work in Canada?", {"authorized", "work", "canada"}),
    ("Do you require relocation assistance?", {"require", "relocation", "assistance"}),
])
def test_question_words_maps_the_phrases_and_drops_neutral_tails_and_filler(text, words):
    assert set(apply_facts.question_words(text)) == words


# the sponsor profile: authorized, needs sponsorship
_SPONSOR = {"requires_sponsorship": "Yes"}


def _field_with(label, help_text="", type_="radio", required=True, options=("Yes", "No"),
                partial=False):
    import dataclasses
    f = dataclasses.replace(_f(0, label, type_, required=required, options=options,
                               help=help_text), label_partial=partial)
    return FormDigest(url_host="x", title="t", text="", fields=[f])


@pytest.mark.parametrize("answers", [{}, _SPONSOR])
@pytest.mark.parametrize("label, help_text", [
    (_CRIT_WITHOUT, ""), (_CRIT_SELECT_NO, ""), (_AUTH, _CRIT_HELP),
    ("Are you authorized to work in Canada?", ""),
    ("Are you eligible to obtain a U.S. security clearance?", "")])
def test_work_authorization_with_another_word_in_its_label_or_help_gets_no_value(
        tmp_path, answers, label, help_text):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label, help_text)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("work_authorized", 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.value, pf.fact_key) == ("skip", None, "", None)
    assert p.park_reason == f"required field without an answer: {label}"
    assert apply_judge.option_questions(digest, p, catalog=cat)[1] == {}
    assert apply_judge.reask_targets(digest, cat, {}, p, what="pick") == []


@pytest.mark.parametrize("answers, option", [({}, "Yes"), (_SPONSOR, "No")])
def test_without_sponsorship_in_parentheses_is_the_derived_facts_question(tmp_path, answers,
                                                                          option):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(_CRIT_WITHOUT)
    p = apply_judge.plan(digest, cat, _page_answers(
        digest, {0: ("authorized_without_sponsorship", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("select", option)
    assert p.park_reason == ""


@pytest.mark.parametrize("label, key", [
    (_REMOTE_PREAMBLE, "remote_only"),
    ("Are you able to commute to our office?", "onsite_ok"),
    ("Would you rather work remotely than in the office?", "onsite_ok"),
    ("Do you require relocation assistance?", "willing_to_relocate"),
    ("Are you located in or willing to relocate to Austin, TX?", "willing_to_relocate"),
    ("Will you require sponsorship to work in the UK?", "requires_sponsorship"),
])
def test_a_yes_no_question_with_a_word_its_fact_does_not_use_gets_no_value(tmp_path, label,
                                                                          key):
    cat = _profile_catalog(tmp_path)
    digest = _field_with(label, required=False)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: (key, 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.value, pf.fact_key) == ("skip", None, "", None)
    assert p.park_reason == ""


@pytest.mark.parametrize("label, action", [
    ("Python - years of experience", "skip"),
    ("Java (years of experience)", "skip"),
    ("How many years of experience do you have managing people?", "skip"),
    ("Years of experience building APIs", "skip"),
    ("Years of overall experience", "fill"),
])
def test_a_number_box_naming_a_skill_anywhere_takes_no_total_years(tmp_path, label, action):
    cat = _profile_catalog(tmp_path)
    digest = _field_with(label, type_="number", options=())
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("years_experience", 1.0)}))
    assert (p.fields[0].action, p.fields[0].value) == (action, "2" if action == "fill" else "")


@pytest.mark.parametrize("value, action, option", [("Yes", "select", "Yes"),
                                                   ("No", "skip", None)])
def test_a_hybrid_list_takes_yes_to_on_site_and_leaves_no_unanswered(tmp_path, value, action,
                                                                    option):
    cat = _profile_catalog(tmp_path, onsite_ok=value)
    digest = _field_with("Are you willing to work a hybrid schedule?")
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("onsite_ok", 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == (action, option)


def test_a_cut_label_parks_a_yes_no_and_a_number_field(tmp_path):
    cat = _profile_catalog(tmp_path)
    for label, key, type_, options in (
            (_AUTH, "work_authorized", "radio", ("Yes", "No")),
            ("Years of experience", "years_experience", "number", ())):
        digest = _field_with(label, type_=type_, options=options, partial=True)
        p = apply_judge.plan(digest, cat, _page_answers(digest, {0: (key, 1.0)}))
        assert (p.fields[0].action, p.fields[0].fact_key) == ("skip", None), label
        assert p.park_reason == f"required field without an answer: {label}"
        whole = _field_with(label, type_=type_, options=options)
        p = apply_judge.plan(whole, cat, _page_answers(whole, {0: (key, 1.0)}))
        assert p.fields[0].action in ("select", "fill"), label


_CLEARANCE = "Do you have an active security clearance?"
_SQL = "How many years of SQL experience do you have?"


def _custom_catalog(tmp_path):
    bank = standard_bank() + [
        custom("clearance", _CLEARANCE, "Yes", type="yes_no"),
        custom("sql_years", _SQL, "4", type="number"),
        custom("why_us", "Why do you want to work here?", "The analytics team's work.")]
    return apply_facts.build(tmp_path, answers=bank, today=date(2026, 9, 21))


@pytest.mark.parametrize("label, help_text, action", [
    (_CLEARANCE, "", "select"),
    ("Do you have an active security clearance? *", "", "select"),
    (_CLEARANCE, "Please select one.", "select"),
    ("Do you have an active TS/SCI security clearance?", "", "skip"),
    ("Are you eligible to obtain a security clearance?", "", "skip"),
    ("Do you have a security clearance?", "", "skip"),
    (_CLEARANCE, "Answer No if it has lapsed.", "skip"),
])
def test_a_custom_yes_no_answer_settles_only_its_own_saved_question(tmp_path, label,
                                                                   help_text, action):
    cat = _custom_catalog(tmp_path)
    digest = _field_with(label, help_text)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("answer_clearance", 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    pf = p.fields[0]
    assert pf.action == action
    if action == "select":
        assert (pf.option, pf.fact_key) == ("Yes", "answer_clearance")
    else:
        assert (pf.option, pf.value, pf.fact_key) == (None, "", None)
        assert p.park_reason == f"required field without an answer: {label}"
    # a cut label never matches the saved question
    cut = _field_with(label, help_text, partial=True)
    p = apply_judge.plan(cut, cat, _page_answers(cut, {0: ("answer_clearance", 1.0)}))
    assert p.fields[0].action == "skip"


@pytest.mark.parametrize("label, action", [
    (_SQL, "fill"),
    ("How many years of SQL experience do you have? *", "fill"),
    ("Years of SQL experience", "skip"),
    ("How many years of SQL experience do you have with Snowflake?", "skip"),
])
def test_a_custom_number_answer_fills_only_its_own_saved_question(tmp_path, label, action):
    cat = _custom_catalog(tmp_path)
    digest = _field_with(label, type_="number", options=())
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("answer_sql_years", 1.0)}))
    assert (p.fields[0].action, p.fields[0].value) == (action, "4" if action == "fill" else "")


def test_a_custom_text_answer_and_the_catalog_check_for_each_type(tmp_path):
    cat = _custom_catalog(tmp_path)
    # a text answer is the judge's to place, as before
    digest = _field_with("Why us?", type_="textarea", options=())
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("answer_why_us", 1.0)}))
    assert (p.fields[0].action, p.fields[0].value) == ("fill", "The analytics team's work.")
    assert cat.answers_field("answer_why_us", "Why us?") is True
    # the yes / no and number answers match their saved question's words
    assert cat.answers_field("answer_clearance", _CLEARANCE) is True
    assert cat.answers_field("answer_clearance", "Do you have a security clearance?") is False
    assert cat.answers_field("answer_clearance", _CLEARANCE, partial=True) is False
    assert cat.answers_field("answer_sql_years", _SQL) is True
    assert cat.answers_field("answer_sql_years", "Years of SQL experience") is False
    # a stored fact reads its own question; its kind stays as it was
    assert cat.answers_field("work_authorized", _AUTH) is True
    assert cat.answers_field("work_authorized", _CRIT_WITHOUT) is False
    assert cat.answers_field("requires_sponsorship", "Will you require H-1B sponsorship?") is True
    assert cat.answers_field("requires_sponsorship", "Will you require H-1B sponsorship?",
                             value="Yes") is False
    assert cat.facts["answer_clearance"].kind == "text"


def test_a_custom_yes_no_list_mapped_by_hand_asks_the_judge_nothing_for_another_question(
        tmp_path):
    cat = _custom_catalog(tmp_path)
    options = ("Yes, I hold an active clearance", "No")
    for label, asked in ((_CLEARANCE, ["field_0_pick"]),
                         ("Do you have an active TS/SCI security clearance?", [])):
        digest = _field_with(label, options=options)
        kept = apply_judge.FillPlan(fields=[apply_judge.PlannedField(
            n=0, locator=(0, "#f0"), label=label, required=True, fact_key="answer_clearance",
            value="Yes", option=None, confidence=1.0, action="skip", options=list(options))])
        assert list(apply_judge.option_questions(digest, kept, catalog=cat)[1]) == asked, label
        assert apply_judge.reask_targets(digest, cat, {}, kept, what="pick") == (
            [0] if asked else []), label


# --- cycle 18 SP6c round 4: "comfortable" with on-site work -----------------------------------

_COMFORT_OFFICE = ("This role requires working in the office 3 days a week. Are you "
                   "comfortable with this?")
_COMFORT_HYBRID = "Are you comfortable with a hybrid schedule?"
_COMFORT_ONSITE = "Are you comfortable working on-site?"
_COMFORT_AUSTIN = "This role is on-site in our Austin office. Are you comfortable with this?"
_COMFORT_NOT = "This role is not in the office. Are you comfortable with this?"
_COMFORT_COMMUTE = "Are you comfortable commuting to our office?"

# (fact, label, fit): the on-site "comfortable" shape is on-site work's own
# question; a remote role, a city, a negation or a commute is another, and
# the new words open no other fact
_COMFORT = [
    # a count of days a week is the hybrid question (final review I2)
    ("onsite_ok", _COMFORT_OFFICE, "narrower"),
    ("onsite_ok", _COMFORT_ONSITE, "own"),
    ("onsite_ok", "This position requires working on-site. Are you comfortable with that?",
     "own"),
    # hybrid stays one-sided: a Yes to on-site work settles it, a No does not
    ("onsite_ok", _COMFORT_HYBRID, "narrower"),
    ("onsite_ok", "This role is hybrid. Are you comfortable with this?", "narrower"),
    # refused
    ("onsite_ok", _REMOTE_PREAMBLE, "other"),
    ("remote_only", _REMOTE_PREAMBLE, "other"),
    ("onsite_ok", _COMFORT_AUSTIN, "other"),
    ("onsite_ok", _COMFORT_NOT, "other"),
    ("onsite_ok", _COMFORT_COMMUTE, "other"),
    ("onsite_ok", "Are you comfortable working remotely?", "other"),
    ("onsite_ok", "This role requires working 3 days a week. Are you comfortable with this?",
     "other"),
    ("onsite_ok", "Are you comfortable with this?", "other"),
    # the new words are on-site work's alone
    ("remote_only", "This role is fully remote. Are you comfortable with this?", "other"),
    ("remote_only", "Are you comfortable working remotely only?", "other"),
    ("willing_to_relocate", "This role requires relocating. Are you comfortable with that?",
     "other"),
    ("work_authorized", "This role requires working in the US. Are you comfortable with this?",
     "other"),
    ("years_experience", "This role requires 5 years of experience. Are you comfortable with "
                         "this?", "other"),
    ("requires_sponsorship", "This role is not eligible for sponsorship. Are you comfortable "
                             "with that?", "other"),
]


@pytest.mark.parametrize("key, label, fit", _COMFORT)
def test_comfortable_with_on_site_work_is_its_own_question_and_no_other_facts(key, label, fit):
    assert apply_judge.question_fit(key, label) == fit
    assert apply_judge.asks_own_question(key, label) is (fit == "own")


@pytest.mark.parametrize("label", [_COMFORT_OFFICE, _COMFORT_ONSITE])
@pytest.mark.parametrize("value", ["Yes", "No"])
def test_comfortable_with_on_site_work_takes_the_on_site_answer(tmp_path, label, value):
    cat = _profile_catalog(tmp_path, onsite_ok=value)
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("onsite_ok", 1.0)}))
    pf = p.fields[0]
    if label == _COMFORT_OFFICE and value == "No":
        # 3 days a week is the hybrid question: a No to on-site work leaves
        # it to the person (final review I2)
        assert (pf.action, pf.option, pf.fact_key) == ("skip", None, None)
        assert p.park_reason == f"required field without an answer: {label}"
        return
    assert (pf.action, pf.option, pf.fact_key) == ("select", value, "onsite_ok")
    assert p.park_reason == ""


@pytest.mark.parametrize("value, action, option", [("Yes", "select", "Yes"),
                                                   ("No", "skip", None)])
def test_comfortable_with_a_hybrid_schedule_takes_yes_to_on_site_alone(tmp_path, value, action,
                                                                      option):
    cat = _profile_catalog(tmp_path, onsite_ok=value)
    digest = _field_with(_COMFORT_HYBRID)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("onsite_ok", 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == (action, option)


@pytest.mark.parametrize("label, key", [
    (_REMOTE_PREAMBLE, "onsite_ok"), (_REMOTE_PREAMBLE, "remote_only"),
    (_COMFORT_AUSTIN, "onsite_ok"), (_COMFORT_NOT, "onsite_ok"),
    (_COMFORT_COMMUTE, "onsite_ok")])
def test_comfortable_with_a_remote_role_a_city_a_negation_or_a_commute_gets_no_value(
        tmp_path, label, key):
    cat = _profile_catalog(tmp_path)
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: (key, 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.value, pf.fact_key) == ("skip", None, "", None)
    assert p.park_reason == f"required field without an answer: {label}"
    assert apply_judge.option_questions(digest, p, catalog=cat)[1] == {}
    assert apply_judge.reask_targets(digest, cat, {}, p, what="pick") == []


# --- cycle 18 SP6c round 5: nothing is dropped before the allowlist reads it -----------------

_E1 = ("Are you legally authorized to work in the United States, e.g. as a citizen or "
       "permanent resident, without the need for sponsorship?")
_E2_HELP = "For example, you hold a green card and will not need sponsorship now or in the future"
_NBSP, _NBHYPHEN, _EN, _EM, _RSQUO = chr(0xA0), chr(0x2011), chr(0x2013), chr(0x2014), chr(0x2019)

# (fact, label, help, fit): the review's wrong settles, each refused or read
# as a narrower form, and the plain forms, each passing
_NOTHING_DROPPED = [
    # 1, 2: an example or a help sentence names a citizen, a green card, sponsorship
    ("work_authorized", _E1, "", "other"),
    ("authorized_without_sponsorship", _E1, "", "other"),
    ("requires_sponsorship", _E1, "", "other"),
    ("work_authorized", _AUTH, _E2_HELP, "other"),
    ("authorized_without_sponsorship", _AUTH, _E2_HELP, "other"),
    ("requires_sponsorship", "Do you require sponsorship (e.g. as a green card holder)?", "",
     "other"),
    # 3: no sponsorship now or on the start date: a Yes settles it, a No does not
    ("authorized_without_sponsorship", "Are you currently able to work without sponsorship?", "",
     "narrower"),
    ("authorized_without_sponsorship", "Will you be able to work without sponsorship on your "
                                       "start date?", "", "narrower"),
    ("authorized_without_sponsorship", "Are you able to work without sponsorship on your start "
                                       "date?", "", "narrower"),
    ("authorized_without_sponsorship", "Do you currently have unrestricted work "
                                       "authorization?", "", "narrower"),
    # 4: accepting remote work
    ("remote_only", "Are you open to a remote-only position?", "", "other"),
    ("remote_only", "Are you willing to work remote only?", "", "other"),
    # 5: the present job, no authorization word
    ("work_authorized", "Are you currently employed in the US?", "", "other"),
    ("work_authorized", "Do you currently work in the US?", "", "other"),
    # 6, 7: a parenthetical or an example on relocation
    ("willing_to_relocate", "Are you willing to relocate to the job location (No Relocation "
                            "Assistance)?", "", "other"),
    ("willing_to_relocate", "Are you willing to relocate to the job location (At Your Own "
                            "Expense)?", "", "other"),
    ("willing_to_relocate", "Are you willing to relocate, for example to Austin or Denver, "
                            "without relocation assistance?", "", "other"),
    # 8, 9: an example skill, a previous, prior, last or current role
    ("years_experience", "How many years of experience do you have, e.g. in Python?", "",
     "other"),
    ("years_experience", "How many years of experience in your previous role?", "", "other"),
    ("years_experience", "How many years of experience in your prior role?", "", "other"),
    ("years_experience", "How many years of experience in your last job?", "", "other"),
    ("years_experience", "How many years of experience in your current role?", "", "other"),
    # 10: sponsorship held, a sponsor, a continued sponsorship
    ("requires_sponsorship", "Do you have visa sponsorship?", "", "other"),
    ("requires_sponsorship", "Do you have a sponsor?", "", "other"),
    ("requires_sponsorship", "Will your employer continue to sponsor your visa?", "", "other"),
    # 11: the present job, on-site
    ("onsite_ok", "Are you currently working in an office?", "", "other"),
    ("onsite_ok", "Do you currently work on-site?", "", "other"),
    # round 6: a word of ability asks about now
    ("onsite_ok", "Are you currently able to work on-site?", "", "own"),
    # the plain forms
    ("work_authorized", "Are you able to work in the US?", "", "own"),
    ("work_authorized", "Do you have the right to work in the US?", "", "own"),
    ("work_authorized", "Do you have the legal right to work in the United States?", "", "own"),
    ("work_authorized", "Are you authorized to work in the U.S?", "", "own"),
    ("work_authorized", "Are you authorized to work in the U.S.?", "", "own"),
    ("work_authorized", "Are you authorized to work in the USA?", "", "own"),
    ("work_authorized", "Are you legally authorized to work in the US?", "", "own"),
    ("work_authorized", "Are you authorized to work in the US? (If no, please explain)", "",
     "own"),
    ("work_authorized", "Are you authorized to work in the US? Please explain.", "", "own"),
    ("work_authorized", "Are you authorized to work in the US? Yes / No", "", "own"),
    ("work_authorized", "Are you authorized to work in the US? Please select", "", "own"),
    ("work_authorized", "Are you authorized to work in the US?", "Select one", "own"),
    ("work_authorized", f"Are you authorized to work in the{_NBSP}US?", "", "own"),
    ("requires_sponsorship", "Sponsorship required?", "", "own"),
    ("requires_sponsorship", "Will you need sponsorship for this position?", "", "own"),
    ("requires_sponsorship", "Do you require sponsorship to work in this role?", "", "own"),
    ("requires_sponsorship", "Will you now, or in the future, require the Company to commence "
                             "(\"sponsor\") an immigration case in order to employ you (for "
                             "example, H-1B or other employment-based immigration case)?", "",
     "own"),
    ("requires_sponsorship", "Will you require H-1B sponsorship?", "", "narrower"),
    ("authorized_without_sponsorship", "Do you have unrestricted work authorization?", "", "own"),
    ("authorized_without_sponsorship", f"Are you authorized to work in the US and don{_RSQUO}t "
                                       "require sponsorship?", "", "own"),
    ("authorized_without_sponsorship", "Are you authorized to work in the US and won't need "
                                       "sponsorship?", "", "own"),
    ("remote_only", "Are you looking for a fully remote position only?", "", "own"),
    ("onsite_ok", "Are you able to work in the office 3 days a week?", "", "narrower"),
    ("onsite_ok", f"Are you willing to work on{_NBHYPHEN}site?", "", "own"),
    ("onsite_ok", f"Can you work on-site 3{_EN}4 days a week?", "", "narrower"),
    ("years_experience", "How many years of experience do you have?", "", "own"),
    ("years_experience", "How many years of experience do you have in similar positions?", "",
     "own"),
    ("years_experience", "Years of experience in similar roles", "", "own"),
    ("willing_to_relocate", "Are you willing to relocate? (Yes/No)", "", "own"),
    # "have" is read: only the facts whose own question uses it hold it
    ("requires_sponsorship", "Will you have visa sponsorship?", "", "other"),
    ("willing_to_relocate", "Have you relocated?", "", "other"),
    # round 6: "authorization" and "allowed" are authorization words
    ("work_authorized", "Do you have work authorization in the US?", "", "own"),
    ("work_authorized", "Are you allowed to work in the US?", "", "own"),
    # a word in another script, a lead off the fixed list, a tail with a comma
    ("work_authorized", f"Are you authorized to work in M{chr(0xE9)}xico?", "", "other"),
    ("work_authorized", f"Are you authorized to work in the US? ({chr(0x65E5)}{chr(0x672C)})", "",
     "other"),
    ("work_authorized", "Are you authorized to work in the US, please select one", "", "other"),
    ("years_experience", f"Python {_EM} years of experience", "", "other"),
]


@pytest.mark.parametrize("key, label, help_text, fit", _NOTHING_DROPPED)
def test_every_word_is_read_before_the_vocabulary_check(key, label, help_text, fit):
    assert apply_judge.question_fit(key, label, help_text) == fit
    assert apply_judge.asks_own_question(key, label, help_text) is (fit == "own")


@pytest.mark.parametrize("text, words", [
    # characters: "U.S." forms, a non-breaking space, hyphen and dashes, a
    # curly apostrophe
    ("Are you authorized to work in the U.S?", ("authorized", "work", "US")),
    ("Are you authorized to work in the U.S.?", ("authorized", "work", "US")),
    ("Are you authorized to work in the U.S.A.?", ("authorized", "work", "US")),
    ("Are you authorized to work in the USA?", ("authorized", "work", "US")),
    ("Are you authorized to work in the US?", ("authorized", "work", "US")),
    (f"Are you authorized to work in the{_NBSP}United States?", ("authorized", "work", "US")),
    (f"Can you work on{_NBHYPHEN}site?", ("work", "ONSITE")),
    (f"Can you work on-site 3{_EN}4 days a week?", ("work", "ONSITE", "DAYSWEEK")),
    (f"Will you require H{_NBHYPHEN}1B sponsorship?", ("require", "VISATYPE", "sponsorship")),
    # a contraction is spelled out: "not" is always read
    (f"Are you authorized to work in the US and don{_RSQUO}t require sponsorship?",
     ("authorized", "work", "US", "and", "NOSPONSOR")),
    ("Are you authorized to work in the US and won't need sponsorship?",
     ("authorized", "work", "US", "and", "NOSPONSOR")),
    ("Don't you require sponsorship?", ("not", "require", "sponsorship")),
    ("Can't you work on-site?", ("cannot", "work", "ONSITE")),
    ("Aren't you authorized to work in the US?", ("not", "authorized", "work", "US")),
    ("Isn't this role on-site?", ("is", "not", "this", "role", "ONSITE")),
    ("Doesn't the role require sponsorship?", ("not", "role", "require", "sponsorship")),
    # an example, a place, a lead and "in order to" keep their words
    ("Are you authorized to work in the US, e.g. as a citizen?",
     ("authorized", "work", "US", "eg", "as", "citizen")),
    ("Are you willing to relocate to the job location (New York)?",
     ("willing", "relocate", "job", "location", "new", "york")),
    ("Please enter your total years of experience",
     ("please", "enter", "total", "years", "experience")),
    ("Will you require sponsorship in order to work here?",
     ("require", "sponsorship", "order", "work", "here")),
    # "able", "have" and "legally" are words
    ("Are you legally able to work in the US?", ("legally", "able", "work", "US")),
    ("Do you have the right to work in the US?", ("have", "RIGHTTOWORK", "US")),
    # the fixed list of neutral tails, each dropped whole
    ("Are you authorized to work in the US? (If not, please explain.)",
     ("authorized", "work", "US")),
    ("Are you authorized to work in the US? (If no, please explain)", ("authorized", "work", "US")),
    ("Are you authorized to work in the US? Please explain.", ("authorized", "work", "US")),
    ("Are you authorized to work in the US? Please select one.", ("authorized", "work", "US")),
    ("Are you authorized to work in the US? Select one", ("authorized", "work", "US")),
    ("Are you authorized to work in the US? Please select", ("authorized", "work", "US")),
    ("Are you authorized to work in the US? (Required)", ("authorized", "work", "US")),
    ("Are you authorized to work in the US? (Yes/No)", ("authorized", "work", "US")),
    ("Are you authorized to work in the US? Yes / No", ("authorized", "work", "US")),
    ("Are you authorized to work in the US? * Required", ("authorized", "work", "US")),
    ("Years of Experience *", ("years", "experience")),
    # a tail off the list, or one inside the sentence, is read
    ("Are you authorized to work in the US? (If yes, please explain why.)",
     ("authorized", "work", "US", "if", "yes", "please", "explain", "why")),
    ("Are you authorized to work in the US, please select one",
     ("authorized", "work", "US", "please", "select", "one")),
    ("Required: are you authorized to work in the US?", ("required", "authorized", "work", "US")),
])
def test_question_words_normalises_characters_and_drops_no_word(text, words):
    assert apply_facts.question_words(text) == words


def test_a_neutral_tail_is_dropped_from_the_label_and_from_the_help():
    assert apply_facts.question_words(_AUTH, "Please select one.") == (
        "legally", "authorized", "work", "US")
    assert apply_facts.question_words(_AUTH, "Answer No if you need sponsorship.") == (
        "legally", "authorized", "work", "US", "answer", "no", "if", "need", "sponsorship")


_NOW_WITHOUT = ["Are you currently able to work without sponsorship?",
                "Will you be able to work without sponsorship on your start date?",
                "Do you currently have unrestricted work authorization?"]


@pytest.mark.parametrize("label", _NOW_WITHOUT)
@pytest.mark.parametrize("answers, action, option", [({}, "select", "Yes"),
                                                     (_SPONSOR, "skip", None)])
def test_no_sponsorship_now_or_on_the_start_date_is_settled_only_by_a_yes(
        tmp_path, label, answers, action, option):
    # a visa holder on OPT is authorized now: a stored No (sponsorship later)
    # is no answer to "now"
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(
        digest, {0: ("authorized_without_sponsorship", 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option) == (action, option)
    assert p.park_reason == ("" if action == "select"
                             else f"required field without an answer: {label}")


@pytest.mark.parametrize("label, help_text, key, answers", [
    (_E1, "", "work_authorized", {}),
    (_E1, "", "work_authorized", _SPONSOR),
    (_E1, "", "authorized_without_sponsorship", _SPONSOR),
    (_AUTH, _E2_HELP, "work_authorized", _SPONSOR),
    ("Are you open to a remote-only position?", "", "remote_only", {}),
    ("Are you willing to work remote only?", "", "remote_only", {}),
    ("Are you currently employed in the US?", "", "work_authorized", {}),
    ("Do you currently work in the US?", "", "work_authorized", {}),
    ("Are you willing to relocate to the job location (No Relocation Assistance)?", "",
     "willing_to_relocate", {}),
    ("Are you willing to relocate to the job location (At Your Own Expense)?", "",
     "willing_to_relocate", {}),
    ("Are you willing to relocate to the job location (New York)?", "",
     "willing_to_relocate", {}),
    ("Are you willing to relocate, for example to Austin or Denver, without relocation "
     "assistance?", "", "willing_to_relocate", {}),
    ("Do you have visa sponsorship?", "", "requires_sponsorship", _SPONSOR),
    ("Do you have a sponsor?", "", "requires_sponsorship", _SPONSOR),
    ("Will your employer continue to sponsor your visa?", "", "requires_sponsorship", _SPONSOR),
    ("Are you currently working in an office?", "", "onsite_ok", {}),
    ("Do you currently work on-site?", "", "onsite_ok", {}),
])
def test_the_reviews_wrong_settles_get_no_value(tmp_path, label, help_text, key, answers):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label, help_text)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: (key, 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.value, pf.fact_key) == ("skip", None, "", None)
    assert p.park_reason == f"required field without an answer: {label}"
    assert apply_judge.option_questions(digest, p, catalog=cat)[1] == {}
    assert apply_judge.reask_targets(digest, cat, {}, p, what="pick") == []


@pytest.mark.parametrize("label", ["Sponsorship required?",
                                   "Will you need sponsorship for this position?",
                                   "Do you require sponsorship to work in this role?"])
@pytest.mark.parametrize("answers, option", [({}, "No"), (_SPONSOR, "Yes")])
def test_a_sponsorship_question_with_a_word_of_need_takes_the_stored_answer(
        tmp_path, label, answers, option):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("requires_sponsorship", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("select", option)
    assert p.park_reason == ""


@pytest.mark.parametrize("label, action", [
    ("How many years of experience do you have, e.g. in Python?", "skip"),
    ("How many years of experience in your previous role?", "skip"),
    ("How many years of experience in your prior role?", "skip"),
    ("How many years of experience do you have in similar positions?", "fill"),
    ("Years of experience in similar roles", "fill"),
])
def test_a_number_box_for_an_example_skill_or_a_previous_role_takes_no_total_years(
        tmp_path, label, action):
    cat = _profile_catalog(tmp_path)
    digest = _field_with(label, type_="number", options=())
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("years_experience", 1.0)}))
    assert (p.fields[0].action, p.fields[0].value) == (action, "2" if action == "fill" else "")


_PREFER = "Do you prefer Python over Java?"
_PORTFOLIO = "Do you have a portfolio?"


@pytest.mark.parametrize("label, saved, same", [
    (_PREFER, _PREFER, True),
    ("Do you prefer Java over Python?", _PREFER, False),
    (_PORTFOLIO, _PORTFOLIO, True),
    ("Will you have a portfolio?", _PORTFOLIO, False),
    ("Can you have a portfolio?", _PORTFOLIO, False),
    ("Do you have a portfolio? *", _PORTFOLIO, True),
    (f"Do you have a portfolio{_NBSP}?", _PORTFOLIO, True),
    ("Do you have portfolio?", _PORTFOLIO, False),
])
def test_same_question_reads_every_word_in_order(label, saved, same):
    assert apply_facts.same_question(label, "", saved) is same


@pytest.mark.parametrize("label, action", [(_PREFER, "select"),
                                           ("Do you prefer Java over Python?", "skip"),
                                           (_PORTFOLIO, "select"),
                                           ("Will you have a portfolio?", "skip")])
def test_a_custom_yes_no_answer_takes_its_saved_question_word_for_word_in_order(
        tmp_path, label, action):
    bank = standard_bank() + [custom("prefer", _PREFER, "Yes", type="yes_no"),
                              custom("portfolio", _PORTFOLIO, "Yes", type="yes_no")]
    cat = apply_facts.build(tmp_path, answers=bank, today=date(2026, 9, 21))
    key = "answer_prefer" if "prefer" in label else "answer_portfolio"
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: (key, 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    assert p.fields[0].action == action
    assert p.fields[0].option == ("Yes" if action == "select" else None)


# --- cycle 18 SP6c round 6: authorization words, more neutral tails, "please enter", now -------

# (fact, label, help, fit): each pass form refused on 12d81cf; each refusal kept
_ROUND6 = [
    # 1: "authorization", "authorisation" and "allowed" name work authorization
    ("work_authorized", "Do you have work authorisation in the United States?", "", "own"),
    ("work_authorized", "Are you allowed to work in the United States?", "", "own"),
    ("work_authorized", "Work authorization", "Do you have work authorization in the US?", "own"),
    # still refused: without sponsorship, unrestricted, another country
    ("work_authorized", "Do you have work authorization in the US without sponsorship?", "",
     "other"),
    ("work_authorized", "Do you currently have unrestricted work authorization?", "", "other"),
    ("work_authorized", "Do you have work authorization in Canada?", "", "other"),
    ("work_authorized", "Are you allowed to work in Canada?", "", "other"),
    # the new topic words open no other fact
    ("requires_sponsorship", "Do you have work authorization in the US?", "", "other"),
    ("authorized_without_sponsorship", "Do you have work authorization in the US?", "", "other"),
    # 2: the new neutral tails, each compared whole
    ("requires_sponsorship", "Will you require visa sponsorship? If yes, please describe.", "",
     "own"),
    ("requires_sponsorship", "Will you require visa sponsorship? If so, please explain:", "",
     "own"),
    ("requires_sponsorship", "Will you require visa sponsorship?", "If yes, please explain.",
     "own"),
    ("requires_sponsorship", "Will you require visa sponsorship? If yes, please explain which "
                             "visa.", "", "other"),
    # 3: "please enter" leads the total
    ("years_experience", "Enter your total years of professional experience", "", "own"),
    ("years_experience", "Years of experience (please enter a number)", "", "own"),
    ("years_experience", "Please enter your years of Python experience", "", "other"),
    ("years_experience", "Years of experience", "Please enter a number of years in Python.",
     "other"),
    # 4: "currently" beside a word of ability or acceptance asks about now
    ("onsite_ok", "Are you currently willing to work in the office?", "", "own"),
    ("onsite_ok", "Are you currently open to working on-site?", "", "own"),
    ("onsite_ok", "Are you currently comfortable working on-site?", "", "own"),
    # the present job, still refused
    ("onsite_ok", "Are you currently working in an office?", "", "other"),
    ("onsite_ok", "Do you currently work on-site?", "", "other"),
    ("onsite_ok", "Are you currently working on-site?", "", "other"),
]


@pytest.mark.parametrize("key, label, help_text, fit", _ROUND6)
def test_authorization_words_new_tails_please_enter_and_an_ability_now(key, label, help_text,
                                                                       fit):
    assert apply_judge.question_fit(key, label, help_text) == fit
    assert apply_judge.asks_own_question(key, label, help_text) is (fit == "own")


@pytest.mark.parametrize("label, help_text, words", [
    ("Do you require sponsorship? (If yes, please explain.)", "", ("require", "sponsorship")),
    ("Do you require sponsorship? If yes, please describe", "", ("require", "sponsorship")),
    ("Do you require sponsorship? If so, please explain:", "", ("require", "sponsorship")),
    ("Years of experience", "Please enter a number.", ("years", "experience")),
    ("Years of experience", "Enter a number", ("years", "experience")),
    ("Years of experience. Please enter a number. *", "", ("years", "experience")),
    # compared whole: a longer sentence keeps its words
    ("Do you require sponsorship? If yes, please describe your visa.", "",
     ("require", "sponsorship", "if", "yes", "please", "describe", "visa")),
    ("Years of experience", "Please enter a number of years",
     ("years", "experience", "please", "enter", "number", "years")),
])
def test_the_new_neutral_tails_are_dropped_whole(label, help_text, words):
    assert apply_facts.question_words(label, help_text) == words


@pytest.mark.parametrize("label", ["Do you have work authorization in the US?",
                                   "Are you allowed to work in the US?"])
@pytest.mark.parametrize("answers", [{}, _SPONSOR])
def test_work_authorization_or_allowed_to_work_takes_the_stored_yes(tmp_path, label, answers):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("work_authorized", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("select", "Yes")
    assert p.park_reason == ""


@pytest.mark.parametrize("answers, option", [({}, "No"), (_SPONSOR, "Yes")])
def test_sponsorship_with_if_yes_please_explain_takes_the_stored_answer(tmp_path, answers,
                                                                        option):
    label = "Do you require sponsorship to work in the US? (If yes, please explain.)"
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("requires_sponsorship", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("select", option)
    assert p.park_reason == ""


@pytest.mark.parametrize("label, help_text, action", [
    ("Years of experience", "Please enter a number.", "fill"),
    ("Please enter your total years of experience", "", "fill"),
    ("Please enter your years of Python experience", "", "skip"),
])
def test_a_years_box_with_please_enter_takes_the_total(tmp_path, label, help_text, action):
    cat = _profile_catalog(tmp_path)
    digest = _field_with(label, help_text, type_="number", options=())
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("years_experience", 1.0)}))
    assert (p.fields[0].action, p.fields[0].value) == (action, "2" if action == "fill" else "")


@pytest.mark.parametrize("value", ["Yes", "No"])
def test_currently_able_to_work_on_site_takes_the_on_site_answer(tmp_path, value):
    cat = _profile_catalog(tmp_path, onsite_ok=value)
    digest = _field_with("Are you currently able to work on-site?")
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("onsite_ok", 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.fact_key) == ("select", value, "onsite_ok")
    assert p.park_reason == ""


# --- cycle 18 SP6c round 7, rule 1: a custom answer compares literal words --------------------

_H1B_HOLD = "Do you currently hold an H-1B visa?"
_F1_HOLD = "Do you currently hold an F-1 visa?"
_H1B_PETITIONS = "How many H-1B petitions have you had?"
_TWO_DAYS = "Are you able to work on-site 2 days a week?"

# (saved question, another field's label): a visa type, a number, a time
# phrase or a place phrase is read as written, never as its set phrase
_LITERAL_DIFFERENT = [
    (_H1B_HOLD, _F1_HOLD),
    ("Are you currently on an F-1 visa?", "Are you currently on an H-1B visa?"),
    ("Do you have OPT?", "Do you have CPT?"),
    ("Do you have an EAD?", "Do you have an OPT?"),
    ("Are you currently in H-1B status?", "Are you currently in F-1 status?"),
    ("Have you been counted against the H-1B cap?", "Have you been counted against the H-4 cap?"),
    ("Do you currently hold an E-3 visa?", "Do you currently hold an O-1 visa?"),
    ("Do you hold a visa (e.g. TN)?", "Do you hold a visa (e.g. H-1B)?"),
    (_H1B_PETITIONS, "How many F-1 petitions have you had?"),
    (_TWO_DAYS, "Are you able to work on-site 5 days a week?"),
    ("Can you come into the office 1 day a week?", "Can you come into the office 4 days a week?"),
    ("Have you worked for a competitor at any time?",
     "Have you worked for a competitor now or in the future?"),
    ("Have you worked for a competitor at any time?", "Have you worked for a competitor now or "
                                                      "later?"),
    ("Can you attend an interview in person?", "Can you attend an interview in the office?"),
    ("Have you lived in the United States?", "Have you lived in America?"),
    ("Is your visa an H-1B?", "Is your visa an L-1?"),
]


@pytest.mark.parametrize("saved, label", _LITERAL_DIFFERENT)
def test_same_question_reads_visa_types_numbers_and_set_phrases_as_written(saved, label):
    assert apply_facts.same_question(label, "", saved) is False
    assert apply_facts.same_question(saved, "", saved) is True


@pytest.mark.parametrize("label, saved", [
    # case, Unicode punctuation, a contraction and a fixed trailing sentence
    ("DO YOU CURRENTLY HOLD AN H-1B VISA?", _H1B_HOLD),
    (f"Do you currently hold an H{_NBHYPHEN}1B visa?", _H1B_HOLD),
    (f"Do you currently hold an H-1B{_NBSP}visa?", _H1B_HOLD),
    ("Do you currently hold an H-1B visa? Please select one.", _H1B_HOLD),
    (f"Don{_RSQUO}t you hold an H-1B visa?", "Do not you hold an H-1B visa?"),
    (_TWO_DAYS, _TWO_DAYS),
])
def test_same_question_still_reads_case_punctuation_contractions_and_tails_as_one(label, saved):
    assert apply_facts.same_question(label, "", saved) is True


@pytest.mark.parametrize("key, saved, value", [("h1b", _H1B_HOLD, "No"),
                                               ("days", _TWO_DAYS, "Yes")])
@pytest.mark.parametrize("other", [False, True])
def test_a_custom_yes_no_answer_settles_only_its_own_visa_type_or_days(tmp_path, key, saved,
                                                                      value, other):
    label = saved
    if other:
        label = _F1_HOLD if key == "h1b" else "Are you able to work on-site 5 days a week?"
    bank = standard_bank(requires_sponsorship="Yes") + [
        custom(key, saved, value, type="yes_no")]
    cat = apply_facts.build(tmp_path, answers=bank, today=date(2026, 9, 21))
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: (f"answer_{key}", 1.0)},
                                                    options={0: (value, 1.0)}))
    pf = p.fields[0]
    if other:
        assert (pf.action, pf.option, pf.fact_key) == ("skip", None, None)
        assert p.park_reason == f"required field without an answer: {label}"
    else:
        assert (pf.action, pf.option) == ("select", value)
        assert p.park_reason == ""


@pytest.mark.parametrize("label, action", [(_H1B_PETITIONS, "fill"),
                                           ("How many F-1 petitions have you had?", "skip")])
def test_a_custom_number_answer_settles_only_its_own_visa_type(tmp_path, label, action):
    bank = standard_bank() + [custom("petitions", _H1B_PETITIONS, "1", type="number")]
    cat = apply_facts.build(tmp_path, answers=bank, today=date(2026, 9, 21))
    digest = _field_with(label, type_="number", options=())
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("answer_petitions", 1.0)}))
    assert (p.fields[0].action, p.fields[0].value) == (action, "1" if action == "fill" else "")


# --- cycle 18 SP6c round 7, rules 2 to 9: anchors, the company name, years, days, tails -------

_LEVER = ('Will you now, or in the future, require the Company to commence ("sponsor") an '
          'immigration case in order to employ you (for example, H-1B or other '
          'employment-based immigration case)? This is sometimes called "sponsorship" for an '
          '"employment-based visa status."')
_LDQUO, _RDQUO = chr(0x201C), chr(0x201D)
_LEVER_CURLY = (f"Will you now, or in the future, require the Company to commence ({_LDQUO}"
                f"sponsor{_RDQUO}) an immigration case in order to employ you (for example, H-1B "
                f"or other employment-based immigration case)? This is sometimes called "
                f"{_LDQUO}sponsorship{_RDQUO} for an {_LDQUO}employment-based visa "
                f"status.{_RDQUO}")
_COMPANY = "Example Co"
_COMPANY_AUTH = "Are you legally authorized to work in the United States for Example Co?"
_COMPANY_SPONSOR = ("Will you now or in the future require Example Co to sponsor you for an "
                    "employment visa?")
_CURRENT_OR_FUTURE = ("Are you authorized to work in the US without the need for current or "
                      "future sponsorship?")
_NOT_IN_FUTURE = ("Are you legally authorized to work in the United States and will not "
                  "require sponsorship in the future?")

# (fact, label): each settled a value on 208241d; each asks another question
_ROUND7_OTHER = [
    # rule 2: a status word needs an authorization word in its own sentence,
    # and "legally" is none
    ("work_authorized", "Are you currently legally employed in the United States?"),
    ("work_authorized", "Are you legally employed in the US?"),
    ("work_authorized", "Are you legally working in the US?"),
    ("work_authorized", "Are you currently legally working in the US?"),
    ("work_authorized", "Are you legally employed?"),
    ("work_authorized", "Do you legally have work in the US?"),
    ("work_authorized", "Are you employed? Do you have work authorization?"),
    ("authorized_without_sponsorship",
     "Are you currently employed in the US without sponsorship?"),
    ("authorized_without_sponsorship", "Do you currently work in the US without sponsorship?"),
    ("authorized_without_sponsorship", "Are you currently working without sponsorship?"),
    ("authorized_without_sponsorship", "Are you employed in the US without sponsorship?"),
    ("authorized_without_sponsorship", "Do you work in the US without sponsorship?"),
    ("authorized_without_sponsorship", "Are you working in the US without sponsorship?"),
    # "without restrictions" with neither the US nor a sponsorship word: a
    # duty or a schedule
    ("authorized_without_sponsorship", "Are you able to work without restrictions?"),
    ("authorized_without_sponsorship", "Can you work without restrictions?"),
    ("authorized_without_sponsorship", "Can you work unrestricted?"),
    # rule 5: "able" and "allowed" with no US ask when the candidate can start
    ("work_authorized", "Are you able to work now?"),
    ("work_authorized", "Are you able to work at this time?"),
    ("work_authorized", "Are you able to take up work now?"),
    ("work_authorized", "Are you allowed to work now?"),
    ("work_authorized", "Are you currently allowed to work?"),
    # rule 3: a plan or a status is no willingness to relocate
    ("willing_to_relocate", "Will you be relocating for this position?"),
    ("willing_to_relocate", "Will you be relocating for this role?"),
    ("willing_to_relocate", "Will you be moving for this job?"),
    ("willing_to_relocate", "Are you relocating to the job location?"),
    ("willing_to_relocate", "Would this position be a relocation for you?"),
    ("willing_to_relocate", "Would this role be a relocation?"),
    ("willing_to_relocate", "Are you moving to this location?"),
    ("willing_to_relocate", "Are you currently relocating?"),
    ("willing_to_relocate", "Are you moving?"),
    # a change of job
    ("willing_to_relocate", "Are you open to a job move?"),
    ("willing_to_relocate", "Are you willing to move to this role?"),
    ("willing_to_relocate", "Would you move to this position?"),
    # the present job or the job's terms are no willingness to work on-site
    ("onsite_ok", "Do you work in an office?"),
    ("onsite_ok", "Do you work on-site?"),
    ("onsite_ok", "Are you working on-site?"),
    ("onsite_ok", "Are you working in an office?"),
    ("onsite_ok", "Do you work at an office?"),
    ("onsite_ok", "Do you work from an office?"),
    ("onsite_ok", "Are you working from our office?"),
    ("onsite_ok", "Do you work in person?"),
    ("onsite_ok", "Do you come into the office?"),
    ("onsite_ok", "Do you report to an office?"),
    ("onsite_ok", "Are you on-site?"),
    ("onsite_ok", "Are you in the office?"),
    ("onsite_ok", "Is your role on-site?"),
    ("onsite_ok", "Is your position on-site?"),
    ("onsite_ok", "Is your work on-site?"),
    ("onsite_ok", "Is that role on-site?"),
    ("onsite_ok", "Is this role on-site?"),
    ("onsite_ok", "Is your role hybrid?"),
    # "currently" with no anchor between it and the work word, or an anchor
    # in another sentence
    ("onsite_ok", "Do you currently work in an open office?"),
    ("onsite_ok", "Do you currently work on-site in an open office?"),
    ("onsite_ok", "Do you currently work in an open office with this schedule?"),
    ("onsite_ok", "Are you currently working in an office that is open?"),
    ("onsite_ok", "Are you currently working in an office open 5 days a week?"),
    ("onsite_ok", "Are you currently working in an office? Are you comfortable with that?"),
    ("onsite_ok", "Are you currently working in an office? Would you be comfortable with this?"),
    ("onsite_ok", "Are you currently working on-site? Is that comfortable?"),
    ("onsite_ok", "Are you currently working on-site at an office you are comfortable with?"),
    ("onsite_ok", "Currently working on-site / able to work on-site"),
    ("onsite_ok", "Are you able to work on-site? Are you currently working on-site?"),
    ("onsite_ok", "Are you willing to work on-site? Do you currently work on-site?"),
    ("onsite_ok", "Currently working in an office: able to come into the office?"),
    ("onsite_ok", "Do you currently work from an office you are willing to report to?"),
    # remote only: the present job or the job's terms
    ("remote_only", "Do you only work remotely?"),
    ("remote_only", "Do you currently work remotely only?"),
    ("remote_only", "Do you work fully remote only?"),
    ("remote_only", "Does the position require remote work?"),
    ("remote_only", "Does your work require you to be remote only?"),
    # rule 6: a number, or one job, in a years question
    ("years_experience", "How many years of experience do you have in 1 role?"),
    ("years_experience", "How many years of experience do you have in 2 or more roles?"),
    ("years_experience", "How many years of experience do you have in 2024?"),
    ("years_experience", "Years of experience in 10 years"),
    ("years_experience", "How many years of experience at this job?"),
    ("years_experience", "Years of experience in this position"),
    ("years_experience", "Years of experience at this position"),
    ("years_experience", "Please enter years of experience at this job"),
    ("years_experience", "Please enter years of experience in this role"),
    ("years_experience",
     "Please enter the number of years of experience you have in this position"),
    ("years_experience", "Please enter your years of experience in 1 role"),
    ("years_experience", "Please enter 2 years of experience"),
    ("years_experience", "Enter how many years of experience you have in 2024"),
    # rule 7: six or seven days a week
    ("onsite_ok", "Are you able to work in the office 7 days a week?"),
    ("onsite_ok", "Are you willing to work on-site six days a week?"),
    # rule 8: an employer's sponsorship, or continuing one
    ("requires_sponsorship", "Does your employer require visa sponsorship?"),
    ("requires_sponsorship", "Will you require the company to continue your visa sponsorship?"),
    ("requires_sponsorship", "Does your company require visa sponsorship?"),
]


@pytest.mark.parametrize("key, label", _ROUND7_OTHER)
def test_a_status_a_plan_the_jobs_terms_a_number_or_one_job_asks_another_question(key, label):
    assert apply_judge.question_fit(key, label) == "other"
    assert apply_judge.answers_question(key, "Yes", label) is False
    assert apply_judge.answers_question(key, "No", label) is False


# (fact, label, help, company, fit): each refused on 208241d
_ROUND7_OWN = [
    # rule 9: Lever's own sponsorship question and its closing sentence
    ("requires_sponsorship", _LEVER, "", "", "own"),
    ("requires_sponsorship", _LEVER_CURLY, "", "", "own"),
    # the fixed trailing sentences
    ("work_authorized", "Are you legally authorized to work in the United States? (Y/N)", "", "",
     "own"),
    ("work_authorized", "Are you legally authorized to work in the United States? Y/N", "", "",
     "own"),
    ("requires_sponsorship", "Will you require visa sponsorship?", "Please select an option", "",
     "own"),
    ("requires_sponsorship", "Will you require visa sponsorship? Choose one", "", "", "own"),
    ("requires_sponsorship", "Will you require visa sponsorship? Required field", "", "", "own"),
    ("requires_sponsorship", "Will you require visa sponsorship? Please select Yes or No.", "",
     "", "own"),
    ("years_experience", "How many years of experience do you have?",
     "Please enter a whole number.", "", "own"),
    ("years_experience", "Years of experience (numbers only)", "", "", "own"),
    # the job's company name reads as the company
    ("work_authorized", _COMPANY_AUTH, "", _COMPANY, "own"),
    ("requires_sponsorship", _COMPANY_SPONSOR, "", _COMPANY, "own"),
    ("onsite_ok", "Are you willing to work on-site at Example Co's office?", "", _COMPANY, "own"),
    # no sponsorship now or later, in two more wordings
    ("authorized_without_sponsorship", _CURRENT_OR_FUTURE, "", "", "own"),
    ("authorized_without_sponsorship", _NOT_IN_FUTURE, "", "", "own"),
]


@pytest.mark.parametrize("key, label, help_text, company, fit", _ROUND7_OWN)
def test_lever_the_new_tails_the_company_name_and_no_sponsorship_later_ask_the_facts_own(
        key, label, help_text, company, fit):
    kwargs = {"company": company} if company else {}
    assert apply_judge.question_fit(key, label, help_text, **kwargs) == fit
    assert apply_judge.asks_own_question(key, label, help_text, **kwargs) is True


# (fact, label, company, fit): read the same on 208241d and now
_ROUND7_KEPT = [
    ("work_authorized", "Are you authorized for employment in the US?", "", "own"),
    ("work_authorized", "Are you able to work in the US?", "", "own"),
    ("work_authorized", "Are you allowed to work in the United States?", "", "own"),
    # no country at all: left as it is (M2)
    ("work_authorized", "Are you legally authorized to work?", "", "own"),
    ("authorized_without_sponsorship", "Are you able to work in the US without restrictions?",
     "", "own"),
    ("authorized_without_sponsorship", "Are you able to work without sponsorship?", "", "own"),
    ("authorized_without_sponsorship",
     "Can you work in the United States without the need for sponsorship?", "", "own"),
    ("willing_to_relocate", "Would you relocate for this position?", "", "own"),
    ("willing_to_relocate", "Are you open to relocating for this role?", "", "own"),
    ("willing_to_relocate", "Willing to relocate", "", "own"),
    ("onsite_ok", "This role requires working in the office. Are you comfortable with this?", "",
     "own"),
    ("onsite_ok", "Are you currently able to work on-site?", "", "own"),
    ("onsite_ok", "Are you currently open to working on-site?", "", "own"),
    ("onsite_ok", "Are you currently comfortable working in an office?", "", "own"),
    ("onsite_ok", "Are you able to work in the office 5 days a week?", "", "narrower"),
    ("onsite_ok", "Can you work onsite?", "", "own"),
    ("remote_only", "Do you require a fully remote role?", "", "own"),
    ("years_experience", "How many years of experience relevant to this role?", "", "own"),
    ("years_experience", "Years of experience for this position", "", "own"),
    # the company name read only when the plan has it
    ("work_authorized", _COMPANY_AUTH, "", "other"),
    ("requires_sponsorship", _COMPANY_SPONSOR, "", "other"),
]


@pytest.mark.parametrize("key, label, company, fit", _ROUND7_KEPT)
def test_ability_willingness_and_the_total_still_ask_the_facts_own(key, label, company, fit):
    kwargs = {"company": company} if company else {}
    assert apply_judge.question_fit(key, label, **kwargs) == fit


@pytest.mark.parametrize("answers", [{}, _SPONSOR])
@pytest.mark.parametrize("label, key", [
    ("Are you currently legally employed in the United States?", "work_authorized"),
    ("Are you able to work now?", "work_authorized"),
    ("Are you employed in the US without sponsorship?", "authorized_without_sponsorship"),
    ("Will you be relocating for this position?", "willing_to_relocate"),
    ("Do you work in an office?", "onsite_ok"),
    ("Does the position require remote work?", "remote_only"),
    ("Does your employer require visa sponsorship?", "requires_sponsorship"),
])
def test_a_status_or_plan_question_gets_no_value_and_parks(tmp_path, answers, label, key):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: (key, 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.fact_key) == ("skip", None, None)
    assert p.park_reason == f"required field without an answer: {label}"


@pytest.mark.parametrize("label, help_text, action", [
    ("How many years of experience at this job?", "", "skip"),
    ("Please enter 2 years of experience", "", "skip"),
    ("How many years of experience do you have?", "Please enter a whole number.", "fill"),
    ("Years of experience (numbers only)", "", "fill"),
])
def test_a_years_box_with_one_job_or_a_number_parks_and_a_number_instruction_fills(
        tmp_path, label, help_text, action):
    cat = _profile_catalog(tmp_path)
    digest = _field_with(label, help_text, type_="number", options=())
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("years_experience", 1.0)}))
    assert (p.fields[0].action, p.fields[0].value) == (action, "2" if action == "fill" else "")


@pytest.mark.parametrize("label", [_LEVER, _LEVER_CURLY])
@pytest.mark.parametrize("answers, option", [({}, "No"), (_SPONSOR, "Yes")])
def test_levers_sponsorship_question_takes_the_stored_answer(tmp_path, label, answers, option):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("requires_sponsorship", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("select", option)
    assert p.park_reason == ""


@pytest.mark.parametrize("label", [_CURRENT_OR_FUTURE, _NOT_IN_FUTURE])
@pytest.mark.parametrize("answers, option", [({}, "Yes"), (_SPONSOR, "No")])
def test_no_sponsorship_current_or_future_takes_the_derived_answer(tmp_path, label, answers,
                                                                  option):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(
        digest, {0: ("authorized_without_sponsorship", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("select", option)
    assert p.park_reason == ""


@pytest.mark.parametrize("label, key, answers, option", [
    (_COMPANY_AUTH, "work_authorized", {}, "Yes"),
    (_COMPANY_SPONSOR, "requires_sponsorship", {}, "No"),
    (_COMPANY_SPONSOR, "requires_sponsorship", _SPONSOR, "Yes"),
])
def test_the_jobs_company_name_in_the_question_reads_as_the_company(tmp_path, label, key,
                                                                    answers, option):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label)
    mapped = _page_answers(digest, {0: (key, 1.0)})
    p = apply_judge.plan(digest, cat, mapped, company=_COMPANY)
    assert (p.fields[0].action, p.fields[0].option) == ("select", option)
    assert p.park_reason == ""
    p = apply_judge.plan(digest, cat, mapped)
    assert (p.fields[0].action, p.fields[0].option, p.fields[0].fact_key) == ("skip", None, None)


def test_the_company_name_reaches_the_pick_question_and_the_second_look(tmp_path):
    cat = _profile_catalog(tmp_path)
    digest = _field_with(_COMPANY_AUTH, options=("Yes, I am authorized", "No, I am not"))
    mapped = _page_answers(digest, {0: ("work_authorized", 1.0)})
    p = apply_judge.plan(digest, cat, mapped, company=_COMPANY)
    assert p.fields[0].fact_key == "work_authorized"
    _, questions = apply_judge.option_questions(digest, p, catalog=cat, company=_COMPANY)
    assert list(questions) == ["field_0_pick"]
    assert apply_judge.reask_targets(digest, cat, {}, p, what="pick", company=_COMPANY) == [0]
    # without the name, neither is asked
    assert apply_judge.option_questions(digest, p, catalog=cat)[1] == {}
    assert apply_judge.reask_targets(digest, cat, {}, p, what="pick") == []


@pytest.mark.parametrize("label", ["Are you able to work in the office 7 days a week?",
                                   "Are you willing to work on-site six days a week?",
                                   "Are you willing to work on-site 6 days per week?"])
def test_six_or_seven_days_a_week_is_no_set_phrase(label):
    assert "DAYSWEEK" not in apply_facts.question_tokens(label)


# --- cycle 18 SP6c round 7, rule 4: a label with no verb settles only a plain Yes / No --------

_STATUS_OPTIONS = ("U.S. Citizen", "Permanent Resident", "H-1B", "F-1 OPT", "TN", "Other")
_NOUN_LABELS = ["Work authorization", "US Work Authorization", "Employment authorization",
                "Work authorisation", "Work Authorization (Required)"]


@pytest.mark.parametrize("label, noun", [
    *[(label, True) for label in _NOUN_LABELS],
    ("Sponsorship", True), ("Relocation", True), ("Right to work in the US", True),
    ("Visa sponsorship needed *", True), ("Work authorization status", True),
    ("Are you authorized to work in the US?", False), ("Authorized to work in the US?", False),
    ("Do you need sponsorship?", False), ("Willing to relocate", False),
    ("Will you require visa sponsorship?", False), ("", False),
])
def test_noun_phrase_reads_a_label_with_no_verb(label, noun):
    assert apply_facts.noun_phrase(label) is noun


@pytest.mark.parametrize("answers", [{}, _SPONSOR])
@pytest.mark.parametrize("label", _NOUN_LABELS)
@pytest.mark.parametrize("type_", ["select", "radio"])
def test_a_status_list_under_a_noun_phrase_label_gets_no_yes_no_pick(tmp_path, answers, label,
                                                                    type_):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label, type_=type_, options=_STATUS_OPTIONS)
    mapped = _page_answers(digest, {0: ("work_authorized", 1.0)})
    p = apply_judge.plan(digest, cat, mapped)
    # the pick is never asked of the judge ...
    assert apply_judge.option_questions(digest, p, catalog=cat)[1] == {}
    assert apply_judge.reask_targets(digest, cat, mapped, p, what="pick") == []
    # ... and a pick that came back anyway is not used
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("work_authorized", 1.0)},
                                                    options={0: ("U.S. Citizen", 1.0)}))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.fact_key) == ("skip", None, None)
    assert p.park_reason == f"required field without an answer: {label}"


def test_a_text_box_under_a_noun_phrase_label_gets_no_yes_no_value(tmp_path):
    cat = _profile_catalog(tmp_path)
    digest = _field_with("Work authorization", type_="text", options=())
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("work_authorized", 1.0)}))
    assert (p.fields[0].action, p.fields[0].value, p.fields[0].fact_key) == ("skip", "", None)


def test_a_custom_yes_no_answer_under_a_noun_phrase_settles_only_a_plain_yes_no(tmp_path):
    bank = standard_bank() + [custom("clearance", "Security clearance", "Yes", type="yes_no")]
    cat = apply_facts.build(tmp_path, answers=bank, today=date(2026, 9, 21))
    digest = _field_with("Security clearance", type_="select",
                         options=("Secret", "Top Secret", "None"))
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("answer_clearance", 1.0)},
                                                    options={0: ("Top Secret", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("skip", None)
    digest = _field_with("Security clearance")
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("answer_clearance", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("select", "Yes")


@pytest.mark.parametrize("answers", [{}, _SPONSOR])
@pytest.mark.parametrize("label", _NOUN_LABELS)
def test_a_plain_yes_no_under_a_noun_phrase_label_still_settles_in_code(tmp_path, answers,
                                                                       label):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label, type_="select", options=("Yes", "No"))
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("work_authorized", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("select", "Yes")
    assert p.park_reason == ""


def test_a_status_list_under_a_question_label_still_goes_to_the_judge(tmp_path):
    cat = _profile_catalog(tmp_path)
    label = "Are you legally authorized to work in the United States?"
    digest = _field_with(label, type_="select", options=_STATUS_OPTIONS)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("work_authorized", 1.0)}))
    assert list(apply_judge.option_questions(digest, p, catalog=cat)[1]) == ["field_0_pick"]


# --- cycle 18 final review: C1, I1, I2, M1 and M5 of the own-question gate --------------------

_TRAVEL = "Are you willing to travel?"
_TRAVEL_FAR = "Are you willing to travel up to 75% of the time internationally?"
_F1_PETITIONS = "How many F-1 petitions have you had?"


@pytest.mark.parametrize("eid, saved, value, asked, type_, options", [
    # the reviewer's repros: a Text custom answer holding a yes / no
    ("hold_h1b", _H1B_HOLD, "No", _F1_HOLD, "select", ("Yes", "No")),
    ("hold_h1b", _H1B_HOLD, "No", _F1_HOLD, "text", ()),
    ("travel", _TRAVEL, "Yes", _TRAVEL_FAR, "radio", ("Yes", "No")),
    ("travel", _TRAVEL, "y", _TRAVEL_FAR, "radio", ("Yes", "No")),
    # and a plain number
    ("petitions", _H1B_PETITIONS, "1", _F1_PETITIONS, "text", ()),
])
@pytest.mark.parametrize("same", [False, True])
def test_a_text_custom_yes_no_or_number_answers_only_its_own_saved_question(
        tmp_path, eid, saved, value, asked, type_, options, same):
    # final review C1: every migrated v1 answer and every answer added with
    # the default type is Text, and the gate read only the typed ones
    bank = standard_bank() + [custom(eid, saved, value)]
    cat = apply_facts.build(tmp_path, answers=bank, today=date(2026, 9, 21))
    key = f"answer_{eid}"
    assert cat.custom_type(key) == "text" and cat.value(key) == value
    label = saved if same else asked
    digest = _field_with(label, type_=type_, options=options)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: (key, 0.9)},
                                                    options={0: (value, 1.0)}))
    pf = p.fields[0]
    if not same:
        assert (pf.action, pf.option, pf.value, pf.fact_key) == ("skip", None, "", None)
        assert p.park_reason == f"required field without an answer: {label}"
        assert apply_judge.option_questions(digest, p, catalog=cat)[1] == {}
        return
    assert p.park_reason == ""
    if options:
        assert (pf.action, pf.option) == ("select", "Yes" if value == "y" else value)
    else:
        assert (pf.action, pf.value) == ("fill", value)


# final re-review N1: a Text custom whose value opens with a yes or a no is a
# yes / no answer too, and settles only its saved question
_CANADA = "Are you legally authorized to work in Canada?"
_US_AUTH = "Are you legally authorized to work in the United States?"


@pytest.mark.parametrize("eid,saved,value,asked,type_,options,pick", [
    ("canada", _CANADA, "No, I would need a work permit", _US_AUTH, "select", ("Yes", "No"), "No"),
    ("hold_h1b", _H1B_HOLD, "Yes, I hold an H-1B visa", _F1_HOLD, "select", ("Yes", "No"),
     "Yes"),
    ("hold_h1b", _H1B_HOLD, "Yes, I hold an H-1B visa", _F1_HOLD, "text", (), None),
])
def test_a_text_custom_opening_with_yes_or_no_answers_only_its_own_saved_question(
        tmp_path, eid, saved, value, asked, type_, options, pick):
    bank = standard_bank() + [custom(eid, saved, value)]
    cat = apply_facts.build(tmp_path, answers=bank, today=date(2026, 9, 21))
    key = f"answer_{eid}"
    assert cat.custom_type(key) == "text"
    digest = _field_with(asked, type_=type_, options=options)
    picks = {0: (pick, 0.9)} if pick else None
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: (key, 0.8)}, options=picks))
    pf = p.fields[0]
    assert (pf.action, pf.option, pf.value, pf.fact_key) == ("skip", None, "", None)
    assert p.park_reason == f"required field without an answer: {asked}"
    # its own question, word for word, still takes the sentence
    own = _field_with(saved, type_="text", options=())
    p = apply_judge.plan(own, cat, _page_answers(own, {0: (key, 0.8)}))
    assert (p.fields[0].action, p.fields[0].value) == ("fill", value)


def test_a_prose_text_custom_answer_still_fills_its_mapped_field(tmp_path):
    bank = standard_bank() + [custom("heard", "How did you hear about us?", "LinkedIn")]
    cat = apply_facts.build(tmp_path, answers=bank, today=date(2026, 9, 21))
    digest = _field_with("How did you first learn about this position?", type_="text",
                         options=())
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("answer_heard", 0.9)}))
    assert (p.fields[0].action, p.fields[0].value) == ("fill", "LinkedIn")
    assert p.park_reason == ""


@pytest.mark.parametrize("type_", ["yes_no", "text"])
def test_a_text_custom_yes_under_a_noun_phrase_settles_only_a_plain_yes_no(tmp_path, type_):
    bank = standard_bank() + [custom("clearance", "Security clearance", "Yes", type=type_)]
    cat = apply_facts.build(tmp_path, answers=bank, today=date(2026, 9, 21))
    assert cat.yes_no("answer_clearance")
    digest = _field_with("Security clearance", type_="select",
                         options=("Secret", "Top Secret", "None"))
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("answer_clearance", 1.0)},
                                                    options={0: ("Top Secret", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("skip", None)
    assert apply_judge.option_questions(digest, p, catalog=cat)[1] == {}


# final review I1: "can you" and "would you" ask whether the candidate
# accepts a remote role; the saved fact says the candidate wants remote only
_REMOTE_ACCEPT = ["Can you work remotely only?", "Can you work remote only?",
                  "Would you work remote only?", "Would you work fully remotely only?"]


@pytest.mark.parametrize("answers", [{}, _SPONSOR, {"onsite_ok": "No"}])
@pytest.mark.parametrize("label", _REMOTE_ACCEPT)
def test_can_you_work_remote_only_gets_no_value_from_remote_only(tmp_path, answers, label):
    cat = _profile_catalog(tmp_path, **answers)
    digest = _field_with(label)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("remote_only", 1.0)},
                                                    options={0: ("No", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option, p.fields[0].fact_key) == ("skip", None, None)
    assert p.park_reason == f"required field without an answer: {label}"


@pytest.mark.parametrize("label", ["Are you looking for a fully remote position only?",
                                   "Are you seeking remote-only roles?",
                                   "Do you require a fully remote role?"])
def test_the_candidates_own_remote_search_still_settles_remote_only(label):
    assert apply_judge.question_fit("remote_only", label) == "own"


# final review I2: a part-week office schedule is the hybrid question, so a
# No to on-site work says nothing about it
_OFFICE_DAYS = ["Are you willing to work in the office 2 days a week?",
                "Are you able to come into the office 3 days a week?",
                "Can you work in the office 1 day a week?",
                "Would you come into the office 3 days a week?",
                "Can you work from our office 3 days a week?"]


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("label", _OFFICE_DAYS)
def test_a_days_a_week_office_question_settles_only_a_yes_to_on_site_work(
        tmp_path, label, required):
    assert apply_judge.question_fit("onsite_ok", label) == "narrower"
    digest = _field_with(label, required=required)
    no = _profile_catalog(tmp_path, onsite_ok="No")
    p = apply_judge.plan(digest, no, _page_answers(digest, {0: ("onsite_ok", 1.0)},
                                                   options={0: ("No", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option, p.fields[0].fact_key) == ("skip", None, None)
    assert p.park_reason == (f"required field without an answer: {label}" if required else "")
    yes = _profile_catalog(tmp_path, onsite_ok="Yes")
    p = apply_judge.plan(digest, yes, _page_answers(digest, {0: ("onsite_ok", 1.0)},
                                                    options={0: ("Yes", 1.0)}))
    assert (p.fields[0].action, p.fields[0].option) == ("select", "Yes")
    assert p.park_reason == ""


# final review M1: a one-word company named for a place reads as the place
@pytest.mark.parametrize("company, key, label", [
    ("Canada", "work_authorized", "Are you legally authorized to work in Canada?"),
    ("Texas", "work_authorized", "Are you legally authorized to work in Texas?"),
    ("New York", "onsite_ok", "Are you willing to work on-site in New York?"),
    # a place the tables do not list, read as one after "in"
    ("Boston", "work_authorized", "Are you legally authorized to work in Boston?"),
])
def test_a_company_named_for_a_place_reads_as_the_place(company, key, label):
    assert apply_judge.question_fit(key, label, company=company) == "other"


@pytest.mark.parametrize("key, label, fit", [
    ("work_authorized", "Are you legally authorized to work in Canada?", "other"),
    ("work_authorized", "Are you legally authorized to work for Acme?", "own"),
    ("onsite_ok", "Are you willing to work on-site at Acme?", "own"),
])
def test_a_company_named_for_no_place_still_reads_as_the_company(key, label, fit):
    assert apply_judge.question_fit(key, label, company="Acme") == fit


# final review M5: status options under a verb label; the yes / no lines say
# Yes to three of them, and the saved authorization statement tells them apart
_STATUS_VERB_OPTIONS = ("Yes, I am a U.S. citizen", "Yes, I am a permanent resident (green card)",
                        "Yes, I have a work visa", "No")
_STATEMENT = "I am a U.S. citizen."


def _pick_request(cat, options):
    digest = _field_with(_AUTH, type_="select", options=options)
    p = apply_judge.plan(digest, cat, _page_answers(digest, {0: ("work_authorized", 1.0)}))
    return apply_judge.option_questions(digest, p, catalog=cat)[1]["field_0_pick"]


def test_status_options_under_a_verb_label_carry_the_authorization_statement(tmp_path):
    cat = _profile_catalog(tmp_path, authorization_statement=_STATEMENT)
    answer = _pick_request(cat, _STATUS_VERB_OPTIONS)["instructions"]["candidate_answer"]
    lines = answer.split("\n")
    assert lines[:-1] == apply_judge.candidate_answer(cat, "work_authorized", "Yes").split("\n")
    assert lines[-1] == f"{apply_judge.STATEMENT_LINE}: {_STATEMENT}"
    # with no statement saved the request is the yes / no story alone
    bare = _profile_catalog(tmp_path, authorization_statement="")
    assert not bare.has(apply_judge.STATEMENT_KEY)
    assert _pick_request(bare, _STATUS_VERB_OPTIONS)["instructions"]["candidate_answer"] == \
        apply_judge.candidate_answer(bare, "work_authorized", "Yes")


@pytest.mark.parametrize("options", [
    ("Yes, I am authorized", "No, I am not authorized"),
    # one Yes option with a status (the Contoso replica, a recorded screening item)
    ("Yes - on a work visa (OPT/H-1B)",
     "U.S. citizen or permanent resident (no visa sponsorship required)", "No"),
])
def test_a_list_the_yes_no_story_reads_keeps_its_request_byte_for_byte(tmp_path, options):
    cat = _profile_catalog(tmp_path, authorization_statement=_STATEMENT)
    old = apply_judge._option_question(
        0, list(options), apply_judge.candidate_answer(cat, "work_authorized", "Yes"), "Yes")
    assert json.dumps(_pick_request(cat, options), sort_keys=True) == \
        json.dumps(old, sort_keys=True)


# --- a held-back answer, asked whether it settles a reworded question (2026-09-26) ------------

_NY_OFFICE = "Are you willing to work in the office in New York?"


def _settle_answer(choice, conf, probs=None):
    probs = probs or {choice: conf}
    return jev.Answer(kind="choice", choice=choice, probabilities=probs, confidence=conf)


def _held_back_plan(tmp_path, label=_NY_OFFICE, key="onsite_ok", bank=None, settle=None,
                    options=("Yes", "No")):
    cat = apply_facts.build(tmp_path, answers=bank or standard_bank(), today=date(2026, 9, 26))
    digest = _field_with(label, options=options)
    answers = _page_answers(digest, {0: (key, 0.95)})
    first = apply_judge.plan(digest, cat, answers)
    state, questions = apply_judge.settle_questions(digest, first, answers, cat)
    if settle is not None:
        answers["field_0_settle"] = settle
    return cat, first, state, questions, apply_judge.plan(digest, cat, answers)


def test_a_reworded_office_question_is_asked_whether_the_saved_answer_settles_it(tmp_path):
    cat, first, state, questions, _ = _held_back_plan(tmp_path)
    assert first.fields[0].action == "skip"            # the gate holds it back
    assert list(questions) == ["field_0_settle"]
    q = questions["field_0_settle"]
    assert list(q["criteria"]) == ["Yes", "No", "not_settled"]
    saved = q["instructions"]["saved_answer"]
    assert saved.splitlines()[0] == f"{apply_facts.DESCRIPTIONS['onsite_ok']}: Yes"
    assert f"{apply_facts.DESCRIPTIONS['willing_to_relocate']}: Yes" in saved
    assert q["instructions"]["saved_question"] == apply_facts.DESCRIPTIONS["onsite_ok"]
    assert "not_settled" in q["instructions"]["question"]
    assert state["fields"][0]["label"] == _NY_OFFICE
    _assert_clean_text(questions)


@pytest.mark.parametrize("settle, filled", [
    (_settle_answer("Yes", 0.97, {"Yes": 0.97, "No": 0.01, "not_settled": 0.02}), "Yes"),
    # under the floor, too close to the next choice, the escape, an option
    # the field does not have: no value, so the required field parks
    (_settle_answer("Yes", 0.80, {"Yes": 0.80, "No": 0.05, "not_settled": 0.15}), None),
    (_settle_answer("Yes", 0.92, {"Yes": 0.92, "No": 0.0, "not_settled": 0.40}), None),
    (_settle_answer("not_settled", 0.95, {"Yes": 0.03, "No": 0.02, "not_settled": 0.95}), None),
    (_settle_answer("Maybe", 0.99, {"Maybe": 0.99}), None),
])
def test_only_a_sure_settle_answer_fills_the_held_back_field(tmp_path, settle, filled):
    *_, p = _held_back_plan(tmp_path, settle=settle)
    pf = p.fields[0]
    if filled:
        assert (pf.action, pf.option, pf.fact_key, pf.value) == ("select", filled, "onsite_ok",
                                                                  "Yes")
        assert p.park_reason == ""
    else:
        assert (pf.action, pf.option, pf.fact_key) == ("skip", None, None)
        assert p.park_reason == f"required field without an answer: {_NY_OFFICE}"


_SURE_YES = _settle_answer("Yes", 0.99, {"Yes": 0.99, "No": 0.0, "not_settled": 0.01})
_SURE_NO = _settle_answer("No", 0.99, {"Yes": 0.0, "No": 0.99, "not_settled": 0.01})
# the Contoso questions (2026-09-27), as the form words them
_CONTOSO_RELOCATE = "We work 5 days on-site in NYC. If you're not local, are you willing to relocate?"
_CONTOSO_SPONSOR = ("Do you require visa sponsorship to work legally in the United States (now or "
                    "in the future)? This includes needing sponsorship for CPT, OPT or other visa "
                    "types to work in the US.")
_CONTOSO_AUTH = ("Are you legally authorized to work in the United States? You are a US citizen, "
                 "already have an employment visa (O1, H1B, etc.), or are specifically covered "
                 "under a TN/H1-B1/E-3.")


@pytest.mark.parametrize("label, key, settle, filled", [
    (_CONTOSO_RELOCATE, "willing_to_relocate", _SURE_YES, "Yes"),
    (_CONTOSO_SPONSOR, "requires_sponsorship", _SURE_NO, "No"),
    (_CONTOSO_AUTH, "work_authorized", _SURE_YES, "Yes"),
    # a US state is no other country
    ("Are you authorized to work in New Mexico?", "work_authorized", _SURE_YES, "Yes"),
])
def test_the_contoso_questions_are_asked_and_a_sure_read_fills_them(tmp_path, label, key,
                                                                    settle, filled):
    _, first, _, questions, p = _held_back_plan(tmp_path, label=label, key=key, settle=settle)
    assert first.fields[0].action == "skip"            # the gate holds each back
    assert list(questions) == ["field_0_settle"]
    assert (p.fields[0].action, p.fields[0].option, p.fields[0].fact_key) == (
        "select", filled, key)
    assert p.park_reason == ""


def test_a_settle_question_carries_the_authorization_statement(tmp_path):
    bank = standard_bank(authorization_statement=_STATEMENT)
    cat, _, _, questions, _ = _held_back_plan(tmp_path, label=_CONTOSO_AUTH,
                                              key="work_authorized", bank=bank)
    saved = questions["field_0_settle"]["instructions"]["saved_answer"].split("\n")
    assert saved[0] == f"{apply_facts.DESCRIPTIONS['work_authorized']}: Yes"
    assert f"{apply_facts.DESCRIPTIONS['requires_sponsorship']}: No" in saved
    assert saved[-1] == f"{apply_judge.STATEMENT_LINE}: {_STATEMENT}"
    assert sum(line.startswith(apply_judge.STATEMENT_LINE) for line in saved) == 1
    # with no statement saved, the yes / no lines alone
    _, _, _, bare, _ = _held_back_plan(tmp_path / "bare", label=_CONTOSO_AUTH,
                                       key="work_authorized",
                                       bank=standard_bank(authorization_statement=""))
    assert apply_judge.STATEMENT_LINE not in bare["field_0_settle"]["instructions"]["saved_answer"]


@pytest.mark.parametrize("label, key, bank", [
    # another country
    ("Will you require sponsorship to work in the UK?", "requires_sponsorship", None),
    ("Do you require sponsorship to work in Canada (now or in the future)? This includes "
     "needing a work permit.", "requires_sponsorship", None),
    # the present job or employer, a visa or sponsor held now, a sponsorship
    # carried on, a fact about the role, relocation help asked for
    ("Are you currently legally employed in the United States?", "work_authorized", None),
    ("Are you employed? Are you authorized to work in the US?", "work_authorized", None),
    ("Does your employer require visa sponsorship?", "requires_sponsorship", None),
    ("Do you have visa sponsorship?", "requires_sponsorship", None),
    ("Do you have a sponsor?", "requires_sponsorship", None),
    ("Will you require the company to continue your visa sponsorship?", "requires_sponsorship",
     None),
    ("Is this role on-site?", "onsite_ok", None),
    ("Do you currently work on-site?", "onsite_ok", None),
    ("Do you require relocation assistance?", "willing_to_relocate", None),
    ("Are you willing to work in the office once you pass a background check?", "onsite_ok", None),
    # no word of work authorization: "restrictions" may ask about health
    ("Are you able to work without restrictions?", "work_authorized", None),
    # a yes to relocating answers willingness only; a yes to on-site work
    # says nothing of commuting or where the candidate lives
    ("Are you relocating to the job location?", "willing_to_relocate", None),
    ("Will you be moving for this job?", "willing_to_relocate", None),
    ("Are you able to commute to our office?", "onsite_ok", None),
    ("We work 5 days on-site in NYC. Do you currently live in the New York City area?",
     "onsite_ok", None),
    # a custom answer is never read against another wording
    ("Are you legally allowed to work in Canada?", "answer_canada",
     standard_bank() + [custom("canada", "Are you authorized to work in Canada?", "No",
                               type="yes_no")]),
])
def test_a_question_the_saved_answers_do_not_say_is_never_asked_nor_settled(tmp_path, label,
                                                                            key, bank):
    _, first, _, questions, p = _held_back_plan(tmp_path, label=label, key=key, bank=bank,
                                                settle=_SURE_YES)
    assert first.fields[0].action == "skip"
    assert questions == {}
    assert (p.fields[0].action, p.fields[0].option) == ("skip", None)


@pytest.mark.parametrize("label, key", [
    ("Are you currently able to work without sponsorship?", "work_authorized"),
    ("Will you be able to work without sponsorship on your start date?", "work_authorized"),
    ("Do you currently have unrestricted work authorization?", "work_authorized"),
    ("Are there any restrictions on your authorization to work in the U.S.?", "work_authorized"),
    ("Will you require H-1B visa sponsorship to work for us?", "requires_sponsorship"),
    (_CONTOSO_AUTH, "work_authorized"),
])
def test_for_a_candidate_who_needs_sponsorship_a_question_that_turns_on_the_visa_is_not_asked(
        tmp_path, label, key):
    """Sponsorship now or later says nothing of now alone, of restrictions,
    of one visa type or of a list of who counts as authorized; for a
    candidate who needs none, the same question is filled or asked."""
    sponsor = standard_bank(requires_sponsorship="Yes")
    _, first, _, questions, p = _held_back_plan(tmp_path, label=label, key=key, bank=sponsor,
                                                settle=_SURE_YES)
    assert questions == {}
    assert (p.fields[0].action, p.fields[0].option) == ("skip", None)
    _, first, _, questions, _ = _held_back_plan(tmp_path / "citizen", label=label, key=key)
    assert first.fields[0].action == "select" or list(questions) == ["field_0_settle"]


def test_for_a_candidate_who_needs_sponsorship_the_contoso_sponsorship_question_is_asked(
        tmp_path):
    """"CPT, OPT or other visa types" widens the question: any visa counts."""
    sponsor = standard_bank(requires_sponsorship="Yes")
    _, _, _, questions, p = _held_back_plan(tmp_path, label=_CONTOSO_SPONSOR,
                                            key="requires_sponsorship", bank=sponsor,
                                            settle=_SURE_YES)
    assert list(questions) == ["field_0_settle"]
    assert (p.fields[0].action, p.fields[0].option) == ("select", "Yes")


@pytest.mark.parametrize("label, asked_for_yes, asked_for_no", [
    # "located in or willing": a yes answers it, a no turns on where the candidate lives
    ("Are you located in or willing to relocate to Austin, TX?", True, False),
    (_CONTOSO_RELOCATE, True, False),
    # a move with no relocation help is a move too
    ("Are you willing to relocate to the job location (No Relocation Assistance)?", True, True),
    # a no to relocating is a no to moving; a yes asks no plan to move
    ("Will you be moving for this job?", False, True),
])
def test_a_relocation_answer_settles_only_what_its_yes_or_no_says(tmp_path, label,
                                                                  asked_for_yes, asked_for_no):
    for value, asked in (("Yes", asked_for_yes), ("No", asked_for_no)):
        bank = standard_bank(willing_to_relocate=value)
        _, first, _, questions, _ = _held_back_plan(tmp_path / value, label=label,
                                                    key="willing_to_relocate", bank=bank)
        assert first.fields[0].action == "skip", value
        assert (list(questions) == ["field_0_settle"]) is asked, value


def test_a_named_office_is_asked_only_for_a_candidate_willing_to_relocate(tmp_path):
    _, _, _, questions, _ = _held_back_plan(tmp_path, label=_NY_OFFICE)
    assert list(questions) == ["field_0_settle"]
    stays = standard_bank(willing_to_relocate="No")
    _, _, _, questions, p = _held_back_plan(tmp_path / "stays", label=_NY_OFFICE, bank=stays,
                                            settle=_SURE_YES)
    assert questions == {}
    assert (p.fields[0].action, p.fields[0].option) == ("skip", None)


@pytest.mark.parametrize("text, other", [
    ("Do you require sponsorship to work in Canada?", True),
    ("Will you require sponsorship to work in the UK?", True),
    ("Are you authorized to work in the U.K.?", True),
    ("Are you authorized to work in the EU?", True),
    ("Are you authorized to work in Georgia?", False),        # a US state first
    ("Are you willing to relocate to New Mexico?", False),
    ("Are you authorized to work in Puerto Rico?", False),
    ("Are you authorized to work in the United States?", False),
])
def test_other_country_reads_the_us_states_first(text, other):
    assert apply_judge._other_country(text) is other


@pytest.mark.parametrize("label, key, options", [
    # the judge read these wrong at up to 0.92 on the screening set
    ("Are you willing to work remote only?", "remote_only", ("Yes", "No")),
    ("How many years of experience in your previous role?", "years_experience",
     ("0-2 years", "3-5 years", "6+ years")),
])
def test_a_years_count_and_remote_work_are_never_asked(tmp_path, label, key, options):
    sure = _settle_answer(options[0], 0.99)
    _, first, _, questions, p = _held_back_plan(tmp_path, label=label, key=key, settle=sure,
                                                options=options)
    assert first.fields[0].action == "skip"
    assert questions == {}
    assert (p.fields[0].action, p.fields[0].option) == ("skip", None)


def test_a_field_the_gate_passes_is_never_asked(tmp_path):
    _, first, _, questions, _ = _held_back_plan(tmp_path,
                                                label="Are you willing to work on-site?")
    assert (first.fields[0].action, first.fields[0].option) == ("select", "Yes")
    assert questions == {}


def test_the_user_s_note_rides_with_the_saved_answer(tmp_path):
    bank = standard_bank()
    for e in bank:
        if e["id"] == "onsite_ok":
            e["note"] = "Only  in NYC or Austin"
    cat, _, _, questions, _ = _held_back_plan(tmp_path, bank=bank)
    saved = questions["field_0_settle"]["instructions"]["saved_answer"]
    assert saved.splitlines()[0] == (f"{apply_facts.DESCRIPTIONS['onsite_ok']}: Yes "
                                     f"(the candidate's note: Only in NYC or Austin)")
    assert cat.note("onsite_ok") == "Only in NYC or Austin"
    assert cat.note("willing_to_relocate") == "" and cat.note("full_name") == ""
    # a text answer carries its note too
    assert apply_judge.candidate_answer(cat, "onsite_ok", "Yes").startswith(
        f"{apply_facts.DESCRIPTIONS['onsite_ok']}: Yes (the candidate's note:")
