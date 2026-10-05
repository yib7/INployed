"""The words that make a control a send, in one place.

The loop (`apply_run`), the judge (`apply_judge`), the popup check
(`apply_fill._open_menu`) and the page scripts (`apply_form`'s extractor,
`apply_fill`'s overlay picker and click arm) all ask whether a control's
words send the application or end it. The word lists live here once, as
regex alternatives. The Python patterns are compiled from them, and the page
scripts splice regex sources built from them (`js_union`, `js_submit`,
`js_send_lead`, `js_send_verb`, `js_send_object`) with the `__TOKEN__`
convention the scripts already use, so a word added here reaches every copy.
`tests/test_send_words.py` pins each built pattern and source to its text.

Pure data and pure functions: this module imports nothing from the project
at run time, so every module above it can import it.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import apply_form

# The words, once, each a regex alternative as the patterns spell it:
# - SUBMIT: a control with one of them sends the application;
# - FINAL: a last step's words ("Complete", "Confirm", "Finalize", "Done");
# - SEND: SUBMIT without "apply", the judge's send words and a popup's send
#   verbs (an Apply is an entry until the judge says it sends);
# - SEND_LEADS: a send verb that makes a short name a send when it leads it;
# - SEND_OBJECTS: what a send verb may be followed by and still name the
#   send: the application or its parts, the send's own words, a time,
#   another send verb.
SUBMIT = ("submit", "apply", "send", "finish")
FINAL = ("complete", "confirm", "finali[sz]e", "done")
SEND = tuple(w for w in SUBMIT if w != "apply")
SEND_VERBS = SEND + FINAL
SEND_LEADS = ("submit", "send")
SEND_OBJECTS = ("applications?", "forms?", "answers?", "responses?", "options?", "request",
                "submission", "now", "here", "everything", "all", "it", "this") + SEND_VERBS + ("apply",)


def _alt(words) -> str:
    """`words` as one regex group: ("a", "b") gives "(a|b)"."""
    return "(" + "|".join(words) + ")"


# The loop's send vocabulary: a button whose text has one of `SUBMIT_WORDS`
# reads as sending the application (`_submit_shaped`), and one with a
# `FINAL_WORDS` word as a last step (`_final_shaped`). The flow harness checks
# every click against the same words.
SUBMIT_WORDS = re.compile(r"\b" + _alt(SUBMIT) + r"\b", re.I)
FINAL_WORDS = re.compile(r"\b" + _alt(FINAL) + r"\b", re.I)
# the loop's words for a send and for a sign-in's own button: one name each;
# the send words are SUBMIT's without "apply" (`SEND`): an Apply is an
# entry until the judge says it sends
SEND_WORDS = re.compile(r"\b" + _alt(SEND) + r"\b", re.I)
# a sign-in's own words: "Sign in", "Log on", "Send code", "Send me a link"
SIGN_IN_WORDS = re.compile(r"\b(sign|log)[\s-]*(in|on)\b|\blogin\b"
                           r"|\bsend\s+(me\s+)?(an?\s+|the\s+)?(verification\s+|sign[\s-]*in\s+)?"
                           r"(code|link)\b", re.I)


def _submit_shaped(digest: apply_form.FormDigest, n: int) -> bool:
    """Whether the text describes a final application submission.

    Native ``type=submit`` is only a form mechanic: multi-step wizards often
    use it for Continue/Next. Explicit submit/apply/send/finish text remains
    gated even if the judge labels that control as an advance.
    """
    button = next((b for b in digest.buttons if b.n == n), None)
    return bool(button) and bool(SUBMIT_WORDS.search(button.text))


def _final_shaped(digest: apply_form.FormDigest, n: int) -> bool:
    """Words a last step's button also uses ("Complete", "Confirm",
    "Finalize", "Done"). In park mode such an advance goes through the submit
    gate, which parks it: a final button judged advance must not send the
    application the user asked to review. With submitting on it stays an
    advance, since "Complete profile" is a step too."""
    button = next((b for b in digest.buttons if b.n == n), None)
    return bool(button) and bool(FINAL_WORDS.search(button.text))


_ACCOUNT_STEP_WORDS = re.compile(r"\b(registration|register|sign[\s-]*up|account|profile)\b",
                                 re.I)
# A step's own words ("Next", "Save and continue", "Sign in", "Log in",
# "Create account", "Back", "Review application", "Preview"): a submit whose
# words are only these, and at least one step verb, names no send
# (`step_only`)
_STEP_VERB = re.compile(r"\b(next|continue|save|back|previous|proceed|sign|log|login|logon"
                        r"|create|register|review|preview)\b", re.I)
_STEP_ONLY = re.compile(r"(?:\b(?:next|continue|save|back|previous|proceed|step|sign|log|in|on"
                        r"|up|login|logon|create|account|register|review|preview|application"
                        r"|and|to|the|my|your|an?)\b"
                        r"|[\W_])+", re.I)


def step_only(text: str) -> bool:
    """Are `text`'s words only a step's ("Save and continue", "Sign in",
    "Create account"), with no send or last-step word (`SUBMIT_WORDS`,
    `FINAL_WORDS`)? Such a button is never the application's send: in
    submit mode the gate refuses it (`_JobRun._gate_read`)."""
    t = " ".join(str(text or "").split())
    return (bool(_STEP_VERB.search(t)) and bool(_STEP_ONLY.fullmatch(t))
            and not SUBMIT_WORDS.search(t) and not FINAL_WORDS.search(t))


# the "send" of a sign-in's code or link ("Send code", "Send me a link"),
# the part of `SIGN_IN_WORDS` that holds a submit word
_SEND_CODE = re.compile(r"\bsend\s+(me\s+)?(an?\s+|the\s+)?(verification\s+|sign[\s-]*in\s+)?"
                        r"(code|link)\b", re.I)


def _sign_in_only(text: str) -> bool:
    """Does a submit-worded control name a sign-in and nothing more? It
    holds a sign-in's words (`SIGN_IN_WORDS`) and its only
    submit words are "apply" ("Sign in to apply") or the "send" of a code
    or link ("Send code"). "Log in and submit application" and "Sign in and
    finish" still send."""
    t = text or ""
    if not SIGN_IN_WORDS.search(t):
        return False
    words = {w.lower() for w in SUBMIT_WORDS.findall(_SEND_CODE.sub(" ", t))}
    return not (words - {"apply"})


def _sends_application(digest: apply_form.FormDigest, n: int, *,
                       account_only: bool = True) -> bool:
    """A button the account step may not click: its text reads as sending the
    application (`_submit_shaped`), unless it names a sign-in and nothing
    more on a screen of the address and the password alone (`account_only`,
    `_sign_in_only`: "Sign in to apply" signs in, "Send code" mails one), or
    as a last step (`_final_shaped`) unless it names the account ("Complete
    registration"). "Create account and apply" counts as a send: the sign-up
    may carry the application. Only the submit gate sends an application."""
    button = next((b for b in digest.buttons if b.n == n), None)
    text = button.text if button else ""
    if _submit_shaped(digest, n):
        return not (account_only and _sign_in_only(text))
    return _final_shaped(digest, n) and not _ACCOUNT_STEP_WORDS.search(text)


def _send_worded(text: str, *, entry: bool = False, account: bool = False) -> bool:
    """A control's text reads as sending the application or as a last step:
    a submit word ("apply" too, unless the click is an Apply entry), unless
    an account step's text names a sign-in and nothing more
    (`_sign_in_only`); or a final word, unless it names the account
    ("Complete registration")."""
    words = {w.lower() for w in SUBMIT_WORDS.findall(text or "")}
    if entry:
        words.discard("apply")
    if words and not (account and _sign_in_only(text)):
        return True
    return bool(FINAL_WORDS.search(text or "")) and not _ACCOUNT_STEP_WORDS.search(text or "")


class PopupRefused(LookupError):
    """A popup whose own words send was not opened (`popup_refusal`)."""


# What a popup's own words must not say for the run to open it: a send or a
# last step, as the run's other clicks read them
# (`_send_worded`), a leading "Apply" too ("Apply with LinkedIn");
# never "Does not apply". They are read as a name (`send_phrase`):
# - a "submit" or "send" that leads a short name names a send, whatever
#   follows it ("Submit for review", "Submit resume", "Send to recruiter");
# - a last-step verb ("finish", "complete", "confirm", "done", "finalize"),
#   leading or not, and a send verb anywhere else, name one when nothing
#   follows, or what follows names the application or the send itself
#   ("Done", "Complete application", "Confirm and submit", "Submit ▾",
#   "More submit options", "Choose how to submit your application"), never
#   another thing ("Finish month", "Confirm your citizenship status",
#   "Expected finish date", "Willing to submit references");
# - a leading "Apply" names one alone or with "with", "now", "for"... ("Apply
#   a location" is a placeholder).
# Words that ask ("... a background check? Select One Required", Workday's
# aria-label) are no name; a shown value or a title under an outside
# question label is an answer ("I confirm" under "Do you agree to the
# terms?", "Send by post" under "Delivery method" or "Document delivery",
# "Complete" under "Resume status"), and under a label that names the
# application, a document, a step or an action ("Your application", "Resume
# *", "Step 3", "Share your profile") it is read.
_POPUP_VERB = re.compile(r"\b" + _alt(SEND_VERBS) + r"\b", re.I)
_POPUP_LEAD_MAX = 6             # words: a name a leading submit or send makes a send
_POPUP_LEAD = re.compile("^" + _alt(SEND_LEADS) + "$", re.I)
_POPUP_APPLY = re.compile(r"^\s*apply\b", re.I)
# what a send's verb may be followed by and still name the send (`SEND_OBJECTS`)
_POPUP_SEND_OBJECT = re.compile("^" + _alt(SEND_OBJECTS) + "$", re.I)
_POPUP_APPLY_OBJECT = re.compile(r"^(with|using|via|through|now|here|for|to|online|today)$",
                                 re.I)
_POPUP_FILLER = frozenset(("your", "the", "my", "this", "our", "a", "an", "and", "or", "&"))
_POPUP_QUESTION_TAIL = re.compile(r"\b(select one|required)\s*$", re.I)
_POPUP_WORD = re.compile(r"[a-z]+", re.I)
# a label that asks: a "?", "all that apply", Workday's "Select One" tail, or
# an interrogative first word
_ASKS = re.compile(
    r"\?|\ball that apply\b|\bselect one\b|^\s*(how|what|which|when|where|why|who|whom|whose|do|does"
    r"|did|are|is|was|were|will|would|can|could|have|has|had|should|may|might|shall)\b", re.I)
# a label that names the application, one of its documents, a step, or an
# action (a card's heading, a section's name): no question of a value's
_NAMES_THE_SEND = re.compile(
    r"\b(applications?|applying|submissions?|resumes?|résumés?|cv|cover\s+letters?"
    r"|documents?|attachments?|profiles?|candidacy|step\s*\d+)\b"
    r"|^\s*(share|send|submit|apply|upload|attach|save|review|continue|complete|finish|confirm"
    r"|finali[sz]e|proceed|next|done)\b", re.I)
# a label that names a document or the application and then the value asked
# of it ("Document delivery", "Resume status", "Application source"): a
# question
_VALUE_TAIL = re.compile(
    r"\b(status|delivery|method|type|format|date|preferences?|source|language|option|choice"
    r"|level|stage|mode|frequency|channel)\s*$", re.I)
_LEADS_ACTION = re.compile(
    r"^\s*(share|send|submit|apply|upload|attach|save|review|continue|complete|finish|confirm"
    r"|finali[sz]e|proceed|next|done)\b", re.I)


def _question_shaped(text: str) -> bool:
    """Words that ask: a "?" in them, or Workday's tail ("... Select One
    Required")."""
    return "?" in text or bool(_POPUP_QUESTION_TAIL.search(text))


def question_label(text: str) -> bool:
    """Is `text` (a label, a labelling element's or a question box's words)
    a question a value answers: words that ask (`_ASKS`), words that name no
    application, document, step or action (`_NAMES_THE_SEND`: "Degree
    status", "Delivery method" ask for a value; "Your application", "Resume
    *", "Step 3", "Share your profile" name the thing a send sends), or
    words that name one and then the value asked of it ("Document
    delivery", "Resume status": `_VALUE_TAIL`)?"""
    t = " ".join(str(text or "").split()).strip(" *✱＊")
    if not t:
        return False
    return bool(_ASKS.search(t)) or not _NAMES_THE_SEND.search(t) or (
        bool(_VALUE_TAIL.search(t)) and not _LEADS_ACTION.search(t))


def send_phrase(text: str) -> bool:
    """Do `text`'s words name a send or a last step (see `_POPUP_VERB`): a
    "submit" or "send" that leads a name of at most `_POPUP_LEAD_MAX` words,
    whatever follows it; any other send or last-step verb, leading or not,
    with nothing after it but fillers or symbols, or followed by the
    application, the send's own words or another send verb ("Finish month"
    and "Confirm your citizenship status" ask); a
    leading "Apply" alone or with "with", "now", "for"..."""
    words = _POPUP_WORD.findall(str(text or ""))
    if words and _POPUP_LEAD.fullmatch(words[0]) and len(words) <= _POPUP_LEAD_MAX:
        return True
    for i, w in enumerate(words):
        verb = bool(_POPUP_VERB.fullmatch(w))
        apply_lead = i == 0 and w.lower() == "apply" and bool(_POPUP_APPLY.search(text))
        if not (verb or apply_lead):
            continue
        rest = [x for x in words[i + 1:] if x.lower() not in _POPUP_FILLER]
        if not rest:
            return True
        if (_POPUP_APPLY_OBJECT if apply_lead else _POPUP_SEND_OBJECT).fullmatch(rest[0]):
            return True
    return False


def popup_refusal(words: dict) -> str:
    """Why a popup whose own words read `words` ({shown, aria, title, label,
    named, box}) must not be opened, or "": its aria-label, its title, the
    element outside it that names it (`named`, aria-labelledby), its
    `<label for>` or its shown text names a send or a
    last step (`send_phrase`). Words that ask (`_question_shaped`) are never
    read as a name. The shown text and the title are never read under an
    outside question label (`question_label` of its label, its labelling
    element or its question box's words) or a question in its own
    aria-label: they are the answer. Under any other outside label they are
    read."""
    aria = " ".join(str(words.get("aria") or "").split())
    answered = (bool(aria) and _question_shaped(aria)) or any(
        question_label(words.get(key) or "") for key in ("label", "named", "box"))
    for key in ("aria", "named", "label", "title", "shown"):
        text = " ".join(str(words.get(key) or "").split())
        if not text:
            continue
        if key in ("shown", "title") and answered:
            continue
        if key != "shown" and _question_shaped(text):
            continue
        if send_phrase(text):
            what = {"shown": "text", "named": "label"}.get(key, key)
            return f"its {what} reads {text[:60]!r}, a send"
    return ""


# The same words as regex sources for the page scripts.
def js_union() -> str:
    """SUBMIT and FINAL as a regex source for a JS string literal (each
    backslash doubled): the overlay picker and the click's arm build their
    `new RegExp` from it (`apply_fill._SEND_JS`)."""
    return r"\\b" + _alt(SUBMIT + FINAL) + r"\\b"


def js_submit() -> str:
    """SUBMIT as a JS regex literal: the extractor's submit hint."""
    return r"/\b" + _alt(SUBMIT) + r"\b/i"


def js_send_lead() -> str:
    """SEND_LEADS as the extractor's whole-word regex literal (`SEND_LEAD`)."""
    return "/^" + _alt(SEND_LEADS) + "$/i"


def js_send_verb() -> str:
    """SEND_VERBS as the extractor's whole-word regex literal (`SEND_VERB`)."""
    return "/^" + _alt(SEND_VERBS) + "$/i"


def js_send_object() -> str:
    """SEND_OBJECTS as the extractor's whole-word regex literal (`SEND_OBJECT`)."""
    return "/^" + _alt(SEND_OBJECTS) + "$/i"
