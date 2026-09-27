"""Every Jev question the auto-apply loop asks, and every threshold it reads
them with, in one reviewable place.

Builders return `(state, questions)` in the HTTP shape `jev.Jev.judge` takes;
readers turn the `Answer`s back into code paths. Nothing here touches a
browser or the network.

    read_questions(digest, url)            the page read, first per page: a
                                           trimmed state (host, path shape,
                                           title, the text's head, a field
                                           census, button texts, a dialog) and
                                           the `page_state` Choice plus one
                                           Noul per signal; `read_page`
                                           combines them with `page_facts`
    page_questions(digest, catalog, job)   the page's mapping, on a page the
                                           run acts on: field -> fact map,
                                           option picks, button roles, the
                                           prohibited flag
    page_requests(digest, catalog, job)    the same mapping in requests sized to
                                           Jev's limits (RES-03), read back as
                                           one by `merge_answers`
    option_questions(digest, plan, catalog)  the second request: option picks
                                           for fields whose fact the first
                                           answer chose (quick_map covers the
                                           rest; code settles a plain Yes / No)
    settle_questions(digest, plan, answers, catalog)  a saved answer the
                                           own-question gate held back, asked
                                           whether it settles the field's
                                           question as worded there
    verify_questions(filled, sheet)        every typed value against the sheet
    inbox_questions / code_pick_questions  the emailed-code path (from-site-or-ATS
                                           and has-code Nouls per message, then
                                           the code pick)
    grounding_questions(sentences, sheet)  a generated answer, sentence by sentence

    read_page, read_page_state, plan, read_verification, read_inbox,
    read_code_pick, read_grounding         the readers

Question ids are never sent to the model, so every `instructions` carries the
full question; state parts are named with backticked paths; every Choice has
an escape option; every Noul is phrased so a high value means yes; no
arithmetic is asked of the model (counting and thresholds live here).

`plan()` is meant to run twice per page: once over the first request's
answers, then again over those answers merged with the second request's
(`option_questions`), because an option pick for a model-mapped select is
only meaningful once the fact is known. The first request carries a
`field_{n}_option` only for a field `quick_map` knew (its instruction carries
the value); a model-mapped select waits for `field_{n}_pick` from the second
request, and until then it is `skip` and, when required, sets `park_reason`.

The thresholds were tuned 2026-09-25 (SP8b) against the recorded live answers
in `tests/fixtures/jev_cache/cache.json` (the runner tests) and
`tests/fixtures/jev_cache/matrix_cache.json` (the flow matrix's real column),
and the live page reads of the matrix and the local captures (first tuned
2026-09-22, SP8); the block below the constants records the distribution each
gate was read against. The constants are the only place to change them.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from urllib.parse import urlsplit

# PLAIN_NUMBER (a number box's answer: "3", "1.5"; "5+", "$120,000" and "3-5"
# are no number, cycle 18 FM-5) and the yes and no forms are the own-question
# gate's too, so both read one list (final review C1)
from apply_facts import (DESCRIPTIONS, NO_FORMS, PLAIN_NUMBER, STORED_YES_NO_KEYS, YES_FORMS,
                         YES_NO_KEYS, FactCatalog, answers_question, asks_willingness,
                         noun_phrase, question_tokens, quick_map)
# the own-question gate's other names, re-exported for the judge's callers
from apply_facts import asks_own_question as asks_own_question
from apply_facts import question_fit as question_fit
from apply_form import FormDigest, password_box
from jev import APOSTROPHES, NOT_SETTLED, PAGE_KIND_NOULS, Answer, request_fits
# pure data (no package imports, no .env): the one US state list the store shares
from resume_tailor.answer_tables import COUNTRIES as _COUNTRIES
from resume_tailor.answer_tables import US_STATES as _US_STATES

log = logging.getLogger("apply_judge")

# --- thresholds (tuned 2026-09-25 against the recorded live answers) ----------------

PAGE_STATE_MIN_CONF = 0.40      # below it: park needs_human (the best guess wins above it)
CONFIRMATION_MIN_CONF = 0.60    # after the submit click, a confirmation read with no
                                # received words on the page needs this to count as the
                                # send's confirmation ("thanks for your interest" misread
                                # at 0.45 must not); before any submit click a confirmation
                                # read never counts as submitted
FIELD_MAP_MIN_CONF = 0.70       # below it: optional -> blank + flagged; required -> park
CONSENT_MIN_CONF = 0.85         # consent_attest needs this much: a tick cannot be taken back
OPTION_MIN_CONF = 0.70          # the same rule for select / radio picks
# A saved answer the own-question gate held back fills its field only when the
# judge says it settles the field's question as worded there (`settle_questions`)
# at this confidence, this far ahead of every other choice (the user's call,
# 2026-09-26: reworded questions are read by the judge when it is sure)
SETTLE_MIN_CONF = 0.85
SETTLE_MIN_GAP = 0.60
BUTTON_SUBMIT_MIN_CONF = 0.50   # a click on a submit-role button needs this
BUTTON_ADVANCE_MIN_CONF = 0.50
BUTTON_SENDS_MIN = 0.40         # an "Apply"-worded button is the submit only with this
                                # `button_{n}_sends` Noul (and the DOM evidence the runner
                                # reads): an Apply entry or "Apply Manually" opens a form
VERIFY_MIN = 0.80               # every filled required field must verify above this
PLACEHOLDER_MAX = 0.50          # and look like a placeholder no more than this
GROUNDING_MIN = 0.70            # a generated sentence below it drops the draft
INBOX_MIN = 0.50                # an inbox message needs from_site and has_code both above it
MAX_PAGES = 20                  # pages per application before parking
PAGE_TEXT_CAP = 4000            # the extractor's cap on the page's visible text

# What parks a job (the user's rule, 2026-09-22): the submit step while park
# mode is on, a required question the user's data cannot answer, and a page the
# run cannot get past (a payment, a dead posting, a check nobody solved, a page
# that will not move). The `asks_for_prohibited` and `has_captcha` flags are
# recorded and never park on their own: the catalog holds only what the user
# chose to share, so a question it cannot answer is the required-field park,
# and a captcha widget that does not block the page is no reason to stop.

# How each gate was read (SP8b, 2026-09-25): `scripts/jev_thresholds.py` over
# the two committed caches (the runner tests' `cache.json`, 72 requests, and
# the flow matrix's `matrix_cache.json`, 322 requests: 3,241 answers from the
# live judge over every runner fixture and all 107 matrix flows) and the page
# reads of the matrix's real column (237) and the 60 local captures. A floor
# moved only where a right answer fell under it (a real miss) or a wrong one
# cleared it (a real false pass). Per gate:
#   PAGE_STATE_MIN_CONF 0.40   297 combined reads, min 0.46, median 1.00; none under
#                              the gate. The captures' 60 labelled pages: 54 right
#                              (min 0.47), 6 wrong at 0.58 to 1.00 (three closed UKG
#                              postings read as postings, two email-first sign-in
#                              screens read as forms, a bot block read as dead). No
#                              floor separates those, so it stays.
#   CONFIRMATION_MIN_CONF 0.60 23 real confirmation reads, min 0.71.
#   FIELD_MAP_MIN_CONF 0.70    373 answers, median 1.00; 12 under the gate, none a
#                              fill the run needed: 7 on boxes `quick_map` answers
#                              first (City 0.48 and 0.49, State 0.65, a state question
#                              0.55, three email boxes 0.46 to 0.66), a password box
#                              (no fact ever lands in one), three optional boxes whose
#                              top pick is leave_blank (a cover letter file 0.37,
#                              pronouns 0.67, Headline 0.59), and a signature that
#                              split full_name 0.61 / signature_today 0.29 (both type
#                              the name: `pooled_confidence` fills it at 0.90). At or
#                              above it one wrong pick, a "Create a password" box read
#                              as email at 0.80, which the password rule overrides.
#   CONSENT_MIN_CONF 0.85      9 answers: eight real consents at 0.92 to 1.00, and a
#                              text-message opt-in at 0.62 the gate rightly held off.
#   OPTION_MIN_CONF 0.70       26 answers, median 1.00; under it "United States of
#                              America" 0.57 (the long list is matched in code first)
#                              and an optional country code "+1" 0.51 (left blank).
#   BUTTON_SUBMIT_MIN_CONF 0.50 (0.90 -> 0.75 on 2026-09-22, 0.75 -> 0.50 now) 74
#                              submit picks: a single-page form's "Submit application"
#                              0.60 (0.70 and 0.74 in the first recording, and 0.71 on
#                              the runner's quiet-submit form), then 0.78 and above.
#                              Each low read parked a true submit, 23 matrix flows and
#                              3 runner tests: a real miss. No other button was picked
#                              submit; the highest submit probability on one was 0.31
#                              ("Notify me", picked other).
#   BUTTON_ADVANCE_MIN_CONF 0.50 72 answers, median 1.00; one under it, Workday's "Use
#                              My Last Application" 0.35, which the run must not click.
#   BUTTON_SENDS_MIN 0.40      (0.80 -> 0.40 now) 18 answers: every posting's Apply
#                              entry and "Apply Manually" 0.08 to 0.15; the true sends
#                              a signup form's "Create account and apply" 0.48 and a
#                              form's own "Apply" 0.55 and 0.67, real misses at 0.80
#                              before and after the true criterion named the account
#                              click. The DOM rule still holds an entry off: an Apply
#                              counts as the submit only beside the fields this run
#                              filled or on a review page after the filled form.
#   VERIFY_MIN 0.80            46 answers, 45 at 0.90 and above; the one no (0.05) is a
#                              headline the site's resume parse typed, which the
#                              sheet does not hold. Pasted letters and search boxes'
#                              matches are compared in code (`shaped_holds`).
#   PLACEHOLDER_MAX 0.50       46 answers, max 0.20.
#   GROUNDING_MIN 0.70         grounded sentences 0.98, the invented one 0.02.
#   INBOX_MIN 0.50             22 answers: the site's code or link mail from_site 0.81
#                              to 0.89, has_code 0.97 and 0.98, has_link 0.99; every
#                              decoy's from_site 0.05 to 0.11 (another ATS's code mail
#                              0.11, its has_code 0.98, so the pair holds it off). The
#                              link pick chose the account's link at 1.00 over the job
#                              alerts' link, and both code picks were 1.00.
#   `asks_for_prohibited` (no gate): 126 answers, 0.13 at most but for the SSN
#                              page's 0.99.
# (SP8, 2026-09-22, on 38 requests: PAGE_STATE_MIN_CONF 0.60 -> 0.40,
# BUTTON_ADVANCE_MIN_CONF 0.75 -> 0.50 and MAX_PAGES 12 -> 20 at the user's call
# to stop only where the run cannot go on; PROHIBITED_MAX and CAPTCHA_MAX went
# with the flag parks.)

HEADLINE_CHARS = 1200           # of the page text sent as `page.headline_text` (600
                                # until 2026-09-22: LinkedIn's posting began past it)

# The page read (SP4): its own small request, first on every page. Code
# combines its answers with the page's structure (`page_facts`) into the read
# (`read_page`): a kind's evidence is the judge's probability for it times
# `READ_CHOICE_WEIGHT`, plus its Nouls (the mean of (noul - 0.5) x 2 over the
# kind's Nouls, so a sure no counts against it), plus what the page's boxes,
# buttons, words and URL say (`STRUCT_*`). The read is the kind with the most
# evidence. Its confidence starts from the judge's own (its confidence when the
# read is its pick, else its probability for the kind), rises by
# `READ_SUPPORT_WEIGHT` per unit of the Nouls' and the structure's word for
# the kind, falls by `READ_AGAINST_WEIGHT` per unit of their word against it
# and by `READ_SUPPORT_WEIGHT` per unit they give the strongest other kind:
# with no Noul and no structure speaking it is the judge's own. A judge sure of
# its read (0.75 and up) outweighs any one structural fact with a Noul
# against it; a misread at 0.30 to 0.60 gives way to the true kind's Nouls and
# structure. Not yet tuned against live answers (SP8).
READ_HEAD_CHARS = 1000          # of the page text sent as `page.head`
READ_LIST_CAP = 12              # field labels and button texts listed in the read
READ_TEXT_CAP = 80              # characters per label or button text in the read
READ_CHOICE_WEIGHT = 2.0
READ_SUPPORT_WEIGHT = 0.25
READ_AGAINST_WEIGHT = 0.5
STRUCT_DECISIVE = 1.0           # a code box, a new-password box, a file box, received words
STRUCT_SUPPORT = 0.5
STRUCT_HINT = 0.3               # the URL's shape
STRUCT_AGAINST = -1.0           # the page cannot be the kind (a sign-in with no box)
STRUCT_RULED_OUT = -2.0         # a confirmation beside a form with no received words
                                # (SP3's rule: never the send's confirmation)
READ_NOUL_MIN = 0.50            # a read Noul at or above it says yes
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
                "upload or screening questions, with a way to continue or submit; also the "
                "application's privacy agreement or data consent step, accepted to go on",
        "not_for": "The job description before the Apply button; a sign-in screen",
        "examples": ["Apply for Software Engineer: First Name, Last Name, Email, Resume",
                     "Application step 2 of 3: work authorization questions",
                     "Privacy Agreement: I Accept, I Decline"]},
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
    "needs_generation": "An open-ended prompt about motivation, the role or the candidate; "
                        "a draft is written for it",
    "leave_blank": "No source applies; the box is left blank",
}

# --- consent ticks: routine or not (controller decision, SP5 round 2) ----------------------------
# `CONSENT_MIN_CONF` was never calibrated, and under the noise model it parked
# every third required privacy box; a live judge reads some routine boxes under
# it too. A `consent_attest` read ticks at `FIELD_MAP_MIN_CONF` only when all of
# these hold (a strict allowlist, review R3-I1); any other keeps
# `CONSENT_MIN_CONF`:
# - the box is required (an optional consent is an opt-in the user never asked
#   for: a talent network, job alerts);
# - the extractor read its label whole (`Field.label_partial` is off: no cut at
#   its cap, no button or widget words skipped inside the label);
# - the label names at least one routine thing (the application's privacy
#   notice, policy or statement; the site's terms; the processing or storing
#   of the application's data; that the information given is true, accurate or
#   complete; contact about this application, role or position, never "any"
#   role or "by email") and no commitment word;
# - every word of it (any script: `[^\W_]+`) is a routine word, a word of
#   assent or a function word, the job's company (its name before a routine
#   noun or a possessive: "the Fabrikam Privacy Notice", "Fabrikam's Privacy
#   Policy"). No other name is taken: "the Biometric Privacy Policy", "the
#   Talent Network Terms" and a vendor's "Checkr's Privacy Policy" are no
#   routine consents (review round 4, M2);
# - it does not end on a function word ("... and the"): such a label was cut
#   (review round 4, M1).
CONSENT_RULES: dict[str, Any] = {
    # at least one routine thing is named
    "routine": re.compile(
        r"\bprivacy\b|\bterms\b|\bconditions\b|\b(process|stor|retain|retention|collect)\w*"
        r"|\b(true|truthful|accurate|complete|correct)\b|\bcontact\w*", re.I),
    # a commitment: any of these words keeps CONSENT_MIN_CONF
    "commitment": re.compile(
        r"background|criminal|\bcredit\b|\bdrug|\bage\b|\baged\b|\b18\b|\bminors?\b"
        r"|non[\s-]?compet|relocat|arbitrat|\bsms\b|\btext(s|ing)?\b|marketing|newsletter"
        r"|\bmedical\b|\bphysical\b|\bscreening\b|\bmonitor\w*|\bsurveillance\b"
        r"|biometric|disclosure|recording|assessment", re.I),
    # a contact consent is routine only about this application, role or position
    "contact": re.compile(r"\bcontact\w*", re.I),
    "this_role": re.compile(r"\bthis\s+(application|role|position)\b", re.I),
    # the words a routine label may use
    "words": frozenset("""
        i we you me my our your us the a an to of for and or in on with by at from that this
        these those it its is are be been being am was were have has had hereby here herein
        above below all as such so s which who how what where when per under via within
        agree agreed agreeing agrees accept accepted accepting acknowledge acknowledged
        acknowledging consent consented consenting confirm confirmed confirming certify
        certified certifying attest attested affirm affirmed declare declared understand
        understood read reviewed review check checking box ticking tick clicking click
        submit submitting
        privacy notice notices policy policies statement statements terms term conditions
        condition use usage service services legal
        data personal information info details application applications applying apply
        candidate candidates applicant applicants candidacy recruitment recruiting hiring
        process processes processed processing store stores stored storing storage retain
        retained retaining retention collect collects collected collecting collection handle
        handled handling used using keep kept
        true truthful accurate complete correct best knowledge provided given supplied
        submitted entered contained stated
        contact contacted contacting regarding about concerning
        role position job opening vacancy
        describes explains sets out outlined described
        """.split()),
    # the words the job's company may stand before ("the Fabrikam Privacy Notice")
    "named": frozenset("privacy notice notices policy policies statement terms candidate "
                       "candidates applicant applicants recruitment careers career processing "
                       "collecting storing using".split()),
    # the legal suffixes of a company's name ("Adatum Corporation, Inc.")
    "suffixes": frozenset("inc incorporated llc ltd limited corp corporation co company gmbh "
                          "plc sa ag lp llp".split()),
    # a label this long is at the extractor's cap: read as cut
    "max_chars": 300,
    # a label that ends on one of these was cut before its object
    "dangling": re.compile(r"\b(the|a|an|and|or|to|of|for|with|by|in|on|at|from)\W*$", re.I),
}


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", str(text or ""))


def routine_consent(label: str, *, company: str = "", partial: bool = False) -> bool:
    """Whether a consent box's own label names only routine things
    (`CONSENT_RULES`, the allowlist above): the label read whole (`partial`
    off), under the extractor's cap, naming a routine thing and no
    commitment, not ending on a function word, a contact consent only
    about this application, role or position, and every word a routine one
    or the job's `company` before a routine noun or a possessive."""
    rules = CONSENT_RULES
    text = " ".join(str(label or "").split())
    if partial or not text or len(text) >= rules["max_chars"] \
            or not rules["routine"].search(text) or rules["commitment"].search(text) \
            or rules["dangling"].search(text):
        return False
    if rules["contact"].search(text) and not rules["this_role"].search(text):
        return False
    tokens = _words(text)
    firm = [w.lower() for w in _words(company) if w.lower() not in rules["suffixes"]]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        # (an "s" only as a possessive: "the candidate's data")
        if token.lower() in rules["words"] and (token.lower() != "s" or i and re.search(
                re.escape(tokens[i - 1]) + r"['\u2019]s\b", text)):
            i += 1
            continue
        taken = _company_at(tokens, i, firm)
        if taken:
            i += taken
            continue
        return False
    return True


def _company_at(tokens: list[str], i: int, firm: list[str]) -> int:
    """How many tokens from `i` are the job's company (`firm`: its words
    without legal suffixes) and its suffixes, standing before a routine noun
    or a possessive; 0 when they are not."""
    if not firm or [t.lower() for t in tokens[i:i + len(firm)]] != firm:
        return 0
    j = i + len(firm)
    while j < len(tokens) and tokens[j].lower() in CONSENT_RULES["suffixes"]:
        j += 1
    if j < len(tokens) and tokens[j].lower() == "s":
        return j + 1 - i
    if j < len(tokens) and tokens[j].lower() in CONSENT_RULES["named"]:
        return j - i
    return 0


def consent_floor(label: str, *, required: bool = False, partial: bool = False,
                  company: str = "") -> float:
    """The confidence a `consent_attest` read of the box needs to tick it:
    `FIELD_MAP_MIN_CONF` for a required box whose whole label is a routine
    consent (`routine_consent`), else `CONSENT_MIN_CONF`."""
    if required and routine_consent(label, company=company, partial=partial):
        return FIELD_MAP_MIN_CONF
    return CONSENT_MIN_CONF


# A login wall's sign-in button and a signup form's create-account button are
# `advance` (spec 3.5): they move the flow forward without sending the
# application. `advance` and `submit` say what they are not for: they are the
# confusable pair, and a wizard's step button is an HTML submit control too.
_BUTTON_CRITERIA: dict[str, dict[str, Any]] = {
    "advance": {
        "what": "The next step: the next form page, signing in, creating the account, "
                "continuing with an email, accepting the application's privacy agreement",
        "not_for": "The final send of the finished application",
        "examples": ["Next", "Continue", "Save and continue", "Sign in", "Log in",
                     "Create account", "Continue with email", "I Accept"]},
    "submit": {
        "what": "Sends the finished application from its last page",
        "not_for": "A step button that opens the next form page: Continue, Next, "
                   "Save and continue",
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
NOT_SETTLED_DESCRIPTION = ("the field asks something the saved answers do not say, or its right "
                           "option turns on it, or it is unclear")

# The subtle boundary of `button_{n}_sends` (the Jev guide: a Noul's
# true / false criteria pin it down).
_SENDS_CRITERIA = {
    "true": "clicking it sends the finished application to the employer, also when the "
            "same click creates the candidate's account",
    "false": "it opens, starts or continues the application, or leaves the page",
}

_APPLY_WORD = re.compile(r"\bapply\b", re.I)


def apply_worded(text: str) -> bool:
    """A control whose only send word is "apply" ("Apply", "Apply now",
    "Apply Manually"): a posting's entry and a form's final button read the
    same, so the text alone never makes it the submit."""
    return bool(_APPLY_WORD.search(text or "")) and not SEND_WORDS.search(text or "")


def read_sends(answers: Mapping[str, Answer], n: int) -> float:
    """The `button_{n}_sends` Noul: clicking the Apply-worded button `n`
    sends the finished application (0.0 when it was not asked)."""
    return noul_of(answers, f"button_{n}_sends")

# Which sources a control can take, by its type. A file input takes a file
# and nothing else; a select / radio / listbox never takes a name, an email,
# a URL or prose; `today` and `signature_name` reach a form through the
# `signature_today` special. Fewer confusable options per question, and the
# request stays inside its size budget on a long page.
_FILE_TYPES = frozenset(("file",))
_OPTION_TYPES = frozenset(("select", "radio", "checkbox", "listbox"))
_ALWAYS = ("needs_generation", "leave_blank")
_FILE_SOURCES = ("resume_file", "cover_letter_file")
_CHECKBOX_SOURCES = ("consent_attest",)
# Specials that stand for a catalog value: offered (and described in `facts`)
# only when the catalog has that value, so the model cannot pick a file that
# is not on disk or a cover letter the sheet does not carry.
_BACKED_SPECIALS = frozenset(("resume_file", "cover_letter_file", "cover_letter_text"))
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
    checkbox, a backed special only when the catalog has its value."""
    if type_ in _FILE_TYPES:
        keys = [k for k in _FILE_SOURCES if k in catalog_keys] + list(_ALWAYS)
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
        keys += [k for k in SPECIAL_SOURCES if k not in keys and k not in _NEVER_IN_TEXT
                 and k not in _BACKED_SPECIALS]
    return {k: None for k in keys}


def _facts_map(catalog: FactCatalog) -> dict[str, Any]:
    """`state.facts`: the special sources' descriptions (a backed special only
    when the catalog has its value) under the catalog's own descriptions."""
    specials = {k: v for k, v in SPECIAL_DESCRIPTIONS.items()
                if k not in _BACKED_SPECIALS or catalog.has(k)}
    return {**specials, **catalog.to_criteria()}


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


# --- options: a value matched in code first (EXT-07, FILL-07) ---------------------------

AUTOFILL_PARSER = "autofill parser"     # the extractor's help for a resume parser's upload

# _US_STATES (code -> full name, DC included) is the answer store's own list
# (`resume_tailor.answer_tables.US_STATES`, imported above): a saved state and a
# form's state options are matched against the same names.
# Names one answer goes by: each set is one meaning (normalised as `_norm_option` does)
_ALIASES: tuple[frozenset[str], ...] = tuple(frozenset(g) for g in (
    ("united states", "us", "usa", "u s", "u s a", "united states of america", "america"),
    ("united kingdom", "uk", "u k", "great britain", "gb", "britain"),
    YES_FORMS, NO_FORMS,
    *(((name.lower(), code.lower())) for code, name in _US_STATES.items()),
))
# the "I decline to answer" family: a stored decline matches any of them. A
# negation counts only when it refuses to answer ("I do not wish to
# self-identify"); "I do not identify as a protected veteran" is a statement
# and no decline (cycle 18, FM-7)
_REFUSED = r"(?:self )?(?:answer|disclose|identify|say|specify|provide|respond|share)\b"
_DECLINE = re.compile(r"\bdecline\b|\bprefer(?:s)? not\b"
                      r"|\b(?:do|does) not (?:wish|want|care|choose|like) to " + _REFUSED
                      + r"|\b(?:don|doesn) t (?:wish|want|care|choose|like) to " + _REFUSED
                      + r"|\b(?:choose|chooses|wish|wishes|elect|elects) not to " + _REFUSED
                      + r"|\bnot (?:to )?(?:answer|disclose|say)\b", re.I)
_OPTION_NORM = re.compile(r"[^a-z0-9]+")
_YES, _NO = _ALIASES[2], _ALIASES[3]


def _norm_option(text: str) -> str:
    return " ".join(_OPTION_NORM.sub(" ", str(text or "").lower().replace("'", " ")).split())


def declines(text: str) -> bool:
    """`text` is a decline ("I decline to answer", "Prefer not to say")."""
    return bool(_DECLINE.search(_norm_option(text)))


def _alias_set(text: str) -> frozenset[str]:
    n = _norm_option(text)
    for group in _ALIASES:
        if n in group:
            return group
    return frozenset((n,))


def yes_no(value: str) -> bool:
    """Is `value` a yes or a no (Yes, Y, True; No, N, False)?"""
    return _alias_set(value) in (_YES, _NO)


def match_option(value: str, options: list[str], *, exact: bool = False) -> str | None:
    """The option that means `value`, found in code over every option (a
    Country list of 250, the United States near its end): the same words
    (case, punctuation and spacing aside), then a name the value goes by
    (US / USA / United States of America; a state's name and its postal
    code; Yes / Y), then, for a stored decline, the one option that declines
    ("Prefer not to say"), then the one option that starts with the value
    ("California (CA)"). None when nothing matches or two options do.

    A yes or a no (`yes_no`), and any value with `exact`, stops after the
    names it goes by (cycle 18, FM-1 and FM-2): "Yes - on a work visa" is no
    "Yes", and a qualified option is the judge's pick."""
    want = _norm_option(value)
    if not want or not options:
        return None
    normed = [_norm_option(o) for o in options]
    same = [o for o, n in zip(options, normed) if n == want]
    if len(same) == 1:
        return same[0]
    aliases = _alias_set(value)
    named = [o for o, n in zip(options, normed) if n in aliases]
    if len(named) == 1:
        return named[0]
    if exact or aliases in (_YES, _NO):
        return None
    if _DECLINE.search(want):
        declines = [o for o, n in zip(options, normed) if _DECLINE.search(n)]
        if len(declines) == 1:
            return declines[0]
    starts = [o for o, n in zip(options, normed) if n.startswith(want + " ")]
    return starts[0] if len(starts) == 1 else None


def code_pick(value: str, options: list[str]) -> str | None:
    """The option code settles for `value` with no judge (cycle 18, FM-1): a
    yes or a no on a list holding its own Yes / No, and on a list past
    `OPTIONS_CAP` the option with the same words or a name the value goes
    by. None leaves the pick to the judge (or, past the cap, blank)."""
    if not options or not (yes_no(value) or len(options) > OPTIONS_CAP):
        return None
    return match_option(value, options, exact=True)


# a status an option names (a citizen, a permanent resident, a green card, a
# visa): the yes / no lines say Yes to every such option that starts with a
# yes, and the saved authorization statement tells them apart (final review M5)
_STATUS_WORDS = re.compile(r"\b(?:citizens?(?:hip)?|permanent residen(?:t|ce|cy)|green card"
                           r"|visas?|h-?1b|opt|cpt|ead|work permit)\b", re.I)
_YES_LEAD = re.compile(r"^\W*(?:yes|y)\b", re.I)
STATEMENT_KEY = "answer_authorization_statement"
STATEMENT_LINE = "Work authorization statement, in the candidate's own words"


def status_choices(options: list[str] | tuple[str, ...]) -> bool:
    """Do two options or more start with a yes and name a status
    (`_STATUS_WORDS`: "Yes, I am a U.S. citizen", "Yes, I have a work
    visa")? The yes / no lines cannot pick among them."""
    return sum(bool(_YES_LEAD.match(str(o)) and _STATUS_WORDS.search(str(o)))
               for o in options or ()) >= 2


def candidate_answer(catalog: FactCatalog | None, key: str, value: str,
                     options: list[str] | tuple[str, ...] = ()) -> str:
    """The answer a pick question carries for `key`'s `value`. For a yes /
    no fact it is every stored yes / no fact the catalog holds, one line each
    and `key` first ("Legally authorized to work ...: Yes"), so a combined
    option ("Yes, I am authorized and need no sponsorship") is read against
    the whole story; a derived fact (`apply_facts.DERIVED_YES_NO`) leads its
    own pick and adds no line to another's, since the stored lines already
    say it. Among `options` naming a status after a yes (`status_choices`),
    the saved authorization statement is the last line (`STATEMENT_LINE`,
    final review M5); any other list's answer is the same with or without
    `options`. Any other fact carries its value alone. Each line carries
    the user's note on its answer when there is one (`_noted`)."""
    if catalog is None or key not in YES_NO_KEYS:
        return _noted(catalog, key, value)
    lines = [_noted(catalog, key, f"{DESCRIPTIONS[key]}: {value}")]
    lines += [_noted(catalog, k, f"{DESCRIPTIONS[k]}: {catalog.value(k)}")
              for k in STORED_YES_NO_KEYS if k != key and catalog.has(k)]
    if catalog.has(STATEMENT_KEY) and status_choices(options):
        lines.append(f"{STATEMENT_LINE}: {catalog.value(STATEMENT_KEY)}")
    return "\n".join(lines)


def _noted(catalog: FactCatalog | None, key: str, line: str) -> str:
    """`line` with the user's note on `key`'s answer after it, when there
    is one (`FactCatalog.note`), so the judge reads what the user meant."""
    note = catalog.note(key) if catalog is not None else ""
    return f"{line} (the candidate's note: {note})" if note else line


_PHONE_NAMED = re.compile(r"phone|mobile|(?<![a-z])tel", re.I)


def phone_named(*names: str) -> bool:
    """Do a box's label or name say phone ("Phone", "mobile_no", "tel",
    "telNumber"; "Hotel nights" does not)?"""
    return bool(_PHONE_NAMED.search(" ".join(str(n or "") for n in names)))


def shortlist(options: list[str], value: str, cap: int = OPTIONS_CAP) -> list[str]:
    """At most `cap` of `options` for a pick question (EXT-07): the ones
    sharing a word with `value` or a name it goes by first, the rest in page
    order after them."""
    if len(options) <= cap:
        return list(options)
    words = set(_norm_option(value).split())
    for alias in _alias_set(value):
        words |= set(alias.split())
    decline = bool(_DECLINE.search(_norm_option(value)))

    def score(o: str) -> int:
        n = _norm_option(o)
        return len(words & set(n.split())) + (2 if decline and _DECLINE.search(n) else 0)
    ranked = sorted(range(len(options)), key=lambda i: (-score(options[i]), i))
    return [options[i] for i in ranked[:cap]]


def _sections(fields) -> list[dict[str, Any]]:
    """The page's section headings in order, each with the fields under it
    (READ-05): [{"heading": ..., "fields": [n, ...]}]; a field under no
    heading is in none."""
    out: list[dict[str, Any]] = []
    for f in fields:
        heading = str(getattr(f, "section", "") or "")[:READ_TEXT_CAP]
        if not heading:
            continue
        if out and out[-1]["heading"] == heading:
            out[-1]["fields"].append(f.n)
        else:
            out.append({"heading": heading, "fields": [f.n]})
    return out


def _option_question(i: int, options: list[str], answer: str,
                     value: str | None = None) -> dict[str, Any]:
    """The pick among a field's options for a known fact value; the answer
    (`candidate_answer()`'s text, every yes / no fact for one of them)
    rides in the instruction as `candidate_answer` so the question is
    self-contained. A list past `OPTIONS_CAP` is cut to the shortlist for
    `value` (the fact's own value; the answer when None)."""
    criteria: dict[str, Any] = {
        o: None for o in shortlist(options, answer if value is None else value)}
    criteria["no_match"] = NO_MATCH_DESCRIPTION
    instructions = {
        "candidate_answer": answer,
        "question": f"Which of these choices means the same as `candidate_answer`, "
                    f"as an answer to the form field `fields[{i}].label`?"}
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def page_questions(digest: FormDigest, catalog: FactCatalog,
                   job: Mapping[str, Any] | None = None, *,
                   fields: bool = True) -> tuple[dict, dict]:
    """The page's mapping, asked once the read (`read_questions`) says the
    run acts on the page. State: the job, the page's host / title / headline,
    the compact fields and buttons, and `facts`, the description of every
    source key a field can take (the catalog's facts and the specials; never
    a value). Questions: `field_{n}_source`, `field_{n}_option` for a field
    with options whose fact `quick_map` knows, `button_{n}_role`,
    `button_{n}_sends` for an Apply-worded button, `asks_for_prohibited`.
    With `fields` False (a posting with no field) the buttons alone."""
    job = job or {}
    text = (digest.text or "")[:PAGE_TEXT_CAP]
    state: dict[str, Any] = {
        "job": {"company": str(job.get("company_name") or job.get("company") or ""),
                "title": str(job.get("job_title") or job.get("title") or "")},
        "page": {"url_host": digest.url_host, "title": digest.title,
                 "headline_text": text[:HEADLINE_CHARS]},
        "fields": [_compact_field(f) for f in digest.fields] if fields else [],
        # the text and three DOM flags (READ-10): the extractor's `kind_hint`
        # is a regex guess ("Apply now" and a wizard's Continue both read
        # `submit`) and the live judge took the word at face value (SP8:
        # apply_entry 0.55 / submit 0.45, advance 0.72 / submit 0.28), so it is
        # left out; `apply_run._submit_shaped` guards on text. `in_form`: the
        # button's form holds the fields; `disabled`: it waits for the form to
        # validate; `primary`: styled as the main action
        "buttons": [{"n": b.n, "text": b.text, "in_form": bool(b.in_form),
                     "disabled": bool(getattr(b, "disabled", False)),
                     "primary": bool(getattr(b, "primary", False))} for b in digest.buttons],
    }
    sections = _sections(digest.fields) if fields else []
    if sections:
        # READ-05: the headings the fields sit under ("Voluntary
        # Self-Identification", "Eligibility"), beside the fields: a field
        # question names `fields[i]` alone
        state["sections"] = sections
    if fields:
        state["facts"] = _facts_map(catalog)
    questions: dict[str, Any] = {}
    catalog_keys = list(catalog.to_criteria())
    for i, f in enumerate(digest.fields if fields else ()):
        questions[f"field_{f.n}_source"] = {
            "type": "choice",
            "instructions": f"Which key of `facts` describes what `fields[{i}]` asks for? "
                            "When nothing fits, `leave_blank`; for an essay question no fact "
                            "answers, `needs_generation`.",
            "criteria": _source_criteria(catalog_keys, f.type),
        }
        if f.options:
            # only a field whose fact quick_map knows has a value to match
            # here; a model-mapped field's pick is the second request's
            # `field_{n}_pick`. Asking for a "default" pick with no value in
            # hand was a coin toss the live judge answered at 0.00 confidence
            # (SP8) and `plan` never read.
            # A pick code settles (`code_pick`) asks nothing (cycle 18, FM-1)
            key = quick_map(f.label, f.id_or_name, f.type)
            if (key and catalog.has(key)
                    and code_pick(catalog.value(key), f.options) is None):
                questions[f"field_{f.n}_option"] = _option_question(
                    i, f.options, candidate_answer(catalog, key, catalog.value(key), f.options),
                    catalog.value(key))
    for i, b in enumerate(digest.buttons):
        questions[f"button_{b.n}_role"] = {
            "type": "choice",
            "instructions": f"Which role does the clickable control `buttons[{i}]` have?",
            "criteria": {role: dict(desc) for role, desc in _BUTTON_CRITERIA.items()},
        }
        if apply_worded(b.text):
            # "Apply" names a posting's entry, Workday's "Apply Manually" and a
            # form's final button alike: the runner takes one as the submit
            # only with this answer and the DOM evidence (`BUTTON_SENDS_MIN`)
            questions[f"button_{b.n}_sends"] = {
                "type": "noul",
                "instructions": f"The text of `buttons[{i}]` says apply. Would clicking it send "
                                "the finished application to the employer, as the last step "
                                "of applying?",
                "criteria": dict(_SENDS_CRITERIA),
            }
    if fields:
        questions["asks_for_prohibited"] = {
            "type": "noul",
            "instructions": "Does `page` or any of `fields` ask for a social security number, "
                            "a birthdate, banking or payment details, or a government "
                            "identification document?",
        }
    return state, questions


def page_requests(digest: FormDigest, catalog: FactCatalog,
                  job: Mapping[str, Any] | None = None, *,
                  fields: bool = True) -> list[tuple[dict, dict]]:
    """`page_questions` as requests that fit Jev's limits (RES-03,
    `jev.request_fits`): the one request when it fits; else without the
    buttons in the site's header, nav or top bar, which sit outside the
    application and whose roles never beat the page's own (`plan`); else
    the fields halved, again and again until every request fits or holds a
    single field, the first part with the buttons and every part with its
    own `fields` (its questions name them by their place in it)."""
    state, questions = page_questions(digest, catalog, job, fields=fields)
    if request_fits(state, questions):
        return [(state, questions)]
    kept = [b for b in digest.buttons if not getattr(b, "chrome", False)]
    slim = dataclasses.replace(digest, buttons=kept)
    if not fields or len(digest.fields) < 2:
        return [page_questions(slim, catalog, job, fields=fields)]
    return _field_parts(slim, catalog, job)


def _field_parts(digest: FormDigest, catalog: FactCatalog,
                 job: Mapping[str, Any] | None) -> list[tuple[dict, dict]]:
    state, questions = page_questions(digest, catalog, job)
    if request_fits(state, questions) or len(digest.fields) < 2:
        return [(state, questions)]
    mid = len(digest.fields) // 2
    first = dataclasses.replace(digest, fields=digest.fields[:mid])
    second = dataclasses.replace(digest, fields=digest.fields[mid:], buttons=[])
    return _field_parts(first, catalog, job) + _field_parts(second, catalog, job)


def merge_answers(parts: list[Mapping[str, Answer]]) -> dict[str, Answer]:
    """The answers of a mapping asked in parts (`page_requests`) as one; a
    Noul every part asks (`asks_for_prohibited`) keeps its highest yes."""
    out: dict[str, Answer] = {}
    for got in parts:
        for qid, a in got.items():
            held = out.get(qid)
            if (held is not None and held.noul is not None and a.noul is not None
                    and held.noul >= a.noul):
                continue
            out[qid] = a
    return out


# --- the page read (SP4) -----------------------------------------------------------

# The words a page shows once an application was received: with the judge's
# read, the deterministic half of a confirmation (after the submit click only
# when they were not on the page before it). "thanks for your interest" is a
# posting's greeting too, so it is left out.
CONFIRMATION_WORDS = re.compile(
    r"thank(?:s| you) for (?:applying|your application|submitting)"
    r"|application (?:has been |was |is )?(?:received|submitted|sent|complete)"
    r"|we(?:'ve| have) received your application"
    r"|successfully (?:applied|submitted)|you(?:'ve| have) (?:successfully )?applied", re.I)
# A job applied to before (TERM-04), a posting that takes no more applications
# (READ-08), a review of the answers, an error page, a bot check.
ALREADY_APPLIED_WORDS = re.compile(
    r"already (?:applied|submitted (?:an|your) application|have an application)"
    r"|you(?:'ve| have) (?:previously|already) applied"
    r"|application (?:is )?already on file|you applied (?:for|to) this", re.I)
# (the past tense only: "open until the position is filled" is an open posting)
CLOSED_WORDS = re.compile(
    r"no longer (?:accepting|taking) applications|no longer (?:available|open|active|posted)"
    r"|(?:position|job|role|posting|requisition) (?:has been|was) (?:filled|closed|removed)"
    r"|(?:this|the) (?:job|position|posting) (?:has )?(?:expired|closed)\b"
    r"|job (?:not found|no longer exists)|page (?:not found|doesn't exist|does not exist)", re.I)
# a job description's headings: the posting's own words
DESCRIPTION_WORDS = re.compile(
    r"\b(responsibilities|qualifications|requirements|about the (?:role|job|position|team)"
    r"|what you(?:'ll| will) do|who you are|job description|job summary)\b", re.I)
REVIEW_WORDS = re.compile(
    r"review (?:your|the) (?:application|answers|details|information)|please review"
    r"|review and submit|review before submitting", re.I)
ERROR_WORDS = re.compile(
    r"something went wrong|an (?:unexpected )?error (?:has )?occurred|(?:internal )?server error"
    r"|access denied", re.I)
# (a challenge's own words; never "This site is protected by reCAPTCHA", the
# notice of an invisible check that asks nothing of the person)
CAPTCHA_WORDS = re.compile(
    r"not a robot|verify (?:that )?you(?:'re| are) (?:a )?human|are you (?:a )?human"
    r"|human verification|complete the (?:security )?(?:check|challenge)|captcha challenge", re.I)
# An Apply that sends a stored profile from another site instead of opening
# the company's form: Easy Apply (LinkedIn's, or a board's "Easy apply"),
# "Apply with LinkedIn / Indeed / Glassdoor / ZipRecruiter ...", and a
# one-click apply, which sends at once. Never an Apply entry the run clicks. A
# "Quick apply" on the application's own site opens its short form and is an
# entry like any other.
PROFILE_APPLY = re.compile(
    r"easy\s*apply|apply\s+(with|using|via|through)\s+(your\s+)?"
    r"(linkedin|indeed|glassdoor|ziprecruiter|seek|xing|google|facebook|monster|dice)"
    r"|(1|one)[\s-]*click\s+apply", re.I)
# A sign-in or a profile from another site ("Continue with LinkedIn", "Sign
# in with Google", "Apply using Indeed"): never a step's way on or a
# posting's entry (ADV-09); it leaves for that site
THIRD_PARTY = re.compile(
    r"\b(?:with|using|via|through)\s+(?:your\s+)?(?:linkedin|indeed|google|facebook|apple|"
    r"microsoft|github|glassdoor|twitter|x|yahoo|amazon|okta|sso)\b", re.I)
_APPLY_ENTRY_WORDS = re.compile(r"\bapply\b|\bi'?m interested\b|\bstart (?:your |an |the )?"
                                r"application\b", re.I)
# the loop's words for a send and for a sign-in's own button: one name each
SEND_WORDS = re.compile(r"\b(submit|send|finish)\b", re.I)
# a sign-in's own words: "Sign in", "Log on", "Send code", "Send me a link"
SIGN_IN_WORDS = re.compile(r"\b(sign|log)[\s-]*(in|on)\b|\blogin\b"
                           r"|\bsend\s+(me\s+)?(an?\s+|the\s+)?(verification\s+|sign[\s-]*in\s+)?"
                           r"(code|link)\b", re.I)
ADVANCE_WORDS = re.compile(r"\b(next|continue)\b|^\s*(i\s+)?(accept|agree)\b", re.I)
# A button that accepts an application's privacy agreement or data consent
# step (study G13: Taleo's "I Accept", Jobvite's "Accept"), and one that
# declines or leaves it: never a step's way on.
ACCEPT_WORDS = re.compile(r"^\s*(i\s+)?(accept|agree)\b", re.I)
DECLINE_WORDS = re.compile(r"\b(decline|disagree|reject|withdraw)\b|\bcancel\b"
                           r"|\bdo(?:n'?t| not)\s+(?:agree|accept)\b|\bno,?\s+thanks\b", re.I)
# the qualifier has to sit on the word "code": a Social Security Number box or
# a work authorization box carries the qualifier and is no place for the code
CODE_WORDS = re.compile(
    r"(?:verification|security|one[- ]?time|auth\w*)[ _-]*code|\botp\b|passcode", re.I)
NOT_CODE_WORDS = re.compile(
    r"zip|post\s*code|postal|country|promo|coupon|discount|referral|invite|area\s*code", re.I)
# A page's words that a verification link was emailed (ACC-05): "We sent a
# verification link", "Click the link in the email", "Check your inbox".
LINK_SENT_WORDS = re.compile(
    r"\b(?:verification|confirmation|activation|verify|confirm|activate)\w*\s+(?:link|e-?mail)\b"
    r"|\bclick(?:ing)?\s+(?:on\s+)?(?:the\s+)?link\b|\b(?:open|follow|use)\s+the\s+link\b"
    r"|\b(?:sent|e-?mailed)\s+(?:you\s+)?(?:a|an)\s+(?:\w+\s+){0,2}link\b"
    r"|\bcheck\s+your\s+(?:e-?mail|inbox)\b", re.I)
# A password box's words when it makes the password rather than signs in with it.
NEW_PASSWORD = re.compile(r"\b(create|new|choose|set|confirm|re-?enter|repeat|verify)\b"
                          r"|new[_-]?pass|confirm[_-]?pass", re.I)
# A box no application asks: a job board's search or sort, a job-alert or
# newsletter sign-up beside the posting (study G3), a sign-in's remember-me.
_NOT_APPLICATION = re.compile(r"search|keyword|(?<![a-z])alerts?(?![a-z])|subscri|newsletter"
                              r"|sort\s*by|filter|remember me|keep me signed|stay signed"
                              r"|show password", re.I)
_CARD_BOX = re.compile(r"card\s*number|credit\s*card|debit\s*card|\bcvv\b|\bcvc\b"
                       r"|expir(?:y|ation)\s*date", re.I)
# The URL shapes of the pages the run meets (known ATS paths and the plain
# words sites use), first match wins: a hint, never the read on its own.
_URL_SHAPES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (kind, re.compile(pattern, re.I)) for kind, pattern in (
        ("confirmation", r"/(thanks|thank-?you|confirmation|confirmed|success|submitted"
                         r"|application-?submitted)(/|$)"),
        ("signup_form", r"/(register|registration|sign-?up|create-?account|createaccount)(/|$)"),
        ("login_wall", r"/(login|log-?in|sign-?in|sso)(/|$)"),
        ("application_form", r"/(apply|application|apply-?now|applymanually|apply-?manually)"
                             r"(/|$)"),
        ("job_posting", r"/(jobs?|careers?|positions?|openings?|postings?|job-?details?"
                        r"|vacanc(y|ies))/[^/]+")))


def confirmation_words(text: str) -> set[str]:
    """The received phrases (`CONFIRMATION_WORDS`) a page's text shows,
    lowercased, typographic apostrophes read as plain ones."""
    plain = str(text or "").translate(APOSTROPHES)
    return {" ".join(m.group(0).lower().split()) for m in CONFIRMATION_WORDS.finditer(plain)}


_NOT_ENTRY = re.compile(r"\bapplied\b|\bapply\s+(filters?|changes|coupon|promo|discount)\b"
                        r"|\bhow\s+to\s+apply\b|\bapplication\s+status\b"
                        r"|\bapply\s+later\b|\bsave\s+for\s+later\b", re.I)


def entry_worded(text: str) -> bool:
    """Does a control's text read as a posting's Apply entry (READ-09): the
    word "apply" ("Apply", "Apply now", "Apply for this job"), "I'm
    interested" (SmartRecruiters) or "Start application"; never "Applying
    tips" or "Apply filters", "Applied", "Apply Later" or "Save for later"
    (study G4), a profile Apply (`PROFILE_APPLY`) or a send word ("Submit
    application")."""
    text = str(text or "").translate(APOSTROPHES)
    return (bool(_APPLY_ENTRY_WORDS.search(text)) and not _NOT_ENTRY.search(text)
            and not PROFILE_APPLY.search(text) and not SEND_WORDS.search(text))


_CONDITION = re.compile(r"(?:if|when|whether)\b", re.I)


def statement_words(pattern: re.Pattern, text: str, labels=()) -> str:
    """The first match of `pattern` in `text` the page states itself: never
    one inside a field's label (`labels`), never one in a question (its
    sentence ends with "?": "Already applied? Sign in", "Have you already
    applied to us before?"), never one in a condition (its clause opens with
    "if", "when" or "whether": "If you have already applied, sign in");
    "" when there is none."""
    plain = str(text or "").translate(APOSTROPHES)
    own = [" ".join(str(label or "").translate(APOSTROPHES).lower().split())
           for label in labels]
    for m in pattern.finditer(plain):
        rest = re.match(r"[^.!?\n]*([.!?\n]|$)", plain[m.end():])
        if rest is not None and rest.group(1) == "?":
            continue
        start = max(plain.rfind(c, 0, m.start()) for c in ".,;:!?\n") + 1
        if _CONDITION.match(plain[start:m.start()].strip()):
            continue
        words = " ".join(m.group(0).lower().split())
        if any(words in label for label in own):
            continue
        return " ".join(m.group(0).split())
    return ""


def _first_words(pattern: re.Pattern, text: str) -> str:
    m = pattern.search(str(text or "").translate(APOSTROPHES))
    return " ".join(m.group(0).split()) if m else ""


_CODE_TYPES = frozenset(("text", "number", "tel", "other", ""))


def code_field(fields):
    """The box the emailed code goes in.

    A code gate can carry a postal code, a country code, a referral or a
    promo box as well, and all of them read as "code". A field whose label or
    id names a verification, security, one-time, auth or OTP code wins; the
    plain "code" match is the fallback, with the address, referral and promo
    words excluded. `autocomplete` `one-time-code` is the strongest signal the
    DOM offers."""
    def blob(f):
        return f"{f.label} {f.id_or_name}"

    # a code is typed: a question of tick boxes or a list that names code
    # ("Which languages do you write code in?") is no code box (SP5: a
    # group's question is its label now)
    fields = [f for f in fields if str(getattr(f, "type", "") or "") in _CODE_TYPES]
    for f in fields:
        # one-character boxes side by side (ACC-06): the extractor's own
        # finding, whatever their label says
        if str(getattr(f, "widget", "") or "") == "otp":
            return f
    for f in fields:
        if str(getattr(f, "autocomplete", "")).lower() == "one-time-code":
            return f
    for f in fields:
        text = blob(f)
        if CODE_WORDS.search(text) and not NOT_CODE_WORDS.search(text):
            return f
    for f in fields:
        text = blob(f)
        if "code" in text.lower() and not NOT_CODE_WORDS.search(text):
            return f
    return None


def link_sent(digest: FormDigest) -> str:
    """ACC-05: the words a page says a verification link was emailed with
    (`LINK_SENT_WORDS`), on a page with no box to fill but tick boxes, no
    code box, no Apply entry (a posting's "click the link below to apply")
    and no received words (a thank-you's "we sent a confirmation email"):
    an account check by link, whose way on is the link in the email; ""
    else."""
    if code_field(digest.fields) is not None or any(f.type != "checkbox" for f in digest.fields):
        return ""
    text = f"{digest.title}\n{digest.text or ''}"
    if confirmation_words(text) or any(entry_worded(b.text) and not b.in_form
                                       for b in digest.buttons if not getattr(b, "chrome", False)):
        return ""
    return _first_words(LINK_SENT_WORDS, text)


_HEX_TOKEN = re.compile(r"[0-9a-fA-F-]+")


def path_shape(url: str) -> str:
    """The URL's path with its ids masked (a run of three digits or more
    `<n>`, a long hex or UUID token `<id>`); no query (a GET form puts
    answers there) and no fragment."""
    path = urlsplit(str(url or "")).path or "/"
    out = []
    for seg in path.split("/"):
        if seg.isdigit():
            seg = "<n>"
        elif len(seg) >= 16 and _HEX_TOKEN.fullmatch(seg) and any(c.isdigit() for c in seg):
            seg = "<id>"
        else:
            seg = re.sub(r"\d{3,}", "<n>", seg)
        out.append(seg)
    return "/".join(out)[:120]


def url_kind(url: str) -> str:
    """The page kind the URL's path suggests (`_URL_SHAPES`), or ""."""
    path = urlsplit(str(url or "")).path or ""
    for kind, pattern in _URL_SHAPES:
        if pattern.search(path):
            return kind
    return ""


@dataclass(frozen=True)
class PageFacts:
    """What the page's structure says, read by code from the digest
    (`page_facts`): the boxes by kind, the buttons by their words, the phrases
    the text shows, the URL's shape."""
    app_fields: int = 0         # boxes an application asks (no password, code, search box)
    files: int = 0
    passwords: int = 0
    new_password: bool = False  # a box that makes a password (autocomplete, confirm)
    current_password: bool = False
    email_first: bool = False   # an address box and nothing else but checkboxes
    code_box: bool = False
    card_box: bool = False
    apply_entries: int = 0      # Apply-worded controls outside any form, no profile Apply
    send_buttons: int = 0
    advance_buttons: int = 0
    received: str = ""          # the phrases the text shows (the first of each kind)
    already_applied: str = ""
    closed: str = ""
    review: str = ""
    description: str = ""       # a job description's heading words
    error: str = ""
    captcha: str = ""           # a bot-check phrase, or "a bot-check frame" the runner saw
    url_kind: str = ""
    text_chars: int = 0
    dialog: str = ""            # an open modal dialog's title (the extractor's)
    link_sent: str = ""         # the words a verification link was emailed with (ACC-05)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v}


def _password_kind(f) -> str:
    """"new password", "current password" or "password" for a password box,
    else ""."""
    if not password_box(f):
        return ""
    auto = str(f.autocomplete or "").lower()
    if auto == "new-password" or NEW_PASSWORD.search(f"{f.label} {f.id_or_name}"):
        return "new password"
    return "current password" if auto == "current-password" else "password"


_CENSUS_TYPES = {"text": "text", "email": "email", "tel": "phone", "url": "link",
                 "number": "number", "date": "date", "textarea": "long text",
                 "select": "choice", "radio": "choice", "listbox": "choice",
                 "checkbox": "checkbox", "file": "file upload"}


def census_kind(f, code) -> str:
    """A field's kind in the read's census: its password kind, "code" for the
    code box, else its type in plain words."""
    if code is not None and f is code:
        return "code"
    return _password_kind(f) or _CENSUS_TYPES.get(f.type, "other")


def page_facts(digest: FormDigest, url: str = "", *, captcha_frame: bool = False) -> PageFacts:
    """The page's structure (`PageFacts`) from its digest and URL;
    `captcha_frame`: the runner saw a bot-check provider's challenge frame."""
    code = code_field(digest.fields)
    passwords = [f for f in digest.fields if _password_kind(f)]
    kinds = [_password_kind(f) for f in passwords]
    # the page's own buttons: never the site's header or top bar (study G4)
    buttons = [b for b in digest.buttons if not getattr(b, "chrome", False)]
    # a send word other than a sign-in's ("Submit application", not "Send code")
    sends = [b for b in buttons if SEND_WORDS.search(b.text)
             and not SIGN_IN_WORDS.search(b.text)]
    # the boxes of the page's own step: no job-alert, search or sort box
    # beside a posting, no remember-me
    own = [f for f in digest.fields
           if not _NOT_APPLICATION.search(f"{f.label} {f.id_or_name} {f.placeholder}")]
    non_check = [f for f in own if f.type != "checkbox"]
    # the address screen of a two-step sign-in: an address box and nothing
    # else but checkboxes, with no button that sends an application (a form's
    # last step can ask for the email alone)
    email_first = bool(non_check) and not sends and all(
        f.type == "email" or str(f.autocomplete or "") in ("username", "email") for f in non_check)
    app = 0
    for f in digest.fields:
        blob = f"{f.label} {f.id_or_name} {f.placeholder}"
        if f is code or _password_kind(f) or _NOT_APPLICATION.search(blob):
            continue
        if f.type == "checkbox" and CAPTCHA_WORDS.search(blob):
            continue
        if f.type == "email" and (passwords or email_first):
            continue            # a sign-in's address
        app += 1
    text = f"{digest.title}\n{digest.text or ''}"
    entries = [b for b in buttons if entry_worded(b.text) and not b.in_form]
    received = sorted(confirmation_words(text))
    labels = [f.label for f in digest.fields]
    # a page's own word that the job was applied to, or is closed, counts only
    # where no application box, no account box and no Apply entry is offered
    # (I1, I2, R2-I1): a sign-in's "Already applied? Sign in" or "If you have
    # already applied, sign in", a screening question, an open posting's
    # "until the position is filled" never do; a site that means "you
    # applied" says so on a status page
    plain_page = (not app and not any(f.type == "file" for f in digest.fields) and not entries
                  and not passwords and not email_first)
    applied_words = statement_words(ALREADY_APPLIED_WORDS, text, labels) if plain_page else ""
    closed_words = statement_words(CLOSED_WORDS, text, labels) if not entries else ""
    return PageFacts(
        app_fields=app, files=sum(1 for f in digest.fields if f.type == "file"),
        passwords=len(passwords), new_password="new password" in kinds,
        current_password="current password" in kinds, email_first=email_first and not passwords,
        code_box=code is not None,
        card_box=any(_CARD_BOX.search(f"{f.label} {f.id_or_name} {f.autocomplete}")
                     for f in digest.fields),
        apply_entries=len(entries),
        send_buttons=len(sends),
        advance_buttons=sum(1 for b in buttons if ADVANCE_WORDS.search(b.text)),
        received=received[0] if received else "",
        already_applied=applied_words,
        closed=closed_words, review=_first_words(REVIEW_WORDS, text),
        description=_first_words(DESCRIPTION_WORDS, text),
        error=_first_words(ERROR_WORDS, text),
        captcha="a bot-check frame" if captcha_frame else _first_words(CAPTCHA_WORDS, text),
        url_kind=url_kind(url), text_chars=len((digest.text or "").strip()),
        dialog=str(getattr(digest, "dialog", "") or ""), link_sent=link_sent(digest))


# The read's Nouls: one signal each, phrased so a high value means yes, with
# structured true / false criteria (the Jev guide's shape for a subtle
# boundary: what counts, and examples). Each names the state parts it reads.
# The page kind each speaks for is `jev.PAGE_KIND_NOULS`.
_READ_NOULS: dict[str, tuple[str, tuple[str, list[str]], tuple[str, list[str]]]] = {
    "page_job_description": (
        "Does `page` show a job description: what the role does, its requirements or its "
        "qualifications?",
        ("the posting describes the job", [
            "responsibilities", "qualifications", "requirements", "about the role",
            "about the job", "about this role", "what you'll do", "what you will do",
            "job description", "who you are", "what we're looking for", "the role",
            "job summary", "about the position", "benefits"]),
        ("a form, a sign-in or a message with no description of the job", [
            "sign in", "create an account", "thank you for applying", "security code",
            "review your application"])),
    "page_apply_entry": (
        "Does `buttons` hold an Apply button or link that starts an application for this "
        "job?",
        ("a control that opens the application", [
            "apply", "apply now", "apply for this job", "apply for this position",
            "apply on company website", "i'm interested", "start application",
            "start your application"]),
        ("a control that sends a finished form, or a third-party quick apply", [
            "submit application", "submit", "easy apply", "apply with linkedin",
            "apply with indeed", "applied"])),
    "page_applicant_details": (
        "Do `fields` ask for the applicant's details: a name, contact details, a resume or "
        "answers to screening questions?",
        ("the boxes of a job application", [
            "first name", "last name", "full name", "name", "phone", "resume", "cv",
            "cover letter", "linkedin", "website", "portfolio", "authorized to work",
            "work authorization", "sponsorship", "years of experience", "address", "city",
            "file upload", "long text", "choice"]),
        ("the boxes of a sign-in, a code check, a payment or a search", [
            "password", "current password", "new password", "code", "card number",
            "keywords", "search"])),
    "page_sign_in": (
        "Do `page`, `fields` and `buttons` ask the visitor to sign in to an account that "
        "already exists?",
        ("a sign-in screen", [
            "sign in", "log in", "login", "forgot password", "forgot your password",
            "current password", "welcome back", "sign in to continue"]),
        ("a screen that creates a new account, or an application form", [
            "create an account", "create account", "create your account", "new password",
            "confirm password", "register", "sign up", "file upload"])),
    "page_create_account": (
        "Do `page`, `fields` and `buttons` ask the visitor to create a new account or "
        "register?",
        ("a sign-up screen", [
            "create an account", "create account", "create your account", "new password",
            "confirm password", "choose a password", "register", "sign up", "join"]),
        ("a sign-in to an account that exists", [
            "forgot password", "forgot your password", "current password", "welcome back"])),
    "page_received": (
        "Does `page` say that an application was received or submitted?",
        ("a thank-you or success message for a sent application", [
            "thank you for applying", "thanks for applying", "application received",
            "application has been received", "application has been submitted",
            "application was submitted", "we have received your application",
            "we've received your application", "successfully submitted",
            "successfully applied", "application complete"]),
        ("a form that still waits for its send", [
            "submit application", "review your application", "required"])),
    "page_already_applied": (
        "Does `page` say that the visitor already applied to this job before?",
        ("a note that an application is already on file", [
            "already applied", "you have already applied", "you've already applied",
            "you previously applied", "already submitted an application",
            "application already on file"]),
        ("a thank-you for the application just sent", ["thank you for applying"])),
    "page_closed": (
        "Does `page` say that the job is closed, filled or no longer available?",
        ("a posting that takes no more applications", [
            "no longer accepting applications", "no longer available", "no longer open",
            "position has been filled", "job has been filled", "job is closed",
            "posting has expired", "job has expired", "job not found", "page not found",
            "does not exist", "doesn't exist"]),
        ("an open posting", ["apply now", "apply for this job"])),
    "page_code": (
        "Do `page` and `fields` ask for a verification or security code that was sent to the "
        "visitor?",
        ("a screen for an emailed or texted code", [
            "security code", "verification code", "one-time code", "one time code",
            "enter the code", "code we sent", "we emailed you", "we sent you", "passcode",
            "otp"]),
        ("a box for another kind of code", [
            "postal code", "zip code", "promo code", "referral code", "country code"])),
    "page_payment": (
        "Do `page` and `fields` ask for a payment, a fee or card details?",
        ("a payment screen", [
            "card number", "credit card", "debit card", "cvv", "cvc", "expiration date",
            "application fee", "pay now", "payment"]),
        ("a question about pay on an application", [
            "salary", "compensation", "expected pay", "desired pay"])),
    "page_review": (
        "Does `page` show a summary of the applicant's answers to check before the final "
        "submit?",
        ("a review step", [
            "review your application", "review and submit", "please review",
            "review before submitting", "review your answers", "summary of your application"]),
        ("a form that still asks for answers", ["required"])),
    "page_error": (
        "Does `page` show an error: something went wrong, a server error or an access "
        "error?",
        ("an error page", [
            "something went wrong", "an error occurred", "error occurred", "server error",
            "internal error", "access denied", "try again later", "unexpected error"]),
        ("a working page", [])),
    "has_captcha": (
        "Do `page` and `fields` show a CAPTCHA, a reCAPTCHA or hCaptcha challenge, or a robot "
        "or human verification check?",
        ("a bot check the visitor must pass", [
            "captcha", "recaptcha", "hcaptcha", "not a robot", "i'm not a robot",
            "verify you are human", "are you human", "human verification"]),
        ("a page with no challenge", [])),
}


def _read_noul(qid: str) -> dict[str, Any]:
    instructions, (yes_what, yes_examples), (no_what, no_examples) = _READ_NOULS[qid]
    return {"type": "noul", "instructions": instructions,
            "criteria": {"true": {"what": yes_what, "examples": list(yes_examples)},
                         "false": {"what": no_what, "examples": list(no_examples)}}}


READ_NOUL_IDS = tuple(_READ_NOULS)


def _cut(text: str, cap: int = READ_TEXT_CAP) -> str:
    return " ".join(str(text or "").split())[:cap]


def read_questions(digest: FormDigest, url: str = "") -> tuple[dict, dict]:
    """The page read's request (Jev guide sections 3, 7, 13: a trimmed state,
    atomic questions). State: `page` (host, path shape, title, the text's
    head, an open dialog's title), `fields` (the first `READ_LIST_CAP`, each
    its label and kind), `field_kinds` (every field counted by kind) and
    `buttons` (their texts, each once). Questions: the `page_state` Choice
    and one Noul per signal (`READ_NOUL_IDS`)."""
    code = code_field(digest.fields)
    counts: dict[str, int] = {}
    census = []
    for f in digest.fields:
        k = census_kind(f, code)
        counts[k] = counts.get(k, 0) + 1
        if len(census) < READ_LIST_CAP:
            census.append({"n": f.n, "label": _cut(f.label), "kind": k})
    seen: set[str] = set()
    buttons = []
    for b in digest.buttons:
        if getattr(b, "chrome", False):
            continue                # the site's header and top bar (study G4)
        text = _cut(b.text)
        if text and text not in seen and len(buttons) < READ_LIST_CAP:
            seen.add(text)
            buttons.append({"n": b.n, "text": text})
    page: dict[str, Any] = {"url_host": digest.url_host, "path": path_shape(url),
                            "title": _cut(digest.title, 160),
                            "headline_text": (digest.text or "")[:READ_HEAD_CHARS]}
    dialog = _cut(getattr(digest, "dialog", "") or "", 160)
    if dialog:
        page["dialog"] = dialog
    state: dict[str, Any] = {
        "page": page,
        "fields": census,
        "field_kinds": counts,
        "buttons": buttons,
    }
    questions: dict[str, Any] = {
        "page_state": {
            "type": "choice",
            "instructions": "Which kind of screen is `page`, given its `fields` and `buttons`?",
            "criteria": _PAGE_STATE_CRITERIA,
        },
    }
    for qid in READ_NOUL_IDS:
        questions[qid] = _read_noul(qid)
    return state, questions


def _structure(f: PageFacts) -> dict[str, float]:
    """What the page's structure says for or against each kind."""
    s = {k: 0.0 for k in PAGE_STATES}
    if f.received and not f.app_fields and not f.send_buttons:
        s["confirmation"] += STRUCT_DECISIVE
    elif not f.received and (f.app_fields or f.send_buttons or f.passwords or f.code_box):
        s["confirmation"] += STRUCT_RULED_OUT
    elif not f.received and (f.advance_buttons or f.apply_entries):
        s["confirmation"] += STRUCT_AGAINST     # a page with a way on asks for more
    if (f.code_box and not f.passwords) or (f.link_sent and not f.received):
        # a code box, or a page that says a verification link was emailed
        # and has no box to fill (ACC-05): an account check
        s["code_gate"] += STRUCT_DECISIVE
    elif not f.code_box:
        s["code_gate"] += STRUCT_AGAINST
    if f.new_password and not f.files:
        s["signup_form"] += STRUCT_DECISIVE
        s["login_wall"] -= STRUCT_SUPPORT
    elif f.current_password:
        s["login_wall"] += STRUCT_SUPPORT
        s["signup_form"] -= STRUCT_SUPPORT
    if (not f.passwords and not f.email_first) or (f.send_buttons and not f.passwords):
        # no account box, or a button that sends an application with no
        # password box beside it: no sign-in or sign-up
        s["login_wall"] += STRUCT_AGAINST
        s["signup_form"] += STRUCT_AGAINST
    if f.files:
        s["application_form"] += STRUCT_DECISIVE
    elif f.app_fields >= 2 or (f.app_fields and (f.send_buttons or f.advance_buttons)):
        s["application_form"] += STRUCT_SUPPORT
    elif f.passwords and not f.app_fields:
        s["application_form"] += STRUCT_AGAINST     # an account's boxes alone
    if f.apply_entries and not f.app_fields and not f.files and not f.passwords:
        s["job_posting"] += STRUCT_SUPPORT
        if f.description or f.text_chars >= 1000:
            s["job_posting"] += STRUCT_SUPPORT
        # an Apply to open and no application box (a job alert's is none):
        # no form step yet
        s["application_form"] += STRUCT_AGAINST
    if f.review:
        s["review_page"] += STRUCT_SUPPORT
    if f.captcha == "a bot-check frame" or (f.captcha and not f.app_fields):
        s["captcha_or_bot_check"] += STRUCT_DECISIVE
    if f.closed:
        s["error_or_dead"] += STRUCT_DECISIVE
    elif f.error:
        s["error_or_dead"] += STRUCT_SUPPORT
    if f.card_box:
        s["payment_request"] += STRUCT_DECISIVE
    if f.url_kind in s:
        s[f.url_kind] += STRUCT_HINT
    return s


@dataclass
class PageRead:
    """The page read (`read_page`): the kind the loop acts on and how sure it
    is, the evidence per kind as a distribution, the judge's own pick, and
    why the read differs from it when it does."""
    state: str
    conf: float
    probabilities: dict[str, float]
    judged: str
    judged_conf: float
    evidence: dict[str, float] = field(default_factory=dict)
    why: str = ""


def read_page(answers: Mapping[str, Answer], facts: PageFacts) -> PageRead:
    """Combine the read's answers with the page's structure (see the
    constants above `READ_HEAD_CHARS`). With no answer at all the read is
    the structure's alone."""
    judged, judged_conf = read_page_state(answers)
    a = answers.get("page_state")
    probs = {str(k): float(v or 0.0) for k, v in (getattr(a, "probabilities", None) or {}).items()}
    if a is not None and a.choice is not None and not probs:
        probs = {str(a.choice): float(a.confidence or 0.0)}
    struct = _structure(facts)
    evidence: dict[str, float] = {}
    others: dict[str, float] = {}       # what the Nouls and the structure say, per kind
    for kind in PAGE_STATES:
        said = struct.get(kind, 0.0)
        nouls = [answers[q].noul for q in PAGE_KIND_NOULS.get(kind, ())
                 if q in answers and answers[q].noul is not None]
        if nouls:
            said += sum((float(n) - READ_NOUL_MIN) * 2 for n in nouls) / len(nouls)
        others[kind] = said
        evidence[kind] = round(READ_CHOICE_WEIGHT * probs.get(kind, 0.0) + said, 4)
    positive = {k: v for k, v in evidence.items() if v > 0}
    total = sum(positive.values())
    if not total:
        return PageRead(judged if a is not None else "other", 0.0, {}, judged, judged_conf,
                        evidence, "no kind has evidence for it")
    shares = {k: round(positive.get(k, 0.0) / total, 4)
              for k in sorted(PAGE_STATES, key=lambda k: -evidence[k])}
    # the judge's pick wins a tie
    best = max(shares, key=lambda k: (shares[k], k == judged))
    why = ""
    if best != judged:
        sides = [f"{k} {struct[k]:+.1f}" for k in (best, judged) if struct.get(k)]
        why = (f"the judge read {judged} {judged_conf:.2f}; the Nouls and the page's structure "
               f"read {best}" + (f" (structure: {', '.join(sides)})" if sides else ""))
    base = judged_conf if best == judged else probs.get(best, 0.0)
    rival = max([others[k] for k in PAGE_STATES if k != best] + [0.0])
    said = others[best]
    conf = (base + READ_SUPPORT_WEIGHT * max(0.0, said) + READ_AGAINST_WEIGHT * min(0.0, said)
            - READ_SUPPORT_WEIGHT * rival)
    conf = round(min(1.0, max(0.0, conf)), 4)
    return PageRead(best, conf, shares, judged, judged_conf, evidence, why)


def read_answer(read: PageRead) -> Answer:
    """The combined read as the `page_state` answer the loop reads
    (`read_page_state`, the trace, a park's evidence)."""
    probs = dict(read.probabilities) or {read.state: read.conf}
    return Answer(kind="choice", choice=read.state, probabilities=probs, confidence=read.conf)


def structural_kind(facts: PageFacts, *, strict: bool = False) -> str | None:
    """The page kind the structure alone gives, for a read that stayed under
    the floor after its second look (the unsure fallback): a bot-check frame,
    or a bot-check's words with no application box, is the check; a code box
    with no password box, or a page that says a verification link was emailed
    (`link_sent`, ACC-05), the code step; a password box with no file box an
    account screen (a sign-up when a box makes the password, a sign-in when
    the box is the current password); an Apply entry with no application box
    a posting; a closed posting's words (and no Apply entry) a dead end; an
    address screen with nothing else a sign-in's first step; application
    boxes a form; a page whose only way on is a Next or a Continue a step of
    the application; else None. `strict`: only a kind the structure settles (a
    lone password box with no autocomplete, a page with a Next alone and a
    single application box settle nothing). A review's words and a send
    button with no box left are the review."""
    if facts.captcha == "a bot-check frame" or (facts.captcha and not facts.app_fields):
        return "captcha_or_bot_check"
    if facts.code_box and not facts.passwords:
        return "code_gate"
    if facts.link_sent and not facts.received:
        return "code_gate"          # an account check by an emailed link (ACC-05)
    if facts.passwords and not facts.files:
        if facts.new_password:
            return "signup_form"
        if facts.current_password or not strict:
            return "login_wall"
        return None
    if facts.apply_entries and not facts.app_fields and not facts.files:
        return "job_posting"
    if facts.closed and not facts.app_fields and not facts.files:
        return "error_or_dead"      # after the Apply-entry rule: an Apply is no closed page
    if facts.review and facts.send_buttons and not facts.app_fields and not facts.files \
            and not facts.received:
        # a review's words and a send button with no box left to fill (SP5:
        # a review step misread as a confirmation, the structure ruling
        # the confirmation out)
        return "review_page"
    if facts.email_first:
        return "login_wall"
    if facts.files or facts.app_fields >= 2:
        return "application_form"
    if facts.app_fields:
        return None if strict else "application_form"
    if facts.advance_buttons and not facts.send_buttons and not strict:
        return "application_form"
    return None


def structure_against(facts: PageFacts, state: str) -> bool:
    """Does the page's structure rule out `state` (a sign-in with no account
    box, a code step with no code box, a confirmation beside a form)?"""
    return _structure(facts).get(state, 0.0) < 0


def already_applied(answers: Mapping[str, Answer], facts: PageFacts, state: str) -> str:
    """The evidence that the job was applied to before (TERM-04, READ-07), or
    "": the page's own words (`page_facts`: a statement, on a page with no
    application box and no Apply entry), with the judge's
    `page_already_applied` named beside them. The Noul alone never parks: a
    pre-submit thanks page keeps SP3's "a confirmation page before any
    submit; check whether ..." (`confirmation_step`)."""
    if not facts.already_applied:
        return ""
    p = noul_of(answers, "page_already_applied")
    said = f"; read as already applied {p:.2f}" if "page_already_applied" in answers else ""
    return f"the page says {facts.already_applied!r}{said}"


def closed_posting(answers: Mapping[str, Answer], facts: PageFacts, state: str) -> str:
    """The evidence that a page the run parks on (`error_or_dead`, `other`)
    is a closed posting (READ-08), or "": the page's words, or on a page read
    as an error or a dead end the judge's `page_closed`; never a page that
    offers an Apply entry."""
    if state not in ("error_or_dead", "other") or facts.apply_entries:
        return ""           # an Apply to open is no closed posting (I2)
    if facts.closed:
        return f"the page says {facts.closed!r}"
    p = noul_of(answers, "page_closed")
    return (f"read as closed ({p:.2f})" if state == "error_or_dead" and p >= READ_NOUL_MIN
            else "")


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
    # the field's widget, as the extractor read it (SP5): the filler acts
    # through these (`apply_form.Field`)
    widget: str = ""
    click_locator: tuple[int, str] | None = None
    option_locators: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    ident: str = ""


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


def noul_of(answers: Mapping[str, Answer], qid: str) -> float:
    a = answers.get(qid)
    return float(a.noul) if a is not None and a.noul is not None else 0.0


# Questions the run never answers, whatever the model maps them to: no fact
# and no drafted text ever goes into one, so a required one parks as
# unanswerable and an optional one stays blank. This is the user's line
# (2026-09-22: the run gives out only the answers they chose to share) made
# deterministic, in place of the removed `asks_for_prohibited` park. A yes/no
# question about holding a passport or a licence is not one of them; its number is.
_SENSITIVE_LABEL = re.compile(
    r"social\s*security|\bssn\b|social\s*insurance|\bsin\b|national\s*insurance"
    r"|taxpayer\s*id|tax\s*(id|file)\s*(number|no\b|#)?|\bi?tin\b"
    r"|date\s*of\s*birth|birth\s*date|birthdate|birthday|\bdob\b"
    r"|bank\s*(account|routing|name)|routing\s*number|account\s*number"
    r"|credit\s*card|debit\s*card|card\s*number|\bcvv\b|\bcvc\b"
    r"|passport\s*(number|no\b|#)|licen[cs]e\s*(number|no\b|#)"
    r"|national\s*id|government[-\s]*issued\s*id|alien\s*(registration\s*)?number"
    r"|maiden\s*name", re.I)


def is_sensitive_field(label: str, id_or_name: str = "") -> bool:
    """Does the control ask for a government ID, a birthdate, bank or card
    details (`_SENSITIVE_LABEL`)? Its label and its DOM id or name are read."""
    return bool(_SENSITIVE_LABEL.search(f"{label or ''} {id_or_name or ''}"))


def sensitive_reason(label: str) -> str:
    """The park reason for a required sensitive question: it is finished by
    hand, and no stored answer would ever be typed into it."""
    return f"asks for {label}, which auto-apply never fills; finish it by hand"


# The action of a password box on a page handled as the application form (an
# account made inside the application). The runner types the master password
# into it from the keyring (`apply_run._JobRun._fill_passwords`); the plan
# carries no value and asks the user no question. The master password is for
# job applications only (the user's rule, 2026-09-22).
PASSWORD_ACTION = "password"


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


# The profile's name sources, the only ones whose probabilities pool
# (`pooled_confidence`): each types the candidate's own name, so two of them
# typing the same words are one answer. An answer-bank entry (`answer_<id>`),
# years of experience or any other fact is left out: the same "Yes" or "5"
# given to two questions says nothing about which question the field asks.
POOL_KEYS = frozenset({"full_name", "first_name", "last_name", "signature_name",
                       "signature_today"})
# a value no two sources can agree on by meaning: a yes/no word or a number
_GENERIC_VALUE = re.compile(r"^(?:yes|no|y|n|true|false|n/?a|none|\d+(?:[.,]\d+)?\+?)$", re.I)


def _typed_words(f, catalog: FactCatalog, key: str) -> str:
    """The words name source `key` (`POOL_KEYS`) would type into field `f`,
    whitespace folded and lowercased; "" for any other source, and for a
    value that is a yes/no word or a number (`_GENERIC_VALUE`)."""
    if key not in POOL_KEYS:
        return ""
    if key == "signature_today":
        if _wants_date(f):
            return ""
        key = "signature_name"
    fact = catalog.facts.get(key)
    if fact is None or fact.kind != "text" or not fact.value:
        return ""
    words = " ".join(fact.value.split()).lower()
    return "" if _GENERIC_VALUE.match(words) else words


def pooled_confidence(f, catalog: FactCatalog, answer: Answer | None, key: str) -> float:
    """The judge's probability that field `f` gets the name `key` types:
    the sum over every name source (`POOL_KEYS`) that types the same words.
    A signature box read live as the full name at 0.61 and as the typed
    signature at 0.29 (2026-09-25) is one answer at 0.90, since both type
    the name. Nothing else pools: an answer-bank entry, a Yes or a number
    shared by two questions of different meaning stays apart. 0.0 when
    `key` is no name source."""
    want = _typed_words(f, catalog, key)
    if not want or answer is None:
        return 0.0
    return sum(float(p or 0.0) for k, p in (answer.probabilities or {}).items()
               if _typed_words(f, catalog, k) == want)


def _number_box_takes(f, fact_key: str, value: str) -> bool:
    """Does a number box take `value` (cycle 18, FM-5)? The phone only when
    the box's label or name say phone and the phone has seven digits or
    more (its digits are typed); any other fact only as a plain number
    (`PLAIN_NUMBER`: "3", "1.5"; "5+", "120k" and "3-5" leave it blank)."""
    if fact_key == "phone":
        return phone_named(f.label, f.id_or_name) and len(re.sub(r"\D", "", value)) >= 7
    return bool(PLAIN_NUMBER.match(str(value or "")))


def plan(digest: FormDigest, catalog: FactCatalog, answers: Mapping[str, Answer], *,
         generation_enabled: bool = True, company: str = "") -> FillPlan:
    """Turn the page answers into a `FillPlan`.

    Per field: a `quick_map` hit whose fact has a value wins over the model's
    mapping (a disagreement is logged at DEBUG; an empty fact falls through to
    the model's mapping); a mapping below `FIELD_MAP_MIN_CONF` or equal to
    `leave_blank` is `skip` (a name source's mapping counts the probability
    of every name source that types the same words, `pooled_confidence`),
    and when the field is required the plan carries
    `park_reason` and a `missing` entry (an optional skip is a `missing` entry
    only); `needs_generation` is `generate` when generation is enabled, else
    the same rule; an option pick below `OPTION_MIN_CONF` or `no_match` follows
    the same rule; `signature_today` fills today's date for a date control or
    a label with `date` / `dated` / `today` as a whole word, else the typed
    name; `consent_attest` (a checkbox's attestation, privacy or contact
    consent) is `select` with option `checked` at or above its floor
    (`consent_floor`: `FIELD_MAP_MIN_CONF` for a required box whose whole
    label is a routine consent, the job's `company` the one name it may
    carry; else `CONSENT_MIN_CONF`), and below it follows the unanswerable
    rule; a sensitive box is never
    answered, and a password box is `PASSWORD_ACTION`, with no value. A
    yes / no or years fact whose value does not answer the field's question
    (`answers_question`: "authorized to work without sponsorship" mapped to
    `work_authorized`, "years of Python" to `years_experience`, a Yes to
    sponsorship under "H-1B sponsorship") gives no value, and the judge's
    pick does not override it (cycle 18, SP6c); nor does a custom yes / no or
    number answer under a question other than its saved one, or any of them under
    a cut label (`FactCatalog.answers_field`); the job's `company` name in a
    question reads as the company (round 7). A yes / no fact under a label
    with no verb (`apply_facts.noun_phrase`: "Work authorization") settles
    only a plain Yes / No in code (`code_pick`): a status list or a text box
    there gets no value from it. Buttons
    keep the highest-confidence n per role. The one
    park reason is a required field without an answer; the flags are recorded only. A
    button of the site's header or top bar (`Button.chrome`) holds a role only
    when no button of the page's own was judged to it; a sign-in with another
    site (`THIRD_PARTY`) never holds the advance or the Apply entry."""
    out = FillPlan()
    required_reason = ""
    sensitive_reason_ = ""
    for f in digest.fields:
        model_key, model_conf = _choice_of(answers, f"field_{f.n}_source")
        quick = quick_map(f.label, f.id_or_name, f.type)
        if quick and not catalog.has(quick):
            log.debug("field %d %r: quick_map %s has no value; using the model's mapping",
                      f.n, f.label, quick)
            quick = None
        pooled = 0.0
        if not quick and model_key not in (None, "leave_blank") \
                and model_conf < FIELD_MAP_MIN_CONF:
            pooled = pooled_confidence(f, catalog, answers.get(f"field_{f.n}_source"), model_key)
        if quick:
            fact_key, conf = quick, 1.0
            if model_key and model_key != quick:
                log.debug("field %d %r: quick_map %s, model %s (%.2f); keeping quick_map",
                          f.n, f.label, quick, model_key, model_conf)
        elif pooled >= FIELD_MAP_MIN_CONF:
            # sources that type the same words: one answer at their sum
            fact_key, conf = model_key, pooled
            log.debug("field %d %r: %s at %.2f, %.2f with the sources that type the same words",
                      f.n, f.label, model_key, model_conf, pooled)
        elif model_key is None or model_key == "leave_blank" or model_conf < FIELD_MAP_MIN_CONF:
            fact_key, conf = None, model_conf
        elif model_key == "consent_attest" and model_conf < consent_floor(
                f.label, required=bool(f.required), company=company,
                partial=bool(getattr(f, "label_partial", False))):
            fact_key, conf = None, model_conf
        else:
            fact_key, conf = model_key, model_conf

        pf = PlannedField(n=f.n, locator=f.locator, label=f.label, required=bool(f.required),
                          fact_key=fact_key, value="", option=None, confidence=conf,
                          action="skip", quick=bool(quick),
                          widget=str(getattr(f, "widget", "") or ""),
                          click_locator=getattr(f, "click_locator", None),
                          option_locators=list(getattr(f, "option_locators", None) or []),
                          options=list(f.options or []), ident=str(getattr(f, "ident", "") or ""))
        if AUTOFILL_PARSER in (f.help or ""):
            # a resume parser's own upload (Ashby's "Autofill from resume"),
            # no question of the application's: left alone
            fact_key, pf.fact_key = None, None
            out.fields.append(pf)
            continue
        if getattr(f, "refused", ""):
            # a popup whose own words send, never opened (review round 8): no
            # answer, so a required one parks on its question
            fact_key, pf.fact_key, pf.confidence = None, None, 0.0
            out.fields.append(pf)
            out.missing.append((f.label, f.help or f.placeholder or f.type))
            if f.required and not required_reason:
                required_reason = f"required field without an answer: {f.label}"
            continue
        if is_sensitive_field(f.label, f.id_or_name):
            # an SSN, a birthdate, bank or card details: never answered, and
            # a masked "Passport number" box is one of these before it is a
            # password box
            fact_key, pf.fact_key = None, None
        elif password_box(f):
            # the only writer of a password field is `ats_accounts.fill_password`,
            # through the accounts hook or the runner's form step; no fact ever
            # lands in one
            fact_key, pf.fact_key = None, None
            pf.action = PASSWORD_ACTION
        elif fact_key and not catalog.answers_field(
                fact_key, f.label, f.help,
                partial=bool(getattr(f, "label_partial", False)), company=company):
            # the fact answers another question than the field's, a
            # narrower form its value does not settle, or a cut label; a
            # custom yes / no or number answer another question than its
            # saved one (cycle 18, SP6c): no value from it, so a required
            # field parks and an optional one stays blank, unless the judge
            # is sure the saved answer settles the question as worded here
            # (`settled_pick`, never for work authorization or sponsorship)
            pick = settled_pick(answers, f, fact_key, catalog)
            if pick is not None:
                log.debug("field %d %r: %s settles it by the judge's read: %r",
                          f.n, f.label, fact_key, pick)
                pf.value, pf.action, pf.option = catalog.value(fact_key), "select", pick
            else:
                log.debug("field %d %r: %s does not answer its question; no value",
                          f.n, f.label, fact_key)
                fact_key, pf.fact_key = None, None
        elif fact_key and catalog.has(fact_key) and _yes_no_fact(catalog, fact_key) \
                and noun_phrase(f.label) and code_pick(catalog.value(fact_key), f.options) is None:
            # a label with no verb ("Work authorization") heads a status list
            # (US Citizen / Permanent Resident / H-1B ...) as often as a Yes /
            # No: its yes / no fact settles only a plain Yes / No in code,
            # never the judge's pick nor a text box (cycle 18 SP6c, round 7)
            log.debug("field %d %r: %s under a label with no verb and no Yes / No; no value",
                      f.n, f.label, fact_key)
            fact_key, pf.fact_key = None, None
        elif fact_key == "needs_generation":
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
                # from the second request, and until it arrives the field waits.
                # A list past `OPTIONS_CAP` is searched in code first (EXT-07:
                # the United States and most states sit past the 40th option)
                qid = f"field_{f.n}_option" if quick else f"field_{f.n}_pick"
                opt, oconf = _choice_of(answers, qid)
                # a plain Yes / No list, and a list past the cap on the same
                # words or a name the value goes by, is settled in code
                # (cycle 18, FM-1 and FM-2)
                found = code_pick(pf.value, f.options)
                if found is not None:
                    pf.option = found
                elif opt is None or opt == "no_match" or oconf < OPTION_MIN_CONF:
                    pf.action = "skip"
                else:
                    pf.option = opt
            elif pf.action == "fill" and f.type == "number" \
                    and not _number_box_takes(f, fact_key, pf.value):
                pf.action = "skip"
        out.fields.append(pf)
        if pf.action == "skip" and is_sensitive_field(f.label, f.id_or_name):
            # no answer is asked for: one would never be used (the loop the
            # SP8 review found), and the user is not nudged to store an SSN
            if f.required and not sensitive_reason_:
                sensitive_reason_ = sensitive_reason(f.label)
        elif pf.action == "skip":
            out.missing.append((f.label, f.help or f.placeholder or f.type))
            if f.required and not required_reason:
                required_reason = f"required field without an answer: {f.label}"

    chrome = {b.n for b in digest.buttons if getattr(b, "chrome", False)}
    for b in digest.buttons:
        role, conf = _choice_of(answers, f"button_{b.n}_role")
        if role is None:
            continue
        if role in ("advance", "apply_entry") and THIRD_PARTY.search(b.text or ""):
            continue        # ADV-09: a sign-in with another site is no way on
        held = out.buttons.get(role)
        # the page's own button beats the site's header for any role (M11)
        if held is None or (b.n not in chrome, conf) > (held[0] not in chrome, held[1]):
            out.buttons[role] = (b.n, conf)

    out.flags = {qid: noul_of(answers, qid)
                 for qid in ("asks_for_prohibited", "requires_account", "has_captcha")}
    if "requires_account" not in answers:
        # the page read asks the sign-in and the sign-up apart (SP4)
        out.flags["requires_account"] = max(noul_of(answers, "page_sign_in"),
                                            noul_of(answers, "page_create_account"))
    out.park_reason = sensitive_reason_ or required_reason
    return out


def _yes_no_fact(catalog: FactCatalog | None, key: str) -> bool:
    """Is `key` a yes / no fact (`FactCatalog.yes_no`; `YES_NO_KEYS` with no
    catalog)?"""
    return catalog.yes_no(key) if catalog is not None else key in YES_NO_KEYS


# --- the second request: option picks for model-mapped fields ------------------------

def option_questions(digest: FormDigest, fill_plan: FillPlan,
                     catalog: FactCatalog | None = None, *,
                     company: str = "") -> tuple[dict, dict]:
    """Option picks (`field_{n}_pick`) for every field with options whose fact
    the model chose (quick_map did not know it), with the fact's answer in the
    instruction (`candidate_answer`: every yes / no fact of `catalog` for
    one of them). A pick code settles (`code_pick`) is never asked, nor one
    for a fact whose value does not answer the field's question
    (`FactCatalog.answers_field`, or `answers_question` with no catalog; the
    job's `company` name reads as the company, as in `plan`), nor one for a
    yes / no fact under a label with no verb (`apply_facts.noun_phrase`: a
    status list there is no yes / no question). Empty when there is nothing
    to ask; merge the answers over the first request's and call `plan`
    again."""
    by_n = {f.n: f for f in digest.fields}
    state: dict[str, Any] = {"fields": []}
    questions: dict[str, Any] = {}
    for pf in fill_plan.fields:
        f = by_n.get(pf.n)
        if f is None or not f.options or not pf.fact_key or not pf.value:
            continue
        if pf.fact_key in SPECIAL_SOURCES or pf.quick:
            continue
        partial = bool(getattr(f, "label_partial", False))
        if catalog is not None:
            fits = catalog.answers_field(pf.fact_key, f.label, f.help, value=pf.value,
                                         partial=partial, company=company)
        else:
            fits = answers_question(pf.fact_key, pf.value, f.label, f.help, partial,
                                    company=company)
        if code_pick(pf.value, f.options) is not None or not fits:
            continue
        if _yes_no_fact(catalog, pf.fact_key) and noun_phrase(f.label):
            continue            # a status list under a label with no verb (`plan`)
        i = len(state["fields"])
        state["fields"].append(_compact_field(f))
        questions[f"field_{f.n}_pick"] = _option_question(
            i, f.options, candidate_answer(catalog, pf.fact_key, pf.value, f.options), pf.value)
    return state, questions


# --- the third request: a held-back answer, asked whether it settles the field -------

# The facts the judge may read a reworded question for. The willingness
# answers came first (2026-09-26). The work authorization answers joined on
# 2026-09-27: a live run on the Contoso form parked on "Do you require visa
# sponsorship ... This includes needing sponsorship for CPT, OPT or other visa
# types" and on an authorization question that listed who counts, both
# answered by the saved answers, because a word list held them back. The
# user's call: the judge reads them. Their saved answer carries every stored
# yes / no line and the authorization statement (`_settle_answer_text`). A
# years count and fully remote work stay with the gate: the recording read
# them wrong at up to 0.92.
SETTLE_KEYS = frozenset(("willing_to_relocate", "onsite_ok", "work_authorized",
                         "requires_sponsorship", "authorized_without_sponsorship"))
# The questions no saved answer settles, whatever the fact: a conviction, a
# background or drug check or a security clearance; the candidate's present
# job or employer ("Are you currently legally employed in the United
# States?", "Does your employer require visa sponsorship?"); a visa or a
# sponsor the candidate holds now, or a sponsorship carried on; a fact about
# the role ("Is this role on-site?"); relocation help the candidate asks for.
# The 2026-09-27 recording read each of these from the saved answers at 0.87
# to 0.97.
_NEVER_SETTLED = re.compile(
    r"clearance|convict|felon|criminal|background check|drug"
    r"|\b(?:are|were) you (?:currently |now |presently )?(?:legally |lawfully )?employed\b"
    r"|\bdo you (?:currently |now |presently )?work\b"
    r"|\byour (?:current |present |previous |former )?employer\b"
    r"|\b(?:do|does) you (?:currently |now )?(?:have|hold|possess) (?:an? |any )?"
    r"(?:(?:current|valid|active|work|employment|u\.?s\.?) )*(?:visa|sponsor)"
    r"|\bcontinue\b[^.?!]*\bsponsor"
    r"|\bis (?:this|the) (?:role|position|job)\b"
    r"|\b(?:need|require|want|expect)\w* (?:any )?relocation "
    r"(?:assistance|support|help|package|benefits?)\b", re.I)
# a country other than the United States, read after the US states and
# territories are taken out ("New Mexico", "Georgia", "Puerto Rico")
_US_PLACES = re.compile(r"\b(?:" + "|".join(
    re.escape(n) for n in sorted((*_US_STATES.values(), "Puerto Rico", "United States"),
                                 key=len, reverse=True)) + r")\b", re.I)
_OTHER_COUNTRY = re.compile(r"\b(?:" + "|".join(
    re.escape(c) for c in sorted((*(c for c in _COUNTRIES
                                    if c not in ("United States", "Puerto Rico")),
                                  "England", "Scotland", "Wales", "Britain", "Europe"),
                                 key=len, reverse=True)) + r")\b", re.I)
_OTHER_COUNTRY_CODE = re.compile(r"\bU\.?K\b\.?|\bEU\b")
# where the candidate lives now, and commuting from there
_WHERE_THEY_LIVE = re.compile(
    r"\b(?:local|locally|located|live|lives|living|reside|resides|residing|commut\w*)\b", re.I)
# a place named after "in", "at", "to", "near" or "from" ("in NYC", "at our
# Austin, TX office"), the United States and the office words aside
_NAMED_PLACE = re.compile(
    r"\b(?:in|at|to|near|from|around) (?:our |the |an? )?"
    r"(?!(?:Office|Offices|On|Onsite|Hybrid|Remote|HQ|Headquarters|U\.?S|USA|America|United)"
    r"\b)[A-Z][A-Za-z.]+")
# a work authorization question names the United States or a word of its own
_AUTH_TOKENS = frozenset(("US", "RIGHTTOWORK", "NOSPONSOR", "VISATYPE", "VISAEXAMPLE"))
_AUTH_WORDS = re.compile(r"authori[sz]|eligib|visa|sponsor|citizen|permanent resident|"
                         r"green card|immigration|permit|\blegal|\blawful", re.I)
# the authorization words a list of visa types counts against ("legally"
# reads as a place of work: "to work legally in the United States")
_AUTHORIZED_WORDS = frozenset(("authorized", "authorised", "authorization", "authorisation",
                               "eligible", "permitted", "RIGHTTOWORK"))
_RESTRICTED = frozenset(("UNRESTRICTED", "restriction", "restrictions", "restricted"))
_SPONSORED = frozenset(("sponsor", "sponsorship", "sponsored", "NOSPONSOR"))
SETTLE_QUESTION = (
    "The candidate saved `saved_answer`: its first line answers `saved_question`, the other "
    "lines are the candidate's other saved answers, and a note is the candidate's own words. "
    "The form field `fields[{i}].label`, with its help, may ask the same thing in the "
    "employer's words: naming the job's city, office or days on site, listing the visa types "
    "it counts or who counts as authorized, or adding a condition such as \"if you are not "
    "local\" or \"if needed\". Read the field as the employer means it and pick the option "
    "every candidate with these saved answers would give. A condition the saved answers "
    "already cover does not stop a pick: a candidate willing to relocate answers \"If you "
    "are not local, are you willing to relocate?\" with yes. Choose not_settled when the "
    "field asks something these answers do not say, or when its right option turns on it: "
    "another country, where the candidate lives now, a cost the candidate pays, a number of "
    "years or a skill, a date, pay, or a fact about the role or the company. Choose "
    "not_settled when unsure.")


def _settle_answer_text(catalog: FactCatalog, key: str, value: str,
                        options: list[str] | tuple[str, ...]) -> str:
    """The saved answer a settle question carries: `candidate_answer`, with
    the authorization statement last for a yes / no fact when the catalog
    holds one and the lines do not carry it yet, so a question that lists
    visa types or who counts as authorized is read against the candidate's
    own words."""
    text = candidate_answer(catalog, key, value, options)
    if (key in YES_NO_KEYS and catalog.has(STATEMENT_KEY)
            and f"{STATEMENT_LINE}:" not in text):
        text += f"\n{STATEMENT_LINE}: {catalog.value(STATEMENT_KEY)}"
    return text


def _said(catalog: FactCatalog, key: str, forms: frozenset[str]) -> bool:
    """Does the catalog hold `key` with a value among `forms` (`YES_FORMS`,
    `NO_FORMS`)?"""
    return catalog.has(key) and str(catalog.value(key) or "").strip().lower() in forms


def _other_country(text: str) -> bool:
    """Does `text` name a country other than the United States ("Canada",
    "the UK"; "New Mexico" and "Georgia" are US states)?"""
    return bool(_OTHER_COUNTRY.search(_US_PLACES.sub(" ", text))
                or _OTHER_COUNTRY_CODE.search(text))


def _unsaid(f, key: str, catalog: FactCatalog) -> bool:
    """Does field `f`'s right option turn on something the saved answers do
    not say? Read in code before the judge is asked and again on its
    answer, since the 2026-09-27 recording read each of these at 0.85 to
    0.98.

    Any fact: a question `_NEVER_SETTLED` holds, or another country.
    Relocation: a yes settles a question that asks the candidate's
    willingness (`asks_willingness`: "If you are not local, are you willing
    to relocate?"; a move with no relocation help is a move too, and a note
    carries any condition the candidate has); a no settles one that
    leaves out where the candidate lives ("Are you located in or willing to
    relocate to Austin, TX?" turns on it). On-site work: a yes settles no
    question of commuting or of where the candidate lives, and one that
    names a place only for a candidate willing to relocate. Work
    authorization: the field names the United States or a word of work
    authorization ("Are you able to work without restrictions?" may ask
    about health); and unless the saved answers say no sponsorship, the
    candidate's visa is unsaid, so no question of restrictions, of
    sponsorship now or on the start date alone, of one visa type, or of
    authorization under a list of visa types."""
    text = f"{f.label}\n{f.help or ''}"
    if _NEVER_SETTLED.search(text) or _other_country(text):
        return True
    if key == "willing_to_relocate":
        if _said(catalog, key, YES_FORMS):
            return not asks_willingness(key, f.label, f.help or "")
        return _said(catalog, key, NO_FORMS) and bool(_WHERE_THEY_LIVE.search(text))
    if key == "onsite_ok":
        if not _said(catalog, key, YES_FORMS):
            return False
        return bool(_WHERE_THEY_LIVE.search(text)) or (
            not _said(catalog, "willing_to_relocate", YES_FORMS)
            and bool(_NAMED_PLACE.search(text)))
    words = set(question_tokens(f.label, f.help or ""))
    if not (words & _AUTH_TOKENS or _AUTH_WORDS.search(text)):
        return True
    if (_said(catalog, "requires_sponsorship", NO_FORMS)
            or _said(catalog, "authorized_without_sponsorship", YES_FORMS)):
        return False
    now_only = bool(words & {"NOW", "STARTDATE"}) and not words & {"NOWFUTURE", "FUTURE"}
    return bool(words & _RESTRICTED or (now_only and words & _SPONSORED)
                or ("VISATYPE" in words
                    and (words & _AUTHORIZED_WORDS or not words & {"other", "any"})))


def _settle_ok(f, key: str, catalog: FactCatalog) -> bool:
    """May the judge read whether `key`'s saved answer settles field `f`:
    a list of options the run picks from, a whole label, a fact it may read
    (`SETTLE_KEYS`) with a value, and no question whose right option turns
    on something the saved answers do not say (`_unsaid`) or never
    answered?"""
    return (bool(f.options) and _action_for(f) == "select"
            and not getattr(f, "label_partial", False)
            and key in SETTLE_KEYS and catalog.has(key)
            and not is_sensitive_field(f.label, f.id_or_name) and not password_box(f)
            and not _unsaid(f, key, catalog))


def settled_pick(answers: Mapping[str, Answer], f, key: str,
                 catalog: FactCatalog) -> str | None:
    """The option the judge's settle answer (`field_{n}_settle`) gives
    field `f` for `key`'s saved answer: one of the field's options at
    `SETTLE_MIN_CONF` or more, `SETTLE_MIN_GAP` ahead of every other choice
    (`not_settled` included); None otherwise, or when `_settle_ok` says no."""
    a = answers.get(f"field_{f.n}_settle")
    if a is None or a.choice is None or not _settle_ok(f, key, catalog):
        return None
    choice = str(a.choice)
    if choice == NOT_SETTLED or choice not in f.options:
        return None
    top = float(a.confidence or 0.0)
    rest = [float(p) for n, p in (a.probabilities or {}).items() if n != choice]
    if top < SETTLE_MIN_CONF or top - max(rest, default=0.0) < SETTLE_MIN_GAP:
        return None
    return choice


def _held_back(f, answers: Mapping[str, Answer], catalog: FactCatalog,
               company: str) -> str | None:
    """The fact `plan` took for field `f` (its `quick_map` hit with a value,
    else the judge's mapping at `FIELD_MAP_MIN_CONF` or more) when the
    own-question gate held its answer back and the judge may read it
    (`_settle_ok`); None otherwise."""
    quick = quick_map(f.label, f.id_or_name, f.type)
    if quick and catalog.has(quick):
        key = quick
    else:
        key, conf = _choice_of(answers, f"field_{f.n}_source")
        if key in (None, "leave_blank") or conf < FIELD_MAP_MIN_CONF:
            return None
    if not _settle_ok(f, key, catalog) or catalog.answers_field(
            key, f.label, f.help, partial=bool(getattr(f, "label_partial", False)),
            company=company):
        return None
    return key


def settle_questions(digest: FormDigest, fill_plan: FillPlan, answers: Mapping[str, Answer],
                     catalog: FactCatalog, *, company: str = "") -> tuple[dict, dict]:
    """The third request: each field the plan left blank because the
    own-question gate held back its fact's saved answer (`_held_back`) is
    asked whether that answer, with its note, the other yes / no lines and
    the authorization statement (`_settle_answer_text`), settles the field's
    question as worded there (`SETTLE_QUESTION`, escape `not_settled`).
    `plan` fills the sure ones (`settled_pick`). Empty when there is nothing
    to ask; merge the answers and call `plan` again."""
    by_n = {f.n: f for f in digest.fields}
    state: dict[str, Any] = {"fields": []}
    questions: dict[str, Any] = {}
    for pf in fill_plan.fields:
        f = by_n.get(pf.n)
        if f is None or pf.action != "skip" or pf.fact_key:
            continue
        key = _held_back(f, answers, catalog, company)
        if key is None:
            continue
        i = len(state["fields"])
        state["fields"].append(_compact_field(f))
        value = catalog.value(key)
        criteria: dict[str, Any] = {o: None for o in shortlist(list(f.options), value)}
        criteria[NOT_SETTLED] = NOT_SETTLED_DESCRIPTION
        questions[f"field_{f.n}_settle"] = {
            "type": "choice",
            "instructions": {"saved_question": catalog.facts[key].description,
                             "saved_answer": _settle_answer_text(catalog, key, value,
                                                                 f.options),
                             "question": SETTLE_QUESTION.format(i=i)},
            "criteria": criteria}
    return state, questions


# --- the second look: a required field's dropped or weak mapping, asked alone ---------

REASK_WHAT = ("source", "pick")


def _pick_qid(pf: PlannedField) -> str:
    """The pick question a field's option rides in: `field_{n}_option` for a
    `quick_map` field (the first request), `field_{n}_pick` else."""
    return f"field_{pf.n}_option" if pf.quick else f"field_{pf.n}_pick"


def reask_targets(digest: FormDigest, catalog: FactCatalog, answers: Mapping[str, Answer],
                  fill_plan: FillPlan, *, what: str, company: str = "") -> list[int]:
    """The fields to ask once more on their own before the run parks on them
    (SP5): required, planned `skip`, never a sensitive or a password box.
    With `what` "source": no `field_{n}_source` answer came back (the first
    look dropped it), its mapping sits under `FIELD_MAP_MIN_CONF` (an unsure
    `leave_blank` too: an unsure "nothing fits" is no answer), or a
    `consent_attest` tick under its floor (`consent_floor`); a field `quick_map`
    settles is never one. With "pick": its fact is known and has a value,
    its value answers the field's question (`FactCatalog.answers_field`), it has options
    and its pick is missing or under
    `OPTION_MIN_CONF`. A confident `leave_blank` or a confident `no_match` is
    the data's own answer: that field parks as it did. A consent tick's
    second look stands alone against its floor (review I1)."""
    if what not in REASK_WHAT:
        raise ValueError(f"unknown re-ask {what!r}")
    by_n = {f.n: f for f in digest.fields}
    out: list[int] = []
    for pf in fill_plan.fields:
        f = by_n.get(pf.n)
        if f is None or not pf.required or pf.action != "skip":
            continue
        if is_sensitive_field(f.label, f.id_or_name) or password_box(f):
            continue
        if what == "source":
            if pf.quick:
                continue
            key, conf = _choice_of(answers, f"field_{f.n}_source")
            if key is None or conf < FIELD_MAP_MIN_CONF \
                    or (key == "consent_attest" and conf < consent_floor(
                        f.label, required=True, company=company,
                        partial=bool(getattr(f, "label_partial", False)))):
                out.append(f.n)
            continue
        if not (f.options and pf.fact_key and pf.fact_key not in SPECIAL_SOURCES
                and catalog.has(pf.fact_key)
                and catalog.answers_field(
                    pf.fact_key, f.label, f.help,
                    partial=bool(getattr(f, "label_partial", False)), company=company)):
            continue
        opt, oconf = _choice_of(answers, _pick_qid(pf))
        if opt is None or (opt != "no_match" and oconf < OPTION_MIN_CONF):
            out.append(f.n)
    return out


def reask_questions(digest: FormDigest, catalog: FactCatalog, fill_plan: FillPlan,
                    ns: list[int], *, what: str,
                    job: Mapping[str, Any] | None = None) -> tuple[dict, dict]:
    """The second look at the fields `ns` (`reask_targets`), one request for
    them all (review M11): the fields alone (label, type, whether required,
    options, help, placeholder) and the headings they sit under. For
    "source", the job, the page's host and title and the descriptions of the
    sources their types can take (`facts`, never a value); each question is
    the first look's `field_{n}_source`. For "pick", each field's pick
    question with the fact's answer in its instruction (`candidate_answer`;
    the first look's `field_{n}_option` or `field_{n}_pick`)."""
    by_n = {x.n: x for x in digest.fields}
    by_pf = {p.n: p for p in fill_plan.fields}
    fields = [by_n[n] for n in ns if n in by_n and n in by_pf]
    state: dict[str, Any] = {"fields": [_compact_field(f) for f in fields]}
    sections = _sections(fields)
    if sections:
        state["sections"] = sections
    questions: dict[str, Any] = {}
    if what == "pick":
        for i, f in enumerate(fields):
            pf = by_pf[f.n]
            questions[_pick_qid(pf)] = _option_question(
                i, f.options, candidate_answer(catalog, pf.fact_key, pf.value, f.options),
                pf.value)
        return state, questions
    job = job or {}
    catalog_keys = list(catalog.to_criteria())
    all_facts = _facts_map(catalog)
    facts: dict[str, Any] = {}
    for i, f in enumerate(fields):
        criteria = _source_criteria(catalog_keys, f.type)
        facts.update({k: all_facts[k] for k in criteria if k in all_facts})
        questions[f"field_{f.n}_source"] = {
            "type": "choice",
            "instructions": f"Which key of `facts` describes what `fields[{i}]` asks for? When "
                            "nothing fits, `leave_blank`; for an essay question no fact "
                            "answers, `needs_generation`.",
            "criteria": criteria}
    state = {"job": {"company": str(job.get("company_name") or job.get("company") or ""),
                     "title": str(job.get("job_title") or job.get("title") or "")},
             "page": {"url_host": digest.url_host, "title": digest.title}, **state,
             "facts": facts}
    return state, questions


# --- validation messages: which field each one names (ADV-02) --------------------------

ERROR_FIELDS_CAP = 40           # fields offered per message
ERROR_MESSAGE_CAP = 200         # characters of a message sent


def error_questions(messages: list[str], fields, *, exclude=None,
                    again: bool = False) -> tuple[dict, dict]:
    """One request for the validation messages the page showed that no
    control names (ADV-02; a message tied to its control needs no judge):
    per message `error_{i}_field`, a Choice over the page's fields (each
    option its question's words) and `none` (the message names no one
    field: a note about the whole page). The state is the messages alone,
    the fields ride in the options, so each question is one small
    judgment (the Jev guide, sections 3 and 7).

    `exclude` (per message, field numbers): the fields not offered for that
    message, the ones an earlier look named for it on this form (SP6 review
    M1). `again`: the second look for a message the first request mapped
    to no field, a fresh question."""
    state = {"messages": [" ".join(str(m or "").split())[:ERROR_MESSAGE_CAP] for m in messages]}
    questions: dict[str, dict] = {}
    for i in range(len(messages)):
        skip = set(exclude[i]) if exclude is not None and i < len(exclude) and exclude[i] \
            else set()
        options: dict[str, Any] = {"none": None}
        for f in [f for f in fields if f.n not in skip][:ERROR_FIELDS_CAP]:
            label = " ".join(str(getattr(f, "label", "") or "").split())[:READ_TEXT_CAP]
            if label:
                options[f"q{f.n}"] = {"question": label}
        # the second look adds words no form's question is likely to hold (a
        # judge that scores words reads the options as it did the first time)
        ask = f"{'Looking again, which' if again else 'Which'} entry of the form does the " \
              f"error message `messages[{i}]` point at?"
        questions[f"error_{i}_field"] = {"type": "choice", "instructions": ask,
                                         "criteria": options}
    return state, questions


def read_error_fields(answers: Mapping[str, Answer], count: int) -> dict[int, tuple[int | None, float]]:
    """i -> (the field `n` message `i` names, or None for `none`; its
    confidence), for every message answered (a dropped answer is left
    out)."""
    out: dict[int, tuple[int | None, float]] = {}
    for i in range(count):
        choice, conf = _choice_of(answers, f"error_{i}_field")
        if choice is None:
            continue
        n = int(choice[1:]) if choice.startswith("q") and choice[1:].isdigit() else None
        out[i] = (n, conf)
    return out


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
        p_correct = noul_of(answers, f"verify_{n}")
        p_placeholder = noul_of(answers, f"placeholder_{n}")
        out.append(VerifyResult(n=n, label=str(f.get("label", "")),
                                ok=p_correct >= VERIFY_MIN and p_placeholder <= PLACEHOLDER_MAX,
                                p_correct=p_correct, p_placeholder=p_placeholder))
    return out


# --- the emailed code ----------------------------------------------------------------

# The display name of each ATS system `apply_queue.infer_ats` knows, for the
# from-site question: the code mail comes from the ATS's own domain, which the
# form's host (a company careers site, a fixture server) does not name.
ATS_NAMES: dict[str, str] = {
    "linkedin": "LinkedIn", "workday": "Workday", "greenhouse": "Greenhouse",
    "lever": "Lever", "icims": "iCIMS", "successfactors": "SAP SuccessFactors",
    "taleo": "Taleo", "oracle": "Oracle Cloud", "brassring": "BrassRing", "adp": "ADP",
    "ukg": "UKG", "dayforce": "Dayforce", "smartrecruiters": "SmartRecruiters",
    "jobvite": "Jobvite", "ashby": "Ashby", "workable": "Workable",
}


_INBOX_WANTS = {
    "code": "Does `messages[{i}]` carry a verification code or a security code for the reader "
            "to enter on a website?",
    # ACC-05: an account check by link (Workday, SuccessFactors, iCIMS)
    "link": "Does `messages[{i}]` ask the reader to open a link to verify an email address or "
            "to activate an account?",
}


def inbox_questions(messages: list[Mapping[str, Any]], site: str, *, ats: str = "",
                    company: str = "", want: str = "code") -> tuple[dict, dict]:
    """Two Nouls per message, one yes/no each: `msg_{n}_from_site` (sent by
    `site`, or by the applicant tracking service `ats` that handles
    `company`'s applications, when the system is a known one) and
    `msg_{n}_has_{want}`: with `want` "code", carries a verification or
    security code; with "link", asks for a link to be opened to verify the
    address or activate the account (ACC-05). `read_inbox` combines them.
    The site alone is the form's host, which rarely sends the mail; the ATS
    name is what the sender address shows."""
    rows = [{"n": int(m["n"]), "sender": str(m.get("sender", "")),
             "subject": str(m.get("subject", "")), "preview": str(m.get("preview", ""))}
            for m in messages]
    state = {"site": site, "messages": rows}
    ats_name = ATS_NAMES.get(str(ats or "").strip().lower(), "")
    questions: dict[str, Any] = {}
    for i, row in enumerate(rows):
        n = row["n"]
        if ats_name:
            from_site: dict[str, Any] = {
                "site_domain": site,
                "applicant_tracking_service": ats_name,
                "company": str(company or ""),
                "question": f"Was `messages[{i}]` sent by `site_domain`, or by "
                            "`applicant_tracking_service` (the applicant tracking service "
                            "that handles `company`'s job applications), judging by the "
                            "sender address and the message text?",
            }
        else:
            from_site = {
                "site_domain": site,
                "question": f"Was `messages[{i}]` sent by `site_domain` or on its behalf, "
                            "judging by the sender address and the message text?",
            }
        questions[f"msg_{n}_from_site"] = {"type": "noul", "instructions": from_site}
        questions[f"msg_{n}_has_{want}"] = {
            "type": "noul",
            "instructions": _INBOX_WANTS[want].format(i=i),
        }
    return state, questions


def read_inbox(answers: Mapping[str, Answer],
               messages: list[Mapping[str, Any]], want: str = "code") -> int | None:
    """The n of the message maximising `from_site * has_{want}`, with both
    above `INBOX_MIN`; None when no message qualifies."""
    best_n, best_p = None, 0.0
    for m in messages:
        n = int(m["n"])
        from_site = noul_of(answers, f"msg_{n}_from_site")
        has = noul_of(answers, f"msg_{n}_has_{want}")
        if from_site <= INBOX_MIN or has <= INBOX_MIN:
            continue
        p = from_site * has
        if p > best_p:
            best_n, best_p = n, p
    return best_n


def link_pick_questions(links: list[tuple[str, str]], body: str) -> tuple[dict, dict]:
    """ACC-05: one Choice `link_pick` over a message's verification links
    (each its text and its host, never its URL: a token rides there) plus
    `none`."""
    rows = [{"n": i, "text": str(text)[:READ_TEXT_CAP], "host": str(host)}
            for i, (text, host) in enumerate(links)]
    state = {"body": str(body or "")[:2000], "links": rows}
    criteria: dict[str, Any] = {f"link_{r['n']}": f"{r['text']} ({r['host']})" for r in rows}
    criteria["none"] = "No listed link verifies the address or activates the account"
    questions = {"link_pick": {
        "type": "choice",
        "instructions": "Which link in `links` verifies the email address or activates the "
                        "account that `body` asks the reader to confirm?",
        "criteria": criteria,
    }}
    return state, questions


def read_link_pick(answers: Mapping[str, Answer], count: int) -> int | None:
    """The picked link's index, or None (`none`, or no pick)."""
    choice, _ = _choice_of(answers, "link_pick")
    m = re.fullmatch(r"link_(\d+)", str(choice or ""))
    return int(m.group(1)) if m and int(m.group(1)) < count else None


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
    probs = [noul_of(answers, f"grounded_{i}") for i in range(len(sentences))]
    weakest = min(probs)
    return weakest >= GROUNDING_MIN, weakest
