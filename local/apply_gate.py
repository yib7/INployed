"""The click guards: the submit switch (`submit_on`, `guard_submit`), the
submit gate (`can_submit`), and `live_refusal`, which refuses a click whose
control now reads as a send.

Split out of `apply_run`, which re-exports these names.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

import apply_judge
import apply_pause
from apply_judge import FillPlan, VerifyResult
from apply_send_words import _send_worded
from apply_outcome import _cap
from apply_account_flow import _page_words
from apply_route import _ACTED


SUBMIT_KEY = "auto_apply_submit"
# Set by `guard_submit` when the submit switch could not be trusted: the
# sentence the drain logs and writes at the top of its report.
PARK_MODE_KEY = "auto_apply_park_mode_why"


def submit_on(settings: Mapping[str, Any]) -> bool:
    """Does the run send applications? Only a stored `auto_apply_submit` that
    is the boolean True says yes: a string ("false", "true"), a number, null
    or a missing key all park at the submit."""
    return settings.get(SUBMIT_KEY) is True


def guard_submit(cfg: dict[str, Any], problem: str = "") -> dict[str, Any]:
    """`cfg` with the submit switch failing closed: when the config file
    could not be read or was started over beside a damaged copy
    (`problem`, `settings.submit_problem`) or its
    `auto_apply_submit` is not a boolean, the switch is False and
    `PARK_MODE_KEY` says why, for the log and the drain report. A switch
    the user turned off stays off with no note."""
    value = cfg.get(SUBMIT_KEY)
    if problem:
        why = f"{problem}, so the submit switch could not be read"
    elif not isinstance(value, bool):
        why = f"{SUBMIT_KEY} holds {type(value).__name__} {_cap(repr(value), 40)}, not true or false"
    else:
        return cfg
    cfg[SUBMIT_KEY] = False
    cfg[PARK_MODE_KEY] = f"Park mode: {why}. Nothing is submitted; fix it in Settings."
    return cfg


# an account button's own words: the one step button a page that types the
# master password may carry as its submit (`_JobRun._gate_read`)
_ACCOUNT_OWN_WORDS = re.compile(r"\b(?:create|register|join)\b|\bsign[\s-]*(?:up|in|on)\b"
                                r"|\blog[\s-]*(?:in|on)\b|\blogin\b|\blogon\b", re.I)


def live_refusal(role: str, expected: str, live: Mapping[str, Any], *,
                 account: bool = False) -> str:
    """Why a click in `role` must not happen on the control as it reads now,
    or "": its live text (`apply_form.live_text`) reads as sending
    the application or as a last step (`_send_worded`) while the text it was
    judged by did not, or the click is an Apply entry. A submit is the
    gate's and is never refused here."""
    if role == "submit":
        return ""
    entry = role == "apply_entry"
    now = " ".join(str(live.get("text") or "").split())
    if _send_worded(now, entry=entry, account=account) and (
            entry or not _send_worded(expected, entry=entry, account=account)):
        return f"it now reads {_cap(now, 60)!r}, a send, and the click was {role}"
    return ""


# --- the submit gate ----------------------------------------------------------------------

# Chrome's own validationMessage can quote the value typed
# ("'jane.doe' is missing an '@'"), so a native check is named by its reason
# code's words; a site's own message (`aria-invalid`, a custom validity)
# keeps its words without what it may quote (`_page_words`)
_VALIDITY_WORDS = {
    "typeMismatch": "not the kind of value the box takes",
    "patternMismatch": "does not match the requested format", "tooShort": "too short",
    "tooLong": "too long", "rangeUnderflow": "below the allowed range",
    "rangeOverflow": "above the allowed range", "stepMismatch": "not an allowed step",
    "badInput": "not a value the box takes", "invalid": "invalid"}


def _invalid_words(row: Mapping[str, Any]) -> str:
    label = " ".join(str(row.get("label") or "a field").split())[:80]
    reason = str(row.get("reason") or "")
    if reason == "valueMissing":
        return f"required field without an answer: {label} (the form reports it empty)"
    said = _VALIDITY_WORDS.get(reason) or _page_words(row.get("message"), 120) \
        or ("marked invalid by the site" if reason == "aria-invalid" else reason or "invalid")
    return f"the form reports an invalid field: {label} ({said})"


def can_submit(plan: FillPlan, verification: list[VerifyResult],
               settings: dict, live: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """(True, "") when the application may be sent, else (False, the first
    failing reason): the setting, the plan's park reason, a required field
    without an answer (any action other than fill / select / upload), a
    required field unverified, the submit button's confidence. The
    prohibited and captcha flags are recorded only (`apply_judge`'s rule).
    A field marked `apply_pause.KEPT` holds the person's own value, typed in
    the browser during a pause: the run never typed or verifies it.
    A password box still marked `PASSWORD_ACTION` holds the master password:
    `_JobRun._fill_passwords` typed it and checked its length in the page,
    and a required box it could not fill parked the job before the gate. The
    judge never sees it, so it has no verification row.

    `live` is the page as the gate read it just before
    (`_JobRun._gate_read`): `no_application` (nothing was filled on this page or
    an earlier one: the page holds no application), `apply_button` (an
    Apply-worded button without the DOM evidence and the judge's word that
    it sends the finished application), `step_button` (in submit mode, a
    submit whose words are only a step's: `step_only`), `invalid` (the
    submit's form holds a control that would not validate, or one marked
    `aria-invalid`), `required_empty` (a required control the extractor
    leaves out is empty) and `unreadable` (the page could not answer those
    two checks); each fails the gate with its evidence."""
    if not submit_on(settings):
        return False, "auto_apply_submit is off"
    if plan.park_reason:
        return False, plan.park_reason
    by_n = {v.n: v for v in verification}
    for pf in plan.fields:
        if not pf.required or pf.action in (apply_judge.PASSWORD_ACTION, apply_pause.KEPT):
            continue
        if pf.action not in _ACTED:
            # a skip, or a `generate` nobody resolved: the field holds nothing
            return False, f"required field without an answer: {pf.label}"
        v = by_n.get(pf.n)
        if v is None:
            return False, f"required field not verified: {pf.label}"
        if not v.ok:
            return False, (f"required field failed verification: {pf.label} "
                           f"(p_correct {v.p_correct:.2f}, p_placeholder "
                           f"{v.p_placeholder:.2f})")
    submit = plan.buttons.get("submit")
    if submit is None:
        return False, "no submit button"
    if submit[1] < apply_judge.BUTTON_SUBMIT_MIN_CONF:
        return False, (f"submit button confidence {submit[1]:.2f} below "
                       f"{apply_judge.BUTTON_SUBMIT_MIN_CONF:.2f}")
    live = live or {}
    if live.get("unreadable"):
        return False, str(live["unreadable"])
    if live.get("no_application"):
        return False, f"no application on the page ({live['no_application']})"
    if live.get("apply_button"):
        return False, str(live["apply_button"])
    if live.get("step_button"):
        return False, str(live["step_button"])
    for row in live.get("invalid") or []:
        return False, _invalid_words(row)
    for row in live.get("required_empty") or []:
        return False, _control_words(row)
    return True, ""


def _control_words(row: Mapping[str, Any]) -> str:
    """A required control the extractor leaves out, still empty, as a park's
    reason: one the run cannot read into says why."""
    label = " ".join(str(row.get("label") or "a control").split())[:80]
    if row.get("kind") == "unreadable":
        return (f"required field without an answer: {label} (a control the run cannot read: "
                f"{row.get('why') or 'its inside is closed'})")
    return (f"required field without an answer: {label} (a {row.get('kind')} control the run "
            f"does not fill)")
