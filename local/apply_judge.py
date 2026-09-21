"""Every Jev question the auto-apply loop asks, and every threshold it reads
them with, in one reviewable place.

Builders return `(state, questions)` in the HTTP shape `jev.Jev.judge` takes;
readers turn the `Answer`s back into code paths. Nothing here touches a
browser or the network.

    page_questions(digest, catalog, job)   one request per page: page state,
                                           field -> fact map, option picks,
                                           button roles, prohibited / account /
                                           captcha flags
    option_questions(digest, plan)         the second request: option picks for
                                           fields whose fact the first answer
                                           chose (quick_map covers the rest)
    verify_questions(filled, sheet)        every typed value against the sheet
    inbox_questions / code_pick_questions  the emailed-code path (from-site and
                                           has-code Nouls per message, then the
                                           code pick)
    grounding_questions(sentences, sheet)  a generated answer, sentence by sentence

    read_page_state, plan, read_verification, read_inbox, read_code_pick,
    read_grounding                         the readers

Question ids are never sent to the model, so every `instructions` carries the
full question; state parts are named with backticked paths; every Choice has
an escape option; every Noul is phrased so a high value means yes; no
arithmetic is asked of the model (counting and thresholds live here).

`plan()` is meant to run twice per page: once over the first request's
answers, then again over those answers merged with the second request's
(`option_questions`), because an option pick for a model-mapped select is
only meaningful once the fact is known. The first request's `field_{n}_option`
is read only for a field `quick_map` knew (its instruction carried the value);
a model-mapped select waits for `field_{n}_pick` from the second request, and
until then it is `skip` and, when required, sets `park_reason`.

THRESHOLDS ARE UNTUNED until SP8 records real Jev answers over the fixtures.
The defaults are the design table's; the constants are the only place to
change them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from apply_facts import FactCatalog, quick_map
from apply_form import FormDigest
from jev import Answer

log = logging.getLogger("apply_judge")

# --- thresholds (UNTUNED until SP8) -----------------------------------------------

PAGE_STATE_MIN_CONF = 0.60      # below it: park needs_human
FIELD_MAP_MIN_CONF = 0.70       # below it: optional -> blank + flagged; required -> park
OPTION_MIN_CONF = 0.70          # the same rule for select / radio picks
BUTTON_SUBMIT_MIN_CONF = 0.90   # a click on a submit-role button needs this
BUTTON_ADVANCE_MIN_CONF = 0.75
VERIFY_MIN = 0.80               # every filled required field must verify above this
PLACEHOLDER_MAX = 0.50          # and look like a placeholder no more than this
PROHIBITED_MAX = 0.30           # above it: park
CAPTCHA_MAX = 0.30              # above it: park
GROUNDING_MIN = 0.70            # a generated sentence below it drops the draft
INBOX_MIN = 0.50                # an inbox message needs from_site and has_code both above it
MAX_PAGES = 12                  # pages per application before parking
PAGE_TEXT_CAP = 4000            # the extractor's cap on the page's visible text

HEADLINE_CHARS = 600            # of the page text sent as `page.headline_text`
HELP_CAP = 200                  # per-field help text sent
OPTIONS_CAP = 40                # per-field options sent

PAGE_STATES = ("application_form", "login_wall", "signup_form", "review_page",
               "confirmation", "code_gate", "captcha_or_bot_check", "payment_request",
               "error_or_dead", "job_posting", "other")
BUTTON_ROLES = ("advance", "submit", "back", "apply_entry", "upload", "other")
SPECIAL_SOURCES = ("resume_file", "cover_letter_file", "cover_letter_text",
                   "signature_today", "consent_attest", "needs_generation", "leave_blank")

ACTIONS = ("fill", "select", "upload", "generate", "skip")

# --- criteria ----------------------------------------------------------------------

_PAGE_STATE_CRITERIA: dict[str, dict[str, Any]] = {
    "application_form": {
        "what": "A job application form: controls for the candidate's name, email, resume "
                "upload or screening questions, with a way to continue or submit",
        "not_for": "The job description before the Apply button; a sign-in screen",
        "examples": ["Apply for Software Engineer: First Name, Last Name, Email, Resume",
                     "Application step 2 of 3: work authorization questions"]},
    "login_wall": {
        "what": "A sign-in screen asking for an existing account's email and password",
        "not_for": "Creating a new account; the application form itself",
        "examples": ["Sign in to continue", "Email, Password, Forgot password?"]},
    "signup_form": {
        "what": "A screen to create a new account: choose a password, accept terms, register",
        "not_for": "Signing in to an account that already exists",
        "examples": ["Create an account to apply", "Register: email, password, confirm password"]},
    "review_page": {
        "what": "A summary of the answers already entered, shown before the final submit",
        "not_for": "A form that still asks for values",
        "examples": ["Review your application", "Please review before submitting"]},
    "confirmation": {
        "what": "The application was received: a thank-you or success message after submit",
        "not_for": "A review step that still needs a submit click",
        "examples": ["Thank you for applying", "Your application has been submitted"]},
    "code_gate": {
        "what": "A screen asking for a verification or security code that was emailed",
        "not_for": "A password prompt",
        "examples": ["Enter the code we sent to your email", "Security code"]},
    "captcha_or_bot_check": {
        "what": "A CAPTCHA, reCAPTCHA, hCaptcha or a robot or human verification challenge",
        "not_for": "An emailed code",
        "examples": ["I am human", "Verify you are human", "Select all images with buses"]},
    "payment_request": {
        "what": "A screen asking for a payment, a card number or a fee",
        "not_for": "A salary expectation question on an application",
        "examples": ["Pay the application fee", "Card number, expiry, CVC"]},
    "error_or_dead": {
        "what": "An error, a closed or expired posting, a 404, or an empty screen",
        "not_for": "A form with a validation message on one field",
        "examples": ["This job is no longer available", "404 Not Found", "Something went wrong"]},
    "job_posting": {
        "what": "The job description itself, with an Apply button, before any form",
        "not_for": "A screen with application form controls",
        "examples": ["About the role, Responsibilities, Qualifications, Apply now"]},
    "other": {
        "what": "None of the listed kinds",
        "not_for": "Any screen one of the listed kinds describes",
        "examples": ["A company home page", "A cookie consent screen"]},
}

# The special sources' descriptions. They join the catalog's descriptions in
# the state's `facts` map, sent once per page, so every `field_{n}_source`
# Choice carries keys only (40 fields on a page is the size budget) and its
# instruction can say "which key of `facts`". A catalog description wins for
# a key both have (`resume_file` when a resume PDF exists).
SPECIAL_DESCRIPTIONS: dict[str, Any] = {
    "resume_file": "The tailored resume PDF, for a file upload",
    "cover_letter_file": "The cover letter PDF, for a file upload",
    "cover_letter_text": "The cover letter as plain text, for a paste box",
    "signature_today": "A typed signature name, or today's date for a signature date box",
    # Offered to checkboxes only. The `not_for` list keeps substantive
    # commitments out: those park the job for the human.
    "consent_attest": {
        "what": "a checkbox that asks the candidate to confirm the accuracy of the "
                "application, agree to the application's privacy notice or terms, or "
                "consent to be contacted about this application",
        "not_for": "background-check, drug-test, age, non-compete, relocation or any "
                   "other substantive commitment"},
    "needs_generation": "An essay or motivation question; a draft is written for it",
    "leave_blank": "No source applies; the box is left blank",
}

# A login wall's sign-in button and a signup form's create-account button are
# `advance` (spec 3.5): they move the flow forward without sending the
# application.
_BUTTON_CRITERIA: dict[str, dict[str, Any]] = {
    "advance": {
        "what": "The next step: the next form page, signing in, creating the account, "
                "continuing with an email",
        "examples": ["Next", "Continue", "Save and continue", "Sign in", "Log in",
                     "Create account", "Continue with email"]},
    "submit": {
        "what": "Sends the finished application",
        "examples": ["Submit", "Submit application", "Send application"]},
    "back": {
        "what": "The previous step",
        "examples": ["Back", "Previous"]},
    "apply_entry": {
        "what": "The posting's own Apply button, before any form",
        "examples": ["Apply", "Apply now", "Apply for this job"]},
    "upload": {
        "what": "Opens a file picker",
        "examples": ["Attach", "Upload resume", "Choose file"]},
    "other": {
        "what": "Anything else: cancel, help, menu, cookie banner, policy link",
        "examples": ["Cancel", "Help", "Accept cookies"]},
}

NO_MATCH_DESCRIPTION = "nothing listed fits"

# Which sources a control can take, by its type. A file input takes a file
# and nothing else; a select / radio / listbox never takes a name, an email,
# a URL or prose; `today` and `signature_name` reach a form through the
# `signature_today` special. Fewer confusable options per question, and the
# request stays inside its size budget on a long page.
_FILE_TYPES = frozenset(("file",))
_OPTION_TYPES = frozenset(("select", "radio", "checkbox", "listbox"))
_ALWAYS = ("needs_generation", "leave_blank")
_FILE_SOURCES = ("resume_file", "cover_letter_file") + _ALWAYS
_CHECKBOX_SOURCES = ("consent_attest",)
_NEVER_IN_OPTIONS = frozenset((
    "full_name", "first_name", "last_name", "email", "phone", "linkedin_url",
    "github_url", "website_url", "resume_file", "cover_letter_file", "cover_letter_text",
    "signature_name", "today", "signature_today", "consent_attest"))
_NEVER_IN_TEXT = frozenset(("resume_file", "cover_letter_file", "signature_name", "today",
                            "consent_attest"))
# A typed input takes only the facts of its shape.
_TYPED_SOURCES: dict[str, tuple[str, ...]] = {
    "email": ("email",),
    "tel": ("phone",),
    "url": ("linkedin_url", "github_url", "website_url"),
    "date": ("signature_today", "education_grad_year"),
    "number": ("years_experience", "education_grad_year", "address_zip", "phone"),
}


def _source_criteria(catalog_keys: list[str], type_: str) -> dict[str, Any]:
    """The field's option set, keys only (`facts` in the state describes them):
    `leave_blank` and `needs_generation` always, `consent_attest` for a
    checkbox."""
    if type_ in _FILE_TYPES:
        keys = list(_FILE_SOURCES)
    elif type_ in _TYPED_SOURCES:
        keys = [k for k in _TYPED_SOURCES[type_]
                if k in catalog_keys or k in SPECIAL_SOURCES] + list(_ALWAYS)
    elif type_ in _OPTION_TYPES:
        keys = [k for k in catalog_keys if k not in _NEVER_IN_OPTIONS]
        if type_ == "checkbox":
            keys += list(_CHECKBOX_SOURCES)
        keys += list(_ALWAYS)
    else:
        keys = [k for k in catalog_keys if k not in _NEVER_IN_TEXT]
        keys += [k for k in SPECIAL_SOURCES if k not in keys and k not in _NEVER_IN_TEXT]
    return {k: None for k in keys}


# --- the page request --------------------------------------------------------------

def _compact_field(f) -> dict[str, Any]:
    obj: dict[str, Any] = {"n": f.n, "label": f.label, "type": f.type,
                           "required": bool(f.required)}
    if f.placeholder:
        obj["placeholder"] = f.placeholder[:HELP_CAP]
    if f.help:
        obj["help"] = f.help[:HELP_CAP]
    if f.options:
        obj["options"] = list(f.options[:OPTIONS_CAP])
    if f.id_or_name:
        obj["id_or_name"] = f.id_or_name[:80]
    return obj


def _option_question(i: int, options: list[str], candidate_answer: str,
                     from_value: bool) -> dict[str, Any]:
    """`from_value`: the instruction carries the fact value to match. Otherwise
    the value is unknown yet and the question is speculative; `plan` never
    reads that answer (see the module docstring)."""
    criteria: dict[str, Any] = {o: None for o in options[:OPTIONS_CAP]}
    criteria["no_match"] = NO_MATCH_DESCRIPTION
    if from_value:
        instructions = {
            "candidate_answer": candidate_answer,
            "question": f"Which of these choices means the same as `candidate_answer`, "
                        f"as an answer to the form field `fields[{i}].label`?"}
    else:
        instructions = (f"Which of these choices is the plain default answer to the form "
                        f"field `fields[{i}].label` when the catalog is silent on it?")
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def page_questions(digest: FormDigest, catalog: FactCatalog,
                   job: Mapping[str, Any] | None = None) -> tuple[dict, dict]:
    """The one request per page. State: the job, the page's host / title /
    headline, the compact fields and buttons, and `facts`, the description of
    every source key a field can take (the catalog's facts and the specials;
    never a value). Questions: `page_state`, `field_{n}_source`, `field_{n}_option`
    for every field with options, `button_{n}_role`, `asks_for_prohibited`,
    `requires_account`, `has_captcha`."""
    job = job or {}
    text = (digest.text or "")[:PAGE_TEXT_CAP]
    state: dict[str, Any] = {
        "job": {"company": str(job.get("company_name") or job.get("company") or ""),
                "title": str(job.get("job_title") or job.get("title") or "")},
        "page": {"url_host": digest.url_host, "title": digest.title,
                 "headline_text": text[:HEADLINE_CHARS]},
        "fields": [_compact_field(f) for f in digest.fields],
        "buttons": [{"n": b.n, "text": b.text, "kind_hint": b.kind_hint}
                    for b in digest.buttons],
        "facts": {**SPECIAL_DESCRIPTIONS, **catalog.to_criteria()},
    }
    questions: dict[str, Any] = {
        "page_state": {
            "type": "choice",
            "instructions": "Which kind of screen is `page`, given its `fields` and `buttons`?",
            "criteria": _PAGE_STATE_CRITERIA,
        },
    }
    catalog_keys = list(catalog.to_criteria())
    for i, f in enumerate(digest.fields):
        questions[f"field_{f.n}_source"] = {
            "type": "choice",
            "instructions": f"Which key of `facts` describes what `fields[{i}]` asks for? "
                            "When nothing fits, `leave_blank`; for prose the facts lack, "
                            "`needs_generation`.",
            "criteria": _source_criteria(catalog_keys, f.type),
        }
        if f.options:
            key = quick_map(f.label, f.id_or_name, f.type)
            if key and catalog.has(key):
                questions[f"field_{f.n}_option"] = _option_question(
                    i, f.options, catalog.value(key), from_value=True)
            else:
                questions[f"field_{f.n}_option"] = _option_question(
                    i, f.options, f.label, from_value=False)
    for i, b in enumerate(digest.buttons):
        questions[f"button_{b.n}_role"] = {
            "type": "choice",
            "instructions": f"Which role does the clickable control `buttons[{i}]` have?",
            "criteria": {role: dict(desc) for role, desc in _BUTTON_CRITERIA.items()},
        }
    questions["asks_for_prohibited"] = {
        "type": "noul",
        "instructions": "Does `page` or any of `fields` ask for a social security number, "
                        "a birthdate, banking or payment details, or a government "
                        "identification document?",
    }
    questions["requires_account"] = {
        "type": "noul",
        "instructions": "Does `page` require the candidate to sign in or create an account "
                        "before applying?",
    }
    questions["has_captcha"] = {
        "type": "noul",
        "instructions": "Does `page` show a CAPTCHA, a reCAPTCHA or hCaptcha challenge, or a "
                        "robot or human verification check?",
    }
    return state, questions


# --- the plan ----------------------------------------------------------------------

@dataclass
class PlannedField:
    n: int
    locator: tuple[int, str]
    label: str
    required: bool
    fact_key: str | None
    value: str
    option: str | None
    confidence: float
    action: str
    quick: bool = False      # the fact came from quick_map (its pick rode in the first request)


@dataclass
class FillPlan:
    fields: list[PlannedField] = field(default_factory=list)
    buttons: dict[str, tuple[int, float]] = field(default_factory=dict)
    flags: dict[str, float] = field(default_factory=dict)
    park_reason: str = ""
    missing: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_page_state(answers: Mapping[str, Answer]) -> tuple[str, float]:
    a = answers.get("page_state")
    if a is None or a.choice is None:
        return "other", 0.0
    return str(a.choice), float(a.confidence or 0.0)


def _choice_of(answers: Mapping[str, Answer], qid: str) -> tuple[str | None, float]:
    a = answers.get(qid)
    if a is None or a.choice is None:
        return None, 0.0
    return str(a.choice), float(a.confidence or 0.0)


def _noul_of(answers: Mapping[str, Answer], qid: str) -> float:
    a = answers.get(qid)
    return float(a.noul) if a is not None and a.noul is not None else 0.0


_DATE_TOKENS = frozenset(("date", "dated", "today"))
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _wants_date(f) -> bool:
    """A `signature_today` field takes the date when its control is a date
    input or its label carries `date` / `dated` / `today` as a whole word
    ("Candidate Signature" contains the letters, and takes the name)."""
    if f.type == "date":
        return True
    return bool(_DATE_TOKENS & set(_TOKEN_RE.findall((f.label or "").lower())))


def _action_for(f) -> str:
    if f.type == "file":
        return "upload"
    if f.options:
        return "select"
    return "fill"


def plan(digest: FormDigest, catalog: FactCatalog, answers: Mapping[str, Answer], *,
         generation_enabled: bool = True) -> FillPlan:
    """Turn the page answers into a `FillPlan`.

    Per field: a `quick_map` hit whose fact has a value wins over the model's
    mapping (a disagreement is logged at DEBUG; an empty fact falls through to
    the model's mapping); a mapping below `FIELD_MAP_MIN_CONF` or equal to
    `leave_blank` is `skip`, and when the field is required the plan carries
    `park_reason` and a `missing` entry (an optional skip is a `missing` entry
    only); `needs_generation` is `generate` when generation is enabled, else
    the same rule; an option pick below `OPTION_MIN_CONF` or `no_match` follows
    the same rule; `signature_today` fills today's date for a date control or
    a label with `date` / `dated` / `today` as a whole word, else the typed
    name; `consent_attest` (a checkbox's attestation, privacy or contact
    consent) is `select` with option `checked`. Buttons keep the highest-confidence n per role. The park
    reasons are checked in the order prohibited, captcha, required field."""
    out = FillPlan()
    required_reason = ""
    for f in digest.fields:
        model_key, model_conf = _choice_of(answers, f"field_{f.n}_source")
        quick = quick_map(f.label, f.id_or_name, f.type)
        if quick and not catalog.has(quick):
            log.debug("field %d %r: quick_map %s has no value; using the model's mapping",
                      f.n, f.label, quick)
            quick = None
        if quick:
            fact_key, conf = quick, 1.0
            if model_key and model_key != quick:
                log.debug("field %d %r: quick_map %s, model %s (%.2f); keeping quick_map",
                          f.n, f.label, quick, model_key, model_conf)
        elif model_key is None or model_key == "leave_blank" or model_conf < FIELD_MAP_MIN_CONF:
            fact_key, conf = None, model_conf
        else:
            fact_key, conf = model_key, model_conf

        pf = PlannedField(n=f.n, locator=f.locator, label=f.label, required=bool(f.required),
                          fact_key=fact_key, value="", option=None, confidence=conf,
                          action="skip", quick=bool(quick))
        if fact_key == "needs_generation":
            pf.action = "generate" if generation_enabled else "skip"
        elif fact_key == "consent_attest":
            pf.action, pf.option, pf.value = "select", "checked", "yes"
        elif fact_key == "signature_today":
            pf.value = catalog.value("today" if _wants_date(f) else "signature_name")
            pf.action = "fill" if pf.value else "skip"
        elif fact_key and catalog.has(fact_key):
            pf.value = catalog.value(fact_key)
            pf.action = _action_for(f)
            if pf.action == "select":
                # a quick_map field's pick rode in the first request with the
                # value in hand; a model-mapped field's pick is `field_{n}_pick`
                # from the second request, and until it arrives the field waits
                qid = f"field_{f.n}_option" if quick else f"field_{f.n}_pick"
                opt, oconf = _choice_of(answers, qid)
                if opt is None or opt == "no_match" or oconf < OPTION_MIN_CONF:
                    pf.action = "skip"
                else:
                    pf.option = opt
        out.fields.append(pf)
        if pf.action == "skip":
            out.missing.append((f.label, f.help or f.placeholder or f.type))
            if f.required and not required_reason:
                required_reason = f"required field without an answer: {f.label}"

    for b in digest.buttons:
        role, conf = _choice_of(answers, f"button_{b.n}_role")
        if role is None:
            continue
        if role not in out.buttons or conf > out.buttons[role][1]:
            out.buttons[role] = (b.n, conf)

    out.flags = {qid: _noul_of(answers, qid)
                 for qid in ("asks_for_prohibited", "requires_account", "has_captcha")}
    if out.flags["asks_for_prohibited"] > PROHIBITED_MAX:
        out.park_reason = (f"page asks for prohibited data "
                           f"(p={out.flags['asks_for_prohibited']:.2f})")
    elif out.flags["has_captcha"] > CAPTCHA_MAX:
        out.park_reason = f"captcha or bot check on the page (p={out.flags['has_captcha']:.2f})"
    elif required_reason:
        out.park_reason = required_reason
    return out


# --- the second request: option picks for model-mapped fields ------------------------

def option_questions(digest: FormDigest, fill_plan: FillPlan) -> tuple[dict, dict]:
    """Option picks (`field_{n}_pick`) for every field with options whose fact
    the model chose (quick_map did not know it), with the fact's value in the
    instruction. Empty when there is nothing to ask; merge the answers over the
    first request's and call `plan` again."""
    by_n = {f.n: f for f in digest.fields}
    state: dict[str, Any] = {"fields": []}
    questions: dict[str, Any] = {}
    for pf in fill_plan.fields:
        f = by_n.get(pf.n)
        if f is None or not f.options or not pf.fact_key or not pf.value:
            continue
        if pf.fact_key in SPECIAL_SOURCES or pf.quick:
            continue
        i = len(state["fields"])
        state["fields"].append(_compact_field(f))
        questions[f"field_{f.n}_pick"] = _option_question(i, f.options, pf.value,
                                                          from_value=True)
    return state, questions


# --- verification --------------------------------------------------------------------

@dataclass(frozen=True)
class VerifyResult:
    n: int
    label: str
    ok: bool
    p_correct: float
    p_placeholder: float


def verify_questions(filled: list[Mapping[str, Any]], sheet_excerpt: str) -> tuple[dict, dict]:
    """Per filled field: `verify_{n}` (the value is the sheet's value for that
    label) and `placeholder_{n}` (the value looks like a placeholder). The
    value and label ride in the instruction so each question is self-contained."""
    rows = [{"n": int(f["n"]), "label": str(f.get("label", "")), "value": str(f.get("value", ""))}
            for f in filled]
    state = {"sheet_excerpt": sheet_excerpt, "filled": rows}
    questions: dict[str, Any] = {}
    for i, row in enumerate(rows):
        n = row["n"]
        questions[f"verify_{n}"] = {
            "type": "noul",
            "instructions": {
                "field_label": row["label"],
                "filled_value": row["value"],
                "question": "Is `filled_value` the correct value for the form field "
                            "`field_label` according to `sheet_excerpt`?",
            },
        }
        questions[f"placeholder_{n}"] = {
            "type": "noul",
            "instructions": f"Does `filled[{i}].value` look like a placeholder or dummy entry, "
                            "such as XXXXX, N/A, TBD, or a lone dash?",
        }
    return state, questions


def read_verification(filled: list[Mapping[str, Any]],
                      answers: Mapping[str, Answer]) -> list[VerifyResult]:
    out = []
    for f in filled:
        n = int(f["n"])
        p_correct = _noul_of(answers, f"verify_{n}")
        p_placeholder = _noul_of(answers, f"placeholder_{n}")
        out.append(VerifyResult(n=n, label=str(f.get("label", "")),
                                ok=p_correct >= VERIFY_MIN and p_placeholder <= PLACEHOLDER_MAX,
                                p_correct=p_correct, p_placeholder=p_placeholder))
    return out


# --- the emailed code ----------------------------------------------------------------

def inbox_questions(messages: list[Mapping[str, Any]], site: str) -> tuple[dict, dict]:
    """Two Nouls per message, one yes/no each: `msg_{n}_from_site` (sent by or
    on behalf of `site`) and `msg_{n}_has_code` (carries a verification or
    security code). `read_inbox` combines them."""
    rows = [{"n": int(m["n"]), "sender": str(m.get("sender", "")),
             "subject": str(m.get("subject", "")), "preview": str(m.get("preview", ""))}
            for m in messages]
    state = {"site": site, "messages": rows}
    questions: dict[str, Any] = {}
    for i, row in enumerate(rows):
        n = row["n"]
        questions[f"msg_{n}_from_site"] = {
            "type": "noul",
            "instructions": {
                "site_domain": site,
                "question": f"Was `messages[{i}]` sent by `site_domain` or on its behalf, "
                            "judging by the sender address and the message text?",
            },
        }
        questions[f"msg_{n}_has_code"] = {
            "type": "noul",
            "instructions": f"Does `messages[{i}]` carry a verification code or a security "
                            "code for the reader to enter on a website?",
        }
    return state, questions


def read_inbox(answers: Mapping[str, Answer],
               messages: list[Mapping[str, Any]]) -> int | None:
    """The n of the message maximising `from_site * has_code`, with both above
    `INBOX_MIN`; None when no message qualifies."""
    best_n, best_p = None, 0.0
    for m in messages:
        n = int(m["n"])
        from_site = _noul_of(answers, f"msg_{n}_from_site")
        has_code = _noul_of(answers, f"msg_{n}_has_code")
        if from_site <= INBOX_MIN or has_code <= INBOX_MIN:
            continue
        p = from_site * has_code
        if p > best_p:
            best_n, best_p = n, p
    return best_n


def code_pick_questions(candidates: list[str], body: str) -> tuple[dict, dict]:
    """One Choice `code_pick` over the regex candidates plus `none`."""
    state = {"body": body, "candidates": list(candidates)}
    criteria: dict[str, Any] = {c: None for c in candidates}
    criteria["none"] = "No listed string is the right one"
    questions = {"code_pick": {
        "type": "choice",
        "instructions": "Which candidate string is the verification code that `body` asks "
                        "the reader to enter?",
        "criteria": criteria,
    }}
    return state, questions


def read_code_pick(answers: Mapping[str, Answer]) -> str | None:
    choice, _ = _choice_of(answers, "code_pick")
    return None if choice in (None, "none") else choice


# --- grounding -----------------------------------------------------------------------

def grounding_questions(sentences: list[str], sheet_excerpt: str) -> tuple[dict, dict]:
    """`grounded_{i}` per sentence: every claim in it is supported by the sheet."""
    state = {"sheet_excerpt": sheet_excerpt, "sentences": list(sentences)}
    questions = {
        f"grounded_{i}": {
            "type": "noul",
            "instructions": {
                "sentence": s,
                "question": "Is every claim in `sentence` supported by `sheet_excerpt`?",
            },
        }
        for i, s in enumerate(sentences)}
    return state, questions


def read_grounding(answers: Mapping[str, Answer], sentences: list[str]) -> tuple[bool, float]:
    """(every sentence at or above `GROUNDING_MIN`, the weakest probability)."""
    if not sentences:
        return True, 1.0
    probs = [_noul_of(answers, f"grounded_{i}") for i in range(len(sentences))]
    weakest = min(probs)
    return weakest >= GROUNDING_MIN, weakest
