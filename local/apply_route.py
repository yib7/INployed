"""Where a page goes next: the state sets, the LinkedIn, unsure,
confirmation, posting and form routes, `loop_step` (the probe's text of the
loop's order), and the fill-check helpers.

Split out of `apply_run`, which re-exports these names.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import apply_fill
import apply_form
import apply_judge
import apply_limits
import apply_linkedin
import apply_pause
import apply_send_words
from apply_judge import FillPlan
from apply_send_words import _final_shaped, _submit_shaped
from apply_outcome import (ALREADY_APPLIED_REASON, CHECK_SENT_REASON, CLOSED_POSTING_REASON,
                           EASY_APPLY_REASON, _Parked, SSO_REASON, WRONG_ANSWER_STAYS)
from apply_sendwatch import confirmation_words
from apply_page import _button_text, _chrome, _code_field
from apply_account_flow import _credential_form, sso_only, _THIRD_PARTY


_ACTED = ("fill", "select", "upload")     # the actions that put a value on the page
_PARK_STATES = {
    "captcha_or_bot_check": "captcha or bot check on the page",
    "payment_request": "payment requested",
    "error_or_dead": "error or dead page",
    "other": "unrecognised page",
}
# A tab on Chrome's own error page after a load the network dropped parks
# with the error state's words once its one retry is spent
ERROR_PAGE_REASON = _PARK_STATES["error_or_dead"]
# The send that never reached the site: nothing was sent
UNSENT_NOTE = "nothing was sent: Re-queue once the site answers again"
# A page read below `apply_judge.PAGE_STATE_MIN_CONF` is still acted on as one
# of these (`_JobRun._check_unsure`): each step has gates of its own (the Apply
# entry's confidence, the fill plan's, the submit gate, the accounts hook's
# site check and `_credential_form`), so a wrong guess stops at one of them or
# moves the run on (the user's rule, 2026-09-22). An unsure confirmation, bot
# check, payment, error or unrecognised page parks.
_UNSURE_ACTS = frozenset(("job_posting", "application_form", "review_page",
                          "login_wall", "signup_form", "code_gate"))
# The kinds an unsure read may also take from the page's structure alone
# (`unsure_step`): a bot check the run waits on, a closed posting it parks on.
_STRUCTURE_ENDS = frozenset(("captcha_or_bot_check", "error_or_dead"))
# The steps that put a value on a page or send it: none of them runs on
# LinkedIn, where a form is Easy Apply's (the user applies there in person).
_LINKEDIN_FORM_STATES = frozenset(("application_form", "review_page", "code_gate"))
# The pages the run acts on, and so maps (`_JobRun._map`): the field and
# button questions are asked only there.
_MAPPED_STATES = frozenset(("job_posting", "application_form", "review_page", "login_wall",
                            "signup_form", "code_gate"))
# An Apply that sends a stored profile from another site instead of opening
# the company's form (`apply_judge.PROFILE_APPLY`): never an Apply entry the
# run clicks; the controls of a LinkedIn or a job board's frame never reach
# the choice (`_JobRun._drop_foreign_controls`).
_PROFILE_APPLY = apply_judge.PROFILE_APPLY
# What the page after a submit click on an account page ("Create account and
# apply") may read as when the click only made the account and opened the
# application (`_JobRun._after_submit`): the job then waits for the user,
# since a sent application's page can look the same.
_OPENED_BY_ACCOUNT = frozenset(("application_form", "login_wall", "signup_form"))
# the reads a page that says a verification link was emailed (and has no box
# to fill) is taken from: it is the account check. A confirmation is
# one of them: `apply_judge.link_sent` holds no page with received words, and
# the live judge read "Check your inbox ... click the link in the email to
# confirm your application" as a confirmation
_LINK_REMAPS = frozenset(("application_form", "review_page", "login_wall", "signup_form",
                          "confirmation"))
# The only Apply entry `probe --follow-apply` clicks: "Apply", "Apply now",
# "Apply for this job", "Apply on company website". A quick, one-click or
# third-party apply may send a stored profile at once.
_PLAIN_APPLY = re.compile(r"^\s*apply(\s+(now|here|online|for\s+this\s+(job|position|role)"
                          r"|on\s+(the\s+)?company(['’]s)?\s+(website|site)))?\s*$", re.I)


def linkedin_step(url: str, decision: apply_linkedin.Decision | None) -> str | None:
    """What the LinkedIn handler does with a page (`_JobRun._linkedin_step`),
    in words, or None when the page goes on to the judge (off LinkedIn, or a
    LinkedIn page other than a job page with nothing for the handler)."""
    kind = apply_linkedin.url_kind(url)
    if not kind:
        return None
    if kind == "signed_out":
        return (f"park: {apply_linkedin.SIGNED_OUT_REASON} "
                f"({apply_linkedin.signed_out_evidence(url)})")
    if kind == "redirector":
        return "follow the LinkedIn redirect to the company's site"
    d = decision or apply_linkedin.Decision("none", "not read")
    if kind == "other" and d.kind not in ("form_dialog", "signed_out"):
        return None
    if d.kind in ("form_dialog", "easy_apply"):
        return f"park: {EASY_APPLY_REASON}"
    if d.kind == "applied":
        return f"park: {apply_linkedin.APPLIED_REASON}"
    if d.kind == "closed":
        return f"park: {apply_linkedin.CLOSED_REASON}"
    if d.kind == "signed_out":
        return f"park: {apply_linkedin.SIGNED_OUT_REASON} ({d.why})"
    if d.kind == "offsite":
        return f"click the offsite Apply {d.control.label!r} (the LinkedIn handler)"
    return f"park: {apply_linkedin.NO_APPLY_REASON}"


def linkedin_view(page, *, wait_s: float, job_title: str = "", company: str = "",
                  clock: Callable[[], float] = time.monotonic,
                  deadline: float | None = None) -> tuple[apply_linkedin.View, int]:
    """The LinkedIn page's `View`, read again every `LINKEDIN_POLL_MS` for up
    to `wait_s` until it decides something waiting cannot change (a top card
    rendered late, 2.5 s after `load` in the fixture); (the view, the ms
    waited). An Easy Apply read, and an offsite Apply found in a list item
    (`Decision.tentative`), count only once they hold after
    `LINKEDIN_EASY_RECHECK_S` and a settle: a list's or a filter's control
    can show before the job's own top card renders. `job_title` and
    `company` are the queued job's (`apply_linkedin.read`). With a
    `deadline` on `clock` (the job's), the polling ends with the job's
    time; an unsure read is still held once more."""
    start = clock()
    if deadline is not None:
        wait_s = min(wait_s, deadline - start)

    def _read() -> apply_linkedin.View:
        return apply_linkedin.read(page, job_title=job_title, company=company)

    def _recheck() -> apply_linkedin.View:
        page.wait_for_timeout(int(apply_limits.LINKEDIN_EASY_RECHECK_S * 1000))
        apply_fill.settle(page, apply_limits.CLICK_TIMEOUT_S)
        return _read()

    view = _read()
    checked = wait_s <= 0
    while clock() - start < wait_s:
        d = apply_linkedin.decide(view)
        unsure = d.kind == "easy_apply" or d.tentative
        if (d.final or d.tentative) and (checked or not unsure):
            break
        if unsure:
            checked = True
            view = _recheck()
        else:
            page.wait_for_timeout(apply_limits.LINKEDIN_POLL_MS)
            view = _read()
    d = apply_linkedin.decide(view)
    if not checked and (d.kind == "easy_apply" or d.tentative):
        view = _recheck()       # the wait ran out on a read that is held once more
    return view, int((clock() - start) * 1000)


def remaps_to_form(state: str, digest: apply_form.FormDigest, url: str) -> bool:
    """A sign-in or sign-up read of a page of form boxes (off LinkedIn) is the
    form: the account step would type the facts in and click its button."""
    return (state in ("login_wall", "signup_form") and bool(digest.fields)
            and not _credential_form(digest) and not apply_linkedin.is_linkedin(url))


def unsure_acts(state: str, digest: apply_form.FormDigest) -> bool:
    """May a read below `PAGE_STATE_MIN_CONF` go on as its guess? Only one of
    `_UNSURE_ACTS`, a code gate only with its code box or the words of an
    emailed verification link (`apply_judge.link_sent`), a sign-in or sign-up
    only on a screen of account boxes (`_credential_form`) or of form boxes
    (the form, `remaps_to_form`)."""
    if state not in _UNSURE_ACTS:
        return False
    if state == "code_gate":
        # its code box, or the emailed link it asks for
        return _code_field(digest.fields) is not None or bool(apply_judge.link_sent(digest))
    if state in ("login_wall", "signup_form"):
        return bool(digest.fields)
    return True


def other_step(facts: apply_judge.PageFacts, digest: apply_form.FormDigest) -> str | None:
    """A page read as `other` (none of the listed kinds) at or above the
    floor: the kind its structure settles (`apply_judge.structural_kind`,
    strict) when the loop acts on it or waits on it, else None (it parks
    as unrecognised)."""
    kind = apply_judge.structural_kind(facts, strict=True)
    if kind is not None and (unsure_acts(kind, digest) or kind == "captcha_or_bot_check"):
        return kind
    return None


def unsure_step(state: str, digest: apply_form.FormDigest,
                facts: apply_judge.PageFacts) -> tuple[str | None, str]:
    """What a read still under the floor after its second look does: the
    kind the page's structure settles (`apply_judge.structural_kind`,
    strict: a code box, a box that makes a password, application boxes, an
    Apply entry with no box, a bot check, a closed posting), "structure";
    else its guess when the loop may act on it (`unsure_acts`) and the
    structure does not rule it out, "guess"; else the kind the structure
    leans to, "structure"; else (None, "") and the job parks. A kind is taken
    only when the loop acts on it, waits on it (a bot check) or parks on it
    (a closed posting)."""
    def takes(kind: str | None) -> bool:
        return kind is not None and (unsure_acts(kind, digest) or kind in _STRUCTURE_ENDS)

    settled = apply_judge.structural_kind(facts, strict=True)
    if takes(settled):
        return settled, "guess" if settled == state else "structure"
    lean = apply_judge.structural_kind(facts)
    ruled_out = (lean is not None and lean != state
                 and apply_judge.structure_against(facts, state))
    if unsure_acts(state, digest) and not ruled_out:
        return state, "guess"
    if takes(lean):
        return lean, "structure"
    return None, ""


def _runner_up(answers: Mapping[str, Any], exclude: str) -> tuple[str, float]:
    """The page state's most probable read other than `exclude`, with its
    probability."""
    a = answers.get("page_state") if answers else None
    probs = dict(getattr(a, "probabilities", None) or {})
    rows = sorted(((float(p), str(s)) for s, p in probs.items()
                   if s != exclude and float(p or 0.0) > 0), reverse=True)
    return (rows[0][1], rows[0][0]) if rows else ("", 0.0)


def confirmation_step(digest: apply_form.FormDigest, answers: Mapping[str, Any], conf: float, *,
                      submit_clicked: bool, code_sent: bool = False,
                      before: str | None = None) -> tuple[str, str, float]:
    """What a page read as a confirmation means: ("submitted", the reason,
    conf), ("go_on", the read to act on, its probability) or ("park", the
    reason, conf).

    After the submit gate's click (`submit_clicked`; the code step never
    sets it): received words on the page (`CONFIRMATION_WORDS`), or the
    read at `CONFIRMATION_MIN_CONF` on a page with no form field and no send
    button, is the confirmation; else the person checks. On a page that
    still shows a form field or a send button, the received words count
    only when they are new since `before` (the page's text just before the
    click, `new_confirmation`): a form that said "we received your
    application" before the click and still stands may have refused it.
    After a code step's click alone (`code_sent`: a sign-up's email "Verify"
    is one, whatever role it was judged in) only received words on a page
    with no form make it the confirmation; else the person checks. Before
    any submit click a confirmation is never the run's own send: a page
    with a form field, a submit button, a Next, a Continue or an accept and
    no received words is a form or a review misread, and goes on as its
    next read when the loop acts on that one (`_UNSURE_ACTS`); any other
    parks (the job may have been applied to before)."""
    words = confirmation_words(digest.text)
    form = bool(digest.fields) or any(apply_send_words.SEND_WORDS.search(b.text) for b in digest.buttons)
    # before any submit, a Next or a Continue asks for more too (a wizard's
    # summary step read as a confirmation)
    step_button = any(apply_judge.ADVANCE_WORDS.search(b.text) for b in digest.buttons)
    if submit_clicked:
        if form and before is not None:
            words = words - confirmation_words(before)
        if words or (conf >= apply_judge.CONFIRMATION_MIN_CONF and not form):
            return "submitted", "confirmation page", conf
        return "park", (f"{CHECK_SENT_REASON}: the page after the submit click reads as "
                        f"confirmation ({conf:.2f}) with a form or a send button and no "
                        f"received words it did not show before the click"), conf
    if code_sent and not form:
        if words:
            return "submitted", "confirmation page after the emailed code", conf
        return "park", (f"{CHECK_SENT_REASON}: after the emailed code the page reads as "
                        f"confirmation ({conf:.2f}) with no received words (an email "
                        f"verification thanks the same way)"), conf
    if code_sent and words:
        return "park", (f"{CHECK_SENT_REASON}: after the emailed code the page shows received "
                        f"words with a form or a send button still on it ({conf:.2f})"), conf
    if (form or step_button) and not words:
        second, p = _runner_up(answers, "confirmation")
        if second in _UNSURE_ACTS and not (second == "code_gate"
                                           and _code_field(digest.fields) is None):
            return "go_on", second, p
    return "park", (f"a confirmation page before any submit ({conf:.2f}); check whether this "
                    f"job was applied to before"), conf


def posting_entry_choice(digest: apply_form.FormDigest, plan: FillPlan, *,
                         apart: frozenset[int] | set[int] = frozenset(),
                         unclassified: int = 0) -> tuple[int | None, str]:
    """(the posting's Apply entry, how it was chosen): the judge's confident
    `apply_entry` (unless it is an Apply that sends a stored profile,
    `_PROFILE_APPLY`; the button of a form that holds controls,
    `Button.in_form`; or, on a page with form fields or controls the
    extractor leaves out (`unclassified`), a submit-worded button that sits
    with them: one `apart` from them, a job-alert box beside the
    posting's Apply, is the entry), else the fieldless text match, else on a
    page with fields an Apply-worded control `apart` from them (the
    judge took the alert box's button for the entry); (None, "") when
    none."""
    entry = plan.buttons.get("apply_entry")
    if entry is not None and _chrome(digest, entry[0]):
        entry = None            # the site's header is no posting's entry
    if entry is not None and entry[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF \
            and not _PROFILE_APPLY.search(_button_text(digest, entry[0])):
        button = next((b for b in digest.buttons if b.n == entry[0]), None)
        fields = bool(digest.fields) or unclassified > 0
        own = (button is not None and button.in_form) or (
            fields and _submit_shaped(digest, entry[0]) and entry[0] not in apart)
        if not own:
            return entry[0], "judged_apply_entry"
    n = fieldless_apply_choice(digest, unclassified=unclassified)
    if n is not None:
        return n, "fieldless_text"
    n = next((b.n for b in digest.buttons if b.n in apart and not b.in_form
              and apply_judge.entry_worded(b.text)), None)
    return (n, "apart_text") if n is not None else (None, "")


_FORM_KINDS = frozenset(("input", "select", "textarea", "textbox", "contenteditable", "spinbutton",
                         "combobox"))


def posting_context(page, digest: apply_form.FormDigest,
                    plan: FillPlan) -> tuple[set[int], int, list[dict[str, Any]]]:
    """What a posting's entry choice reads from the live page: the
    Apply-worded controls outside any form (the judged Apply
    entry when it is submit-worded, and every control that reads as an
    entry, `apply_judge.entry_worded`) that sit apart from the page's form
    fields (`apply_form.same_scope`), the number of form controls the
    extractor leaves out (`apply_form.control_scan`: a shadow-DOM or ARIA
    textbox), and those controls."""
    apart: set[int] = set()
    try:
        scan = [r for r in apply_form.control_scan(page) if r.get("kind") in _FORM_KINDS]
    except Exception:       # noqa: BLE001  (a page double)
        scan = []
    if not digest.fields:
        return apart, len(scan), scan
    entry = plan.buttons.get("apply_entry")
    wanted = {entry[0]} if entry is not None and _submit_shaped(digest, entry[0]) else set()
    wanted |= {b.n for b in digest.buttons if apply_judge.entry_worded(b.text)}
    for button in digest.buttons:
        if button.n not in wanted or button.in_form:
            continue
        try:
            verdict, _ = apply_form.same_scope(page, button.locator,
                                               [f.locator for f in digest.fields])
        except Exception:       # noqa: BLE001
            verdict = "unclear"
        if verdict == "apart":
            apart.add(button.n)
    return apart, len(scan), scan


_NEXT_WORDS = re.compile(r"\b(next|continue)\b", re.I)
# a Continue that leaves the application: "Continue later", "Continue browsing jobs"
# (never one that goes on to the application: "Continue to job application")
_NOT_NEXT = re.compile(r"\blater\b|\bbrows\w*|\bsearch\w*|\bshopping\b"
                       r"|\b(more|other|similar|all|saved)\s+(jobs|roles|openings|positions)\b",
                       re.I)
_WITH_WORDS = re.compile(r"\bwith\b|\bsign[\s-]*(in|up)\b|\blog[\s-]*in\b", re.I)


_STEP_OF = re.compile(r"\b(?:step|page)\s+(\d+)\s*(?:of|/)\s*(\d+)\b", re.I)


def step_position(digest: apply_form.FormDigest) -> tuple[int, int] | None:
    """(this step, the steps in all) when the page says so ("Step 2 of 4",
    "Page 1 / 3"), else None. A page that shows several different markers
    (a progress list that names every step) says nothing of which one it
    is on: None."""
    marks = {(int(m.group(1)), int(m.group(2)))
             for m in _STEP_OF.finditer(f"{digest.title or ''}\n{digest.text or ''}")}
    if len(marks) != 1:
        return None
    here, total = next(iter(marks))
    return (here, total) if 0 < here <= total else None


def form_route(digest: apply_form.FormDigest, plan: FillPlan, *,
               park_mode: bool, submit_apart: bool = False,
               judged: Mapping[int, str] | None = None) -> tuple[str, tuple[int, float] | None,
                                                                  str]:
    """A filled page's way on, as (step, button, why): "advance" with the
    confident advance to click; "gate" with the button the submit gate
    judges (the judged submit, a submit-shaped advance, a final-shaped one
    in park mode, or a form's own submit-worded Apply); "stuck" when there
    is neither. `why` names a routing to the gate. An advance that reads as
    declining ("I Decline", "Cancel"), or that signs in or applies with
    another site ("Continue with LinkedIn"), is never the way on;
    with no advance and no submit, a step's own accept-worded button (a
    privacy agreement's "I Accept") is, at the advance floor. With a
    confident advance and a judged submit both, the advance is the
    way on unless the page shows it is the last step: its step marker says
    so, or, with no marker, the submit sits with the page's own fields
    (`submit_apart` False: the caller reads the page).

    The roles look exchanged when the judged advance
    is no way on (the site's header, a decline, a sign-in elsewhere) or is
    a stranger to the step (outside any form that holds the page's fields,
    with no step's word: a chat window's "Start chat") while the page's own
    Next or Continue was judged other: that button is the way on; never a
    "Continue with ..." sign-in, never a send, never disabled. `judged`: the
    role the judge gave each button (the runner's read); without it only
    the plan's own "other" is looked at. An unjudged Next stays unclicked."""
    advance = plan.buttons.get("advance")
    submit = plan.buttons.get("submit")
    why = ""
    by_n = {b.n: b for b in digest.buttons}
    others = ([n for n, role in judged.items() if role == "other"] if judged is not None
              else [plan.buttons["other"][0]] if plan.buttons.get("other") else [])
    own_next = next((b for b in digest.buttons if b.n in others and _NEXT_WORDS.search(b.text)
                     and not _WITH_WORDS.search(b.text) and not _NOT_NEXT.search(b.text)
                     and not apply_judge.DECLINE_WORDS.search(b.text)
                     and not _submit_shaped(digest, b.n) and not getattr(b, "chrome", False)
                     and not getattr(b, "disabled", False)), None)
    header_advance = advance is not None and _chrome(digest, advance[0])
    excluded = advance is not None and (
        apply_judge.DECLINE_WORDS.search(_button_text(digest, advance[0]))
        or _THIRD_PARTY.search(_button_text(digest, advance[0])) or header_advance)
    if excluded:
        advance = None          # a decline, a sign-in elsewhere or the site's header
    elif advance is not None and submit is None and own_next is not None \
            and own_next.n != advance[0] and advance[0] in by_n \
            and not by_n[advance[0]].in_form and own_next.in_form \
            and not _NEXT_WORDS.search(by_n[advance[0]].text) \
            and not _submit_shaped(digest, advance[0]):
        # a stranger took the advance (a chat window's "Start chat") while
        # the form's own Next was judged other: the roles look exchanged
        advance = (own_next.n, apply_judge.BUTTON_ADVANCE_MIN_CONF)
    if advance is None and submit is None:
        accept = next((b for b in digest.buttons if apply_judge.ACCEPT_WORDS.search(b.text)
                       and not apply_judge.DECLINE_WORDS.search(b.text)
                       and "cookie" not in b.text.lower()
                       and not getattr(b, "chrome", False)), None)
        if accept is None and excluded and own_next is not None:
            # the header or a stranger took the advance while the page's own
            # Next or Continue was judged other (Workday's "Save and
            # Continue" beside its header's Sign In)
            accept = own_next
        if accept is not None:
            advance = (accept.n, apply_judge.BUTTON_ADVANCE_MIN_CONF)
    if advance is not None and (_submit_shaped(digest, advance[0])
                                or (park_mode and _final_shaped(digest, advance[0]))):
        why = "the advance button is submit-shaped"
        if submit is None:
            submit = advance
        advance = None
    if submit is not None and advance is not None \
            and advance[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF:
        at = step_position(digest)
        if (at is not None and at[0] < at[1]) or (at is None and submit_apart):
            # A feedback box's or a talent network's Submit beside a
            # step's Next; the step goes on, the gate waits for the last one
            return "advance", advance, ""
    entry = plan.buttons.get("apply_entry")
    if submit is None and entry is not None and _submit_shaped(digest, entry[0]):
        why = "the apply_entry button on a form is submit-shaped"
        submit = entry
    if submit is None and advance is not None \
            and advance[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF:
        return "advance", advance, why
    if submit is not None:
        return "gate", submit, why
    return "stuck", None, why


def review_route(digest: apply_form.FormDigest, plan: FillPlan, *, submit_apart: bool = False,
                 judged: Mapping[int, str] | None = None
                 ) -> tuple[str, tuple[int, float] | None, str]:
    """A page read as a review's way on, as `form_route` gives it:
    with no submit, its confident advance is clicked (a wizard's middle step
    read as the review carries only Next); a submit, a submit-shaped advance
    and a final-shaped one ("Confirm") go to the gate in either mode, since
    on a review a last-step word is the send; "stuck" when there is
    neither. A step's Next beside another box's Submit goes on as on a
    form (`submit_apart`, the step marker)."""
    return form_route(digest, plan, park_mode=True, submit_apart=submit_apart, judged=judged)


def _entry_shaped(b: apply_form.Button | None) -> bool:
    """A button that may be clicked as an Apply entry: no form's own button
    (`Button.in_form`) and no Apply that sends a stored profile."""
    return b is not None and not b.in_form and not _PROFILE_APPLY.search(b.text)


def plan_fills(plan: FillPlan) -> bool:
    """Does the plan put a value on the page (the probe's stand-in for the
    run's fill)?"""
    return any(pf.action in _ACTED and (pf.value or pf.option) for pf in plan.fields) or any(
        pf.action == apply_judge.PASSWORD_ACTION for pf in plan.fields)


def form_entry_choice(page, digest: apply_form.FormDigest, plan: FillPlan, *, park_mode: bool,
                      filled: bool) -> apply_form.Button | None:
    """The one rule the run and the probe share for a form step's Apply
    entry: with nothing of the application on the page or before it
    (`filled` False: the run's fill flags, the probe's `plan_fills`), the
    candidate `form_step_entry` picks, unless the live page puts it in the
    same form or box as the page's fields (`apply_form.same_scope` "same":
    the form's own Apply, whatever its fill came to)."""
    if filled:
        return None
    step, button, _ = form_route(digest, plan, park_mode=park_mode)
    b = form_step_entry(digest, plan, step, button)
    if b is not None and digest.fields and page is not None:
        try:
            verdict, _ = apply_form.same_scope(page, b.locator, [f.locator for f in digest.fields])
        except Exception:       # noqa: BLE001  (a page double)
            verdict = "unclear"
        if verdict == "same":
            return None
    return b


def form_step_entry(digest: apply_form.FormDigest, plan: FillPlan, step: str,
                    button: tuple[int, float] | None) -> apply_form.Button | None:
    """On a form step with nothing of the application on it or before it,
    the Apply entry to click instead of the gate: the judged
    `apply_entry`, else an Apply-worded advance or gate button (`button`,
    `form_route`'s), each confident and `_entry_shaped`; None when the step
    is an advance or none fits."""
    if step == "advance":
        return None
    by_n = {b.n: b for b in digest.buttons}
    picks = [plan.buttons.get("apply_entry"), plan.buttons.get("advance"), button]
    for i, pick in enumerate(picks):
        if pick is None or pick[1] < apply_judge.BUTTON_ADVANCE_MIN_CONF:
            continue
        b = by_n.get(pick[0])
        if _entry_shaped(b) and (i == 0 or apply_judge.apply_worded(b.text)):
            return b
    return None


def loop_step(url: str, digest: apply_form.FormDigest, plan: FillPlan, state: str,
              conf: float, reads: str = "", *, park_mode: bool = False,
              linkedin: apply_linkedin.Decision | None = None,
              answers: Mapping[str, Any] | None = None,
              apart: frozenset[int] | set[int] = frozenset(), unclassified: int = 0,
              form_entry: int | None = None,
              facts: apply_judge.PageFacts | None = None) -> str:
    """What the loop does with a fresh page, in words (`probe` prints it),
    from the loop's own decision helpers: the LinkedIn handler
    (`linkedin_step`, on the page's `linkedin` decision), a job the site says
    was applied to (`apply_judge.already_applied`), the remap of a sign-in
    read of form boxes, a confirmation read before any submit
    (`confirmation_step`, over the judge's `answers`), a form step on
    LinkedIn, a sign-in with another site's account only (`sso_only`), the
    unsure-read rule (`unsure_step`, over the page's `facts`), a page that
    says a verification link was emailed (the link step),
    the posting's entry (with the live page's `apart` and `unclassified`,
    `posting_context`), a form step's Apply entry (`form_entry`, the shared
    `form_entry_choice`), the form's route to an advance or the submit gate,
    a closed posting's park."""
    def named(n):
        return f"[{n}] {_button_text(digest, n)!r}"

    handled = linkedin_step(url, linkedin)
    if handled is not None:
        return handled
    facts = facts if facts is not None else apply_judge.page_facts(digest, url)
    applied = apply_judge.already_applied(answers or {}, facts)
    if applied:
        return f"park: {ALREADY_APPLIED_REASON} ({applied})"
    unsure = conf < apply_judge.PAGE_STATE_MIN_CONF
    lead = ""
    if remaps_to_form(state, digest, url):
        lead = f"read as {state} with no account boxes: it is the form; "
        state = "application_form"
    if state == "confirmation" and not facts.link_sent:
        step, detail, then = confirmation_step(digest, answers or {}, conf, submit_clicked=False)
        if step == "park" and unsure:
            pass                # the unsure rule below decides, as the loop's does
        elif step != "go_on":
            return lead + (f"park: {detail}" if step == "park" else "finish: submitted")
        else:
            lead += f"read as confirmation on a form: going on as {detail}; "
            state, conf = detail, then
            unsure = conf < apply_judge.PAGE_STATE_MIN_CONF
    on_linkedin = apply_linkedin.is_linkedin(url)
    if on_linkedin and state in _LINKEDIN_FORM_STATES:
        return lead + f"park: {EASY_APPLY_REASON}"
    if state != "confirmation" and not on_linkedin:
        sites = sso_only(digest)
        if sites:
            return lead + (f"park: {SSO_REASON} ({', '.join(sites)}); the run never signs "
                           "in with another site")
    if unsure:
        step, how = unsure_step(state, digest, facts)
        if step is None:
            suffix = f"; reads: {reads}" if reads else ""
            return lead + f"park: unsure what this page is ({state}, {conf:.2f}){suffix}"
        if how == "structure":
            lead += f"unsure ({state}, {conf:.2f}), its structure reads {step}; "
            state = step
            if on_linkedin and state in _LINKEDIN_FORM_STATES:
                return lead + f"park: {EASY_APPLY_REASON}"
        else:
            lead += f"go on with the unsure read ({state}, {conf:.2f}); "
    elif state == "other":
        settled = other_step(facts, digest)
        if settled is not None:
            lead += f"read as other ({conf:.2f}), its structure reads {settled}; "
            state = settled
            if on_linkedin and state in _LINKEDIN_FORM_STATES:
                return lead + f"park: {EASY_APPLY_REASON}"
    if state in _LINK_REMAPS and facts.link_sent:
        lead += (f"read as {state}; the page says {facts.link_sent!r} and has no box to "
                 "fill: an account check by an emailed link; ")
        state = "code_gate"
    if state == "job_posting" and on_linkedin:
        if digest.fields and posting_entry_choice(digest, plan)[0] is None:
            return lead + f"park: {EASY_APPLY_REASON}"
        return lead + f"park: {apply_linkedin.NO_APPLY_REASON}"
    if state == "job_posting":
        n, how = posting_entry_choice(digest, plan, apart=apart, unclassified=unclassified)
        if n is not None:
            return lead + f"click the Apply entry {named(n)} ({how})"
        if digest.fields:
            state = "application_form"
            lead += "a posting with form fields is the form; "
        else:
            return lead + "park: no Apply button on the posting"
    if state == "application_form":
        step, button, _ = form_route(digest, plan, park_mode=park_mode)
        if form_entry is not None:
            return lead + f"click the Apply entry {named(form_entry)} (judged_apply_entry)"
        if step == "advance":
            return lead + f"fill the page, then click the advance {named(button[0])}"
        if step == "gate":
            return lead + f"fill the page, then the submit gate with {named(button[0])}"
        return lead + "fill the page, then park: no way forward on this page"
    if state == "review_page":
        step, button, _ = review_route(digest, plan)
        if step == "advance":
            return lead + f"fill the page, then click the advance {named(button[0])}"
        if step == "gate":
            return lead + f"fill the page, then the submit gate with {named(button[0])}"
        return lead + "fill the page, then park: no submit button"
    if state in ("login_wall", "signup_form"):
        return lead + ("the account step (the master password on the application's site)"
                       if _credential_form(digest) else "the account step")
    if state == "code_gate":
        return lead + "the code step (the emailed code from the inbox)"
    if state == "captcha_or_bot_check":
        return lead + "wait for the person to solve the check"
    closed = apply_judge.closed_posting(answers or {}, facts, state)
    if closed:
        return lead + f"park: {CLOSED_POSTING_REASON} ({closed})"
    return lead + f"park: {_PARK_STATES.get(state, state)}"


def fieldless_apply_choice(digest: apply_form.FormDigest, *,
                           unclassified: int = 0) -> int | None:
    """A posting without form fields (none extracted, and no form control
    the extractor leaves out, `unclassified`): its first control
    that reads as an Apply entry (`apply_judge.entry_worded`: the
    word apply, never "Applying tips", "Apply filters", an Apply that sends
    a stored profile or a send word) and is no form's own button
    (`Button.in_form`), the loop's fallback when no confident `apply_entry`
    was judged."""
    if digest.fields or unclassified:
        return None
    return next((b.n for b in digest.buttons
                 if apply_judge.entry_worded(b.text) and not b.in_form and not b.chrome), None)


def _usage_delta(before: dict, after: dict) -> dict[str, Any]:
    return {"requests": after["requests"] - before["requests"],
            "input_tokens": after["input_tokens"] - before["input_tokens"],
            "usd": after["usd"] - before["usd"]}


def generated_count(pages: list[dict]) -> int:
    """Accepted generated answers across the job's page records (a draft
    reused on a page read again counts once)."""
    return sum(1 for p in pages for g in p.get("generated", [])
               if g.get("ok") and not g.get("reused"))


def _drafts(plan: FillPlan, digest: apply_form.FormDigest | None = None) -> dict[int, str]:
    """n -> the accepted draft, for every field a generator filled, and the
    person's own text for every field a pause filled whose shape the page
    does not change (`apply_pause.user_drafts`): both are checked in
    code, never against the sheet."""
    out = {pf.n: pf.value for pf in plan.fields
           if pf.fact_key == "needs_generation" and pf.action == "fill"}
    out.update(apply_pause.user_drafts(plan, _shaped(plan, digest)))
    return out


def _same_text(a: str, b: str) -> bool:
    """Equal after whitespace runs collapse (a textarea normalises line ends)."""
    return " ".join(str(a).split()) == " ".join(str(b).split())


def _picks(plan: FillPlan) -> dict[int, tuple[str, bool]]:
    """n -> (the option planned, a question's tick boxes), for every pick (a
    select, a radio group, a tick box, a dropdown, a question's tick boxes):
    checked in code against the read-back, never by the judge
    against the sheet."""
    return {pf.n: (str(pf.option), pf.widget == "checkbox_group") for pf in plan.fields
            if pf.action == "select" and pf.option is not None}


_SUGGESTION_WIDGETS = frozenset(("typeahead", "combo"))


def _shaped(plan: FillPlan, digest: apply_form.FormDigest | None = None) -> dict[int, tuple[str, str]]:
    """n -> (kind, the value planned) for every value whose shape the page
    may change and code can compare: a phone
    ("phone": its digits), a date ("date": the day it names, in any shape),
    an upload ("upload": the file's name shown by the box or its widget),
    the cover letter pasted whole ("text": the same words, its line breaks
    aside), a value typed into a search box that takes one of its matches
    ("suggestion": a typeahead's or a list box's match, "Anytown, California,
    United States" for "Anytown, CA"). Checked in code against the read-back
    (`shaped_holds`), never by the judge against the sheet; a suggestion the
    words do not show goes to the judge as before. The judge read the cover
    letter's read-back, the sheet's own words, at 0.79 to 0.88 and a match
    at 0.20 and 0.44 (live, 2026-09-25): a string comparison is exact where
    the judge was not."""
    types = {f.n: f.type for f in digest.fields} if digest is not None else {}
    out: dict[int, tuple[str, str]] = {}
    for pf in plan.fields:
        if pf.action == "upload" and pf.value:
            out[pf.n] = ("upload", str(pf.value))
        elif pf.action != "fill" or not pf.value or pf.fact_key == "needs_generation":
            continue
        elif pf.fact_key == "cover_letter_text":
            out[pf.n] = ("text", str(pf.value))
        elif pf.fact_key == "phone" or (types.get(pf.n) == "tel"
                                        and len(apply_fill.phone_digits(pf.value)) >= 7):
            out[pf.n] = ("phone", str(pf.value))
        elif apply_fill.parse_date(str(pf.value)) is not None:
            out[pf.n] = ("date", str(pf.value))
        elif pf.widget in _SUGGESTION_WIDGETS or types.get(pf.n) == "listbox":
            out[pf.n] = ("suggestion", str(pf.value))
    return out


_PLACE_FACTS = ("location", "address_city", "address_state", "address_country")


def _place_parts(text: str) -> list[str]:
    return [p for p in (apply_judge._norm_option(x) for x in str(text or "").split(",")) if p]


def place_words(catalog: Any) -> frozenset[str]:
    """Every comma part of the candidate's own place facts (the location,
    the city, the state, the country), in `_norm_option`'s words: what a
    search box's match may add to the value typed."""
    return frozenset(p for key in _PLACE_FACTS for p in _place_parts(catalog.value(key)))


def _same_place(a: str, b: str) -> bool:
    return a == b or b in apply_judge._alias_set(a)


def suggestion_holds(value: str, planned: str, places: frozenset[str] = frozenset()) -> bool:
    """Does the match a search box took (`value`) name the value typed
    (`planned`): its first comma part the same words or a name they go by
    (CA for California), every further part of the planned value among the
    match's parts, and every part the match adds one of the candidate's own
    place words (`places`, `place_words`). With the candidate in Anytown,
    California, "Anytown, California, United States" holds "Anytown, CA"
    and "Anytown"; "Anytown, Texas" holds neither, nor "Springfield"."""
    want, got = _place_parts(planned), _place_parts(value)
    if not want or not got or not _same_place(want[0], got[0]):
        return False
    if not all(any(_same_place(w, g) for g in got) for w in want[1:]):
        return False
    return all(any(_same_place(g, w) for w in want) or any(_same_place(g, p) for p in places)
               for g in got[1:])


def shaped_holds(value: str, kind: str, planned: str,
                 places: frozenset[str] = frozenset()) -> bool:
    """Does the read-back `value` hold the `planned` value in the page's
    shape: a phone's digits (a leading US 1 aside), the same day in any
    date shape, the uploaded file's name, the same words (`_same_text`),
    a match that names the value (`suggestion_holds` over `places`)."""
    if kind == "phone":
        want = apply_fill.phone_digits(planned)
        return bool(want) and apply_fill.phone_digits(value) == want
    if kind == "date":
        want = apply_fill.parse_date(planned)
        return want is not None and apply_fill.parse_date(value) == want
    if kind == "upload":
        return bool(value) and Path(str(value)).name == Path(str(planned)).name
    if kind == "text":
        return bool(str(value or "").strip()) and _same_text(value, planned)
    if kind == "suggestion":
        return _same_text(value, planned) or suggestion_holds(value, planned, places)
    return False


_TYPED_TYPES = frozenset(("text", "email", "tel", "url", "number", "textarea", "date"))


def _typed_box(digest: apply_form.FormDigest, n: int) -> bool:
    """Is field `n` a plain box a person types into (no widget)?"""
    f = next((x for x in digest.fields if x.n == n), None)
    return f is not None and f.type in _TYPED_TYPES and not f.widget


def new_fields(before: apply_form.FormDigest,
               after: apply_form.FormDigest) -> list[apply_form.Field]:
    """The fields of `after` that `before` did not have (a
    follow-up question an answer revealed): matched by the control's
    identity (`apply_form.same_ident`) in its frame, else by locator and
    label."""
    old = [(int(f.locator[0]), f.ident, f.locator[1], " ".join(f.label.split()))
           for f in before.fields]
    out = []
    for f in after.fields:
        idx, label = int(f.locator[0]), " ".join(f.label.split())
        known = any(i == idx and ((ident and f.ident and apply_form.same_ident(ident, f.ident))
                                  or (css == f.locator[1] and lab == label))
                    for i, ident, css, lab in old)
        if not known:
            out.append(f)
    return out


# labels that name no question of their own: a follow-up box under the
# question it follows, normalised as `apply_judge._norm_option`
_GENERIC_LABELS = frozenset((
    "please explain", "if yes please explain", "if so please explain", "explain", "details",
    "please specify", "other", "comments", "additional information",
    "if other please specify"))
_REQUIRED_WORD = re.compile(r"\s+(?:required|optional)$")


def _park_on_stuck(stuck: list[str]) -> None:
    """An optional field whose wrong answer could not be taken out
    (`_JobRun._clear_wrong_optional`) parks the job."""
    if stuck:
        raise _Parked("needs_human", f"{WRONG_ANSWER_STAYS}: {stuck[0]}")


def _draft_key(f) -> str | None:
    """A generated answer's question, as the draft cache keys it:
    its label, its help (a length budget) and the section it sits in, case
    and spacing aside. None for a label that names no question of its own
    ("If yes, please explain", `_GENERIC_LABELS`): its draft is never
    reused or kept."""
    label = _REQUIRED_WORD.sub("", apply_judge._norm_option(getattr(f, "label", "")))
    if label in _GENERIC_LABELS:
        return None
    return " | ".join(" ".join(str(getattr(f, attr, "") or "").lower().split())
                      for attr in ("label", "help", "section"))


def buttons_moved(before: apply_form.FormDigest, after: apply_form.FormDigest) -> bool:
    """Did the fill change the page's buttons: a button shown,
    gone, renamed, or enabled or disabled?"""
    def row(d):
        return [(" ".join(b.text.split()), bool(b.disabled), bool(b.chrome)) for b in d.buttons]
    return row(before) != row(after)


def pick_holds(value: str, option: str, group: bool = False) -> bool:
    """Does the read-back `value` show the planned `option`: a tick
    reads "checked"; any other pick reads the option (case, punctuation and
    spacing aside) or a name it goes by (`apply_judge._alias_set`: United
    States of America for United States, CA for California); a question's
    tick boxes (`group`) read the option among the ticked ones, each
    compared whole (`apply_fill.Ticked`: "Yes, I am authorized" is one
    option, and "Asian" is no "Asian, including Indian"); a read-back that
    carries no list is one option.
    Words that only contain the option never hold ("Yes, but I will need
    sponsorship" is no "Yes"), and neither do words the option only starts
    with ("Yes" is no "Yes, I will need sponsorship")."""
    if str(option).strip().lower() == "checked":
        return str(value).strip().lower() == "checked"
    norm = apply_judge._norm_option
    ticked = getattr(value, "options", None) if group else None
    parts = [str(p) for p in ticked] if ticked is not None else [str(value)]
    o = norm(option)
    if not o:
        return False
    names = apply_judge._alias_set(str(option))
    for part in parts:
        v = norm(part)
        if v and (v == o or v in names or o in apply_judge._alias_set(part)):
            return True
    return False
