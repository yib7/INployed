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
    return Field(n=n, locator=(0, f"#f{n}"), label=label, type=type_, required=required,
                 options=list(options), id_or_name=ident, help=help)


def _greenhouse():
    """A 12-field Greenhouse-shaped page."""
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


_CONTRAST = re.compile(r",\s*not\s|,\s*never\s|\bnot just\b|\brather than\b|\binstead of\b", re.I)


def _assert_clean_text(obj):
    blob = json.dumps(obj, ensure_ascii=False)
    assert chr(0x2014) not in blob
    assert not _CONTRAST.search(blob), _CONTRAST.search(blob).group(0)


# --- constants --------------------------------------------------------------------

def test_threshold_constants_match_the_spec_table():
    assert apply_judge.PAGE_STATE_MIN_CONF == 0.60
    assert apply_judge.FIELD_MAP_MIN_CONF == 0.70
    assert apply_judge.OPTION_MIN_CONF == 0.70
    assert apply_judge.BUTTON_SUBMIT_MIN_CONF == 0.90
    assert apply_judge.BUTTON_ADVANCE_MIN_CONF == 0.75
    assert apply_judge.VERIFY_MIN == 0.80
    assert apply_judge.PROHIBITED_MAX == 0.30
    assert apply_judge.CAPTCHA_MAX == 0.30
    assert apply_judge.GROUNDING_MIN == 0.70
    assert apply_judge.MAX_PAGES == 12
    assert apply_judge.PAGE_TEXT_CAP == 4000
    assert "UNTUNED until SP8" in apply_judge.__doc__


def test_the_option_tuples():
    assert apply_judge.PAGE_STATES == (
        "application_form", "login_wall", "signup_form", "review_page", "confirmation",
        "code_gate", "captcha_or_bot_check", "payment_request", "error_or_dead",
        "job_posting", "other")
    assert apply_judge.BUTTON_ROLES == ("advance", "submit", "back", "apply_entry",
                                        "upload", "other")
    assert apply_judge.SPECIAL_SOURCES == ("resume_file", "cover_letter_file",
                                           "cover_letter_text", "signature_today",
                                           "needs_generation", "leave_blank")


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
    assert state["buttons"][0] == {"n": 0, "text": "Submit application", "kind_hint": "submit"}
    assert state["facts"] == catalog.to_criteria()
    assert "Jane" not in json.dumps(state)


def test_page_questions_emit_every_question(catalog):
    digest = _greenhouse()
    _, q = apply_judge.page_questions(digest, catalog, _JOB)
    assert q["page_state"]["type"] == "choice"
    assert set(q["page_state"]["criteria"]) == set(apply_judge.PAGE_STATES)
    for opt in apply_judge.PAGE_STATES:
        assert set(q["page_state"]["criteria"][opt]) == {"what", "not_for", "examples"}
    for f in digest.fields:
        src = q[f"field_{f.n}_source"]
        assert src["type"] == "choice"
        assert "leave_blank" in src["criteria"] and "needs_generation" in src["criteria"]
        assert f"fields[{f.n}]" in json.dumps(src["instructions"])
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
                               "cover_letter_text", "signature_today"}
        else:
            assert set(catalog.to_criteria()) - {"resume_file", "cover_letter_file",
                                                 "signature_name", "today"} <= keys
            assert "signature_today" in keys and "cover_letter_text" in keys
            assert "resume_file" not in keys
        if f.options:
            opt = q[f"field_{f.n}_option"]
            assert list(opt["criteria"]) == f.options + ["no_match"]
        else:
            assert f"field_{f.n}_option" not in q
    for b in digest.buttons:
        role = q[f"button_{b.n}_role"]
        assert set(role["criteria"]) == set(apply_judge.BUTTON_ROLES)
        assert f"buttons[{b.n}]" in json.dumps(role["instructions"])
    for qid in ("asks_for_prohibited", "requires_account", "has_captcha"):
        assert q[qid]["type"] == "noul"
    # every id is a real question the model sees in full (ids are never sent)
    assert all("instructions" in v for v in q.values())


def test_page_questions_option_instruction_carries_the_quick_map_value(catalog):
    digest = FormDigest(url_host="x", title="t", text="",
                        fields=[_f(0, "Country", "select", options=("United States", "Canada")),
                                _f(1, "Gender", "select", options=("Male", "Female"))])
    _, q = apply_judge.page_questions(digest, catalog, _JOB)
    assert "United States" in json.dumps(q["field_0_option"]["instructions"])
    # no quick_map hit: the label is named by its path, never a value
    blob = json.dumps(q["field_1_option"]["instructions"])
    assert "fields[1].label" in blob and "Decline" not in blob


def test_page_questions_cap_help_options_and_text(catalog):
    long_help = "h" * 500
    digest = FormDigest(url_host="x", title="t", text="z" * 10_000,
                        fields=[_f(0, "Pick", "select", help=long_help,
                                   options=[f"opt{i}" for i in range(100)])])
    state, q = apply_judge.page_questions(digest, catalog, _JOB)
    assert len(state["page"]["headline_text"]) == 600
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


def test_plan_park_order_prohibited_then_captcha_then_required(catalog):
    digest = FormDigest(url_host="x", title="t", text="",
                        fields=[_f(0, "Social Security Number", required=True)])
    answers = _page_answers(digest, {}, prohibited=0.8, captcha=0.9)
    p = apply_judge.plan(digest, catalog, answers)
    assert p.park_reason.startswith("page asks for prohibited data")
    answers = _page_answers(digest, {}, captcha=0.9)
    assert apply_judge.plan(digest, catalog, answers).park_reason.startswith("captcha")
    answers = _page_answers(digest, {}, captcha=0.3)      # at the max, never above it
    assert apply_judge.plan(digest, catalog, answers).park_reason == \
        "required field without an answer: Social Security Number"


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
    messages = [{"n": 0, "sender": "news@example.com", "subject": "Weekly digest",
                 "preview": "Top stories this week"},
                {"n": 1, "sender": "no-reply@greenhouse.io",
                 "subject": "Your Greenhouse verification code",
                 "preview": "Enter the code 482913 to continue"}]
    state, q = apply_judge.inbox_questions(messages, "greenhouse.io")
    assert state["site"] == "greenhouse.io" and state["messages"] == messages
    assert set(q) == {"msg_0_is_code", "msg_1_is_code"}
    assert "greenhouse.io" in json.dumps(q["msg_1_is_code"]["instructions"])
    answers = jev.FakeJev().judge(state, q)
    assert answers["msg_1_is_code"].noul == 0.9 and answers["msg_0_is_code"].noul == 0.1
    assert apply_judge.read_inbox(answers, messages) == 1
    assert apply_judge.read_inbox({"msg_0_is_code": _noul(0.6), "msg_1_is_code": _noul(0.69)},
                                  messages) is None
    assert apply_judge.read_inbox({"msg_0_is_code": _noul(0.75), "msg_1_is_code": _noul(0.9)},
                                  messages) == 1


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
    state, q = apply_judge.page_questions(digest, catalog, _JOB)
    answers = fake.judge(state, q)
    assert apply_judge.read_page_state(answers) == ("application_form", 1.0)

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


def test_fake_jev_flags_a_captcha_page_and_a_login_wall(catalog):
    captcha = FormDigest(url_host="jobs.example.com", title="Verify you are human",
                         text="reCAPTCHA: please complete the robot check to continue.",
                         fields=[], buttons=[])
    state, q = apply_judge.page_questions(captcha, catalog, _JOB)
    p = apply_judge.plan(captcha, catalog, jev.FakeJev().judge(state, q))
    assert p.flags["has_captcha"] == 0.9
    assert p.park_reason.startswith("captcha")

    login = FormDigest(url_host="jobs.example.com", title="Sign in to your account",
                       text="Sign in with your existing account email and password.",
                       fields=[_f(0, "Email", "email", required=True),
                               _f(1, "Password", "other", required=True)],
                       buttons=[Button(n=0, locator=(0, "#s"), text="Sign in")])
    state, q = apply_judge.page_questions(login, catalog, _JOB)
    answers = jev.FakeJev().judge(state, q)
    assert answers["requires_account"].noul == 0.9
    assert apply_judge.read_page_state(answers)[0] == "login_wall"
