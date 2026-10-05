"""How a job ends: the park reasons and tab notes, `Outcome`, and the
signals the run raises to end a job early (`_Parked`, `_Unsent`,
`_PauseClosed`, `_SentSeen`, `_NotClicked`, `_Refused`), with `_cap`
(evidence cut to a readable length) and the closed-browser checks
(`_closed_error`, `_context_gone`, `_no_connection`).

Split out of `apply_run`, which re-exports these names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import apply_limits
import apply_linkedin


LOGIN_NOTE = "log in manually, then Re-queue"
LINKEDIN_LOGIN_NOTE = "run `python local/apply_run.py login`, sign in to LinkedIn, then Re-queue"
EASY_APPLY_REASON = apply_linkedin.EASY_APPLY_REASON
EASY_APPLY_NOTE = apply_linkedin.EASY_APPLY_NOTE
LINKEDIN_RETURN_REASON = "the application went back to LinkedIn after the company's form"
CODE_NOTE = "enter the emailed code manually, then Re-queue"
LINK_REASON = "emailed verification link needed"
# a page that moved the job onto another company's account on its ATS, or
# onto another platform, after the job's own was known
TENANT_REASON = "the application left the job's own account on its application platform"
PASSWORD_HTTP_REASON = "this site asks for a password over an unencrypted connection"
FIELDS_MAX_REASON = "the page holds more boxes than the run reads on one page"
ACCOUNT_EXISTS_REASON = "an account exists"
SSO_REASON = "sign-in only through another site"
SSO_NOTE = "sign in once in the auto-apply profile, then Re-queue"
# the parks of an account screen the run could not pass, which fall back to
# the SSO one on a screen whose only way on is a sign-in with another site
# (`sso_fallback_sites`, `_JobRun._account_park`). A dead end (the page did
# not advance, no way forward, a way on that did nothing) keeps its own park:
# its words cannot tell a dead control of the page's own way on ("Proceed")
# from a sign-in's
ACCOUNT_PARK_REASONS = ("login wall", "account signup needed")
PASSWORD_RULE_REASON = "the master password does not meet the password rules"
PASSWORD_RULE_NOTE = ("make the account yourself with another password, or change the "
                      "master password, then Re-queue")
LINK_NOTE = "open the verification link in the email, then Re-queue"
# park mode's submit end at an emailed link after the application's answers:
# the link may be what sends the application
LINK_SUBMIT_NOTE = "review, then open the link in the email to send the application"
# submit mode's park at an emailed link the run did not use once the
# application's answers are on the site: the link may
# send the application, so the person opens it and marks the job; a Re-queue
# after it would apply a second time
LINK_HELD_NOTE = "the emailed link may send the application: open it yourself, then Mark applied"
# the server redirects a verification link may take, as a browser's limit
LINK_MOVES_MAX = 20
# a verification link's page that refused it
LINK_FAILED_WORDS = re.compile(
    r"\b(?:link|token|code)\s+(?:has\s+|is\s+)?(?:expired|invalid|no longer valid)\b"
    r"|\b(?:expired|invalid)\s+(?:link|token)\b|\balready\s+been\s+used\b"
    r"|\bcould\s+not\s+(?:be\s+)?verif", re.I)
# a verification link's page that is the site's bot check:
# the statuses a check answers with (`_link_challenge`: a status alone is no
# check), and the words of a check's page
LINK_CHALLENGE_STATUS = (403, 429, 503)
# the words of such a status's page that say what it is first: the address
# verified already (the link's work done), and the site
# down or busy
LINK_VERIFIED_WORDS = re.compile(
    r"\balready\s+(?:been\s+)?(?:verified|confirmed|activated)\b"
    r"|\b(?:e-?mail(?:\s+address)?|address|account)\s+(?:(?:has|have)\s+(?:now\s+)?been\s+"
    r"|is\s+(?:now\s+)?|was\s+)?(?:successfully\s+)?(?:verified|confirmed|activated)\b", re.I)
LINK_DOWN_WORDS = re.compile(
    r"\b(?:down\s+for\s+|under\s+|scheduled\s+)?maintenance\b"
    r"|\b(?:temporarily|service)\s+unavailable\b|\btoo\s+many\s+requests\b"
    r"|\btry\s+again\s+later\b", re.I)
LINK_BOT_WORDS = re.compile(
    r"\bverify(?:ing)?\s+(?:that\s+)?you\s+are\s+(?:a\s+)?human\b"
    r"|\bchecking\s+(?:your\s+browser|if\s+the\s+site\s+connection\s+is\s+secure)\b"
    r"|\bare\s+you\s+a\s+robot\b|\bi(?:'m|\u2019m|\s+am)\s+not\s+a\s+robot\b", re.I)
LINK_BOT_NOTE = "open the emailed link yourself, then Re-queue"
# a check met after the link's own address answered: the link may have done
# its work
LINK_USED_NOTE = "Re-queue first; if the site still asks for the link, open it yourself"
REVIEW_NOTE = "review and submit"
SUBMIT_FAILED_NOTE = "submit did not register; review and submit"
NOT_SENT_REASON = "the submit did not go through"
CHECK_SENT_REASON = "check whether the application went through"
CHECK_SENT_NOTE = "check whether the application went through, then Mark applied or Re-queue"
CHECKBOX_NOTE = "a CAPTCHA checkbox is on the form: tick it, then submit"
# a box whose options tie on the answer (`apply_fill.OptionTie`): none was
# chosen
OPTION_TIE_WORDS = "the options that hold its answer tie and differ in meaning"
# a list whose options were never read ahead, and none of them is the answer
# in code (`apply_fill.OptionsUnread`): none was chosen
OPTIONS_UNREAD_WORDS = "its options could not be read"
# an optional field's answer that failed its check and stays on the page
# (a radio group keeps its choice)
WRONG_ANSWER_STAYS = "a wrong answer could not be removed"
# The site's own dead ends: a job it says was applied to before
# (on the ATS), a posting that takes no more applications.
ALREADY_APPLIED_REASON = "already applied: the site says this job was applied to before"
ALREADY_APPLIED_NOTE = "the site shows this job as applied; Mark applied if you sent it"
CLOSED_POSTING_REASON = "closed: the posting no longer takes applications"
# A posting the run cannot apply to itself: an aggregator's with no link
# to the company's site, an Apply that is an email address.
AGGREGATOR_REASON = "aggregator posting"
AGGREGATOR_NOTE = "a job board's posting: apply on the company's own site"
MAILTO_REASON = "apply by email"
MAILTO_NOTE = "the posting asks for an email application: send it yourself"
CLOSED_REASON = "the browser window was closed"
TAB_CLOSED_REASON = "the job's tab was closed"
# A pause's wait failed with the window and the tab both still open (a
# renderer crash, a dropped connection): ended as a close (`_pause_closed`)
PAUSE_UNANSWERED_REASON = "the job's page stopped answering during the wait"
# The judge stayed down through the retries: the job goes back to
# `queued` with its attempt not counted and the drain stops; no park.
JUDGE_DOWN_REASON = "judge unavailable"
REQUEUED_NOTE = "re-queued, this attempt not counted"
# A refused key (401, 402, 403) or a retired model (404, 410) is none of the
# job's doing and no wait mends it: the job goes back with this attempt
# counted and no outage, and the drain stops
KEY_REFUSED_NOTE = ("re-queued, this attempt counted (the judge refused the key or the "
                    "account, or no longer has the model)")


def _cap(text: str, limit: int = apply_limits.EVIDENCE_CAP) -> str:
    """Evidence cut to `limit` characters, so queue reasons stay readable."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


# --- outcomes and the record ----------------------------------------------------------

@dataclass
class Outcome:
    job_id: str
    status: str
    reason: str
    record_path: str
    pages: int
    jev_usage: dict[str, Any] = field(default_factory=dict)
    browser_closed: bool = False    # the window closed under the job: the drain stops
    judge_down: bool = False        # the judge went down under the job: the drain stops
    trace_dir: str = ""             # this attempt's trace folder, when one was written


def _closed_error(e: BaseException) -> bool:
    """An error Playwright raises once the page, the context or the browser
    is gone (`TargetClosedError`, or its message on an older driver)."""
    text = str(e)
    return (type(e).__name__ == "TargetClosedError" or "has been closed" in text
            or "Browser closed" in text or "Target closed" in text)


def _context_gone(ctx) -> bool:
    """Is the browser context closed or its browser gone? A cheap round trip
    (`cookies()`) also lets the sync API deliver a pending close event."""
    try:
        browser = getattr(ctx, "browser", None)
        if browser is not None and not browser.is_connected():
            return True
    except Exception as e:      # noqa: BLE001
        return _closed_error(e)
    try:
        ctx.cookies()
    except Exception as e:      # noqa: BLE001
        return _closed_error(e)
    return False


class _Parked(Exception):
    """Raised inside the state loop to end the job in a terminal status."""

    def __init__(self, status: str, reason: str, tab_note: str = ""):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.tab_note = tab_note


class _Unsent(_Parked):
    """A park after the submit click whose send never reached the site (a
    refused connection, a name that did not resolve: `_no_connection`): it
    stands as raised, never read as a possible send."""


class _PauseClosed(_Parked):
    """The window or the tab closed during a pause's wait
    (`_JobRun._pause_closed`): already the check-whether end with its note,
    so the run's handler finishes it as raised. `window` says the whole
    window went (the drain stops)."""

    def __init__(self, status: str, reason: str, tab_note: str = "", *, window: bool = False):
        super().__init__(status, reason, tab_note)
        self.window = window


class _SentSeen(_Parked):
    """A "submitted (unconfirmed)" end whose only evidence is a request the
    submit's watch saw go to the application's sites (`SendWatch.sent`).
    A form post the guard stopped is such a row too, when its host is an
    admitted job board (`_JobRun._stopped_post`)."""


class _NotClicked(_Parked):
    """A click the live check stopped before it was made
    (`_JobRun._refused_click`): a step that marked the job a possible send
    before its click takes the mark back."""


# Chrome's errors for a request that never reached its site: the connection
# was refused or its address unreachable, or the name did not resolve. A
# reset, a timeout or an empty answer may come after the request
# left, so none of them is here.
_NO_CONNECTION = ("ERR_CONNECTION_REFUSED", "ERR_ADDRESS_UNREACHABLE", "ERR_NAME_NOT_RESOLVED",
                  "ERR_NAME_RESOLUTION_FAILED")


def _no_connection(failure: str) -> bool:
    """Did the load fail before any connection to its site was made?"""
    return any(code in str(failure or "") for code in _NO_CONNECTION)


class _Refused(Exception):
    """The form refused the submit as typed and nothing left the page
    (`_JobRun._not_sent`): `problems` are the form's messages, `park` the
    park it would be without a repair."""

    def __init__(self, problems: list[dict[str, Any]], park: _Parked):
        super().__init__(park.reason)
        self.problems = problems
        self.park = park
