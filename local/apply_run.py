"""The auto-apply drain loop: claim a queued job, drive a persistent Chromium
through the page state machine, submit behind the confidence gate or park,
write `apply_record.md`, finish the queue entry.

    python local/apply_run.py drain [--cap N] [--no-submit] [--headless]
                                    [--jev fake|replay|typesafe] [--profile DIR]
    python local/apply_run.py one <job_id> [same flags]
    python local/apply_run.py login      sign in to LinkedIn and the inbox once
    python local/apply_run.py doctor     key, SDK, Playwright, Chromium, profile
    python local/apply_run.py probe <url> [--follow-apply] [--judge] [--headed]
                                         read one page as the run would; changes nothing

Per job (`Runner.run_job`): an Easy Apply entry (`is_easy_apply`) ends at
once, `needs_human` with `EASY_APPLY_REASON`, and no page is opened. Else the
fact catalog from the job folder's apply.md (`apply_facts.build`), the first
load (to `domcontentloaded`, settled), then one page at a time up to
`apply_judge.MAX_PAGES` and the job's wall clock (`JOB_WALL_CLOCK_S`): a
visible cookie or consent banner dismissed by its reject control (never its
accept), the page read once it holds still (`_read_digest` reads an empty
page or a loading skeleton again), LinkedIn's pages decided without the
judge (`apply_linkedin`: a job page's offsite Apply is clicked; Easy Apply,
already applied, closed and signed out park; nothing is ever filled on
LinkedIn), a job board's posting by its link to the company's site
(`_aggregator_step`), and every other page through `apply_form.extract` ->
the page read (`_read`: its own small Jev request,
`apply_judge.read_questions`, combined with the page's structure,
`apply_judge.read_page`; a read under the floor is taken once more after a
settle, then goes on as the kind the structure settles, `unsure_step`) ->
the page's mapping on a page the run acts on (`_map`,
`apply_judge.page_questions`) -> the state table from the design (section
3.5):

    job_posting            click the Apply entry, follow a popup (an email
                           Apply parks with its address)
    application_form       plan, fill, verify, type the keyring password into a
                           password box, read the page again (SP6: fields the
                           fill revealed, values the page changed, buttons it
                           enabled), then advance (a step the form refuses is
                           repaired and clicked once more), or the submit gate
    review_page            fill and verify editable controls, then its advance
                           when it has no submit (a wizard step), else the gate
    login_wall / signup    fill the account email and hidden keyring password
    code_gate              read the emailed code in a separate inbox tab
    confirmation           before any submit click never the run's send: a
                           form or review misread goes on as its next read,
                           anything else parks (`confirmation_step`)
    captcha / payment / error / other / low confidence
                           park needs_human (a closed posting and a job the
                           site says was applied to with their own reasons;
                           an `other` whose structure settles a kind is it)

The submit gate is `can_submit(plan, verification, settings, live)`; it
returns the first failing reason. `live` is the page as the gate reads it
(`_JobRun._gate_read`): an application on it, an Apply-worded button that is
the form's own sending button (the DOM and the judge's `button_{n}_sends`),
the submit's form's validity, the required controls the extractor leaves
out; an unticked CAPTCHA checkbox waits for the person at the gate (park
mode leaves it to them), never before the page is filled. The requests the
page and the tabs it opens send after the click are watched (`SendWatch`)
and the page after it is read until it settles (`_JobRun._after_submit`): a
confirmation, a challenge, validation errors (nothing sent), a code screen,
an error banner; "submitted (unconfirmed)" only when a request to the
application's sites was seen leaving, and every other park after the click
asks the person to check. A
submit click is recorded before the page is judged again, so a crash after
it never reads as unsent. Every click reads the live control first and
refuses one that turned into a send (INV-04). Only a confirmation closes the
job's tab. Every terminal moment writes the record (fields, uploads,
verification, buttons, flags, missing questions, Jev totals; a password
field's value is never written) and calls `apply_queue.finish`. The record
links the job's trace (`apply_trace`: per page the digest, the judge's
answers, the plan, what was done and why, a masked screenshot; the job's log
lines) and keeps the earlier attempts' records.

A browser window closed mid-run (or a crashed browser) ends the running job
`needs_human` with `CLOSED_REASON` and stops the drain; the jobs after it stay
queued with their attempt counts untouched.

Playwright is imported inside the functions that open a browser, so the
module imports without it; tests inject a `context`.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import posixpath
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import apply_answergen  # noqa: E402
import apply_facts  # noqa: E402
import apply_fill  # noqa: E402
import apply_form  # noqa: E402
import apply_judge  # noqa: E402
import apply_inbox  # noqa: E402
import apply_linkedin  # noqa: E402
import apply_queue  # noqa: E402
import apply_trace  # noqa: E402
import ats_accounts  # noqa: E402
import jev  # noqa: E402
from apply_judge import FillPlan, VerifyResult  # noqa: E402

log = logging.getLogger("apply_run")

JOB_WALL_CLOCK_S = 15 * 60         # per job, on the injectable clock (a solved CAPTCHA's wait is added back)
GENERATE_MAX = 3                   # generated answers per job (spec 3.7)
POPUP_TIMEOUT_MS = 5_000           # for the Apply entry to open a new tab
POPUP_GRACE_S = 0.5                # after a same-tab DOM change, for a popup that follows it
ENTRY_POLL_MS = 100                # the entry click's watch for a popup, a navigation or a change
GOTO_TIMEOUT_MS = 45_000           # the first load, to `domcontentloaded`
GOTO_RETRY_S = 2.0                 # before the one retry of a first load that failed on the network
GOTO_ERROR_PAGE_S = 10.0           # at most, for Chromium's error page to be up before that retry
EMPTY_TEXT_MIN = 200               # a fieldless read with less visible text is read again
EMPTY_READ_MAX_S = 10.0            # an empty read is re-read after a settle for up to this long
EMPTY_READ_STABLE_S = 3.0          # or until the page has held the same empty read this long
EMPTY_READ_POLL_S = 0.5
LOADING_WAIT_S = 3.0               # a read with a loading placeholder up waits this long, once
                                   # per page (an ad or widget region may never clear, M10)
LINKEDIN_READY_S = 12.0            # for a LinkedIn job page's top card to render
LINKEDIN_POLL_MS = 250
LINKEDIN_EASY_RECHECK_S = 1.5      # an Easy Apply read is read again after this, and a settle
LINKEDIN_CLICKS_MAX = 3            # offsite Apply clicks the handler makes per job
CONSENT_MAX = 3                    # consent banners dismissed per job
CONSENT_WAIT_S = 5                 # for a consent click's effect (the banner gone, a reload)
CLICK_TIMEOUT_S = 20               # click_button's wait for a change
FILL_ROUNDS_MAX = 3                # re-reads after a page's fill: revealed fields, the page's
                                   # own changes (FILL-10, FILL-03)
DISABLED_WAIT_S = 2.0              # a way on still disabled after the fill: waited on this long
REPAIR_ROUNDS = 2                  # repairs of the fields a form refused, per step (ADV-02)
BUSY_WAIT_S = 60                   # a loading indicator after a click: waited on this long (ADV-07)
STEP_SETTLE_S = 20                 # a quiet click that set a request going: waited on this long
                                   # more, never clicked again (ADV-06)
BUSY_POLL_S = 0.25
SUBMIT_SETTLE_S = 10               # after a quiet submit click: wait this long for the page
POST_SUBMIT_WAIT_S = 45            # after the submit click, the page is read again while a
                                   # request it sent is in flight or the page still moves
POST_SUBMIT_POLL_S = 1.0
POST_SUBMIT_QUIET_S = 2.0          # a page this still, with no request in flight, is read as is
POST_SUBMIT_READS = 5              # judge requests the post-submit read makes at most
HOLD_POLL_S = 1.0                  # while holding the window open
FINISH_RETRY_S = 1.0               # before the one retry of a failed queue finish
LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_HOSTS = ("linkedin.com", "www.linkedin.com")
REDIRECT_TIMEOUT_S = 20            # for that hop's script to send the tab on
HUMAN_CHECK_WAIT_S = 5 * 60        # for the user to solve a CAPTCHA in the visible window
HUMAN_CHECK_POLL_S = 2.0
HUMAN_CHECK_AUTO_S = 16            # headless: for a whole-page check to clear itself
HUMAN_CHECK_MIN_PX = 150           # a bot-check frame this tall is a challenge, not a badge
# The sites (registrable domains) of the platforms most applications run on. A
# page or a frame on one of them is part of the application wherever the flow
# met it: a company page embeds Greenhouse, a careers site hands off to Workday,
# an iCIMS portal signs in on login.icims.com.
ATS_SITES = frozenset((
    "greenhouse.io", "lever.co", "ashbyhq.com", "icims.com", "myworkdayjobs.com",
    "myworkdaysite.com", "myworkday.com", "workday.com", "smartrecruiters.com",
    "jobvite.com", "workable.com", "bamboohr.com", "taleo.net", "oraclecloud.com",
    "successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu", "brassring.com",
    "adp.com", "ultipro.com", "ukg.com", "dayforcehcm.com", "applytojob.com",
    "jazzhr.com", "recruitee.com", "teamtailor.com", "breezy.hr", "pinpointhq.com",
    "rippling.com", "paylocity.com", "paycomonline.net", "avature.net", "eightfold.ai",
    "gem.com", "comeet.com", "personio.de", "personio.com", "csod.com",
    "clearcompany.com", "hrmdirect.com", "applicantpro.com", "isolvedhire.com",
    "phenompeople.com", "trinethire.com",
    # ALLOW-01 (the audit's list, SP4)
    "jobs2web.com", "selectminds.com", "saashr.com", "pageuppeople.com", "silkroad.com",
    "hirebridge.com", "zohorecruit.com", "bullhornstaffing.com", "jobdiva.com", "ceipal.com",
    "paycor.com", "recruitingbypaycor.com", "freshteam.com", "hireology.com", "careerplug.com",
    "catsone.com", "applicantstack.com", "trakstar.com", "careers-page.com", "jobscore.com",
    "peopleadmin.com", "governmentjobs.com", "jazz.co", "harri.com", "fountain.com",
    "gusto.com", "dover.com", "wellfound.com"))
# Programmatic-ad trackers and link shorteners between a posting's Apply and
# the careers site (NAV-07): a hop to wait out, never the destination, never
# a place for the master password.
TRACKER_SITES = frozenset((
    "appcast.io", "joveo.com", "pandologic.com", "recruitics.com", "grnh.se", "lnkd.in",
    "bit.ly", "tinyurl.com", "ow.ly", "buff.ly", "rebrand.ly", "clickcast.cloud",
    "jobadx.com", "talentify.io", "cvtrack.com", "jobs2careers.com"))
# Job boards and aggregators (NAV-08): a posting there is followed once to
# the company's own site through its "Apply on company site" control, and
# never signed in on or filled.
AGGREGATOR_SITES = frozenset((
    "dice.com", "lensa.com", "jobright.ai", "talent.com", "ziprecruiter.com", "indeed.com",
    "jooble.org", "glassdoor.com", "builtin.com", "jobot.com", "welcometothejungle.com",
    "hiring.cafe", "simplyhired.com", "careerbuilder.com", "monster.com", "snagajob.com",
    "adzuna.com", "jobleads.com", "theladders.com"))
TRACKER_HOPS_MAX = 3               # tracker hops waited out after one entry click
AGGREGATOR_BOARDS_MAX = 2          # job boards read for their company link in one job
# Bot-check providers. Their frames' controls are never filled or clicked: a
# challenge is the user's to solve in the visible window. DataDome
# (`captcha-delivery.com`) and PerimeterX serve full-page checks (study G11).
CAPTCHA_SITES = frozenset(("hcaptcha.com", "recaptcha.net", "arkoselabs.com",
                           "funcaptcha.com", "geetest.com", "captcha-delivery.com",
                           "perimeterx.net", "px-cloud.net", "px-cdn.net"))
_SECOND_LEVEL = frozenset(("co", "com", "org", "net", "ac", "gov", "edu", "ne", "or", "go"))
# Hosting domains whose subdomains belong to different owners: each
# `<name>.github.io` is its own site, never one site with every other.
_SHARED_HOSTING = frozenset((
    "github.io", "herokuapp.com", "azurewebsites.net", "vercel.app", "netlify.app",
    "pages.dev", "workers.dev", "web.app", "firebaseapp.com", "appspot.com",
    "cloudfront.net", "amazonaws.com", "blogspot.com", "wixsite.com", "webflow.io",
    "onrender.com", "fly.dev", "glitch.me", "ngrok.io", "ngrok-free.app", "surge.sh",
    "wordpress.com", "sharepoint.com", "notion.site"))
VIEWPORT = {"width": 1400, "height": 1000}
BROWSER_CHANNEL = "chrome"         # the installed Google Chrome; the bundled Chromium is the fallback
RECORD_NAME = "apply_record.md"
HIDDEN = "<hidden>"

LOGIN_NOTE = "log in manually, then Re-queue"
LINKEDIN_LOGIN_NOTE = "run `python local/apply_run.py login`, sign in to LinkedIn, then Re-queue"
EASY_APPLY_REASON = apply_linkedin.EASY_APPLY_REASON
EASY_APPLY_NOTE = apply_linkedin.EASY_APPLY_NOTE
LINKEDIN_RETURN_REASON = "the application went back to LinkedIn after the company's form"
CODE_NOTE = "enter the emailed code manually, then Re-queue"
REVIEW_NOTE = "review and submit"
SUBMIT_FAILED_NOTE = "submit did not register; review and submit"
NOT_SENT_REASON = "the submit did not go through"
CHECK_SENT_REASON = "check whether the application went through"
CHECK_SENT_NOTE = "check whether the application went through, then Mark applied or Re-queue"
CHECKBOX_NOTE = "a CAPTCHA checkbox is on the form: tick it, then submit"
# The site's own dead ends (SP4): a job it says was applied to before
# (TERM-04's ATS part), a posting that takes no more applications (READ-08).
ALREADY_APPLIED_REASON = "already applied: the site says this job was applied to before"
ALREADY_APPLIED_NOTE = "the site shows this job as applied; Mark applied if you sent it"
CLOSED_POSTING_REASON = "closed: the posting no longer takes applications"
# A posting the run cannot apply to itself (SP4): an aggregator's with no link
# to the company's site (NAV-08), an Apply that is an email address (NAV-09).
AGGREGATOR_REASON = "aggregator posting"
AGGREGATOR_NOTE = "a job board's posting: apply on the company's own site"
MAILTO_REASON = "apply by email"
MAILTO_NOTE = "the posting asks for an email application: send it yourself"
CLOSED_REASON = "the browser window was closed"
TAB_CLOSED_REASON = "the job's tab was closed"
EVIDENCE_CAP = 300                 # characters of evidence a park reason carries
PROBE_SETTLE_S = 10                # the probe's wait for a page to hold still
PROBE_GOTO_MS = 30_000

DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_apply_submit": True,
    "auto_apply_headless": False,
    "auto_apply_jev_mode": "typesafe",
    "auto_apply_batch_cap": 10,
    "auto_apply_generate": True,
}
_ACTED = ("fill", "select", "upload")     # the actions that put a value on the page
_PARK_STATES = {
    "captcha_or_bot_check": "captcha or bot check on the page",
    "payment_request": "payment requested",
    "error_or_dead": "error or dead page",
    "other": "unrecognised page",
}
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
# button questions are asked only there (SP4).
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
# The loop's send vocabulary: a button whose text has one of `SUBMIT_WORDS`
# reads as sending the application (`_submit_shaped`), and one with a
# `FINAL_WORDS` word as a last step (`_final_shaped`). The flow harness checks
# every click against the same words.
SUBMIT_WORDS = re.compile(r"\b(submit|apply|send|finish)\b", re.I)
FINAL_WORDS = re.compile(r"\b(complete|confirm|finali[sz]e|done)\b", re.I)
# The words a page shows once an application was received
# (`apply_judge.CONFIRMATION_WORDS`): with the judge's read, the deterministic
# half of a confirmation after the submit click, and only when they were not
# on the page before it.
CONFIRMATION_WORDS = apply_judge.CONFIRMATION_WORDS
confirmation_words = apply_judge.confirmation_words
# The only Apply entry `probe --follow-apply` clicks: "Apply", "Apply now",
# "Apply for this job", "Apply on company website". A quick, one-click or
# third-party apply may send a stored profile at once.
_PLAIN_APPLY = re.compile(r"^\s*apply(\s+(now|here|online|for\s+this\s+(job|position|role)"
                          r"|on\s+(the\s+)?company(['’]s)?\s+(website|site)))?\s*$", re.I)


def _cap(text: str, limit: int = EVIDENCE_CAP) -> str:
    """Evidence cut to `limit` characters, so queue reasons stay readable."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def launch_profile(pw, profile_dir: Path, *, headless: bool, log=None):
    """Open the auto-apply profile in the installed Google Chrome, or in the
    bundled Playwright Chromium when Chrome will not start.

    The profile is its own directory, apart from the user's everyday Chrome
    profile: Chrome refuses automation on its default profile and locks a
    profile to one running browser. The logins made once through `login`
    stay in this directory for every later run."""
    log = log or logging.getLogger("apply_run")
    try:
        return pw.chromium.launch_persistent_context(
            str(profile_dir), channel=BROWSER_CHANNEL, headless=headless, viewport=VIEWPORT)
    except Exception as e:      # noqa: BLE001  (Chrome absent or broken: use the bundled build)
        first = str(e).strip().splitlines()[0][:200] if str(e).strip() else ""
        log.warning("Google Chrome did not start (%s: %s); using the bundled Chromium",
                    type(e).__name__, first)
    return pw.chromium.launch_persistent_context(
        str(profile_dir), headless=headless, viewport=VIEWPORT)


def default_profile_dir() -> Path:
    appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return appdata / "linkedin_watcher" / "browser_profile"


def load_settings() -> dict[str, Any]:
    """The auto-apply keys from the dashboard's config (`settings.load`), with
    the defaults for anything unset or unreadable."""
    try:
        import settings
        stored = settings.load()
    except Exception as e:      # noqa: BLE001  (a bad config file is not a reason to stop)
        log.warning("settings unreadable (%s); using defaults", e)
        stored = {}
    return {k: stored.get(k, d) for k, d in DEFAULT_SETTINGS.items()}


# --- hooks SP5 / SP6 implement -----------------------------------------------------

class NotConfigured:
    """The default hook object: every capability answers "cannot", so the
    runner parks the job for the human with the matching note."""

    def login(self, page, digest, host: str) -> bool:
        return False

    def signup(self, page, digest, host: str) -> bool:
        return False

    def fetch_code(self, page, site: str, inbox_url: str) -> str | None:
        return None

    def answer(self, field, catalog, judge, *, budget: int) -> str | None:
        return None


def _is_email_box(field) -> bool:
    """An account screen's address box: an email input, an autocomplete of
    `username` / `email`, or a text box `quick_map` reads as the email."""
    return (field.type == "email" or field.autocomplete in ("username", "email")
            or (field.type == "text"
                and apply_facts.quick_map(field.label, field.id_or_name, field.type) == "email"))


_PASSWORD_NAME = re.compile(r"pass\s*word|passwd|\bpwd\b", re.I)
# A password box's words when it makes the password rather than signs in with it.
_NEW_PASSWORD = apply_judge.NEW_PASSWORD
_CODE_PASSWORD = re.compile(r"one[\s_-]*time|\botp\b|verification\s*code|temporary|pass\s*code",
                            re.I)


def _not_an_account_password(field) -> bool:
    """A masked box on an account screen that is no place for the master
    password: a sensitive question (`apply_judge.is_sensitive_field`) or a
    one-time or verification code. The screen's other masked boxes are its
    passwords, labelled or not."""
    text = f"{field.label or ''} {field.id_or_name or ''}"
    return (apply_judge.is_sensitive_field(field.label, field.id_or_name)
            or bool(_CODE_PASSWORD.search(text)))


def _names_password(field) -> bool:
    """A box the master password may go into on a form: its autocomplete is
    a password's, or its label or id says password and not a one-time or
    verification one. A masked passcode, security answer or ID number is a
    password-shaped box too (`apply_form.is_password_field` matches `pass`
    and `secret`), and none of them takes it."""
    if _not_an_account_password(field):
        return False
    if str(field.autocomplete or "").lower() in apply_form.PASSWORD_AUTOCOMPLETE:
        return True
    return bool(_PASSWORD_NAME.search(f"{field.label or ''} {field.id_or_name or ''}"))


def _email_first(digest) -> bool:
    """The first screen of a two-step sign-in: an address box and nothing
    else but checkboxes (remember me, the terms)."""
    has_email = False
    for f in digest.fields:
        if _is_email_box(f):
            has_email = True
        elif f.type != "checkbox":
            return False
    return has_email


# What a sign-up screen asks besides the address and the password: the name,
# the phone, the address, the terms. A screen that asks anything else carries
# the application (`_Accounts._fill`).
_ACCOUNT_FACTS = frozenset((
    "full_name", "first_name", "last_name", "email", "phone", "location",
    "address_street", "address_city", "address_state", "address_zip", "address_country",
    "consent_attest"))


def _password_boxes(digest) -> list:
    return [f for f in digest.fields
            if apply_form.is_password_field(f.type, f.id_or_name, f.label, f.autocomplete)]


def _fills_the_application(digest, plan: FillPlan, filled) -> bool:
    """Did this page's fill put the application's own answers on it? A page
    with a password box whose boxes a sign-up asks for (a name, the address,
    a phone: `_ACCOUNT_FACTS`) makes an account; an application is filled
    only once a page carries something else (a resume, a profile link, a
    written answer). A page without a password box counts whatever it held."""
    if not filled:
        return False
    if not _password_boxes(digest):
        return True
    done = {f.n for f in filled}
    return any(pf.n in done and pf.fact_key not in _ACCOUNT_FACTS for pf in plan.fields)


def account_forms(page, digest) -> list:
    """ADV-08: a sign-in and a sign-up side by side (Taleo, SuccessFactors):
    when the page's password boxes sit in two forms or more, each such
    form's own fields and buttons as a digest of their own; else []."""
    if len(_password_boxes(digest)) < 2 or page is None:
        return []
    try:
        where = apply_form.form_index(page, [f.locator for f in digest.fields]
                                      + [b.locator for b in digest.buttons])
    except Exception:       # noqa: BLE001  (a page double)
        return []
    fields_at = where[:len(digest.fields)]
    buttons_at = where[len(digest.fields):]
    pw_forms = []
    for f, at in zip(digest.fields, fields_at):
        if at[1] >= 0 and apply_form.is_password_field(f.type, f.id_or_name, f.label,
                                                        f.autocomplete) and at not in pw_forms:
            pw_forms.append(at)
    if len(pw_forms) < 2:
        return []
    return [dataclasses.replace(
        digest, fields=[f for f, at in zip(digest.fields, fields_at) if at == form],
        buttons=[b for b, at in zip(digest.buttons, buttons_at) if at == form])
        for form in pw_forms]


def _credential_form(digest) -> bool:
    """A sign-in or sign-up screen by its boxes: a password box, or the
    address box of a two-step sign-in, and no file box (an account screen
    never takes a resume; one that does is the application form creating an
    account). A page of other boxes is a form, whatever it was read as; only
    a screen of this shape reaches `_Accounts`' typing and clicking."""
    if any(f.type == "file" for f in digest.fields):
        return False
    return bool(_password_boxes(digest)) or _email_first(digest)


class _NavGuard:
    """While the credentials are on the page, the page and the frames that
    hold them (`frames`, by id) may not navigate off the application's sites:
    a form that posts there is stopped and the host lands in `blocked`. Every
    other request goes through (a fetch, a popup, another frame), so the
    site's own bot check, sign-in API and scripts work as they would for a
    person; the password is only ever typed on the application's site
    (`_password_ok`), which is the protection that matters. `before` names
    the page as it was when the guard went on (a stopped navigation leaves
    the tab on a browser error page)."""

    def __init__(self, run, page):
        self.run = run
        self.page = page
        self.frames: set[int] = set()   # the page's main frame joins at `start`
        self.blocked: list[str] = []
        self.posted = False             # a stopped navigation carried a form post
        self.before = ""
        self._on = False

    def _route(self, route, request) -> None:
        target_host = _host(request.url)
        try:
            frame_id = id(request.frame)
        except Exception:       # noqa: BLE001  (a service-worker request has no frame)
            frame_id = None
        if (target_host and request.is_navigation_request() and frame_id in self.frames
                and not self.run._allowed_site(target_host)):
            self.blocked.append(target_host)
            self.posted = self.posted or request.method.upper() == "POST"
            route.abort()
            return
        route.fallback()        # on to any other handler, then the network

    def start(self) -> None:
        if not self._on:
            self.frames.add(id(self.page.main_frame))
            try:
                self.before = f"{self.page.url} | {self.page.title()}"
            except Exception:       # noqa: BLE001  (a page mid-navigation has no title yet)
                self.before = str(self.page.url)
            self.page.route("**/*", self._route)
            self._on = True

    def stop(self) -> None:
        if self._on:
            self._on = False
            try:
                self.page.unroute("**/*", self._route)
            except Exception:       # noqa: BLE001  (the page is gone, and its routes with it)
                pass


# Hosts whose requests are never an application's send: analytics, ads,
# tag managers, error reporting, consent logging (by registrable site). A
# POST there after the submit click proves nothing either way.
_TRACKING_SITES = frozenset((
    "google-analytics.com", "googletagmanager.com", "doubleclick.net", "googleadservices.com",
    "googlesyndication.com", "facebook.com", "facebook.net", "bing.com", "clarity.ms",
    "hotjar.com", "hotjar.io", "segment.io", "segment.com", "mixpanel.com", "amplitude.com",
    "fullstory.com", "heapanalytics.com", "nr-data.net", "newrelic.com", "sentry.io",
    "datadoghq.com", "datadoghq.eu", "quantserve.com", "quantcast.com", "adroll.com",
    "tiktok.com", "twitter.com", "ads-twitter.com", "snapchat.com", "pinterest.com",
    "reddit.com", "criteo.com", "taboola.com", "outbrain.com", "hs-analytics.net",
    "hsadspixel.net", "licdn.com", "cookielaw.org", "onetrust.com", "cookiebot.com",
    "trustarc.com", "usercentrics.eu", "osano.com", "didomi.io", "bugsnag.com",
    "rollbar.com", "logrocket.io", "logrocket.com", "mouseflow.com", "crazyegg.com",
    "optimizely.com", "demdex.net", "omtrdc.net", "adobedc.net", "everesttech.net",
    "adsrvr.org", "adnxs.com", "rubiconproject.com", "pubmatic.com", "casalemedia.com",
    "criteo.net", "scorecardresearch.com", "intercom.io", "intercomcdn.com", "drift.com",
    "driftt.com", "crisp.chat", "tawk.to", "livechatinc.com", "olark.com", "zdassets.com",
    "sc-static.net", "t.co", "cloudflareinsights.com"))
# Google's own tag and ad paths; a form on docs.google.com is a real send
_GOOGLE_TRACKING = ("/ccm/", "/pagead/", "/ads/", "/g/collect", "/j/collect", "/recaptcha")
# Cloudflare's beacon and challenge paths, served on the site's own host
_CLOUDFLARE_PATHS = ("/cdn-cgi/rum", "/cdn-cgi/challenge-platform/")


def _tracking(url: str) -> bool:
    parts = urlsplit(str(url or ""))
    site = _site(parts.hostname or "")
    if site in _TRACKING_SITES or parts.path.startswith(_CLOUDFLARE_PATHS):
        return True
    return site == "google.com" and parts.path.startswith(_GOOGLE_TRACKING)


class SendWatch:
    """What the page sends after the submit click (TERM-01), from the page,
    its frames and any tab it opens:

    - `sent`: a navigation of the main frame or of the submit's own frame,
      or a POST, PUT or PATCH, to the application's sites
      (`_JobRun._allowed_site`): the evidence for "submitted (unconfirmed)";
    - `possible`: a POST, PUT or PATCH to any other host (a form backend on
      another domain): it may have been the send, so the job never reads as
      unsent after it.

    Only the job's page and the tabs it opens after `start` count (R4): an
    earlier job's parked tab, the inbox tab and a tab the person uses never
    do. A tab's first request comes before its page is known (Playwright
    gives no frame for it): it is held (`_unplaced`) and counted when the
    job's page reports that tab, at the same URL. A held POST, PUT or PATCH
    never matched (the tab's first answer redirected, or was an error page,
    so the tab reports another URL; or a service worker sent it) counts as a
    possible send (`unplaced_sends`, in `any` and `first`, and moved to
    `possible` when the watch stops): only a brand-new tab or a worker makes
    such a request, and it may have been the send. A bot-check provider, an
    analytics, ad, chat or consent host (`_tracking`), LinkedIn (its Insight
    Tag posts from company pages) and the inbox's host (unless the job's page
    is served from it) never count. Each row
    keeps the method and the URL without its query (a GET form puts the
    answers there); `pending` keeps the read waiting while one of the job's
    page is in flight."""

    _SEND_METHODS = ("POST", "PUT", "PATCH")

    def __init__(self, run, page, frame=None):
        self.run = run
        self.page = page
        self.frames: set[int] = set()
        for f in (getattr(page, "main_frame", None), frame):
            if f is not None:
                self.frames.add(id(f))
        self.sent: list[str] = []
        self.possible: list[str] = []
        self.pending: set[int] = set()
        self._targets: list = []
        self._before: set[int] = set()     # the context's tabs before `start`
        self._opened: set[int] = set()     # the tabs the job's page opened since
        self._unplaced: list[tuple[str, str, str]] = []    # (url, kind, row)
        self._inbox = ""
        self._on = False

    @staticmethod
    def _bare(url: str) -> str:
        parts = urlsplit(str(url or ""))
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    def _owner(self, request) -> str:
        """"job" (the job's page or a tab it opened), "other", or "unknown"
        (a new tab's first request, whose page Playwright cannot name yet)."""
        try:
            page = request.frame.page
        except Exception:       # noqa: BLE001  (no frame yet, or a service worker's request)
            return "unknown"
        if page is self.page or id(page) in self._opened:
            return "job"
        if page is None or id(page) in self._before:
            return "other"
        try:
            return "job" if page.opener() is self.page else "other"
        except Exception:       # noqa: BLE001
            return "other"

    def _kind(self, request, owner: str) -> str:
        url = str(request.url)
        host = _host(url)
        if not host or _is_captcha_url(url) or apply_linkedin.is_linkedin(host) or _tracking(url):
            return ""
        if self._inbox and host == self._inbox:
            return ""
        method = str(request.method).upper()
        navigation = False
        if owner == "job":
            try:
                navigation = request.is_navigation_request() and id(request.frame) in self.frames
            except Exception:       # noqa: BLE001
                navigation = False
        send = method in self._SEND_METHODS
        if self.run._allowed_site(host) and (navigation or send):
            return "sent"
        return "possible" if send else ""

    def _request(self, request) -> None:
        try:
            owner = self._owner(request)
            if owner == "other":
                return
            kind = self._kind(request, owner)
            if not kind:
                return
            row = f"{str(request.method).upper()} {self._bare(request.url)}"
            if owner == "unknown":
                self._unplaced.append((self._bare(request.url), kind, row))
                return
            (self.sent if kind == "sent" else self.possible).append(row)
            self.pending.add(id(request))
        except Exception:       # noqa: BLE001  (a request that cannot be read counts as nothing)
            pass

    def _done(self, request) -> None:
        self.pending.discard(id(request))

    def _popup(self, popup) -> None:
        """A tab the job's page opened: its requests count, and a first
        request held for it (the same URL) is counted now."""
        self._opened.add(id(popup))
        try:
            url = self._bare(popup.url)
        except Exception:       # noqa: BLE001
            return
        keep = []
        for bare, kind, row in self._unplaced:
            if bare == url:
                (self.sent if kind == "sent" else self.possible).append(row)
            else:
                keep.append((bare, kind, row))
        self._unplaced = keep

    def start(self) -> None:
        """Listen on the page's context (a new tab's first request reaches
        only the context) and for the tabs the page opens."""
        if self._on:
            return
        self._on = True
        try:
            self._inbox = _host(str(self.run.r.run_context().get("inbox_url") or ""))
            if self._inbox and _host(str(self.page.url)) == self._inbox:
                # the application is served from the inbox's own host: only
                # the tab rule keeps the inbox out
                self._inbox = ""
        except Exception:       # noqa: BLE001  (a runner double)
            self._inbox = ""
        try:
            self._before = {id(p) for p in self.page.context.pages if p is not self.page}
        except Exception:       # noqa: BLE001  (a page double)
            self._before = set()
        context = getattr(self.page, "context", None) or self.page
        for target, events in ((context, (("request", self._request),
                                          ("requestfinished", self._done),
                                          ("requestfailed", self._done))),
                               (self.page, (("popup", self._popup),))):
            for event, fn in events:
                try:
                    target.on(event, fn)
                    self._targets.append((target, event, fn))
                except Exception:   # noqa: BLE001  (a page double)
                    pass

    def stop(self) -> None:
        if not self._on:
            return
        self._on = False
        for target, event, fn in self._targets:
            try:
                target.remove_listener(event, fn)
            except Exception:   # noqa: BLE001  (the page is gone)
                pass
        self._targets = []
        # a held send no tab claimed may have been the send (U1)
        self.possible += [r for r in self.unplaced_sends() if r not in self.possible]
        self._unplaced = []

    def unplaced_sends(self) -> list[str]:
        """The held POST, PUT or PATCH rows no tab of the job's page claimed
        (U1)."""
        return [row for _, _, row in self._unplaced
                if row.split(" ", 1)[0] in self._SEND_METHODS]

    def any(self) -> bool:
        """Something that may have been the send left."""
        return bool(self.sent or self.possible or self.unplaced_sends())

    def first(self) -> str:
        return (self.sent or self.possible or self.unplaced_sends() or [""])[0]


def new_confirmation(before: str, after: str) -> str:
    """A received phrase the page shows now and did not show before the
    submit click, or ""."""
    fresh = sorted(confirmation_words(after) - confirmation_words(before))
    return fresh[0] if fresh else ""


# Text that changes while a page stands still (READ-04): a relative time
# ("posted 3 minutes ago"), a clock, a count; left out of `page_signature`.
_VOLATILE_TEXT = re.compile(
    r"\b\d+\s*(?:s|sec|second|min|minute|h|hr|hour|d|day|week|month|year)s?\s+ago\b"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|\d+", re.I)
SIGNATURE_TEXT_CHARS = 400         # of the steadied text `page_signature` keeps
# A wizard's step marker ("Step 2 of 5", "1 / 3"): a number that moves only
# when the page does, kept in the signature (review M2)
_STEP_MARKER = re.compile(r"\b(?:step\s+)?\d+\s*(?:of|/)\s*\d+\b", re.I)


def page_signature(url: str, digest: apply_form.FormDigest) -> tuple:
    """The page as it stands, for "did not advance" (READ-04): its host and
    path, its title, its fields (label, type, required), its button texts,
    its step markers ("2 of 3", `_STEP_MARKER`), and the head of its text
    with times, clocks and numbers taken out. Never the judge's read (a read
    that flips on the same page is the same page) and never a ticker, a
    timestamp or a counter."""
    parts = urlsplit(str(url or ""))
    raw = f"{digest.title or ''}\n{digest.text or ''}"
    steps = tuple(" ".join(m.group(0).lower().split()) for m in _STEP_MARKER.finditer(raw))
    text = " ".join(_VOLATILE_TEXT.sub(" ", digest.text or "").split())
    return (parts.hostname or "", parts.path, " ".join((digest.title or "").split()),
            tuple((" ".join((f.label or "").split()), f.type, bool(f.required))
                  for f in digest.fields),
            tuple(" ".join((b.text or "").split()) for b in digest.buttons),
            steps, text[:SIGNATURE_TEXT_CHARS])


def _fields_sig(digest: apply_form.FormDigest) -> tuple:
    """The page's form as the run saw it: each field's label and type."""
    return tuple((" ".join((f.label or "").split()), f.type) for f in digest.fields)


def _record_verification(rec: dict, verification: list[VerifyResult]) -> None:
    """The page record's verification, merged by field (SP6 review M3): a
    row for each field the page's fills verified, the latest read of each;
    a sub-fill (a revealed field, a repair) or a re-verification (a value
    the page changed, a retyped box) replaces its own fields' rows and
    keeps the rest."""
    rows = {int(r["n"]): r for r in rec.get("verification") or []}
    for v in verification:
        rows[v.n] = {"n": v.n, "label": v.label, "ok": v.ok, "p_correct": v.p_correct,
                     "p_placeholder": v.p_placeholder}
    rec["verification"] = list(rows.values())


def _spare_judged(plan: FillPlan, judged: set[int]) -> None:
    """The fields of a repair's plan that only the judge's reading of a
    message named (`judged`, SP6 review M1) are asked as required but kept
    optional in the plan: one without an answer stays blank, never the
    plan's park reason and never a missing question for the person (a
    confident wrong reading must not park on another field or ask for an
    answer the form never asked for). `_repair_named` decides on them."""
    if not judged:
        return
    labels = set()
    for pf in plan.fields:
        if pf.n in judged:
            pf.required = False
            if pf.action == "skip":
                labels.add(pf.label)
    plan.missing = [(q, c) for q, c in plan.missing if q not in labels]
    head = "required field without an answer: "
    if plan.park_reason.startswith(head) and plan.park_reason[len(head):] in labels:
        hard = next((pf for pf in plan.fields if pf.action == "skip" and pf.required), None)
        plan.park_reason = f"{head}{hard.label}" if hard is not None else ""


# A button outside any form (a wizard's footer): the fields of the lowest box
# above it that holds any, by their id, name or type; a box of another form
# (a talent-community sign-up) holds none of them (SP6 review R2-I1)
_BUTTON_HOME_JS = r"""el => {
  const FIELD = 'input:not([type=hidden]):not([type=submit]):not([type=button])'
    + ':not([type=image]):not([type=reset]), select, textarea';
  for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
    const got = Array.from(p.querySelectorAll(FIELD));
    if (got.length) return got.map((f) => f.id || f.getAttribute('name') || f.type).slice(0, 60);
  }
  return [];
}"""


def _ident_attrs(ident: str) -> str:
    """An identity (`apply_form.IDENT_FN_JS`) without its label part: the
    control's own tag, type, id, name, aria-label, test attributes and
    placeholder."""
    return str(ident).rsplit("|", 1)[0]


_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                         "\u00ab": '"', "\u00bb": '"', "`": "'"})


def _plain(text: str) -> str:
    """Words for a literal label match: case folded, curly quotes made
    straight, spaces folded, a required mark at either end dropped."""
    t = " ".join(str(text or "").translate(_QUOTES).lower().split())
    return t.strip(" *\u2731\uff0a:")


def field_named_in(message: str, fields) -> int | None:
    """The one field whose whole label `message` holds, literally, after
    `_plain` (a label inside a longer word never counts: "Name" in
    "Username"); None when no label or more than one does. A label that also
    appears inside another field's label ("Email" in "Email confirmation")
    is ambiguous and never names a field. SP6 review, round 2's addition."""
    labels = {f.n: _plain(f.label) for f in fields if _plain(f.label)}
    text = _plain(message)
    found = []
    for n, label in labels.items():
        if any(m != n and label in other for m, other in labels.items()):
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(label)}(?![a-z0-9])", text):
            found.append(n)
    return found[0] if len(found) == 1 else None


def _message_key(text: str) -> str:
    """A form's message, spaces folded (the key of `_JobRun._spared`)."""
    return " ".join(str(text or "").split())


def _label_key(label: str) -> str:
    """A field's question words, spaces and case folded (the key the
    messages' tried fields keep, M1)."""
    return " ".join((label or "").split()).lower()


class _AsForm(Exception):
    """Raised by the account step, before anything is typed, for an account
    screen that carries the application (a question only an application
    asks, or a written answer): the loop hands the screen to the form step,
    whose submit gate is the only sender."""

    def __init__(self, digest, answers: dict, plan: FillPlan):
        super().__init__("account screen that sends the application")
        self.digest = digest
        self.answers = answers
        self.plan = plan


class _Accounts:
    """Account transitions for one job; secrets bypass the generic filler.

    Sign-in comes in two shapes: the address and the password on one screen,
    or the address first (`Next`) and the password or the create-account form
    on the screen after it (iCIMS, Workday). Either way the loop judges every
    screen and calls back here; a password-only screen is taken once the
    address went in on the same site this job, or when the ledger knows the
    account. Other boxes on an account screen (a name, a phone, a privacy
    checkbox) are filled from the user's facts through the ordinary plan; one
    the facts cannot answer parks the job with its question, like any form."""

    MAX_STEPS_PER_SITE = 4      # account screens handled per site before the job parks

    def __init__(self, run):
        self.run = run
        self.email_sites: set[str] = set()      # sites where the address went in this job
        self.steps: dict[str, int] = {}
        # (site, "login" | "signup") where the password went in this job: a
        # second password screen of the same kind is a rejected password, and
        # typing it again only moves the account toward a lockout
        self.password_typed: set[tuple[str, str]] = set()

    def login(self, page, digest, host: str) -> bool:
        account = ats_accounts.lookup(host)
        if account:
            if account.get("method") != "master_password":
                return False
            return self._fill(page, digest, host, str(account.get("email") or ""), False)
        if not ats_accounts.has_password():
            return False
        if _email_first(digest) or _site(host) in self.email_sites:
            # the address screen of a two-step sign-in, or the password screen
            # after it: the next screen says whether the account exists
            return self._fill(page, digest, host, self._signup_email(), False)
        if any(password_step(g) == "signup" for g in account_forms(page, digest)):
            # ADV-08: a sign-up beside the sign-in, and no account in the
            # ledger for the site: the sign-up's form is the step
            return self._fill(page, digest, host, self._signup_email(), True)
        # Expose account-creation links as buttons to the same role judge.
        links = page.get_by_role("link").filter(has_text=re.compile(r"create.*account|sign up|register", re.I))
        try:
            count = links.count()
            if not count:
                return False
            # a header link and a body link that point at the same page are one
            # offer; two different destinations are a choice nobody made. The
            # hrefs are resolved against the page first, so an absolute link
            # and a relative one to the same target count once.
            hrefs = [links.nth(i).get_attribute("href") for i in range(count)]
            targets = {urljoin(page.url, h) for h in hrefs if h}
            if len(targets) != 1:
                return False
            target = targets.pop()
            self.run._check_host(target)
            link_digest = apply_form.FormDigest(
                url_host=host, title=digest.title, text=digest.text,
                buttons=[apply_form.Button(0, (0, "a"), links.first.inner_text(), "")])
            plan = apply_judge.plan(link_digest, self.run.catalog,
                                    self.run._map(link_digest, {}, "job_posting", discover=False,
                                                  own_page=False), company=self.run._company())
            advance_conf = plan.buttons.get("advance", (None, 0))[1]
            self.run._decide("signup_link", "the sign-in page's one create-account link",
                             target=target, advance=advance_conf)
            if advance_conf < apply_judge.BUTTON_ADVANCE_MIN_CONF:
                return False
            page.goto(target, timeout=self._nav_timeout())
            self.run._check_host(page.url)
            # the sign-up page renders like any other: it is read once it
            # holds still (NAV-03)
            info = apply_fill.settle(page, CLICK_TIMEOUT_S)
            self.run._decide_next("settled", f"settled {_settled_ms(info)} ms after the "
                                             "create-account link")
            fresh = self.run._drop_foreign_controls(self.run._extract(page))
            # the loop's own read (NAV-03): the judge with the page's
            # structure (a box that makes a password is a sign-up), an unsure
            # read taken once more after a settle, then the structure alone
            answers = self.run._read(fresh)
            state, confidence = apply_judge.read_page_state(answers)
            if confidence < apply_judge.PAGE_STATE_MIN_CONF:
                fresh, answers, state, confidence = self.run._reread(fresh, answers, state,
                                                                     confidence)
            if confidence < apply_judge.PAGE_STATE_MIN_CONF \
                    and apply_judge.structural_kind(self.run._facts) == "signup_form":
                self.run._decide_next("structural_fallback", f"unsure of the page the "
                                                             f"create-account link led to "
                                                             f"({state}, {confidence:.2f}); a box "
                                                             f"makes the password: a sign-up",
                                      to="signup_form")
                state = "signup_form"
                confidence = apply_judge.PAGE_STATE_MIN_CONF
            # the page the link led to is a page of the job: the record and
            # the trace carry it, and the sign-up's step is written on it
            self.run._new_page_record(state, confidence, digest=fresh, answers=answers)
            # a park from here names the sign-up page and its own evidence: its
            # read, boxes and buttons (the stored answers are its answers now)
            if state != "signup_form" or confidence < apply_judge.PAGE_STATE_MIN_CONF:
                raise _Parked("needs_human", f"login wall (the create-account link led to "
                                             f"{_cap(page.url, 120)}: "
                                             f"{self.run._account_evidence(state, fresh)})",
                              LOGIN_NOTE)
            if not self.signup(page, fresh, fresh.url_host or _host(page.url)):
                raise _Parked("needs_human", f"account signup needed (the create-account link "
                                             f"led to {_cap(page.url, 120)}: "
                                             f"{self.run._account_evidence(state, fresh)})",
                              LOGIN_NOTE)
            return True
        except (_Parked, _AsForm):
            # the loop's own park (a host check, an unanswerable box): the
            # reason names what was refused and the loop ends the job with
            # it; a sign-up that sends the application goes to the form step
            raise
        except Exception as e:  # noqa: BLE001  (account details stay out of errors)
            self.run._trace("error", step="accounts.login", error=type(e).__name__)
            return False

    def signup(self, page, digest, host: str) -> bool:
        return self._fill(page, digest, host, self._signup_email(), True)

    def _signup_email(self) -> str:
        return str(self.run.r.run_context().get("signup_email") or "")

    def _timeout(self) -> int:
        """Milliseconds for one action on the page, inside the job's clock."""
        return max(1, int(min(5, self.run.deadline - self.run.r.clock()) * 1000))

    def _nav_timeout(self) -> int:
        """Milliseconds for a page load, which takes longer than an action and
        gets the loop's own click budget (`CLICK_TIMEOUT_S`)."""
        return max(1, int(min(CLICK_TIMEOUT_S,
                              self.run.deadline - self.run.r.clock()) * 1000))

    def _record(self, digest, email: str, advance_n: int, passwords: int,
                others: list | None = None) -> None:
        """Write the account step into the current page's record (spec 3.5):
        the address that was used, one hidden row per password box, the other
        boxes filled from the facts, and the button that was clicked. The
        password value is never carried; the row is marked hidden and
        `write_record` writes `<hidden>`."""
        rec = self.run.pages[-1] if self.run.pages else None
        if rec is None:
            return
        if email:
            rec["filled"].append({"n": -1, "label": "Email", "value": email,
                                  "type": "email", "id_or_name": "account_email",
                                  "upload": False})
        for i in range(passwords):
            rec["filled"].append({"n": -2 - i, "label": "Password", "value": "",
                                  "type": "other", "id_or_name": "account_password",
                                  "upload": False, "hidden": True})
        for f in others or []:
            rec["filled"].append({"n": f.n, "label": f.label, "value": f.value,
                                  "type": "", "id_or_name": "", "upload": False})
        button = next((b for b in digest.buttons if b.n == advance_n), None)
        rec["clicked"].append(f"{button.text if button else 'account'} (advance)")

    def _fill(self, page, digest, host: str, email: str, signup: bool) -> bool:
        if not email or not ats_accounts.has_password() or self.run.r.clock() >= self.run.deadline:
            return False
        if not _credential_form(digest):
            # a page of other boxes is a form whatever it was read as: typing
            # the facts into it and clicking its button would send it
            return False
        site = _site(host)
        self.steps[site] = self.steps.get(site, 0) + 1
        if self.steps[site] > self.MAX_STEPS_PER_SITE:
            return False
        groups = account_forms(page, digest)
        if groups:
            # ADV-08: a sign-in and a sign-up side by side: the sign-in when
            # the ledger knows the account, else the sign-up; its own form's
            # boxes and button alone are acted on
            want = "signin" if ats_accounts.lookup(host) else "signup"
            chosen = next((g for g in groups if password_step(g) == want), None)
            if chosen is not None:
                self.run._decide("account_form", f"a sign-in and a sign-up side by side; the "
                                                 f"{'sign-in' if want == 'signin' else 'sign-up'} "
                                                 "form is the step",
                                 fields=[f.label for f in chosen.fields])
                digest, signup = chosen, want == "signup"
        frames = apply_form.frames(page)
        guard = _NavGuard(self.run, page)
        try:
            answers = self.run._map(digest, {}, "signup_form" if signup else "login_wall",
                                    discover=False)
            plan = apply_judge.plan(digest, self.run.catalog, answers,
                                    company=self.run._company())
            rec = self.run.pages[-1] if self.run.pages else {"flags": {}}
            plan = self.run._complete_option_plan(digest, answers, plan, rec)
            advance = account_advance(digest, plan, signup=signup)
            if advance is None:
                return False
            by_n = {pf.n: pf for pf in plan.fields}
            passwords, emails, others, drafts = [], [], [], []
            password_hosts = {_host(page.url)}
            for field in digest.fields:
                loc = apply_form.resolve(page, field.locator).first
                idx = int(field.locator[0])
                if (loc.get_attribute("type") or "").lower() == "password":
                    if _not_an_account_password(field):
                        # a masked SSN, ID number or one-time code on the
                        # screen: the master password never goes there
                        if field.required:
                            raise _Parked("needs_human", apply_judge.sensitive_reason(
                                field.label or "a masked box"))
                        continue
                    passwords.append(loc)
                    self.run._keep_secret_box(field.locator)
                    if 0 <= idx < len(frames):
                        guard.frames.add(id(frames[idx]))
                        password_hosts.add(_host(self.run._frame_url(frames, idx)))
                elif _is_email_box(field):
                    emails.append(loc)
                    if 0 <= idx < len(frames):
                        guard.frames.add(id(frames[idx]))
                else:
                    pf = by_n.get(field.n)
                    if pf is not None and pf.action == "generate":
                        drafts.append(pf)
                    elif pf is not None and pf.action in ("fill", "select", "upload") \
                            and (pf.value or pf.option):
                        others.append(pf)
                    elif field.required and apply_judge.is_sensitive_field(field.label,
                                                                           field.id_or_name):
                        raise _Parked("needs_human", apply_judge.sensitive_reason(field.label))
                    elif field.required:
                        self.run._add_missing(field.label, field.help or field.placeholder
                                              or field.type)
                        raise _Parked("needs_human",
                                      f"required field without an answer: {field.label}")
            if not passwords and not emails:
                return False
            # A screen that asks what only an application asks (a profile
            # link, work authorization, a written answer) is the form: the
            # form step writes and verifies the answers, types the password,
            # and clicks its way on or stops at the submit gate.
            carries = bool(drafts) or any(pf.fact_key not in _ACCOUNT_FACTS for pf in others)
            sends = _sends_application(digest, advance[0], account_only=not others)
            if carries:
                if sends:
                    # the button is the page's submit, never an advance to click
                    plan.buttons.setdefault("submit", advance)
                raise _AsForm(digest, answers, plan)
            if sends:
                # nothing is typed: the click would send the application past
                # the gate. With the boxes of a sign-up alone (a name, a phone,
                # the terms) the gate cannot tell a button that starts the
                # application from one that sends it (a wrong "submitted" loses
                # the job), so the human signs in.
                text = next((b.text for b in digest.buttons if b.n == advance[0]), "")
                raise _Parked("needs_human", f"the sign-in's button reads as sending the "
                                             f"application ({text})", LOGIN_NOTE)
            if passwords and not emails and not signup \
                    and site not in self.email_sites and not ats_accounts.lookup(host):
                return False
            if passwords:
                # the last word before the password is typed: the page and
                # every frame holding a password box are on the application's
                # site, whichever path (a sign-up link, a redirect) led here
                outside = sorted(h for h in password_hosts if h and not self.run._password_ok(h))
                if outside:
                    raise _Parked("needs_human",
                                  f"a sign-in on {outside[0]}, outside the application site",
                                  LOGIN_NOTE)
                kind = "signup" if signup else "login"
                if (site, kind) in self.password_typed:
                    raise _Parked("needs_human", f"the {kind} on {host} did not take the "
                                                 "master password", LOGIN_NOTE)
            guard.start()
            try:
                for loc in emails:
                    try:
                        current = str(loc.input_value(timeout=self._timeout()) or "")
                    except Exception:       # noqa: BLE001  (a box that cannot be read is filled)
                        current = ""
                    if current.strip().lower() != email.strip().lower():
                        loc.fill(email, timeout=self._timeout())
                filled = apply_fill.apply(page, FillPlan(fields=others),
                                          deadline=self.run.deadline, clock=self.run.r.clock)
                for loc in passwords:
                    if not ats_accounts.fill_password(page, loc):
                        return False
                if passwords:
                    self.password_typed.add((site, "signup" if signup else "login"))
                self.run._filled_any = True
                if self.run._human_check_showing(checkbox=True):
                    # the sign-in's button would fail without the person's tick
                    self.run._wait_for_human_check("a CAPTCHA check is on the account form",
                                                   checkbox=True)
                self._record(digest, email if emails else "", advance[0], len(passwords), filled)
                # an aborted navigation leaves the tab on a browser error page,
                # so the note for the human names the page before the click
                before = f"{page.url} | {page.title()}"
                result = apply_fill.click(page, digest, advance[0],
                                          timeout_s=self._timeout() / 1000,
                                          check=self.run._live_check("advance", account=True))
            finally:
                guard.stop()
            if result.refused:
                text = next((b.text for b in digest.buttons if b.n == advance[0]), "")
                raise _Parked("needs_human", f"the account step's button ({_cap(text, 60)}) "
                                             f"changed before the click: {result.refused}; "
                                             f"nothing was clicked", LOGIN_NOTE)
            if signup and passwords and result.clicked:
                # the click landed, so the account may already exist whatever
                # the page did next; a ledger entry for an account that was
                # never created costs one failed login, a missing one costs a
                # second signup with the same address
                ats_accounts.record(host, email)
            if guard.blocked:
                raise _Parked("needs_human", f"left the allowed sites: {guard.blocked[0]}", before)
            if emails:
                self.email_sites.add(site)
            if not result.changed:
                if not self.run._human_check_showing():
                    return False
                self.run._wait_for_human_check("a CAPTCHA challenge appeared at sign-in")
            self.run._check_host(page.url)
            # the loop judges whatever comes next: the password screen, the
            # form, a code gate, or the same screen with an error (which the
            # per-site step cap ends)
            return True
        except (_Parked, _AsForm):
            raise
        except Exception as e:  # noqa: BLE001  (Playwright may include filled values)
            self.run._trace("error", step="accounts.fill", error=type(e).__name__)
            return False


class _Inbox:
    def __init__(self, run):
        self.run = run

    def fetch_code(self, page, site: str, inbox_url: str) -> str | None:
        self.run._check_host(inbox_url)
        entry = self.run.entry
        errors: list[str] = []
        code = apply_inbox.fetch_code(page, site, inbox_url, jev=self.run.r.jev,
                                      clock=self.run.r.clock, sleep=self.run.r.sleep,
                                      deadline=self.run.deadline,
                                      ats=str((entry.get("ats") or {}).get("system") or ""),
                                      company=str(entry.get("company") or ""), errors=errors)
        self.run._trace("inbox", found=bool(code), errors=errors)
        return code


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


class _Refused(Exception):
    """The form refused the submit as typed and nothing left the page
    (`_JobRun._not_sent`): `problems` are the form's messages, `park` the
    park it would be without a repair (ADV-02)."""

    def __init__(self, problems: list[dict[str, Any]], park: _Parked):
        super().__init__(park.reason)
        self.problems = problems
        self.park = park


def _host(url_or_netloc: str) -> str:
    """The lowercased hostname of a URL or a netloc, without a port."""
    raw = str(url_or_netloc or "").strip()
    if "://" in raw:
        return (urlsplit(raw).hostname or "").lower()
    return raw.split("/")[0].rsplit("@", 1)[-1].split(":")[0].lower()


def _site(url_or_host: str) -> str:
    """The registrable domain of a URL or host, near enough without a public
    suffix list: the last two labels, three under a two-letter country code
    whose second level is generic (`jobs.example.co.uk` -> `example.co.uk`)."""
    host = _host(url_or_host)
    if not host or host.replace(".", "").isdigit() or "." not in host:
        return host             # an IP address, `localhost`
    labels = [p for p in host.split(".") if p]
    if len(labels) >= 3 and (len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL
                             or ".".join(labels[-2:]) in _SHARED_HOSTING):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _is_captcha_url(url: str) -> bool:
    """A bot-check provider's page: hCaptcha, reCAPTCHA (Google's `/recaptcha`
    paths too), Cloudflare's challenge host, Arkose, GeeTest."""
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if _site(host) in CAPTCHA_SITES or host == "challenges.cloudflare.com":
        return True
    return _site(host) == "google.com" and parts.path.startswith("/recaptcha")


def _tracker(url_or_host: str) -> bool:
    """A programmatic-ad tracker or a link shortener (`TRACKER_SITES`)."""
    return bool(_host(url_or_host)) and _site(url_or_host) in TRACKER_SITES


def _aggregator(url_or_host: str) -> bool:
    """A job board or aggregator (`AGGREGATOR_SITES`)."""
    return bool(_host(url_or_host)) and _site(url_or_host) in AGGREGATOR_SITES


# The control on an aggregator's posting that leads to the company's own site
_COMPANY_SITE = re.compile(
    r"\bapply\s+(?:on|at|via|through)\s+(?:the\s+)?(?:company|employer)(?:'s|s)?\s+"
    r"(?:site|website|page)\b|\b(?:continue|go)\s+to\s+(?:the\s+)?(?:company|employer)"
    r"(?:'s|s)?\s+(?:site|website)\b|\bvisit\s+(?:the\s+)?(?:company|employer)(?:'s|s)?\s+"
    r"(?:site|website)\b|\bapply\s+externally\b", re.I)


def company_site_control(digest: apply_form.FormDigest, *, board: str = "",
                         targets: Mapping[int, str] | None = None) -> apply_form.Button | None:
    """The control of an aggregator's posting that leads to the company's own
    site: one that says so ("Apply on company site", "Continue to the
    employer's website"), else the one Apply-worded link (`targets`: each
    button's link target) that leads off the board (`board`: the board's
    host; M7: a board whose off-site control reads just "Apply"). Never a
    form's own button or the site's chrome; None when there is none."""
    said = next((b for b in digest.buttons if _COMPANY_SITE.search(
        str(b.text or "").translate(jev.APOSTROPHES)) and not b.in_form), None)
    if said is not None:
        return said
    off = [b for b in digest.buttons
           if apply_judge.entry_worded(b.text) and not b.in_form and not b.chrome
           and (targets or {}).get(b.n) and _site((targets or {})[b.n]) != _site(board)]
    return off[0] if len(off) == 1 else None


_LINK_TARGET_JS = ("el => { const a = el.closest('a[href]'); "
                   "return a ? String(a.href || '') : ''; }")


def link_targets(page, digest: apply_form.FormDigest) -> dict[int, str]:
    """The link each Apply-worded control leads to (its own `a[href]` or its
    enclosing one), by button number; a control that is no link is left
    out."""
    out: dict[int, str] = {}
    for b in digest.buttons:
        if not apply_judge.entry_worded(b.text):
            continue
        try:
            href = str(apply_form.resolve(page, b.locator).first.evaluate(
                _LINK_TARGET_JS, timeout=apply_fill.ACTION_TIMEOUT_MS) or "")
        except Exception:       # noqa: BLE001  (a page double, a detached control)
            continue
        if href.lower().startswith(("http://", "https://")):
            out[b.n] = href
    return out


def content_frame_site(frame_url: str, page_url: str, hosts=()) -> bool:
    """May a child frame at `frame_url` be read before its page (study G9,
    R2-M1)? Only a frame of the page's own site, a known ATS platform
    (`ATS_SITES`) or an admitted application host (`hosts`): an iCIMS
    content frame, a Greenhouse embed; never an embedded video or an ad. A
    blank or srcdoc frame is the page's own."""
    host = _host(frame_url)
    if not host:
        return True
    site = _site(host)
    return site == _site(page_url) or site in ATS_SITES or any(site == _site(h) for h in hosts)


def _on_linkedin_redirector(url: str) -> bool:
    """Is `url` LinkedIn's `/safety/go/` hop to an off-site Apply page (on
    any LinkedIn host)?"""
    return apply_linkedin.url_kind(url) == "redirector"


def _easy_apply(entry: Mapping[str, Any]) -> bool:
    """Is the queue entry an Easy Apply job? A legacy entry may carry the
    flag as text."""
    flag = entry.get("is_easy_apply")
    return flag is True or str(flag or "").strip().lower() in ("true", "1", "yes")


def _empty_read(digest: apply_form.FormDigest) -> bool:
    """A read taken before the page rendered (study G5: 0 characters and 0
    controls at `load` on five ATSs): no button at all (a form whose footer
    renders late), or no form field and under `EMPTY_TEXT_MIN` characters of
    visible text. A page with form fields and a button has rendered, however
    short it is: a sign-in box and its button make a whole page."""
    if not digest.buttons:
        return True
    if digest.fields:
        return False
    return len((digest.text or "").strip()) < EMPTY_TEXT_MIN


def _dropped_load(e: BaseException) -> bool:
    """A load the network dropped: Playwright's `net::ERR_...`, or Chromium
    swapping the page for its own error page mid-load."""
    text = str(e)
    return "net::ERR_" in text or "chrome-error://" in text


def _settled_ms(info: Any) -> int:
    """The milliseconds `apply_fill.settle` reported (0 from a stand-in)."""
    return int(info.get("ms", 0)) if isinstance(info, Mapping) else 0


def _settle_capped(info: Any) -> bool:
    return bool(info.get("capped")) if isinstance(info, Mapping) else False


def settled_words(info: Any, then: str = "") -> str:
    """A settle in the trace's words: "settled N ms", and when the cap
    released it (a loading placeholder left up, a page that keeps moving),
    "settled N ms, capped", so a live run shows a page that makes every
    settle wait its whole cap."""
    words = f"settled {_settled_ms(info)} ms"
    if _settle_capped(info):
        words += (", capped (a loading placeholder stayed up or the page kept moving; "
                  "read as it was)")
    return f"{words} {then}".strip()


def _has_content(page) -> bool:
    try:
        return bool(page.evaluate(
            "() => !!document.body && (document.body.innerText || '').trim().length > 0"))
    except Exception:       # noqa: BLE001
        return False


def open_page(page, url: str, *, timeout_ms: int | None = None,
              settle_s: float | None = None) -> list[dict[str, Any]]:
    """The first load of a job's page, and the probe's (NAV-01, NAV-02): to
    `domcontentloaded` (a page whose `load` never fires, a stalled image or
    a script, is read all the same), one retry after `GOTO_RETRY_S` of a
    load the network dropped (`_dropped_load`), a timeout with a page on the
    screen read as it is; then the page settles. Returns the decisions
    taken ({what, why, ...}); a load that failed raises."""
    rows: list[dict[str, Any]] = []
    timeout = GOTO_TIMEOUT_MS if timeout_ms is None else int(timeout_ms)
    for attempt in (1, 2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            break
        except Exception as e:      # noqa: BLE001  (Playwright's Error and TimeoutError)
            if _closed_error(e):
                raise
            if type(e).__name__ == "TimeoutError" and _has_content(page):
                rows.append({"what": "goto_timeout", "why": "the first load timed out with a "
                                                            "page on the screen; reading it "
                                                            "as it is"})
                break
            if attempt == 2 or not _dropped_load(e):
                raise
            rows.append({"what": "goto_retry", "why": f"the first load failed on the network "
                                                      f"({type(e).__name__}); one retry",
                         "error": _cap(str(e).splitlines()[0] if str(e) else "", 120)})
            # Chromium swaps in its own error page after a dropped load; its
            # navigation would cut the retry short, so the retry waits for it
            # to be up (a fixed pause lost that race on a busy machine, SP5
            # fix round 4), then pauses
            _error_page_up(page, GOTO_ERROR_PAGE_S)
            page.wait_for_timeout(int(GOTO_RETRY_S * 1000))
    info = apply_fill.settle(page, CLICK_TIMEOUT_S if settle_s is None else settle_s)
    rows.append({"what": "settled", "why": settled_words(info, "after the first load"),
                 **(info if isinstance(info, Mapping) else {})})
    return rows


def _error_page_up(page, cap_s: float) -> bool:
    """Wait, up to `cap_s` in all, for Chromium's error page (chrome-error://)
    to be the page and loaded: the condition `open_page`'s retry needs. False
    at the cap (a browser that shows no error page) or when the error page
    does not load in the time left (review round 5, Minor 2): the retry goes
    on either way."""
    step_ms = 50
    cap_ms = max(step_ms, int(cap_s * 1000))
    waited = 0
    while waited < cap_ms:
        try:
            up = str(page.url).startswith("chrome-error://")
        except Exception:       # noqa: BLE001  (a page mid-navigation)
            up = False
        if up:
            try:
                page.wait_for_load_state("load", timeout=max(1, cap_ms - waited))
            except Exception:   # noqa: BLE001  (Playwright's TimeoutError: no load in time)
                return False
            return True
        page.wait_for_timeout(step_ms)
        waited += step_ms
    return False


def _page_closed(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:       # noqa: BLE001  (a page double)
        return False


def _snapshot_or_none(page) -> Any:
    try:
        return apply_fill._snapshot(page)
    except Exception:       # noqa: BLE001  (a page double, a page mid-navigation)
        return None


def _is_password(row: dict) -> bool:
    """A recorded row that came from a password-shaped control
    (`apply_form.is_password_field`, the one definition the planner and the
    accounts hook read too): its value is written as `<hidden>`."""
    return apply_form.is_password_field(str(row.get("type", "")),
                                        str(row.get("id_or_name", "")),
                                        str(row.get("label", "")),
                                        str(row.get("autocomplete", "")))


# The box the emailed code goes in (`apply_judge.code_field`: a verification,
# security, one-time or OTP code box first, never a postal or promo code).
_code_field = apply_judge.code_field


def _button_text(digest: apply_form.FormDigest, n: int) -> str:
    return next((b.text for b in digest.buttons if b.n == n), "")


def _chrome(digest: apply_form.FormDigest, n: int) -> bool:
    """Is button `n` in the site's header or top bar (`Button.chrome`)?"""
    return any(b.n == n and b.chrome for b in digest.buttons)


# an account screen's own button by its words: sign in, log in, create an
# account, register, sign up, continue, next
_ACCOUNT_BUTTON = re.compile(r"\b(sign|log)[\s-]*(in|on|up)\b|\blogin\b|\bregister\b"
                             r"|\bcreate\b.*\baccount\b|\b(continue|next)\b", re.I)


_SIGN_UP_WORDS = re.compile(r"\bcreate\b|\bregister\b|\bsign[\s-]*up\b|\bjoin\b", re.I)
_SIGN_IN_ONLY = re.compile(r"\b(sign|log)[\s-]*(in|on)\b|\blogin\b", re.I)


def password_step(digest: apply_form.FormDigest) -> str:
    """What an account screen's password boxes say it is (review R2-I4):
    "signup" for a `new-password` box or two boxes (a password and its
    confirmation), "signin" for `current-password` alone, or for one box
    that names neither beside a "Forgot your password?" control (a sign-in's
    own tie-break, review round 4, M4); "" (the read decides) with no box,
    one box that names neither and nothing else to go on (a one-box sign-up
    looks like a sign-in, review round 3, M2), or boxes that say both (a
    change of password)."""
    boxes = [f for f in digest.fields
             if apply_form.is_password_field(f.type, f.id_or_name, f.label, f.autocomplete)]
    if not boxes:
        return ""
    tokens = {str(f.autocomplete or "").lower() for f in boxes}
    if "new-password" in tokens and "current-password" in tokens:
        return ""
    if "new-password" in tokens or len(boxes) >= 2:
        return "signup"
    if "current-password" in tokens:
        return "signin"
    forgot = any(_FORGOT.search(b.text or "") for b in digest.buttons) \
        or bool(_FORGOT.search(digest.text or ""))
    return "signin" if forgot else ""


_FORGOT = re.compile(r"\bforgot(ten)?\s+(your\s+)?password\b", re.I)


def account_advance(digest: apply_form.FormDigest, plan: FillPlan, *,
                    signup: bool = False) -> tuple[int, float] | None:
    """The button an account screen's step clicks: the judged advance, else
    the judged submit, at `BUTTON_ADVANCE_MIN_CONF` or above, never one of
    the site's header (a header's "Sign In" on a sign-up screen, review
    M11), never a sign-in with another site ("Continue with Google",
    ADV-09), and never one whose words name the other step while the screen
    has its own button that names this one and its password boxes say no
    other step than the read (`password_step`: a sign-up screen's "Already
    have an account? Sign In" beside its "Create Account"; a screen whose only way
    on says "Sign in" is a sign-in whatever it was read as; a sign-in read
    as a sign-up keeps its judged Sign In, review R2-I4). With neither: the
    screen's own buttons whose words name the account step ("Create
    Account", "Sign in", "Continue"), one per text (Workday draws "Create
    Account" twice, a click filter over the real button); a sign-up takes the
    one that makes the account, a sign-in the one that signs in (review M1),
    the step the password boxes say when they say one, else the read's; a
    lone one either way; at the advance floor. None when no single button
    fits."""
    read = "signup" if signup else "signin"
    boxes = password_step(digest)
    step = boxes or read
    fits = _SIGN_UP_WORDS if step == "signup" else _SIGN_IN_ONLY
    other = _SIGN_IN_ONLY if step == "signup" else _SIGN_UP_WORDS
    own: dict[str, apply_form.Button] = {}
    for b in digest.buttons:
        if getattr(b, "chrome", False) or getattr(b, "disabled", False) \
                or not _ACCOUNT_BUTTON.search(b.text) or apply_judge.DECLINE_WORDS.search(b.text) \
                or _THIRD_PARTY.search(b.text):
            continue
        own.setdefault(" ".join(b.text.lower().split()), b)
    buttons = list(own.values())
    fitting = [b for b in buttons if fits.search(b.text)]
    text = {b.n: b.text for b in digest.buttons}
    for role in ("advance", "submit"):
        held = plan.buttons.get(role)
        if held is None or held[1] < apply_judge.BUTTON_ADVANCE_MIN_CONF \
                or _chrome(digest, held[0]) or _THIRD_PARTY.search(text.get(held[0], "")):
            continue
        words = text.get(held[0], "")
        if step == read and fitting and other.search(words) and not fits.search(words):
            # the judge rated the screen's own "Sign In" the advance above
            # "Create Account", and the sign-up clicked it and landed on the
            # sign-in screen (the fix round's Workday misses)
            continue
        return held
    if len(buttons) > 1:
        buttons = fitting
    if len(buttons) == 1:
        return buttons[0].n, apply_judge.BUTTON_ADVANCE_MIN_CONF
    return None


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


def _sends_application(digest: apply_form.FormDigest, n: int, *,
                       account_only: bool = True) -> bool:
    """A button the account step may not click: its text reads as sending the
    application (`_submit_shaped`), unless it names a sign-in on a screen of
    the address and the password alone (`account_only`: "Sign in to apply"
    signs in, "Send code" mails one), or as a last step (`_final_shaped`)
    unless it names the account ("Complete registration"). "Create account
    and apply" counts as a send: the sign-up may carry the application. Only
    the submit gate sends an application."""
    button = next((b for b in digest.buttons if b.n == n), None)
    text = button.text if button else ""
    if _submit_shaped(digest, n):
        return not (account_only and apply_judge.SIGN_IN_WORDS.search(text))
    return _final_shaped(digest, n) and not _ACCOUNT_STEP_WORDS.search(text)


def _send_worded(text: str, *, entry: bool = False, account: bool = False) -> bool:
    """A control's text reads as sending the application or as a last step:
    a submit word ("apply" too, unless the click is an Apply entry), unless
    an account step's text names a sign-in; or a final word, unless it names
    the account ("Complete registration")."""
    words = {w.lower() for w in SUBMIT_WORDS.findall(text or "")}
    if entry:
        words.discard("apply")
    if words and not (account and apply_judge.SIGN_IN_WORDS.search(text or "")):
        return True
    return bool(FINAL_WORDS.search(text or "")) and not _ACCOUNT_STEP_WORDS.search(text or "")


def live_refusal(role: str, expected: str, live: Mapping[str, Any], *,
                 account: bool = False) -> str:
    """Why a click in `role` must not happen on the control as it reads now
    (INV-04), or "": its live text (`apply_form.live_text`) reads as sending
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


def linkedin_view(page, *, wait_s: float, job_title: str = "",
                  company: str = "") -> tuple[apply_linkedin.View, int]:
    """The LinkedIn page's `View`, read again every `LINKEDIN_POLL_MS` for up
    to `wait_s` until it decides something waiting cannot change (a top card
    rendered late, 2.5 s after `load` in the fixture); (the view, the ms
    waited). An Easy Apply read, and an offsite Apply found in a list item
    (`Decision.tentative`), count only once they hold after
    `LINKEDIN_EASY_RECHECK_S` and a settle: a list's or a filter's control
    can show before the job's own top card renders. `job_title` and
    `company` are the queued job's (`apply_linkedin.read`)."""
    start = time.monotonic()

    def _read() -> apply_linkedin.View:
        return apply_linkedin.read(page, job_title=job_title, company=company)

    def _recheck() -> apply_linkedin.View:
        page.wait_for_timeout(int(LINKEDIN_EASY_RECHECK_S * 1000))
        apply_fill.settle(page, CLICK_TIMEOUT_S)
        return _read()

    view = _read()
    checked = wait_s <= 0
    while time.monotonic() - start < wait_s:
        d = apply_linkedin.decide(view)
        unsure = d.kind == "easy_apply" or d.tentative
        if (d.final or d.tentative) and (checked or not unsure):
            break
        if unsure:
            checked = True
            view = _recheck()
        else:
            page.wait_for_timeout(LINKEDIN_POLL_MS)
            view = _read()
    d = apply_linkedin.decide(view)
    if not checked and (d.kind == "easy_apply" or d.tentative):
        view = _recheck()       # the wait ran out on a read that is held once more
    return view, int((time.monotonic() - start) * 1000)


def remaps_to_form(state: str, digest: apply_form.FormDigest, url: str) -> bool:
    """A sign-in or sign-up read of a page of form boxes (off LinkedIn) is the
    form: the account step would type the facts in and click its button."""
    return (state in ("login_wall", "signup_form") and bool(digest.fields)
            and not _credential_form(digest) and not apply_linkedin.is_linkedin(url))


def unsure_acts(state: str, digest: apply_form.FormDigest) -> bool:
    """May a read below `PAGE_STATE_MIN_CONF` go on as its guess? Only one of
    `_UNSURE_ACTS`, a code gate only with its code box, a sign-in or sign-up
    only on a screen of account boxes (`_credential_form`) or of form boxes
    (the form, `remaps_to_form`)."""
    if state not in _UNSURE_ACTS:
        return False
    if state == "code_gate":
        return _code_field(digest.fields) is not None
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
                      submit_clicked: bool, code_sent: bool = False) -> tuple[str, str, float]:
    """What a page read as a confirmation means: ("submitted", the reason,
    conf), ("go_on", the read to act on, its probability) or ("park", the
    reason, conf).

    After a click in the submit role (the code step's): received words on
    the page (`CONFIRMATION_WORDS`), or the read at `CONFIRMATION_MIN_CONF`
    on a page with no form field and no send button, is the confirmation;
    else the person checks. After a code step's click alone (`code_sent`: a
    sign-up's email "Verify" is one) only received words make it the
    confirmation; else the person checks. Before any submit click a
    confirmation is never the run's own send: a page with a form field, a
    submit button, a Next, a Continue or an accept and no received words is
    a form or a review misread, and goes on as its next read when the loop
    acts on that one (`_UNSURE_ACTS`); any other parks (the job may have
    been applied to before)."""
    words = confirmation_words(digest.text)
    form = bool(digest.fields) or any(apply_judge.SEND_WORDS.search(b.text) for b in digest.buttons)
    # before any submit, a Next or a Continue asks for more too (SP4: a
    # wizard's summary step read as a confirmation)
    step_button = any(apply_judge.ADVANCE_WORDS.search(b.text) for b in digest.buttons)
    if submit_clicked:
        if words or (conf >= apply_judge.CONFIRMATION_MIN_CONF and not form):
            return "submitted", "confirmation page", conf
        return "park", (f"{CHECK_SENT_REASON}: the page after the submit click reads as "
                        f"confirmation ({conf:.2f}) with a form or a send button and no "
                        f"received words"), conf
    if code_sent and not form:
        if words:
            return "submitted", "confirmation page after the emailed code", conf
        return "park", (f"{CHECK_SENT_REASON}: after the emailed code the page reads as "
                        f"confirmation ({conf:.2f}) with no received words (an email "
                        f"verification thanks the same way)"), conf
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
    `Button.in_form`, INV-03; or, on a page with form fields or controls the
    extractor leaves out (`unclassified`), a submit-worded button that sits
    with them, INV-01: one `apart` from them, a job-alert box beside the
    posting's Apply, is the entry), else the fieldless text match, else on a
    page with fields an Apply-worded control `apart` from them (READ-09: the
    judge took the alert box's button for the entry); (None, "") when
    none."""
    entry = plan.buttons.get("apply_entry")
    if entry is not None and _chrome(digest, entry[0]):
        entry = None            # the site's header is no posting's entry (M11)
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
    """What a posting's entry choice reads from the live page (INV-01,
    INV-03): the Apply-worded controls outside any form (the judged Apply
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
# (never one that goes on to the application: "Continue to job application", R2 Minor 5)
_NOT_NEXT = re.compile(r"\blater\b|\bbrows\w*|\bsearch\w*|\bshopping\b"
                       r"|\b(more|other|similar|all|saved)\s+(jobs|roles|openings|positions)\b",
                       re.I)
_WITH_WORDS = re.compile(r"\bwith\b|\bsign[\s-]*(in|up)\b|\blog[\s-]*in\b", re.I)


_STEP_OF = re.compile(r"\b(?:step|page)\s+(\d+)\s*(?:of|/)\s*(\d+)\b", re.I)
# a sign-in or a profile from another site: never a form's way on (ADV-09)
_THIRD_PARTY = apply_judge.THIRD_PARTY


def step_position(digest: apply_form.FormDigest) -> tuple[int, int] | None:
    """(this step, the steps in all) when the page says so ("Step 2 of 4",
    "Page 1 / 3"), else None. A page that shows several different markers
    (a progress list that names every step) says nothing of which one it
    is on: None (SP6 review M5)."""
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
    another site ("Continue with LinkedIn", ADV-09), is never the way on;
    with no advance and no submit, a step's own accept-worded button (study
    G13: a privacy agreement's "I Accept") is, at the advance floor. With a
    confident advance and a judged submit both (ADV-05), the advance is the
    way on unless the page shows it is the last step: its step marker says
    so, or, with no marker, the submit sits with the page's own fields
    (`submit_apart` False: the caller reads the page).

    The roles look exchanged (SP5, widened in SP6) when the judged advance
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
        advance = None          # a decline, a sign-in elsewhere or the site's header (M11)
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
            # ADV-05: a feedback box's or a talent network's Submit beside a
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
    """A page read as a review's way on (READ-06), as `form_route` gives it:
    with no submit, its confident advance is clicked (a wizard's middle step
    read as the review carries only Next); a submit, a submit-shaped advance
    and a final-shaped one ("Confirm") go to the gate in either mode, since
    on a review a last-step word is the send; "stuck" when there is
    neither. A step's Next beside another box's Submit goes on as on a
    form (ADV-05: `submit_apart`, the step marker)."""
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
    entry (INV-01): with nothing of the application on the page or before it
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
    the Apply entry to click instead of the gate (INV-01): the judged
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
    LinkedIn, the unsure-read rule (`unsure_step`, over the page's `facts`),
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
    applied = apply_judge.already_applied(answers or {}, facts, state)
    if applied:
        return f"park: {ALREADY_APPLIED_REASON} ({applied})"
    unsure = conf < apply_judge.PAGE_STATE_MIN_CONF
    lead = ""
    if remaps_to_form(state, digest, url):
        lead = f"read as {state} with no account boxes: it is the form; "
        state = "application_form"
    if state == "confirmation":
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
    the extractor leaves out, `unclassified`: INV-03): its first control
    that reads as an Apply entry (`apply_judge.entry_worded`, READ-09: the
    word apply, never "Applying tips", "Apply filters", an Apply that sends
    a stored profile or a send word) and is no form's own button
    (`Button.in_form`), the loop's fallback when no confident `apply_entry`
    was judged."""
    if digest.fields or unclassified:
        return None
    return next((b.n for b in digest.buttons
                 if apply_judge.entry_worded(b.text) and not b.in_form and not b.chrome), None)


class LateWatch:
    """The popups an entry click opens after `click_entry` stopped waiting
    (a site that shows "Opening..." and opens the tab a second later): the
    listener stays on the page until `stop`."""

    def __init__(self, page, signal: str, source_url: str):
        self.page = page
        self.signal = signal
        self.source_url = source_url
        self.popups: list = []
        self._on = False

    def _add(self, popup) -> None:
        self.popups.append(popup)

    def start(self) -> None:
        try:
            self.page.on("popup", self._add)
            self._on = True
        except Exception:       # noqa: BLE001  (a page double)
            pass

    def stop(self) -> None:
        if self._on:
            self._on = False
            try:
                self.page.remove_listener("popup", self._add)
            except Exception:   # noqa: BLE001  (the page is gone)
                pass


@contextmanager
def _popups(page):
    """The tabs `page` opens while the block runs (NAV-05), in order."""
    opened: list = []

    def _add(p) -> None:
        opened.append(p)
    try:
        page.on("popup", _add)
        listening = True
    except Exception:       # noqa: BLE001  (a page double)
        listening = False
    try:
        yield opened
    finally:
        if listening:
            try:
                page.remove_listener("popup", _add)
            except Exception:   # noqa: BLE001  (the page is gone)
                pass


_MAILTO_JS = ("el => { const a = el.closest('a[href]'); "
              "return a ? (a.getAttribute('href') || '') : ''; }")


def mailto_address(loc) -> str:
    """The address an Apply control mails to (`mailto:` on it or its link),
    without the query; "" when it is no email link (NAV-09)."""
    try:
        href = str(loc.first.evaluate(_MAILTO_JS, timeout=apply_fill.ACTION_TIMEOUT_MS) or "")
    except Exception:       # noqa: BLE001  (a page double, a detached element)
        return ""
    if not href.lower().startswith("mailto:"):
        return ""
    return _cap(href[len("mailto:"):].split("?", 1)[0], 120)


def click_entry(page, loc, *, timeout_ms: int | None = None) -> tuple[Any, str, int]:
    """Click an Apply entry and wait for what it does, whichever comes
    first: a new tab (the popup), a same-tab navigation, or a same-tab DOM
    change (study G15: none of 24 real entry clicks opened a popup, and a
    fixed popup wait cost 5 s on each). A link that opens a new tab
    (`target=_blank`) waits the whole window for its popup; a DOM change
    gets `POPUP_GRACE_S` more for a popup that follows it. Returns (the popup
    or None, "popup" | "navigation" | "dom" | "none" | "failed: <error>", the
    ms waited). A click that raised sent nothing on its way: "failed". A
    popup later still is the caller's (`LateWatch`)."""
    window_s = (POPUP_TIMEOUT_MS if timeout_ms is None else int(timeout_ms)) / 1000
    popups: list = []

    def _on_popup(p) -> None:
        popups.append(p)

    try:
        page.on("popup", _on_popup)
    except Exception:       # noqa: BLE001  (a page double)
        pass
    start = time.monotonic()
    try:
        try:
            new_tab = bool(loc.first.evaluate(
                "el => { const a = el.closest('a[href]'); return !!a && a.target === '_blank'; }",
                timeout=apply_fill.ACTION_TIMEOUT_MS))
        except Exception:   # noqa: BLE001  (the element is read again by the click)
            new_tab = False
        before = _snapshot_or_none(page)
        url0 = str(page.url)
        try:
            loc.first.click(timeout=apply_fill.ACTION_TIMEOUT_MS)
        except Exception as e:  # noqa: BLE001  (an overlay took the click, the element went)
            if _closed_error(e):
                raise
            return None, f"failed: {type(e).__name__}", int((time.monotonic() - start) * 1000)
        changed_at = None
        while True:
            now = time.monotonic()
            if popups:
                signal = "popup"
                break
            if str(page.url) != url0:
                signal = "navigation"
                break
            if changed_at is None and _snapshot_or_none(page) != before:
                changed_at = now
            if changed_at is not None and not new_tab and now - changed_at >= POPUP_GRACE_S:
                signal = "dom"
                break
            if now - start >= window_s:
                signal = "dom" if changed_at is not None else "none"
                break
            page.wait_for_timeout(ENTRY_POLL_MS)
        return (popups[0] if popups else None), signal, int((time.monotonic() - start) * 1000)
    finally:
        try:
            page.remove_listener("popup", _on_popup)
        except Exception:   # noqa: BLE001
            pass


def await_destination(page, log: logging.Logger | None = None,
                      job_id: str = "") -> tuple[Any, dict[str, Any]]:
    """Settle `page` after an Apply click. On LinkedIn's `/safety/go/` hop,
    whose script sends the tab to the company's site a few seconds after it
    boots, wait for the tab to leave it and settle again; the job-search
    safety interstitial, which waits for a click instead, gets its visible
    "Continue" clicked (a tab that opens is the destination). A hop that
    never moves on stays on LinkedIn and admits nothing. Returns (the page
    the destination is on, {"settled_ms", "continue"})."""
    logger = log or logging.getLogger("apply_run")
    first = apply_fill.settle(page, CLICK_TIMEOUT_S)
    info: dict[str, Any] = {"settled_ms": _settled_ms(first), "capped": _settle_capped(first),
                            "continue": ""}
    if not _on_linkedin_redirector(page.url):
        return page, _past_trackers(page, info, logger, job_id)
    cont = apply_linkedin.continue_control(page)
    if cont is not None:
        info["continue"] = cont.label
        logger.info("job %s: the LinkedIn interstitial waits for %r; clicking it", job_id,
                    cont.label)
        popup, signal, _ = click_entry(page, page.main_frame.locator(cont.css))
        info["continue_signal"] = signal
        if popup is not None:
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=CLICK_TIMEOUT_S * 1000)
            except Exception:   # noqa: BLE001
                pass
            return popup, info
    if _on_linkedin_redirector(page.url):
        try:
            page.wait_for_url(lambda u: not _on_linkedin_redirector(u),
                              timeout=REDIRECT_TIMEOUT_S * 1000)
        except Exception as e:      # noqa: BLE001  (the loop reads whatever the tab shows)
            logger.info("job %s: the LinkedIn redirect did not move on (%s)", job_id,
                        type(e).__name__)
            return page, info
    again = apply_fill.settle(page, CLICK_TIMEOUT_S)
    info["settled_ms"] += _settled_ms(again)
    info["capped"] = info["capped"] or _settle_capped(again)
    return page, _past_trackers(page, info, logger, job_id)


def _past_trackers(page, info: dict[str, Any], logger: logging.Logger,
                   job_id: str) -> dict[str, Any]:
    """Wait out an ad tracker's or a link shortener's hop (NAV-07: Appcast,
    Joveo, `grnh.se`, `bit.ly` send the tab on by script, a few seconds
    later): up to `TRACKER_HOPS_MAX` hops of `REDIRECT_TIMEOUT_S` each, a
    settle after each. The hops land in `info["trackers"]`; a hop that never
    moves on leaves the page on it."""
    hops: list[str] = []
    while _tracker(page.url) and len(hops) < TRACKER_HOPS_MAX:
        hops.append(_host(page.url))
        try:
            page.wait_for_url(lambda u: not _tracker(u), timeout=REDIRECT_TIMEOUT_S * 1000)
        except Exception as e:      # noqa: BLE001  (the loop parks on a hop that stays)
            logger.info("job %s: the tracker hop %s did not move on (%s)", job_id, hops[-1],
                        type(e).__name__)
            break
        again = apply_fill.settle(page, CLICK_TIMEOUT_S)
        info["settled_ms"] = info.get("settled_ms", 0) + _settled_ms(again)
    if hops:
        info["trackers"] = hops
    return info


def _usage_delta(before: dict, after: dict) -> dict[str, Any]:
    return {"requests": after["requests"] - before["requests"],
            "input_tokens": after["input_tokens"] - before["input_tokens"],
            "usd": after["usd"] - before["usd"]}


def generated_count(pages: list[dict]) -> int:
    """Accepted generated answers across the job's page records (a draft
    reused on a page read again counts once, FILL-12)."""
    return sum(1 for p in pages for g in p.get("generated", [])
               if g.get("ok") and not g.get("reused"))


def _drafts(plan: FillPlan) -> dict[int, str]:
    """n -> the accepted draft, for every field a generator filled."""
    return {pf.n: pf.value for pf in plan.fields
            if pf.fact_key == "needs_generation" and pf.action == "fill"}


def _same_text(a: str, b: str) -> bool:
    """Equal after whitespace runs collapse (a textarea normalises line ends)."""
    return " ".join(str(a).split()) == " ".join(str(b).split())


def _picks(plan: FillPlan) -> dict[int, tuple[str, bool]]:
    """n -> (the option planned, a question's tick boxes), for every pick (a
    select, a radio group, a tick box, a dropdown, a question's tick boxes):
    checked in code against the read-back (FILL-13), never by the judge
    against the sheet."""
    return {pf.n: (str(pf.option), pf.widget == "checkbox_group") for pf in plan.fields
            if pf.action == "select" and pf.option is not None}


def _shaped(plan: FillPlan, digest: apply_form.FormDigest | None = None) -> dict[int, tuple[str, str]]:
    """n -> (kind, the value planned) for every value whose shape the page
    may change and code can compare (FILL-04, FILL-05, FILL-01): a phone
    ("phone": its digits), a date ("date": the day it names, in any shape),
    an upload ("upload": the file's name shown by the box or its widget).
    Checked in code against the read-back (`shaped_holds`), never by the
    judge against the sheet."""
    types = {f.n: f.type for f in digest.fields} if digest is not None else {}
    out: dict[int, tuple[str, str]] = {}
    for pf in plan.fields:
        if pf.action == "upload" and pf.value:
            out[pf.n] = ("upload", str(pf.value))
        elif pf.action != "fill" or not pf.value or pf.fact_key == "needs_generation":
            continue
        elif pf.fact_key == "phone" or (types.get(pf.n) == "tel"
                                        and len(apply_fill.phone_digits(pf.value)) >= 7):
            out[pf.n] = ("phone", str(pf.value))
        elif apply_fill.parse_date(str(pf.value)) is not None:
            out[pf.n] = ("date", str(pf.value))
    return out


def shaped_holds(value: str, kind: str, planned: str) -> bool:
    """Does the read-back `value` hold the `planned` value in the page's
    shape: a phone's digits (a leading US 1 aside), the same day in any
    date shape, the uploaded file's name."""
    if kind == "phone":
        want = apply_fill.phone_digits(planned)
        return bool(want) and apply_fill.phone_digits(value) == want
    if kind == "date":
        want = apply_fill.parse_date(planned)
        return want is not None and apply_fill.parse_date(value) == want
    if kind == "upload":
        return bool(value) and Path(str(value)).name == Path(str(planned)).name
    return False


_TYPED_TYPES = frozenset(("text", "email", "tel", "url", "number", "textarea", "date"))


def _typed_box(digest: apply_form.FormDigest, n: int) -> bool:
    """Is field `n` a plain box a person types into (no widget)?"""
    f = next((x for x in digest.fields if x.n == n), None)
    return f is not None and f.type in _TYPED_TYPES and not f.widget


def new_fields(before: apply_form.FormDigest,
               after: apply_form.FormDigest) -> list[apply_form.Field]:
    """The fields of `after` that `before` did not have (FILL-10: a
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


def _draft_key(f) -> str:
    """A generated answer's question, as the draft cache keys it (FILL-12):
    its label and its help (a length budget), case and spacing aside."""
    return " | ".join(" ".join(str(getattr(f, attr, "") or "").lower().split())
                      for attr in ("label", "help"))


def buttons_moved(before: apply_form.FormDigest, after: apply_form.FormDigest) -> bool:
    """Did the fill change the page's buttons (study G10): a button shown,
    gone, renamed, or enabled or disabled?"""
    def row(d):
        return [(" ".join(b.text.split()), bool(b.disabled), bool(b.chrome)) for b in d.buttons]
    return row(before) != row(after)


def pick_holds(value: str, option: str, group: bool = False) -> bool:
    """Does the read-back `value` show the planned `option` (FILL-13): a tick
    reads "checked"; any other pick reads the option (case, punctuation and
    spacing aside) or a name it goes by (`apply_judge._alias_set`: United
    States of America for United States, CA for California); a question's
    tick boxes (`group`) read the option among the ticked ones ("A, B").
    Words that only contain the option never hold ("Yes, but I will need
    sponsorship" is no "Yes", review M3), and neither do words the option
    only starts with ("Yes" is no "Yes, I will need sponsorship", review R2
    Minor 4)."""
    if str(option).strip().lower() == "checked":
        return str(value).strip().lower() == "checked"
    norm = apply_judge._norm_option
    parts = str(value).split(", ") if group else [str(value)]
    o = norm(option)
    if not o:
        return False
    names = apply_judge._alias_set(str(option))
    for part in parts:
        v = norm(part)
        if v and (v == o or v in names or o in apply_judge._alias_set(part)):
            return True
    return False


_TRACE_LINE = "- Trace: "
_TRACE_LINK = re.compile(r"\]\((" + re.escape(apply_trace.TRACE_DIR) + r"/[^)]*)\)")


def _link_from(target: str, here: str) -> str:
    """`target` (relative to the job folder) as a link from the folder
    `here` (relative to the job folder too)."""
    rel = posixpath.relpath(target.rstrip("/") or ".", here)
    return rel + "/" if target.endswith("/") else rel


def _record_head(path: Path) -> tuple[str, str, str]:
    """(status, reason, written) from a record's header lines."""
    found = {"Status": "", "Reason": "", "Written": ""}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            for key in found:
                if not found[key] and line.startswith(f"- {key}: "):
                    found[key] = line[len(key) + 4:].strip()
            if line.startswith("## "):
                break
    except OSError:
        pass
    return found["Status"], found["Reason"], found["Written"]


def _keep_untraced_record(folder: Path) -> None:
    """A record about to be replaced that no attempt folder holds (one
    written before the trace, or by a run whose trace could not start) is
    kept as `apply_trace/earlier-<k>.md`."""
    path = folder / RECORD_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    if any(line.startswith(_TRACE_LINE) for line in text.splitlines()):
        return      # its attempt folder has its copy
    keep = folder / apply_trace.TRACE_DIR
    try:
        keep.mkdir(parents=True, exist_ok=True)
        k = 1 + max((int(m.group(1)) for p in keep.glob("earlier-*.md")
                     if (m := re.match(r"earlier-(\d+)\.md$", p.name))), default=0)
        (keep / f"earlier-{k}.md").write_text(text, encoding="utf-8")
    except OSError as e:
        log.warning("the earlier record in %s was not kept: %s", folder, type(e).__name__)


def _earlier_attempts(folder: Path, current: str) -> list[str]:
    """One line per earlier record: those kept from before the trace, then
    each attempt folder's (`current`, this attempt's folder, left out)."""
    rows = []
    keep = folder / apply_trace.TRACE_DIR
    earlier = sorted(keep.glob("earlier-*.md"),
                     key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)) if keep.is_dir() else []
    for path in earlier:
        status, reason, written = _record_head(path)
        rows.append(f"- Before the trace: {status}: {reason} ({written}); "
                    f"[record]({apply_trace.TRACE_DIR}/{path.name})")
    for n, path in apply_trace.attempt_dirs(folder):
        rel = f"{apply_trace.TRACE_DIR}/{path.name}"
        if rel == current:
            continue
        record = path / RECORD_NAME
        if record.exists():
            status, reason, written = _record_head(record)
            rows.append(f"- Attempt {n}: {status}: {reason} ({written}); "
                        f"[record]({rel}/{RECORD_NAME})")
        else:
            rows.append(f"- Attempt {n}: no record; [trace]({rel}/)")
    return rows


def write_record(folder: Path, entry: dict, outcome_status: str, reason: str,
                 pages: list[dict], jev_usage: dict, page_text: str, *,
                 missing: list[dict] | None = None, trace_dir: str = "",
                 attempt: int = 0) -> Path:
    """`apply_record.md` in the job folder. A value from a password field
    (`_is_password`) or a row marked `hidden` (the emailed code) is written
    as `<hidden>`.

    `trace_dir` is this attempt's trace folder relative to `folder`
    (`apply_trace/attempt-<n>`): the record links it and each page's trace
    file, and a copy of the record goes into it. The earlier attempts'
    records stay in their own folders and are listed at the end; a record
    being replaced that no attempt folder holds is kept first
    (`_keep_untraced_record`), so no attempt's record is lost."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _keep_untraced_record(folder)
    lines = [f"# Apply record: {entry.get('title', '')} at {entry.get('company', '')}", "",
             f"- Job id: {entry.get('job_posting_id', '')}",
             f"- Company: {entry.get('company', '')}",
             f"- Title: {entry.get('title', '')}",
             f"- Status: {outcome_status}",
             f"- Reason: {reason}",
             f"- Written: {datetime.now().isoformat(timespec='seconds')}",
             f"- Apply URL: {entry.get('apply_url', '')}"]
    if attempt:
        lines.append(f"- Attempt: {int(attempt)}")
    if trace_dir:
        lines.append(f"{_TRACE_LINE}[{trace_dir}]({trace_dir}/) (per page: page-<n>.json and "
                     f"page-<n>.jpg; end.jpg, run.json, {apply_trace.LOG_NAME})")
    lines.append("")
    for i, p in enumerate(pages, 1):
        lines.append(f"## Page {i}: {p.get('url', '')}")
        lines.append(f"- State: {p.get('state', '')} ({float(p.get('confidence', 0.0)):.2f})")
        if trace_dir:
            lines.append(f"{_TRACE_LINE}[page-{i}.json]({trace_dir}/page-{i}.json), "
                         f"[page-{i}.jpg]({trace_dir}/page-{i}.jpg)")
        filled = [r for r in p.get("filled", []) if not r.get("upload")]
        uploads = [r for r in p.get("filled", []) if r.get("upload")]
        if filled:
            lines.append("- Filled:")
            for r in filled:
                value = HIDDEN if r.get("hidden") or _is_password(r) else str(r.get("value", ""))
                mark = " (generated)" if r.get("generated") else ""
                lines.append(f"  - {r.get('label', '')}: {value}{mark}")
        if uploads:
            lines.append("- Uploads:")
            for r in uploads:
                lines.append(f"  - {r.get('label', '')}: {r.get('value', '')}")
        if p.get("fill_outcomes"):
            # FILL-15: how each field was acted on, and the error's type when
            # the act failed (never a value)
            lines.append("- Fill outcomes:")
            for o in p["fill_outcomes"]:
                what = (f"failed ({o.get('error')})" if o.get("error")
                        else str(o.get("how") or o.get("action") or ""))
                lines.append(f"  - {o.get('label', '')}: {what}")
        if p.get("verification"):
            lines.append("- Verification:")
            for v in p["verification"]:
                mark = "ok" if v.get("ok") else "FAILED"
                lines.append(f"  - {v.get('label', '')}: {mark} (p_correct "
                             f"{float(v.get('p_correct', 0.0)):.2f}, p_placeholder "
                             f"{float(v.get('p_placeholder', 0.0)):.2f})")
        if p.get("generated"):
            lines.append("- Generated answers:")
            for g in p["generated"]:
                state = "generated" if g.get("ok") else "rejected"
                lines.append(f"  - {g.get('label', '')}: {state} ({g.get('note', '')})")
        if p.get("clicked"):
            lines.append("- Clicked: " + "; ".join(str(c) for c in p["clicked"]))
        if p.get("flags"):
            lines.append("- Flags: " + ", ".join(f"{k} {float(v):.2f}"
                                                   for k, v in p["flags"].items()))
        lines.append("")
    lines.append("## Missing questions")
    rows = missing if missing is not None else entry.get("missing_answers") or []
    if rows:
        for m in rows:
            ctx = str(m.get("context", "") or "")
            lines.append(f"- {m.get('question', '')}" + (f" ({ctx})" if ctx else ""))
    else:
        lines.append("- none")
    lines += ["", "## Jev",
              f"- Requests: {int(jev_usage.get('requests', 0))}",
              f"- Input tokens: {int(jev_usage.get('input_tokens', 0))}",
              f"- Cost: ${float(jev_usage.get('usd', 0.0)):.4f}",
              f"- Model: {jev.MODEL}",
              f"- Generated answers used: {generated_count(pages)}", ""]
    if page_text:
        lines += ["## Final page text", "", "```", page_text.strip(), "```", ""]
    earlier = _earlier_attempts(folder, trace_dir)
    if earlier:
        lines += ["## Earlier attempts", *earlier, ""]
    text = "\n".join(lines)
    path = folder / RECORD_NAME
    path.write_text(text, encoding="utf-8")
    if trace_dir:
        # the copy sits in the attempt folder: its links point from there
        copy = _TRACE_LINK.sub(lambda m: f"]({_link_from(m.group(1), trace_dir)})", text)
        try:
            (folder / trace_dir / RECORD_NAME).write_text(copy, encoding="utf-8")
        except OSError as e:
            log.warning("the record copy in %s was not written: %s", trace_dir,
                        type(e).__name__)
    return path


# --- the submit gate ----------------------------------------------------------------------

def _invalid_words(row: Mapping[str, Any]) -> str:
    label = " ".join(str(row.get("label") or "a field").split())[:80]
    message = " ".join(str(row.get("message") or "").split())[:120]
    if row.get("reason") == "valueMissing":
        return (f"required field without an answer: {label} (the form reports it empty"
                + (f": {message})" if message else ")"))
    return f"the form reports an invalid field: {label} ({message or row.get('reason')})"


def can_submit(plan: FillPlan, verification: list[VerifyResult],
               settings: dict, live: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """(True, "") when the application may be sent, else (False, the first
    failing reason): the setting, the plan's park reason, a required field
    without an answer (any action other than fill / select / upload), a
    required field unverified, the submit button's confidence. The
    prohibited and captcha flags are recorded only (`apply_judge`'s rule).
    A password box still marked `PASSWORD_ACTION` holds the master password:
    `_JobRun._fill_passwords` typed it and checked its length in the page,
    and a required box it could not fill parked the job before the gate. The
    judge never sees it, so it has no verification row.

    `live` is the page as the gate read it just before (`_JobRun._gate_read`,
    INV-01 and INV-02): `no_application` (nothing was filled on this page or
    an earlier one: the page holds no application), `apply_button` (an
    Apply-worded button without the DOM evidence and the judge's word that
    it sends the finished application), `invalid` (the submit's form holds a
    control that would not validate, or one marked `aria-invalid`) and
    `required_empty` (a required control the extractor leaves out is
    empty); each fails the gate with its evidence."""
    if not settings.get("auto_apply_submit", True):
        return False, "auto_apply_submit is off"
    if plan.park_reason:
        return False, plan.park_reason
    by_n = {v.n: v for v in verification}
    for pf in plan.fields:
        if not pf.required or pf.action == apply_judge.PASSWORD_ACTION:
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
    if live.get("no_application"):
        return False, f"no application on the page ({live['no_application']})"
    if live.get("apply_button"):
        return False, str(live["apply_button"])
    for row in live.get("invalid") or []:
        return False, _invalid_words(row)
    for row in live.get("required_empty") or []:
        return False, _control_words(row)
    return True, ""


def _control_words(row: Mapping[str, Any]) -> str:
    """A required control the extractor leaves out, still empty, as a park's
    reason: one the run cannot read into (EXT-01) says why."""
    label = " ".join(str(row.get("label") or "a control").split())[:80]
    if row.get("kind") == "unreadable":
        return (f"required field without an answer: {label} (a control the run cannot read: "
                f"{row.get('why') or 'its inside is closed'})")
    return (f"required field without an answer: {label} (a {row.get('kind')} control the run "
            f"does not fill)")


def hold_until_closed(ctx, *, sleep: Callable[[float], None] = time.sleep,
                      log: logging.Logger | None = None) -> None:
    """Keep the process alive until the user closes the window: every page
    closed, or the context's close event (the idea `apply_playwright._hold`
    uses for its parked tab)."""
    logger = log if log is not None else logging.getLogger("apply_run")
    closed: list[bool] = []
    try:
        ctx.on("close", lambda *_: closed.append(True))
    except Exception:       # noqa: BLE001  (a context without events)
        pass
    logger.info("holding the window open; close it to end the run")
    while not closed:
        try:
            if len(ctx.pages) == 0:
                break
        except Exception:       # noqa: BLE001  (the context is gone)
            break
        if _wait_for_close(ctx, HOLD_POLL_S, sleep):
            break


def _wait_for_close(ctx, seconds: float, sleep: Callable[[float], None]) -> bool:
    """Wait up to `seconds` inside a Playwright call; True when the context
    closed. The sync API dispatches the browser's events (the user closing a
    page, the context closing) only while one of its own calls runs, and
    `time.sleep` blocks that: a hold that sleeps never sees `ctx.pages`
    shrink. A context without `wait_for_event` (a test double) sleeps."""
    wait = getattr(ctx, "wait_for_event", None)
    if wait is None:
        sleep(seconds)
        return False
    try:
        wait("close", timeout=seconds * 1000)
        return True
    except Exception as e:      # noqa: BLE001  (Playwright's TimeoutError, or the context is gone)
        return type(e).__name__ != "TimeoutError"


# --- the runner -----------------------------------------------------------------------------

class Runner:
    """Drains the queue through one browser context.

    `jev` is any `jev.Jev`; `settings` carries the `auto_apply_*` keys
    (`DEFAULT_SETTINGS` fills gaps); `clock` and `sleep` are injectable for
    the wall clock and the hold; `accounts` and `inbox` can override the
    built-in adapters, while `answergen` defaults to `NotConfigured`;
    `context` is an already open Playwright browser context (tests), else the
    persistent profile at
    `profile_dir` is launched per drain and, when a page is parked and the
    window is visible, held open until the user closes it; `run_context` is
    `apply_queue.build_context()`'s dict (the inbox URL), computed on demand.
    """

    def __init__(self, *, jev: Any, queue_path: Path | None = None,
                 profile_dir: Path | None = None, settings: dict | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 accounts: Any = None, inbox: Any = None, answergen: Any = None,
                 log: logging.Logger | None = None, context: Any = None,
                 run_context: dict | None = None):
        self.jev = jev
        self.queue_path = Path(queue_path) if queue_path else None
        self.profile_dir = Path(profile_dir) if profile_dir else default_profile_dir()
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        self.clock = clock
        self.sleep = sleep
        default = NotConfigured()
        self.accounts = accounts
        self.inbox = inbox
        self.answergen = answergen if answergen is not None else default
        self.log = log if log is not None else logging.getLogger("apply_run")
        self._injected = context
        self._ctx = context
        self._run_context = run_context
        self.parked_pages: list = []

    # -- browser lifecycle --------------------------------------------------------------

    def _launch(self):
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            ctx = launch_profile(pw, self.profile_dir,
                                 headless=bool(self.settings["auto_apply_headless"]), log=self.log)
        except Exception:
            pw.stop()
            raise
        return pw, ctx

    def _hold(self, ctx) -> None:
        hold_until_closed(ctx, sleep=self.sleep, log=self.log)

    def _with_browser(self, work: Callable[[Any], Any]) -> Any:
        if self._injected is not None:
            return work(self._injected)
        pw, ctx = self._launch()
        self._ctx = ctx
        try:
            result = work(ctx)
            if self.parked_pages and not self.settings["auto_apply_headless"]:
                self._hold(ctx)
            return result
        finally:
            self._ctx = None
            try:
                ctx.close()
            except Exception:       # noqa: BLE001  (already closed by the user)
                pass
            pw.stop()

    def run_context(self) -> dict:
        if self._run_context is None:
            self._run_context = apply_queue.build_context(self.queue_path)
        return self._run_context

    # -- the drain ----------------------------------------------------------------------

    def drain(self, cap: int | None = None) -> list[Outcome]:
        """Claim FIFO until the queue is empty or `cap` jobs ran; one
        `Outcome` per job. A closed window or a crashed browser stops the
        drain: the job it ended is `needs_human` (`CLOSED_REASON`) and
        nothing more is claimed, so the rest stay queued with their attempt
        counts untouched."""
        limit = int(cap if cap is not None else self.settings["auto_apply_batch_cap"])

        def _work(ctx) -> list[Outcome]:
            outcomes: list[Outcome] = []
            closed: list[bool] = []
            try:
                ctx.on("close", lambda *_: closed.append(True))
            except Exception:       # noqa: BLE001  (a context without events)
                pass
            while len(outcomes) < limit:
                if closed or _context_gone(ctx):
                    self.log.warning("the browser window is closed; the drain stops and the "
                                     "queued jobs stay queued")
                    break
                entry = apply_queue.claim("apply_run", path=self.queue_path)
                if entry is None:
                    break
                outcome = self._run_job(ctx, entry)
                outcomes.append(outcome)
                if outcome.browser_closed:
                    self.log.warning("the browser window closed during job %s; the drain "
                                     "stops and the queued jobs stay queued", outcome.job_id)
                    break
            self.log.info(summary_line(outcomes))
            return outcomes
        return self._with_browser(_work)

    def run_job(self, entry: dict) -> Outcome:
        """One claimed entry through the state machine (opens the browser
        when no drain is running)."""
        if self._ctx is not None:
            return self._run_job(self._ctx, entry)
        return self._with_browser(lambda ctx: self._run_job(ctx, entry))

    def _run_job(self, ctx, entry: dict) -> Outcome:
        job = _JobRun(self, ctx, entry)
        return job.run()


# --- one job ---------------------------------------------------------------------------------

class _JobRun:
    """The state machine for one queue entry. Every terminal path goes
    through `_finish`, which writes the record and finishes the entry."""

    def __init__(self, runner: Runner, ctx, entry: dict):
        self.r = runner
        self.ctx = ctx
        self.entry = entry
        self.job_id = str(entry.get("job_posting_id", ""))
        self.log = runner.log
        self.page = None
        self.pages: list[dict] = []
        self.missing: list[dict] = []
        self.submit_clicked = False
        self.form_filled = False      # the application's answers went on a page (`_fills_the_application`)
        self.form_password_sites: set[str] = set()  # sites whose form took the password
        self.form_had_password = False  # a form page carried a password box, typed or not
        self.handed_off = False         # the page at the gate came from the account step
        self.gen_budget = GENERATE_MAX
        self.catalog: apply_facts.FactCatalog | None = None
        self.allowed: set[str] = set()
        self.ats_host = ""
        self.ats_hosts: set[str] = set()       # every admitted ATS host; matched by site
        self.ats_transition_used = False
        self._aggregator_host = ""        # the job board the tab is on (NAV-08)
        self._aggregator_left = False     # its company-site link was followed
        self._boards: list[str] = []      # the boards read in this job, in order
        self.last_sig: tuple | None = None
        self.usage_before = jev.usage()
        self.start = runner.clock()
        self.deadline = self.start + JOB_WALL_CLOCK_S
        self.folder = self._folder()
        self.accounts = runner.accounts if runner.accounts is not None else _Accounts(self)
        self.inbox = runner.inbox if runner.inbox is not None else _Inbox(self)
        # the trace (`apply_trace`); off until `run` starts it, so a test that
        # drives one step of a `_JobRun` needs no folder
        self.trace = apply_trace.Trace.off()
        self.browser_closed = False
        self._last_answers: Mapping[str, Any] = {}   # the page read the loop acts on
        self._facts = apply_judge.PageFacts()        # the last read page's structure
        self._loading_waited: set[str] = set()       # pages whose placeholder was waited on
        # the second look's answers per page (its URL path and fields) and kind:
        # a re-read of the same page reuses them, never asks again (review M11)
        self._reask_cache: dict[tuple, dict[str, Any]] = {}
        self._last_dropped: dict[int, str] = {}      # frames `_drop_foreign_controls` left out
        self._last_click: tuple[str, str] | None = None     # (text, role) of the last click
        # (page, frame, locator) of every box the master password or an
        # emailed code went into: every later screenshot masks them
        self._secret_boxes: list[tuple[Any, Any, Any]] = []
        self._secret_locators: set[tuple[int, str]] = set()     # the same boxes' digest locators
        # decisions taken before the page they belong to is recorded (a
        # settle, a consent banner, a re-read): `_new_page_record` writes them
        self._pending: list[dict[str, Any]] = []
        self._consent_clicks = 0
        self._linkedin_clicks: dict[str, int] = {}    # a LinkedIn job id -> the handler's clicks
        self._late_watch: LateWatch | None = None     # tabs the last entry click opens late
        # the locators this page's fill put a value in (the gate's evidence
        # that an application is on the page, INV-01)
        self._filled_here: list[tuple[int, str]] = []
        self._filled_any = False        # a value went on a page of this job (the account step too)
        # this page's fill as read back (n -> `apply_fill.Filled`), and the text
        # boxes it left alone with their values before it: the re-read after
        # the fill compares against both (FILL-03)
        self._last_filled: dict[int, apply_fill.Filled] = {}
        self._idle: list[tuple[Any, str | None]] = []
        self._refilled: set[int] = set()    # fields put back once after the page changed them
        self._drafts_by_question: dict[str, str] = {}   # FILL-12: accepted drafts, this job
        self._options_seen: dict[tuple, list[str]] = {}  # FILL-09: a page's listbox options
        self._repaired = False              # the last `_repair` acted on the page
        self._submit_repairs = 0            # repairs after the form refused the submit (ADV-02)
        self._gate_repairs = 0              # repairs of what the gate read invalid, this page
        # (the form's fields, a message no control names) -> the fields the
        # judge named for it on that form: never offered for it again (M1)
        self._error_tried: dict[tuple, set[str]] = {}
        # a message's words -> (the field only the judge named for it, which
        # had no answer; the form's words): the park when the rounds end with
        # the message still shown (SP6 review R2-I4); this page's
        self._spared: dict[str, tuple[str, str]] = {}
        self._code_sent = False         # the code step clicked on (a code can finish a send)
        self._send_watch: SendWatch | None = None     # the requests after the submit click
        self._sent_when = "after the submit click"      # or "during the CAPTCHA wait" (m5)
        self._before_submit: dict[str, Any] | None = None   # the page just before it
        self._submit_at: tuple[int, str] | None = None      # the submit button's locator

    # -- the trace --------------------------------------------------------------------------

    def _trace(self, kind: str, **data: Any) -> None:
        """One step on the current page's trace (see `apply_trace`)."""
        self.trace.event(kind, **data)

    def _decide(self, what: str, why: str, **evidence: Any) -> None:
        """A decision the loop took and why: a shortcut, a remap, a guess
        acted on, a route to the submit gate."""
        self.trace.event("decision", what=what, why=why, **evidence)

    def _decide_next(self, what: str, why: str, **evidence: Any) -> None:
        """A decision about the page about to be read (a settle, a consent
        banner, a re-read): it joins that page's trace once the page is
        recorded (`_new_page_record`), or the run's at the end."""
        self._pending.append({"what": what, "why": why, **evidence})

    def _flush_decisions(self) -> None:
        pending, self._pending = self._pending, []
        for row in pending:
            self.trace.event("decision", **row)

    def _watch(self, page) -> None:
        """Keep `page`'s main-frame navigations (redirects included) in the
        trace's URL chain."""
        try:
            main = page.main_frame
            page.on("framenavigated",
                    lambda frame: frame == main and frame.url != "about:blank"
                    and self.trace.nav(frame.url))
        except Exception:       # noqa: BLE001  (a page double)
            pass
        # the page's own sends from its first request: a click's evidence
        # leaves them out (SP6 review R2-I2)
        apply_fill.watch_requests(page)

    def _reads(self, answers: Mapping[str, Any] | None = None) -> str:
        """The judge's most probable reads of the page state (its own pick,
        before the read combined it with the Nouls and the structure), as a
        park reason's evidence."""
        answers = self._last_answers if answers is None else answers
        judged = answers.get("page_state_judged") if answers else None
        return apply_trace.page_state_reads({"page_state": judged} if judged is not None
                                            else answers)

    def _buttons_seen(self, digest: apply_form.FormDigest) -> str:
        """Each button of the page with the role and confidence the judge gave
        it, as a park reason's evidence."""
        rows = []
        for b in digest.buttons:
            role, conf = apply_judge._choice_of(self._last_answers, f"button_{b.n}_role")
            text = " ".join(b.text.split())[:40]
            rows.append(f"{text} {role} {conf:.2f}" if role else f"{text} (no role)")
        return _cap("; ".join(rows)) if rows else "none"

    def _last_states(self) -> str:
        """The states of the job's last pages, as a budget park's evidence."""
        states = [str(p.get("state", "")) for p in self.pages[-3:]]
        return f"; last: {', '.join(states)}" if states else ""

    def _account_evidence(self, state: str, digest: apply_form.FormDigest) -> str:
        """An account park's evidence: the read, whether a master password is
        stored, the boxes and the buttons with their roles."""
        _, conf = apply_judge.read_page_state(self._last_answers)
        boxes = ", ".join(" ".join((f.label or f.type).split())[:30]
                          for f in digest.fields) or "none"
        stored = "yes" if ats_accounts.has_password() else "no"
        return _cap(f"read as {state} {conf:.2f}; master password stored: {stored}; "
                    f"boxes: {boxes}; buttons: {self._buttons_seen(digest)}")

    def _window_closed(self) -> bool:
        return _context_gone(self.ctx)

    def _keep_secret_box(self, locator: tuple[int, str]) -> None:
        """Note a box the run typed the master password or an emailed code
        into (a digest locator on the current page), for the screenshots'
        masks: the label-found code box and a box inside a frame are masked
        whatever their names say."""
        self._secret_locators.add((int(locator[0]), str(locator[1])))
        try:
            frame = apply_form.frames(self.page)[int(locator[0])]
            self._secret_boxes.append((self.page, frame, frame.locator(str(locator[1]))))
        except Exception:       # noqa: BLE001  (a page double, a frame that went away)
            pass

    def _secret_masks(self, page) -> list:
        """The noted secret boxes on `page` whose frames are still there."""
        out = []
        for owner, frame, loc in self._secret_boxes:
            try:
                if owner is page and not frame.is_detached():
                    out.append(loc)
            except Exception:   # noqa: BLE001
                continue
        return out

    # -- setup ------------------------------------------------------------------------------

    def _folder(self) -> Path | None:
        arts = self.entry.get("artifacts") or {}
        apply_md = str(arts.get("apply_md") or "")
        if apply_md:
            return Path(apply_md).parent
        folder = str(arts.get("folder") or "")
        return Path(folder) if folder else None

    def _build_allowlist(self) -> None:
        self.allowed = set(LINKEDIN_HOSTS)
        ats_host = _host(str((self.entry.get("ats") or {}).get("domain") or ""))
        if ats_host:
            self.allowed.add(ats_host)
        if ats_host and not apply_linkedin.is_linkedin(ats_host):
            self.ats_host = ats_host
            self.ats_hosts.add(ats_host)
            self.ats_transition_used = True
        inbox_host = _host(str(self.r.run_context().get("inbox_url") or ""))
        if inbox_host:
            self.allowed.add(inbox_host)

    def _allowed_site(self, host: str) -> bool:
        """LinkedIn (any `*.linkedin.com` host, the country subdomains too:
        NAV-10) and the inbox by its exact host (its domain carries other
        people's content: `docs.google.com`, `forms.office.com`, a Google
        sign-in frame); the admitted ATS by its whole site (`login.icims.com`
        next to `careers-gtsx.icims.com`); a known ATS platform
        (`ATS_SITES`) anywhere. The master password never goes to LinkedIn
        (`_password_ok`)."""
        host = _host(host)
        if host in self.allowed or apply_linkedin.is_linkedin(host):
            return True
        site = _site(host)
        return site in ATS_SITES or any(site == _site(h) for h in self.ats_hosts)

    def _check_host(self, url: str) -> None:
        host = _host(url)
        if host and not self._allowed_site(host):
            raise _Parked("needs_human", f"left the allowed sites: {host}")

    def _password_ok(self, host: str) -> bool:
        """May the master password be typed on `host`? Only on the application
        itself: the admitted ATS site or a known ATS platform, never on
        LinkedIn's or the inbox provider's domain."""
        host = _host(host)
        site = _site(host)
        if not site or site == _site(LINKEDIN_HOSTS[0]):
            return False
        if site == _site(str(self.r.run_context().get("inbox_url") or "")):
            return False
        if site in TRACKER_SITES or site in AGGREGATOR_SITES:
            return False            # ALLOW-02: a job board or a tracker is never the application
        return site in ATS_SITES or any(site == _site(h) for h in self.ats_hosts)

    def _extract(self, page=None) -> apply_form.FormDigest:
        """`apply_form.extract` of `page` (the job's page by default), a child
        frame read first only when it is the page's site, an ATS platform or
        the admitted application (`content_frame_site`)."""
        page = page if page is not None else self.page
        apply_fill.watch_requests(page)
        return apply_form.extract(page, content_site=lambda url: content_frame_site(
            url, str(page.url), self.ats_hosts))

    def _frame_url(self, frames: list, idx: int) -> str:
        """The URL a frame's controls answer to; a blank or srcdoc frame takes
        its parent's."""
        frame = frames[idx]
        url = str(getattr(frame, "url", "") or "")
        seen: set[int] = set()
        while url in ("about:blank", "about:srcdoc") and id(frame) not in seen:
            seen.add(id(frame))
            frame = getattr(frame, "parent_frame", None)
            if frame is None:
                return str(self.page.url)
            url = str(getattr(frame, "url", "") or "")
        return url

    def _drop_foreign_controls(self, digest: apply_form.FormDigest) -> apply_form.FormDigest:
        """The digest without the controls of a frame from another site or a
        bot-check provider (a CAPTCHA widget, a chat or cookie widget), or a
        LinkedIn frame on a page off LinkedIn (an "Apply with LinkedIn"
        widget, study G4): they are never judged, filled or clicked, and the
        page goes on without them. The page text keeps every frame's words, and
        a dialog the page read stays its dialog."""
        frames = list(self.page.frames)
        on_linkedin = self._on_linkedin()
        dropped: dict[int, str] = {}
        for idx in sorted({int(item.locator[0]) for item in (*digest.fields, *digest.buttons)}):
            if not 0 <= idx < len(frames):
                dropped[idx] = "gone"
                continue
            url = self._frame_url(frames, idx)
            # a blank document (about:blank written by the page's own
            # script) belongs to no other site
            host = "" if url.startswith("about:") else _host(url)
            if _is_captcha_url(url) or (host and not self._allowed_site(host)) \
                    or (idx > 0 and not on_linkedin and apply_linkedin.is_linkedin(host)):
                dropped[idx] = host
        self._last_dropped = dropped
        if not dropped:
            return digest
        self.log.info("job %s: ignoring the controls of frame(s) %s", self.job_id,
                      ", ".join(f"{i} ({h})" for i, h in sorted(dropped.items())))
        return dataclasses.replace(       # the rest as read: its dialog too (R2-M2)
            digest, fields=[f for f in digest.fields if int(f.locator[0]) not in dropped],
            buttons=[b for b in digest.buttons if int(b.locator[0]) not in dropped])

    def _discover_listbox_options(self, digest: apply_form.FormDigest) -> None:
        """Read choices rendered only after a listbox is opened, before
        planning. A page read again (a step the form sent back) takes the
        options its listboxes showed before, by the page's path, the
        control's locator and its label, and opens none of them again
        (FILL-09)."""
        path = urlsplit(str(getattr(self.page, "url", "") or "")).path
        for control in digest.fields:
            if control.type != "listbox" or control.options \
                    or getattr(control, "widget", "") == "typeahead":
                continue
            key = (path, tuple(control.locator), " ".join(control.label.split()))
            if self._options_seen.get(key):
                control.options = list(self._options_seen[key])
                continue
            try:
                control.options = apply_fill.open_listbox_options(self.page, control)
                if control.options:
                    self._options_seen[key] = list(control.options)
            except apply_fill.PopupRefused as e:
                # its own words send: never opened, left unanswered (round 8)
                control.refused = str(e)
                self._decide("popup_refused", f"a popup was left unopened: {e}",
                             field=control.n)
            except Exception as e:      # noqa: BLE001  (a widget may detach while opening)
                self.log.info("job %s: listbox %r did not expose options: %s",
                              self.job_id, control.label, type(e).__name__)

    def _admit_ats_transition(self, url: str, source_url: str) -> None:
        """Record where the application lives. A known ATS platform is
        admitted wherever the flow met it; any other site only as the one
        destination LinkedIn's Apply led to."""
        host = _host(url)
        if not host or host in LINKEDIN_HOSTS or _site(host) == _site(LINKEDIN_HOSTS[0]):
            return
        if _tracker(host):
            # a hop that never moved on (NAV-07): never the destination
            raise _Parked("needs_human", f"the tracker hop ({host}) did not move on to the "
                                         f"company's site")
        if any(_site(host) == _site(h) for h in self.ats_hosts):
            return
        if _site(host) not in ATS_SITES:
            from_board = (self._aggregator_host and not self._aggregator_left
                          and _host(source_url) == self._aggregator_host)
            if (from_board and _aggregator(host) and _site(host) != _site(self._aggregator_host)
                    and any(_site(host) == _site(b) for b in self._boards)):
                # a board's company link back to a board already read (review
                # R2-M3): the boards link to each other and to no company
                # site, and reading them again would only loop
                chain = " -> ".join([*self._boards, host])
                raise _Parked("needs_human", f"{AGGREGATOR_REASON} on {host}: the job boards "
                                             f"link to each other ({chain}) and to no company "
                                             f"site", AGGREGATOR_NOTE)
            if host in self.allowed:
                return
            from_linkedin = not self.ats_transition_used and apply_linkedin.is_linkedin(source_url)
            if _aggregator(host) and from_linkedin:
                # a job board LinkedIn's Apply led to (NAV-08): the tab may
                # stay there and follow its company-site link once; nothing
                # is ever filled or signed in on it (`_password_ok`)
                self.allowed.add(host)
                self._aggregator_host = host
                self._boards = [host]
                self.ats_transition_used = True
                self._decide_next("aggregator", f"LinkedIn's Apply led to a job board ({host})")
                return
            if _aggregator(host) and from_board:
                # a board's company link that lands on another board (review
                # I4): that board is read the same way, for its own company
                # link (chains such as one board handing to another are
                # common), up to `AGGREGATOR_BOARDS_MAX`; a board is never
                # the application's site (`ats_hosts`)
                chain = " -> ".join([*self._boards, host])
                if len(self._boards) >= AGGREGATOR_BOARDS_MAX:
                    raise _Parked("needs_human", f"{AGGREGATOR_REASON} on {host}: a chain of job "
                                                 f"boards ({chain}) and no company site",
                                  AGGREGATOR_NOTE)
                self.allowed.add(host)
                self._aggregator_host = host
                self._aggregator_left = False
                self._boards.append(host)
                self._decide_next("aggregator", f"a job board's company link led to another "
                                                f"board ({chain}); it is read the same way")
                return
            if not (from_linkedin or from_board):
                self._check_host(url)
            if from_board:
                self._aggregator_left = True
        inferred = apply_queue.infer_ats(url)
        apply_queue.update(self.job_id, path=self.r.queue_path,
                           ats={"domain": host, "system": inferred["system"]})
        ats = self.entry.get("ats") or {}
        self.entry["ats"] = {**ats, "domain": host, "system": inferred["system"]}
        self.allowed.add(host)
        self.ats_hosts.add(host)
        self.ats_host = host
        self.ats_transition_used = True

    # -- the run ----------------------------------------------------------------------------

    def _prepare(self) -> str:
        """The catalog, the PDFs from the entry's artifacts, the allowlist;
        returns the apply URL. Raises `_Parked("failed", ...)` when the sheet
        or the URL is missing."""
        if self.folder is None or not (self.folder / "apply.md").exists():
            raise _Parked("failed", "no apply.md")
        self.catalog = apply_facts.build(self.folder)
        self._apply_artifacts()
        self._build_allowlist()
        url = str(self.entry.get("apply_url") or "")
        if not url:
            raise _Parked("failed", "no apply_url")
        return url

    def _apply_artifacts(self) -> None:
        """The entry's `resume_pdf` / `cover_letter_pdf` paths win over the
        folder scan; a path that names a missing file blanks the fact and
        records the gap through `add_missing`."""
        arts = self.entry.get("artifacts") or {}
        for art_key, fact_key, label in (("resume_pdf", "resume_file", "Resume PDF"),
                                         ("cover_letter_pdf", "cover_letter_file",
                                          "Cover letter PDF")):
            path = str(arts.get(art_key) or "")
            if not path:
                continue
            fact = self.catalog.facts.get(fact_key)
            if fact is None:
                continue
            value = path if Path(path).is_file() else ""
            self.catalog.facts[fact_key] = apply_facts.Fact(
                key=fact_key, value=value, description=fact.description, kind=fact.kind)
            if not value:
                self._add_missing(label, f"the file is missing: {path}")

    def _start_trace(self) -> None:
        loggers = [self.log] if isinstance(self.log, logging.Logger) else []
        self.trace = apply_trace.Trace(self.folder, attempt=int(self.entry.get("attempts") or 0),
                                       job_id=self.job_id, loggers=loggers)
        self.trace.start()

    def run(self) -> Outcome:
        try:
            try:
                self._start_trace()
                self.log.info("job %s: start (%s)", self.job_id, self.entry.get("apply_url", ""))
                if _easy_apply(self.entry):
                    # the user applies to an Easy Apply job on LinkedIn in
                    # person: no page is opened, nothing is read or clicked
                    self._decide("easy_apply", "the queue entry is an Easy Apply job")
                    raise _Parked("needs_human", EASY_APPLY_REASON, EASY_APPLY_NOTE)
                url = self._prepare()
                self._trace("start", url=url, submit=bool(self.r.settings.get("auto_apply_submit",
                                                                              True)))
                self._check_host(url)
                self.page = self.ctx.new_page()
                self._watch(self.page)
                self._open(url)
                self._loop()
                raise _Parked("needs_human",
                              f"page budget exhausted ({apply_judge.MAX_PAGES} pages"
                              f"{self._last_states()})")
            except _Parked as p:
                self._trace("park", status=p.status, reason=p.reason)
                if p.status != "submitted":
                    # whatever the loop made of it, the window or the tab went
                    # away under it
                    if self._window_closed():
                        return self._closed(f"the run had reached: {p.reason}")
                    if self._tab_closed():
                        return self._tab_gone(f"the run had reached: {p.reason}")
                return self._finish(p.status, p.reason, p.tab_note)
            except Exception as e:      # noqa: BLE001  (the entry must leave in_progress)
                # the context or the browser gone is a closed window; a closed
                # page with the context alive (the user closed the job's tab,
                # the site closed its own popup) ends this job only
                closed = self._window_closed()
                tab = not closed and (_closed_error(e) or self._tab_closed())
                self._trace("exception", error=type(e).__name__, closed=closed, tab_closed=tab)
                if closed or tab:
                    self.log.warning("job %s: the browser %s closed (%s)", self.job_id,
                                     "window" if closed else "tab", type(e).__name__)
                else:
                    self.log.exception("job %s: unexpected error", self.job_id)
                if self.submit_clicked:
                    self.browser_closed = closed
                    why = (CLOSED_REASON if closed else TAB_CLOSED_REASON if tab
                           else f"{type(e).__name__}: {e}")
                    watch = self._send_watch
                    if watch is not None and watch.sent:
                        return self._finish("submitted", f"submitted (unconfirmed): {why} "
                                                         f"(after {_cap(watch.first(), 120)})")
                    # no request to the application's sites was seen: the
                    # run claims no send, and the job is never re-queued on
                    # its own
                    left = (f"; a request left: {_cap(watch.first(), 120)}"
                            if watch is not None and watch.any() else "")
                    return self._finish("needs_human", f"{CHECK_SENT_REASON}: the run stopped "
                                                       f"after the submit click "
                                                       f"({_cap(why, 160)}){left}",
                                        CHECK_SENT_NOTE)
                if closed:
                    return self._closed(type(e).__name__)
                if tab:
                    return self._tab_gone(type(e).__name__)
                return self._finish("failed", f"{type(e).__name__}: {e}")
        finally:
            self.trace.close()

    def _tab_closed(self) -> bool:
        try:
            return self.page is not None and bool(self.page.is_closed())
        except Exception:       # noqa: BLE001  (a page double)
            return False

    def _closed(self, evidence: str) -> Outcome:
        """The window closed or the browser went away: the job waits for the
        user and the drain stops (`Outcome.browser_closed`)."""
        self.browser_closed = True
        self._trace("closed", evidence=evidence)
        return self._finish("needs_human", CLOSED_REASON)

    def _tab_gone(self, evidence: str) -> Outcome:
        """The job's tab closed while the window stayed: this job waits for
        the user and the drain goes on."""
        self._trace("tab_closed", evidence=evidence)
        return self._finish("needs_human", f"{TAB_CLOSED_REASON} ({_cap(evidence, 120)})")

    def _loop(self) -> None:
        """One page per turn: the consent banner out of the way, a read that
        waits for the page to render (`_read_digest`), LinkedIn's pages by
        the handler (`_linkedin_step`, no judge), every other page judged
        (an unsure read taken once more after a settle, `_reread`) and
        handled by its state."""
        for page_no in range(apply_judge.MAX_PAGES):
            if self.r.clock() >= self.deadline:
                raise _Parked("needs_human", f"time budget exhausted "
                                             f"({JOB_WALL_CLOCK_S // 60} min; {len(self.pages)} "
                                             f"page(s){self._last_states()})")
            self._take_late_popup()
            self._check_host(self.page.url)
            self._filled_here = []
            self._last_filled, self._idle, self._refilled = {}, [], set()
            self._gate_repairs = 0
            self._spared = {}
            if self._human_check_showing():
                self._wait_for_human_check("a CAPTCHA challenge is showing")
            self._dismiss_consent()
            t0 = time.monotonic()
            digest = self._read_digest()
            t1 = time.monotonic()
            if self._linkedin_step(digest):
                continue
            if self._aggregator_step(digest):
                continue
            marker = self._page_marker()        # the page as judged: a bot check that
                                                # clears itself shows as a change
            answers = self._read(digest)
            state, conf = apply_judge.read_page_state(answers)
            if conf < apply_judge.PAGE_STATE_MIN_CONF:
                digest, answers, state, conf = self._reread(digest, answers, state, conf)
                marker = self._page_marker()
            facts = self._facts
            sig = page_signature(str(self.page.url), digest)
            rec = self._new_page_record(state, conf, digest=digest, answers=answers,
                                        timings={"extract_s": round(t1 - t0, 3),
                                                 "judge_s": round(time.monotonic() - t1, 3)})
            self.log.info("job %s page %d: %s (%.2f) at %s", self.job_id, page_no + 1,
                          state, conf, self.page.url)
            if sig == self.last_sig:
                after = (f" after {self._last_click[0]} ({self._last_click[1]})"
                         if self._last_click else "")
                raise _Parked("needs_human", f"page did not advance (read as {state} "
                                             f"{conf:.2f} again{after})")
            self.last_sig = sig
            unsure = conf < apply_judge.PAGE_STATE_MIN_CONF
            applied = apply_judge.already_applied(answers, facts, state)
            if applied and not self.submit_clicked and not self._code_sent:
                # TERM-04's ATS part: a job the site says was applied to
                # before is never applied to again
                self._decide("already_applied", applied)
                raise _Parked("needs_human", f"{ALREADY_APPLIED_REASON} ({_cap(applied, 160)}; "
                                             f"{_cap(digest.url_host or _host(self.page.url), 60)})",
                              ALREADY_APPLIED_NOTE)
            if remaps_to_form(state, digest, str(self.page.url)):
                # a page of form boxes is the form, whatever it was read as:
                # the account step would type the facts in and click its button.
                # An address screen with another box (a country) goes this way
                # too, so its site takes the password screen after it.
                self.log.info("job %s: read as %s (%.2f) with no account boxes; it is the "
                              "form", self.job_id, state, conf)
                self._decide("remap", f"read as {state} ({conf:.2f}) with no account boxes; "
                                      "it is the form", to="application_form")
                state = "application_form"
                if any(_is_email_box(f) for f in digest.fields):
                    self.accounts.email_sites.add(_site(digest.url_host or _host(self.page.url)))
            if state == "confirmation":
                step, detail, then = confirmation_step(
                    digest, answers, conf, submit_clicked=self.submit_clicked,
                    code_sent=self._code_sent)
                if step == "park" and unsure:
                    pass                # the unsure read parks below with its own words
                elif step == "go_on":
                    self._decide("confirmation_contradicted",
                                 f"read as confirmation ({conf:.2f}) on a page with a form field "
                                 f"or a submit button and no received words; going on as the "
                                 f"next read ({detail} {then:.2f})", to=detail)
                    state, conf = detail, then
                    unsure = conf < apply_judge.PAGE_STATE_MIN_CONF
                else:
                    raise _Parked("submitted" if step == "submitted" else "needs_human", detail,
                                  CHECK_SENT_NOTE if step == "park" else "")
            if state in _LINKEDIN_FORM_STATES:
                self._no_form_on_linkedin(f"read as {state} ({conf:.2f})")
            if unsure:
                state = self._check_unsure(digest, state, conf)
                if state in _LINKEDIN_FORM_STATES:
                    self._no_form_on_linkedin(f"read by its structure as {state}")
            elif state == "other":
                settled = other_step(facts, digest)
                if settled is not None:
                    # `other` is none of the listed kinds; a page whose
                    # structure settles one of them is that one
                    self._decide("structure_over_other", f"read as other ({conf:.2f}); its "
                                                         f"structure reads it as {settled}",
                                 facts=facts.to_dict(), to=settled)
                    state = settled
                    if state in _LINKEDIN_FORM_STATES:
                        self._no_form_on_linkedin(f"read by its structure as {state}")
            if state == "application_form" and _email_first(digest):
                # the address screen of a two-step sign-in taken as a form:
                # its site takes the password screen after it all the same
                sites = getattr(self.accounts, "email_sites", None)
                if sites is not None:
                    sites.add(_site(digest.url_host or _host(self.page.url)))
            self._map(digest, answers, state)
            plan = apply_judge.plan(digest, self.catalog, answers,
                                    generation_enabled=bool(self.r.settings["auto_apply_generate"]),
                                    company=self._company())
            rec["flags"] = dict(plan.flags)
            self._trace("plan", plan=apply_trace.plan_json(plan))
            if state == "job_posting":
                self._job_posting(digest, answers, plan, rec)
            elif state == "application_form":
                self._application_form(digest, answers, plan, rec)
            elif state == "review_page":
                self._review_page(digest, answers, plan, rec)
            elif state in ("login_wall", "signup_form"):
                self._account_step(state, digest)
            elif state == "code_gate":
                self._code_gate(digest, plan, rec)
            elif state == "captcha_or_bot_check":
                self._wait_for_human_check(
                    f"{_PARK_STATES[state]} (has_captcha p={plan.flags.get('has_captcha', 0.0):.2f})",
                    before=marker)
            else:
                closed = apply_judge.closed_posting(answers, facts, state)
                if closed:
                    # READ-08: a closed posting has its own reason
                    raise _Parked("needs_human", f"{CLOSED_POSTING_REASON} ({_cap(closed, 160)})",
                                  apply_linkedin.CLOSED_NOTE)
                reason = _PARK_STATES.get(state, state)
                if state == "payment_request":
                    reason += (f" (page_payment p="
                               f"{apply_judge.noul_of(answers, 'page_payment'):.2f})")
                elif state == "other":
                    reason += self._reads_suffix()
                raise _Parked("needs_human", reason)

    def _reads_suffix(self) -> str:
        reads = self._reads()
        return f"; reads: {reads}" if reads else ""

    # -- reading a page ------------------------------------------------------------------

    def _open(self, url: str) -> None:
        """The first load (`open_page`), its decisions in the first page's
        trace."""
        for row in open_page(self.page, url):
            self._decide_next(**row)

    def _company(self) -> str:
        """The queue entry's company: the one name a routine consent's label
        may carry (`apply_judge.routine_consent`, review R3-I1)."""
        return str((self.entry or {}).get("company") or "")

    def _busy(self) -> bool:
        """Whether a loading placeholder shows in the viewport (an `aria-busy`
        region, a skeleton: `apply_fill`'s readiness read)."""
        try:
            return bool(apply_fill.ready_snapshot(self.page)[1])
        except Exception:       # noqa: BLE001  (a page double, a page mid-navigation)
            return False

    def _loading(self, digest: apply_form.FormDigest, busy: bool) -> bool:
        """A read with no field taken while a loading placeholder showed
        (`busy`: `_read_busy`'s look before or after the extract): a skeleton
        is no read of the page (NAV-04, READ-02)."""
        return busy and not digest.fields

    def _read_busy(self, look: bool) -> tuple[apply_form.FormDigest, bool]:
        """(the page's digest, whether a loading placeholder showed before or
        after it was read). With `look` off, no look. The look before the read
        catches a skeleton that clears between the read and a later look (SP5
        round 2: a busy machine read the skeleton, the form came, and the look
        saw none); the look after it, when the read has no field, a skeleton
        painted between the first look and the read (review round 3, M1)."""
        before = look and self._busy()
        digest = self._drop_foreign_controls(self._extract())
        after = look and not before and not digest.fields and self._busy()
        return digest, bool(before or after)

    def _read_digest(self) -> apply_form.FormDigest:
        """The page's digest, read once more while it is still empty
        (`_empty_read`: no button, or no field and under `EMPTY_TEXT_MIN`
        characters) or still loading (`_loading`: a skeleton or an
        `aria-busy` region and no field): an empty read gets a settle, then a
        read every `EMPTY_READ_POLL_S` until it is not empty, has held the
        same for `EMPTY_READ_STABLE_S` (a short page that is done), or
        `EMPTY_READ_MAX_S` has passed (G5: content arrives 0.3 to 1.7 s after
        `load` on SPA postings). A read that is only loading is read again
        every `EMPTY_READ_POLL_S` for at most `LOADING_WAIT_S`, once per page
        (M10: an ad's or a widget's placeholder may never clear; the trace
        says when it stayed up). The host is checked before every read
        again."""
        url = str(self.page.url)
        watch = url not in self._loading_waited
        digest, busy = self._read_busy(watch)
        loading = watch and self._loading(digest, busy)
        empty = _empty_read(digest)
        if not empty and not loading:
            return digest
        if loading:
            self._loading_waited.add(url)
        first = (f"{len(digest.fields)} field(s), {len(digest.buttons)} button(s), "
                 f"{len((digest.text or '').strip())} characters")
        what = "an empty read" if empty else "a loading placeholder"
        start = time.monotonic()
        last = json.dumps(digest.to_dict(), sort_keys=True)
        stable_since = start
        info = apply_fill.settle(self.page, CLICK_TIMEOUT_S) if empty else {"ms": 0}
        lingered = False
        while True:
            # the page may have moved on while it settled or between reads:
            # a page off the allowed sites is never read, let alone judged
            self._check_host(self.page.url)
            digest, busy = self._read_busy(loading)
            now = time.monotonic()
            empty = _empty_read(digest)
            loading = loading and self._loading(digest, busy)
            if loading and now - start >= LOADING_WAIT_S:
                # the placeholder had its one short wait: it no longer holds
                # the read (the empty-read rules still do)
                loading, lingered = False, True
            if not empty and not loading:
                break
            seen = json.dumps(digest.to_dict(), sort_keys=True)
            if seen != last:
                last, stable_since = seen, now
            if now - start >= EMPTY_READ_MAX_S or (
                    not loading and now - stable_since >= EMPTY_READ_STABLE_S):
                break
            self.page.wait_for_timeout(int(EMPTY_READ_POLL_S * 1000))
        waited = int((time.monotonic() - start) * 1000)
        then = ("and read again" if not lingered else
                f"and read as it was: a loading placeholder stayed up past {waited} ms")
        self._decide_next("reread_after_settle", f"{what} ({first}); "
                                                 + settled_words(info, then),
                          still_empty=empty, still_loading=lingered,
                          capped=_settle_capped(info), waited_ms=waited)
        return digest

    def _reread(self, digest: apply_form.FormDigest, answers: dict, state: str,
                conf: float) -> tuple[apply_form.FormDigest, dict, str, float]:
        """A read below `PAGE_STATE_MIN_CONF`, taken once more after a
        further settle: a fresh extract and a fresh judge request (READ-02:
        a page read mid-render, an interstitial that clears itself)."""
        first, reads = f"{state} {conf:.2f}", self._reads(answers)
        info = apply_fill.settle(self.page, CLICK_TIMEOUT_S)
        self._check_host(self.page.url)     # the page may have moved on while it settled
        digest = self._read_digest()
        answers = self._read(digest)
        state, conf = apply_judge.read_page_state(answers)
        self._decide_next("reread_unsure", f"a read below the page-state floor ({first}); "
                                           + settled_words(info, "and read once more"),
                          first_reads=reads, now=f"{state} {conf:.2f}",
                          settled_ms=_settled_ms(info), capped=_settle_capped(info))
        return digest, answers, state, conf

    def _dismiss_consent(self) -> None:
        """A visible cookie or consent banner is dismissed before the page is
        read (study G1: on Teamtailor and bunq it took the Apply click): its
        reject, decline or necessary-only control, else its close; never an
        accept, allow or agree (`apply_form.consent_control`), and only in
        the page's own frames on the allowed sites, never a bot check's.
        Then the page settles and its host is checked again. At most
        `CONSENT_MAX` per job, so a banner that comes back cannot hold the
        run."""
        if self._consent_clicks >= CONSENT_MAX:
            return
        try:
            frames = apply_form.frames(self.page)

            def _own(idx: int, frame) -> bool:
                url = self._frame_url(frames, idx)
                host = _host(url)
                return not _is_captcha_url(url) and (not host or self._allowed_site(host))
            found = apply_form.consent_control(self.page, allow=_own)
        except Exception:       # noqa: BLE001  (a page double, a page mid-navigation)
            return
        if not found:
            return
        idx, control = found
        self._consent_clicks += 1
        error = ""
        loc = apply_form.resolve(self.page, (idx, str(control.get("css") or ""))).first
        try:
            # the click's effect (the banner gone, a reload, a navigation) is
            # waited for before the page is read
            info = apply_fill.act_and_settle(
                self.page, lambda: loc.click(timeout=apply_fill.ACTION_TIMEOUT_MS),
                timeout_s=CONSENT_WAIT_S)
        except Exception as e:      # noqa: BLE001  (the banner went away on its own)
            if _closed_error(e):
                raise
            error = type(e).__name__
            info = apply_fill.settle(self.page, CLICK_TIMEOUT_S)
        text = str(control.get("text") or "")
        self.log.info("job %s: consent banner: clicked %r (%s)", self.job_id, text,
                      control.get("kind"))
        self._decide_next("consent_dismissed", f"a consent banner ({control.get('banner')}): "
                                               f"clicked its {control.get('kind')} control; "
                                               + settled_words(info),
                          text=text, error=error, settled_ms=_settled_ms(info),
                          capped=_settle_capped(info))
        self._check_host(self.page.url)     # the click may have taken the page elsewhere

    def _linkedin_step(self, digest: apply_form.FormDigest) -> bool:
        """LinkedIn's pages, without the judge (`apply_linkedin`): True when
        the page was handled and the loop reads the next one; a park raises.
        A signed-out URL parks; the `/safety/go/` hop is followed; a job page
        waits up to `LINKEDIN_READY_S` for its top card, then takes its
        offsite Apply or parks (Easy Apply, already applied, closed, signed
        out, no Apply); another LinkedIn page parks on an open form dialog
        (Easy Apply's) or a sign-in, and otherwise goes to the judge (False),
        whose form steps park on LinkedIn (`_no_form_on_linkedin`)."""
        url = str(self.page.url or "")
        kind = apply_linkedin.url_kind(url)
        if not kind:
            return False
        if kind == "signed_out":
            why = apply_linkedin.signed_out_evidence(url)
            self._new_page_record("login_wall", 1.0, digest=digest, answers={})
            self._decide("linkedin_handler", f"LinkedIn's {why}", url_kind=kind)
            raise _Parked("needs_human", f"{apply_linkedin.SIGNED_OUT_REASON} ({why})",
                          LINKEDIN_LOGIN_NOTE)
        if kind == "redirector":
            self._new_page_record("linkedin_redirect", 1.0, digest=digest, answers={})
            self._decide("linkedin_handler", "LinkedIn's redirect to the company's site",
                         url_kind=kind)
            dest, info = self._await_destination(self.page)
            self._trace("redirect", destination=str(dest.url), **info)
            if dest is not self.page:
                self._follow_popup(dest, source_url=url)
                return True
            if _on_linkedin_redirector(self.page.url):
                raise _Parked("needs_human", f"the LinkedIn redirect did not move on "
                                             f"({_cap(url, 160)})")
            self._admit_ats_transition(self.page.url, url)
            self._check_host(self.page.url)
            self.last_sig = None
            return True
        view, waited = linkedin_view(self.page, wait_s=LINKEDIN_READY_S if kind == "job" else 0,
                                     job_title=str(self.entry.get("title") or ""),
                                     company=str(self.entry.get("company") or ""))
        d = apply_linkedin.decide(view)
        if kind == "other" and d.kind not in ("form_dialog", "signed_out"):
            self._decide_next("linkedin_handler", "a LinkedIn page other than a job page, "
                                                  "with nothing for the handler; the judge "
                                                  "reads it", url_kind=kind, found=d.kind)
            return False
        if waited:
            digest = self._drop_foreign_controls(self._extract())
        state = {"form_dialog": "application_form", "signed_out": "login_wall"}.get(
            d.kind, "job_posting")
        rec = self._new_page_record(state, 1.0, digest=digest, answers={})
        self._decide("linkedin_handler", d.why, found=d.kind, url_kind=kind,
                     view=view.to_dict(), waited_ms=waited)
        self.log.info("job %s: LinkedIn %s page: %s", self.job_id, kind, d.why)
        if d.kind in ("form_dialog", "easy_apply"):
            raise _Parked("needs_human", EASY_APPLY_REASON, EASY_APPLY_NOTE)
        if d.kind == "applied":
            raise _Parked("needs_human", f"{apply_linkedin.APPLIED_REASON} ({view.applied})",
                          apply_linkedin.APPLIED_NOTE)
        if d.kind == "closed":
            raise _Parked("needs_human", f"{apply_linkedin.CLOSED_REASON} ({view.closed})",
                          apply_linkedin.CLOSED_NOTE)
        if d.kind == "signed_out":
            raise _Parked("needs_human", f"{apply_linkedin.SIGNED_OUT_REASON} ({d.why})",
                          LINKEDIN_LOGIN_NOTE)
        if d.kind == "none":
            raise _Parked("needs_human", f"{apply_linkedin.NO_APPLY_REASON} ({d.why}; "
                                         f"title {view.title!r}; waited {waited} ms)")
        if self.submit_clicked or self.form_filled:
            raise _Parked("needs_human", f"{LINKEDIN_RETURN_REASON} ({_cap(url, 160)})")
        # the job's clicks are counted by the job LinkedIn shows (a URL that
        # gains a tracking parameter per click is the same job) and in all
        job = apply_linkedin.job_id(url) or url
        clicks = self._linkedin_clicks.get(job, 0) + 1
        self._linkedin_clicks[job] = clicks
        total = sum(self._linkedin_clicks.values())
        control = d.control
        if clicks > 1:
            raise _Parked("needs_human", f"the offsite Apply ({control.label}) did not open the "
                                         f"company's site (clicked twice)")
        if total > LINKEDIN_CLICKS_MAX:
            raise _Parked("needs_human", f"the offsite Apply ({control.label}) did not open the "
                                         f"company's site ({LINKEDIN_CLICKS_MAX} LinkedIn "
                                         f"Apply clicks in this job)")
        loc = self.page.main_frame.locator(control.css)
        self._click_entry(rec, loc, control.label, how="linkedin_handler")
        return True

    def _aggregator_step(self, digest: apply_form.FormDigest) -> bool:
        """A job board's posting (NAV-08): its company-site control
        (`company_site_control`: one that says so, or the one Apply link off
        the board) is clicked once as the entry, and the page it leads to is
        the application's (`_admit_ats_transition`; another board is read the
        same way, `AGGREGATOR_BOARDS_MAX`); a board's posting without one
        parks at once, never filled or signed in on. False off a board, or
        once its link was followed."""
        host = _host(str(self.page.url or ""))
        if not _aggregator(host) or self._aggregator_left:
            return False
        rec = self._new_page_record("job_posting", 1.0, digest=digest, answers={})
        control = company_site_control(digest, board=host,
                                       targets=link_targets(self.page, digest))
        if control is None:
            self._decide("aggregator", f"a job board's posting ({host}) with no link to the "
                                       "company's site")
            raise _Parked("needs_human", f"{AGGREGATOR_REASON} on {host}: no link to the "
                                         f"company's site (buttons: "
                                         f"{_cap(', '.join(b.text for b in digest.buttons), 120)})",
                          AGGREGATOR_NOTE)
        self._decide("aggregator", f"a job board's posting ({host}): its link to the company's "
                                   f"site is the entry", text=control.text)
        self._click_entry(rec, apply_form.resolve(self.page, control.locator), control.text,
                          how="aggregator_company_site", n=control.n)
        if not self._aggregator_left and _host(str(self.page.url or "")) == host:
            raise _Parked("needs_human", f"{AGGREGATOR_REASON} on {host}: its link to the "
                                         f"company's site ({_cap(control.text, 60)}) stayed on "
                                         f"the board", AGGREGATOR_NOTE)
        return True

    def _no_form_on_linkedin(self, why: str) -> None:
        """Nothing is filled, ticked, picked, uploaded or sent on LinkedIn: a
        form step there is Easy Apply's, and the job parks with its reason;
        one reached after the company's form was filled or sent is the
        application come back to LinkedIn (`LINKEDIN_RETURN_REASON`)."""
        if self._on_linkedin():
            self._decide("linkedin_form", f"a form step on LinkedIn ({why}); nothing is "
                                          "filled there")
            if self.form_filled or self.submit_clicked:
                raise _Parked("needs_human", f"{LINKEDIN_RETURN_REASON} ({why}; "
                                             f"{_cap(self.page.url, 120)})")
            raise _Parked("needs_human", EASY_APPLY_REASON, EASY_APPLY_NOTE)

    # -- per state ------------------------------------------------------------------------

    def _account_step(self, state: str, digest: apply_form.FormDigest) -> None:
        """A sign-in or sign-up screen goes to the accounts hook when the
        master password may be typed on its site (`_password_ok`); LinkedIn
        signed out, or a sign-in on some other site, parks for the human. A
        screen the hook hands back (`_AsForm`: it carries the application)
        goes to the form step, and its submit is judged as a sign-up's
        (`_after_submit`'s `handoff`)."""
        host = digest.url_host or _host(self.page.url)
        if not self._password_ok(host):
            if apply_linkedin.is_linkedin(host):
                raise _Parked("needs_human", f"{apply_linkedin.SIGNED_OUT_REASON} (read as "
                                             f"{state})", LINKEDIN_LOGIN_NOTE)
            raise _Parked("needs_human", f"a sign-in on {host}, outside the application site",
                          LOGIN_NOTE)
        try:
            if state == "login_wall":
                if not self.accounts.login(self.page, digest, host):
                    raise _Parked("needs_human", f"login wall "
                                                 f"({self._account_evidence(state, digest)})",
                                  LOGIN_NOTE)
            elif not self.accounts.signup(self.page, digest, host):
                raise _Parked("needs_human", f"account signup needed "
                                             f"({self._account_evidence(state, digest)})",
                              LOGIN_NOTE)
        except _AsForm as form:
            self.log.info("job %s: the account screen carries the application; it is the "
                          "form", self.job_id)
            self.handed_off = True
            try:
                self._application_form(form.digest, form.answers, form.plan, self.pages[-1],
                                       completed=True)
            finally:
                self.handed_off = False

    def _human_check_showing(self, *, checkbox: bool = False) -> bool:
        """Is a bot check waiting for the person on the page: a frame from a
        CAPTCHA provider that is visible and at least `HUMAN_CHECK_MIN_PX`
        tall (a challenge), or, with `checkbox`, a visible reCAPTCHA,
        hCaptcha or Turnstile checkbox (`size=normal`, whatever its height:
        study G11) whose response token is still empty
        (`apply_form.unsolved_checkbox`)? The invisible badge
        (`size=invisible`) is neither. A checkbox blocks only the send, so it
        is read at the gate and before the account step's click; the page is
        filled first. The run never ticks it: the person does, and the token
        it sets ends the wait."""
        try:
            frames = list(self.page.frames)
        except Exception:       # noqa: BLE001  (a page double, or the page is gone)
            return False
        for frame in frames:
            if not _is_captcha_url(str(getattr(frame, "url", "") or "")):
                continue
            try:
                element = frame.frame_element()
                if not element.is_visible():
                    continue
                box = element.bounding_box()
            except Exception:       # noqa: BLE001  (a frame detached while looking)
                continue
            if box and box["height"] >= HUMAN_CHECK_MIN_PX and box["y"] + box["height"] > 0:
                return True
        return checkbox and bool(apply_form.unsolved_checkbox(self.page))

    def _page_marker(self) -> tuple[str, str]:
        try:
            text = apply_fill.page_text(self.page)[:2000]
        except Exception:       # noqa: BLE001
            text = ""
        return str(self.page.url), text

    def _wait_for_human_check(self, reason: str, *, before: tuple | None = None,
                              checkbox: bool = False) -> None:
        """A CAPTCHA is the user's to solve; the run never touches it. The
        check is over when its challenge frame closes or, for a whole-page
        check, when the page differs from `before` (the page as it was judged;
        a check that clears itself, Cloudflare's "Just a moment", may already
        have). In a visible window the run waits up to `HUMAN_CHECK_WAIT_S`
        for that, then carries on, and the wait is added back to the job's
        clock. Headless, an open challenge parks at once and a whole-page
        check gets `HUMAN_CHECK_AUTO_S` to clear itself, as does a Turnstile
        checkbox (its managed mode ticks itself). `checkbox`: an unticked
        checkbox counts as the check (`_human_check_showing`)."""
        framed = self._human_check_showing(checkbox=checkbox)
        if not framed and before is None:
            before = self._page_marker()
        headless = bool(self.r.settings.get("auto_apply_headless"))
        clears_itself = (checkbox and framed and not self._human_check_showing()
                         and apply_form.unsolved_checkbox(self.page) == "turnstile")
        if headless and framed and not clears_itself:
            raise _Parked("needs_human", reason)
        limit = HUMAN_CHECK_AUTO_S if headless else HUMAN_CHECK_WAIT_S
        polls = int(limit / HUMAN_CHECK_POLL_S)
        start = self.r.clock()
        for i in range(polls + 1):
            if framed:
                done = not self._human_check_showing(checkbox=checkbox)
            else:
                done = self._page_marker() != before
            if done:
                break
            if i == polls:
                raise _Parked("needs_human", reason if headless else f"{reason}; not solved in time")
            if i == 0 and not headless:
                self.log.warning("job %s: %s; solve it in the browser window (waiting up to "
                                 "%d min)", self.job_id, reason, HUMAN_CHECK_WAIT_S // 60)
            self.r.sleep(HUMAN_CHECK_POLL_S)
        self.deadline += max(0.0, self.r.clock() - start)
        self.last_sig = None
        self.log.info("job %s: the check is done; going on", self.job_id)
        apply_fill.settle(self.page, CLICK_TIMEOUT_S)

    def _read(self, digest: apply_form.FormDigest) -> dict:
        """The page read (SP4): its own small request
        (`apply_judge.read_questions`) combined with the page's structure
        (`apply_judge.page_facts`, `apply_judge.read_page`). The answers carry
        the combined read as `page_state`, the judge's own pick as
        `page_state_judged`, and every Noul; a read that differs from the
        judge's pick says why in the page's trace."""
        url = str(getattr(self.page, "url", "") or "")
        facts = apply_judge.page_facts(digest, url, captcha_frame=self._human_check_showing())
        state, questions = apply_judge.read_questions(digest, url)
        raw = dict(self.r.jev.judge(state, questions))
        read = apply_judge.read_page(raw, facts)
        answers: dict[str, Any] = {k: v for k, v in raw.items() if k in questions}
        if "page_state" in answers:
            answers["page_state_judged"] = answers["page_state"]
        answers["page_state"] = apply_judge.read_answer(read)
        self._facts = facts
        if read.why:
            self._decide_next("read_combined", read.why,
                              judged=f"{read.judged} {read.judged_conf:.2f}",
                              read=f"{read.state} {read.conf:.2f}", facts=facts.to_dict())
        return answers

    def _map(self, digest: apply_form.FormDigest, answers: dict, state: str, *,
             discover: bool = True, own_page: bool = True) -> dict:
        """The page's mapping (`apply_judge.page_questions`), asked only on a
        page the run acts on (`_MAPPED_STATES`): the fields' facts and the
        buttons' roles on a form, an account or a code screen and on a
        posting with fields, the buttons' roles alone on a posting with none.
        Merged into `answers` in place (only its own questions' answers) and
        into the page's trace (`own_page`; a digest of something else, a
        sign-in's create-account link, is traced as an event). `discover`:
        open the page's listboxes first to read their options (never on
        LinkedIn)."""
        if state not in _MAPPED_STATES:
            return answers
        with_fields = state != "job_posting" or bool(digest.fields)
        if with_fields and discover and not self._on_linkedin():
            self._discover_listbox_options(digest)
        s, q = apply_judge.page_questions(digest, self.catalog, self.entry, fields=with_fields)
        if q:
            got = {k: v for k, v in self.r.jev.judge(s, q).items() if k in q}
            answers.update(got)
            if own_page:
                self.trace.add_answers(got)
            else:
                self._trace("mapping", state=state, answers=apply_trace.answers_json(got))
        return answers

    def _judge_page(self, digest: apply_form.FormDigest, *, discover: bool = False) -> dict:
        """The page read and, on a page the run acts on, its mapping."""
        answers = self._read(digest)
        state, _ = apply_judge.read_page_state(answers)
        return self._map(digest, answers, state, discover=discover)

    def _new_page_record(self, state: str, conf: float, *,
                         digest: apply_form.FormDigest | None = None,
                         answers: Mapping[str, Any] | None = None,
                         timings: Mapping[str, float] | None = None) -> dict:
        """The record's entry for a judged page; with the `digest` and the
        `answers`, the trace's page too (its JSON and a masked screenshot)."""
        rec = {"url": self.page.url, "state": state, "confidence": conf,
               "filled": [], "verification": [], "clicked": [], "flags": {},
               "generated": []}
        self.pages.append(rec)
        if answers is not None:
            self._last_answers = answers
        if digest is not None:
            n = len(self.pages)
            self.trace.page(n, self.page.url, digest, answers or {}, state, conf,
                            dropped=self._last_dropped, timings=timings)
            self._flush_decisions()
            self.trace.screenshot(self.page, f"page-{n}",
                                  extra_mask=self._secret_masks(self.page))
        return rec

    def _check_unsure(self, digest: apply_form.FormDigest, state: str, conf: float) -> str:
        """A read still below `PAGE_STATE_MIN_CONF` after its second look
        (`unsure_step`): it goes on as its guess when that is one of
        `_UNSURE_ACTS` and the page has that step's boxes (a code gate its
        code box; a sign-in read of form boxes is already the form, and the
        account step takes a screen of account boxes alone); else as the kind
        the page's structure gives (`apply_judge.structural_kind`: a code
        box, an account screen, application boxes, an Apply entry with no
        box); else it parks with the read's distribution. Returns the state
        the loop acts on."""
        step, how = unsure_step(state, digest, self._facts)
        if step is None:
            raise _Parked("needs_human", f"unsure what this page is ({state}, {conf:.2f})"
                                         + self._reads_suffix())
        if how == "structure":
            self.log.info("job %s: unsure of the page (%s, %.2f); its structure reads %s",
                          self.job_id, state, conf, step)
            self._decide("structural_fallback", f"unsure of the page ({state}, {conf:.2f}); "
                                                f"its structure reads it as {step}",
                         reads=self._reads(), facts=self._facts.to_dict(), to=step)
            return step
        self.log.info("job %s: unsure of the page (%s, %.2f); going on with that read",
                      self.job_id, state, conf)
        self._decide("unsure_goes_on", f"unsure of the page ({state}, {conf:.2f}); going on "
                                       "with that read", reads=self._reads())
        return state

    def _on_linkedin(self) -> bool:
        page = self.page
        return page is not None and apply_linkedin.is_linkedin(str(getattr(page, "url", "") or ""))

    def _job_posting(self, digest: apply_form.FormDigest, answers: dict, plan: FillPlan,
                     rec: dict) -> None:
        """Click the posting's Apply entry. A page that carries form fields
        is only clicked through a confident `apply_entry` role whose text is
        not submit-shaped; otherwise it is treated as the application form (a
        form's own Apply button is a submit, and clicking it before the fill
        would send an empty form). A fieldless posting keeps the text match.
        An Easy Apply control is never the entry. After a form was filled,
        an Apply button sends that form: the page goes the form's way, to the
        submit gate. In park mode so does one after a form page with a
        password box, typed or left blank: a page of a sign-up's boxes does
        not count as the filled application (`_fills_the_application`), and
        the Apply after it may be the review of an application that asked for
        no more. On LinkedIn the judge never picks the entry: the handler
        (`_linkedin_step`) takes a job page's offsite Apply, and a posting
        read anywhere else on LinkedIn parks."""
        if self._on_linkedin():
            if digest.fields and (self.form_filled
                                  or posting_entry_choice(digest, plan)[0] is None):
                self._no_form_on_linkedin("a posting with form fields is the form")
            raise _Parked("needs_human", f"{apply_linkedin.NO_APPLY_REASON} (read as a posting "
                                         f"at {_cap(self.page.url, 120)}; buttons: "
                                         f"{self._buttons_seen(digest)})")
        park_mode = not self.r.settings.get("auto_apply_submit", True)
        if self.form_filled or (park_mode and self.form_had_password):
            self.log.info("job %s: a posting read after a filled form; treating it as the "
                          "form's next page", self.job_id)
            self._decide("posting_as_form", "a posting read after a filled form (or a "
                                            "password page in park mode) is the form's next "
                                            "page")
            self._application_form(digest, answers, plan, rec)
            return
        apart, unclassified, scan = posting_context(self.page, digest, plan)
        if apart:
            self._decide("entry_apart", "the submit-worded Apply sits apart from the page's "
                                        "form fields (a job-alert or search box); it is the "
                                        "entry", buttons=sorted(apart))
        n, how = posting_entry_choice(digest, plan, apart=apart, unclassified=unclassified)
        if n is None:
            if digest.fields:
                self.log.info("job %s: posting with %d form field(s) and no confident Apply "
                              "entry; treating it as the application form",
                              self.job_id, len(digest.fields))
                self._decide("posting_as_form", f"a posting with {len(digest.fields)} form "
                                                "field(s) and no confident Apply entry")
                self._application_form(digest, answers, plan, rec)
                return
            held = (f"; controls the run does not read: "
                    f"{_cap(', '.join(str(r.get('label')) for r in scan), 120)}" if scan else "")
            raise _Parked("needs_human", f"no Apply button on the posting (buttons: "
                                         f"{self._buttons_seen(digest)}{held})")
        button = next(b for b in digest.buttons if b.n == n)
        loc = apply_form.resolve(self.page, button.locator)
        self._click_entry(rec, loc, button.text, how=how, n=n)

    def _click_entry(self, rec: dict, loc, text: str, *, how: str, n: int | None = None) -> None:
        """Click an Apply entry (`click_entry`: a popup, a same-tab
        navigation or a DOM change, whichever comes first) and follow it to
        its destination: a new tab is adopted (`_follow_popup`), the same tab
        waits out LinkedIn's redirect; the destination's host is admitted and
        checked. With no popup yet, a tab the click opens later is watched
        for until the next page is read (`_take_late_popup`)."""
        live = apply_form.live_text(loc)
        why = live_refusal("apply_entry", text, live) if live else ""
        if why:
            self._refused_click("apply_entry", text, why)
        address = mailto_address(loc)
        if address:
            # NAV-09: the Apply opens an email to the employer; nothing to
            # click through (it would read as a page that did not advance)
            self._decide("mailto", f"the Apply ({_cap(text, 60)}) is an email address",
                         address=address)
            raise _Parked("needs_human", f"{MAILTO_REASON} to {address}", MAILTO_NOTE)
        rec["clicked"].append(f"{text} (apply_entry)")
        self._last_click = (text, "apply_entry")
        source_url = self.page.url
        popup, signal, waited = click_entry(self.page, loc)
        if popup is None:
            self._stop_late_watch()
            self._late_watch = LateWatch(self.page, signal, str(source_url))
            self._late_watch.start()
            dest, info = self._await_destination(self.page)
            self._trace("apply_entry", n=n, text=text, how=how, popup=False, signal=signal,
                        waited_ms=waited, destination=str(dest.url), **info)
            if dest is not self.page:
                # the interstitial's Continue opened the destination's tab: it
                # is followed here, and the late-tab watch (which saw it open
                # too) ends before it could take it for a stray
                self._stop_late_watch()
                self._follow_popup(dest, source_url=source_url)
                return
            self._admit_ats_transition(self.page.url, source_url)
            self._check_host(self.page.url)
            return
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=CLICK_TIMEOUT_S * 1000)
        except Exception:       # noqa: BLE001
            pass
        self.log.info("job %s: Apply opened %s", self.job_id, popup.url)
        try:
            self._follow_popup(popup, source_url=source_url)
        finally:
            self._trace("apply_entry", n=n, text=text, how=how, popup=True, signal=signal,
                        waited_ms=waited, destination=str(self.page.url))

    def _follow_popup(self, popup, *, source_url: str | None = None) -> None:
        """Adopt the tab Apply opened once it has reached its destination:
        its host is admitted and checked after the redirects, never at the
        popup event, which on LinkedIn still shows the `/safety/go/` hop. A
        tab the safety interstitial's Continue opens is followed the same
        way."""
        source = source_url or self.page.url
        for _ in range(2):
            if popup is not self.page:
                self.trace.nav(str(getattr(popup, "url", "")))  # its first load came before the watch
                self._watch(popup)
            dest, info = self._await_destination(popup)
            if info.get("trackers"):
                self._decide_next("tracker_hops", "waited out an ad tracker's hop to the "
                                                  "company's site", trackers=info["trackers"])
            if dest is popup:
                break
            popup = dest
        self._admit_ats_transition(popup.url, source)
        self._check_host(popup.url)
        self.page = popup
        self.last_sig = None

    def _await_destination(self, page) -> tuple[Any, dict[str, Any]]:
        return await_destination(page, self.log, self.job_id)

    def _stop_late_watch(self) -> LateWatch | None:
        watch, self._late_watch = self._late_watch, None
        if watch is not None:
            watch.stop()
        return watch

    def _take_late_popup(self) -> None:
        """A tab the last entry click opened after `click_entry` stopped
        waiting (M-7): when the click left the tab where it was (a DOM change
        or nothing), the new tab is the application and is adopted; when the
        tab itself navigated, the late one is a stray and is closed. Either
        way the trace says so; the watch ends here, before the next read.
        The page the run is on, and a tab already closed, are never among
        them (a tab the run followed some other way, the interstitial's)."""
        watch = self._stop_late_watch()
        if watch is None:
            return
        tabs = []
        for p in watch.popups:
            if p is self.page or _page_closed(p) or any(p is t for t in tabs):
                continue
            tabs.append(p)
        if not tabs:
            return
        popup = tabs[0]
        extra = tabs[1:]
        if watch.signal in ("dom", "none") and watch.page is self.page:
            self._decide_next("late_popup", f"the Apply's tab opened after the click's wait "
                                            f"({watch.signal}); it is the destination",
                              url=_cap(str(getattr(popup, "url", "")), 160))
            self.log.info("job %s: Apply opened %s late; adopting it", self.job_id, popup.url)
            self._follow_popup(popup, source_url=watch.source_url)
        else:
            extra.insert(0, popup)
        for stray in extra:
            self._decide_next("late_popup_closed", f"a tab opened after the Apply click "
                                                   f"({watch.signal}); closed",
                              url=_cap(str(getattr(stray, "url", "")), 160))
            try:
                stray.close()
            except Exception:   # noqa: BLE001  (already closed)
                pass

    def _application_form(self, digest: apply_form.FormDigest, answers: dict,
                          plan: FillPlan, rec: dict, *, completed: bool = False) -> None:
        """Fill and verify the page, then click its advance or go to the
        submit gate. `completed`: the plan already has its option picks.
        Never on LinkedIn (`_no_form_on_linkedin`)."""
        self._no_form_on_linkedin("the form step")
        if not completed:
            plan = self._complete_option_plan(digest, answers, plan, rec)
        with self._password_guard() as guard:
            verification = self._fill_and_verify(digest, plan, rec)
            self._fill_passwords(digest, plan, rec, guard)
            digest, plan, verification = self._after_fill(digest, plan, verification, rec)
            self._form_buttons(digest, plan, verification, rec)

    def _form_buttons(self, digest: apply_form.FormDigest, plan: FillPlan,
                      verification: list[VerifyResult], rec: dict) -> None:
        """The filled page's way on: a submit role, a submit-shaped advance
        (a final-shaped one in park mode) or a form's own Apply goes to the
        submit gate; otherwise a confident advance is clicked."""
        park_mode = not self.r.settings.get("auto_apply_submit", True)
        plan = self._own_submit(digest, plan)
        step, button, why = form_route(digest, plan, park_mode=park_mode,
                                       submit_apart=self._submit_apart(digest, plan),
                                       judged=self._judged_roles(digest))
        b = self._form_entry(digest, plan, step, button)
        if b is not None:
            self._click_entry(rec, apply_form.resolve(self.page, b.locator), b.text,
                              how="judged_apply_entry", n=b.n)
            return
        if why:
            # a submit-shaped advance (a final-shaped one in park mode), or a
            # form's own "Apply" judged apply_entry: it sends the form, so it
            # is the submit and goes through the gate like any other
            self.log.info("job %s: %s; routing it through the submit gate", self.job_id, why)
            self._decide("to_gate", why, button=button[0] if button else None,
                         text=_button_text(digest, button[0]) if button else "")
        if step in ("advance", "gate"):
            self._unreadable(digest, button[0])
            digest, plan, verification, n = self._still_disabled(digest, plan, verification,
                                                                 button[0], rec,
                                                                 gate=step == "gate")
            button = (n, button[1])
        if step == "advance":
            self._advance(digest, plan, verification, rec, button[0], button[1])
            return
        if step == "gate":
            plan.buttons["submit"] = button
            self._submit_gate(digest, plan, verification, rec)
            return
        raise _Parked("needs_human", f"no way forward on this page (buttons: "
                                     f"{self._buttons_seen(digest)})")

    def _unreadable(self, digest: apply_form.FormDigest, n: int) -> None:
        """EXT-01: the controls of the way on's frame the run cannot read
        into (a closed shadow root, a form-associated custom element) are
        named in the page's trace, never skipped in silence; a required one
        still empty parks the job before any click, naming it (the gate
        reads the same, `_gate_read`)."""
        button = next((b for b in digest.buttons if b.n == n), None)
        frames_ = [int(button.locator[0])] if button is not None else None
        try:
            rows = [r for r in apply_form.control_scan(self.page, frames_)
                    if r.get("kind") == "unreadable"]
            # the required empty ones from a scan of their own, whose filter
            # runs before the scan's 40-row cut: no number of other controls
            # before one hides it (SP6 review M6)
            blocking = [r for r in apply_form.control_scan(self.page, frames_, required_only=True)
                        if r.get("kind") == "unreadable" and r.get("required") and r.get("empty")]
        except Exception:       # noqa: BLE001  (a page double)
            return
        rows += [r for r in blocking if r not in rows]
        if not rows:
            return
        self._decide("unreadable", f"{len(rows)} control(s) the run cannot read: "
                                   + _cap("; ".join(f"{r.get('label')} ({r.get('why')})"
                                                    for r in rows), 200),
                     required=[r.get("label") for r in rows if r.get("required")])
        if blocking:
            raise _Parked("needs_human", _control_words(blocking[0]))

    def _judged_roles(self, digest: apply_form.FormDigest) -> dict[int, str]:
        """n -> the role the judge gave each of the page's buttons."""
        out = {}
        for b in digest.buttons:
            role, _ = apply_judge._choice_of(self._last_answers, f"button_{b.n}_role")
            if role:
                out[b.n] = role
        return out

    def _required_filled(self, plan: FillPlan) -> list[tuple[int, str]]:
        """The locators of the required fields this page's fill put a value in."""
        acted = {tuple(loc) for loc in self._filled_here}
        return [pf.locator for pf in plan.fields
                if pf.required and tuple(pf.locator) in acted]

    def _submit_apart(self, digest: apply_form.FormDigest, plan: FillPlan) -> bool:
        """ADV-05: with both a judged advance and a judged submit, does the
        submit sit apart from the required fields this page filled (another
        form: a feedback box, a talent network sign-up)?"""
        submit, advance = plan.buttons.get("submit"), plan.buttons.get("advance")
        fields = self._required_filled(plan)
        button = next((b for b in digest.buttons if submit and b.n == submit[0]), None)
        if advance is None or button is None or not fields:
            return False
        try:
            verdict, _ = apply_form.same_scope(self.page, button.locator, fields)
        except Exception:       # noqa: BLE001  (a page double)
            return False
        return verdict == "apart"

    def _own_submit(self, digest: apply_form.FormDigest, plan: FillPlan) -> FillPlan:
        """ADV-05: of the buttons judged submit at `BUTTON_SUBMIT_MIN_CONF`
        (a form's own and a feedback box's), the one that sits with the
        required fields this page filled holds the role, whichever the
        judge rated higher."""
        fields = self._required_filled(plan)
        held = plan.buttons.get("submit")
        if not fields or held is None:
            return plan
        judged = [(b, conf) for b in digest.buttons
                  for role, conf in [apply_judge._choice_of(self._last_answers,
                                                            f"button_{b.n}_role")]
                  if role == "submit" and conf >= apply_judge.BUTTON_SUBMIT_MIN_CONF]
        if len(judged) < 2:
            return plan
        own = []
        for b, conf in judged:
            try:
                verdict, _ = apply_form.same_scope(self.page, b.locator, fields)
            except Exception:       # noqa: BLE001  (a page double)
                return plan
            if verdict == "same":
                own.append((b, conf))
        if len(own) == 1 and own[0][0].n != held[0]:
            b, conf = own[0]
            self._decide("own_submit", f"{_cap(b.text, 40)} sits with the page's fields; it "
                                       f"holds the submit role over "
                                       f"{_cap(_button_text(digest, held[0]), 40)}")
            return dataclasses.replace(plan, buttons={**plan.buttons, "submit": (b.n, conf)})
        return plan

    def _still_disabled(self, digest: apply_form.FormDigest, plan: FillPlan,
                        verification: list[VerifyResult], n: int, rec: dict, *,
                        gate: bool = False
                        ) -> tuple[apply_form.FormDigest, FillPlan, list[VerifyResult], int]:
        """The way on the filled page chose (study G10): when its button is
        still disabled once the fill settled, and stays so for
        `DISABLED_WAIT_S`, the fields the form reports as invalid are
        repaired (`_repair`, up to `REPAIR_ROUNDS`); a button that stays
        disabled parks the job with the fields that keep it so as the
        evidence: the form's own report (a control that would not validate,
        a required control left empty) and the boxes the plan left blank.
        Nothing is clicked. Returns the page, its plan, its verification and
        the button's `n` as the page now numbers it (the same control,
        `_same_button`).

        A CAPTCHA checkbox on the page comes first (SP6 review I4): a way on
        disabled until the person ticks it is the gate's to hand over
        (`gate`: `_submit_gate`'s CAPTCHA path, in either mode), and an
        advance waits for the person's tick like the account step does."""
        button = next((b for b in digest.buttons if b.n == n), None)
        if button is None or not button.disabled:
            return digest, plan, verification, n
        if self._human_check_showing(checkbox=True):
            if gate:
                self._decide("disabled_captcha", f"the {_cap(button.text, 40)} button is disabled "
                                                 "and a CAPTCHA checkbox is on the page: the "
                                                 "gate's CAPTCHA path")
                return digest, plan, verification, n
            self._wait_for_human_check("a CAPTCHA check is on the form before the step",
                                       checkbox=True)
        for round_no in range(REPAIR_ROUNDS + 1):
            loc = apply_form.resolve(self.page, button.locator)
            deadline = time.monotonic() + DISABLED_WAIT_S
            while True:
                try:
                    if loc.count() == 1 and loc.first.is_enabled():
                        return digest, plan, verification, button.n
                except Exception:       # noqa: BLE001  (a page double; the click finds out)
                    return digest, plan, verification, button.n
                if time.monotonic() >= deadline:
                    break
                self.page.wait_for_timeout(200)
            invalid: list = []
            empty: list = []
            try:
                invalid = apply_form.validity_report(self.page, button.locator,
                                                     self._filled_here)["invalid"]
                empty = [r for r in apply_form.control_scan(self.page,
                                                            [int(button.locator[0])],
                                                            required_only=True)
                         if r.get("required") and r.get("empty")]
            except Exception as e:      # noqa: BLE001  (a page double)
                self._trace("error", step="still_disabled", error=type(e).__name__)
            if invalid and round_no < REPAIR_ROUNDS:
                problems = [{**r, "text": r.get("message") or "", "kind": "invalid"}
                            for r in invalid]
                who = self._button_identity(digest, button.n)
                digest, plan, verification = self._repair(digest, plan, verification,
                                                          problems, rec, why="disabled")
                if self._repaired:
                    n = self._same_button(digest, who)
                    if n is None:
                        raise self._button_lost(who)
                    button = next(b for b in digest.buttons if b.n == n)
                    continue
            break
        blank = [pf.label for pf in plan.fields if pf.action == "skip" and pf.label]
        text = _cap(button.text, 60)
        self._decide("still_disabled", f"the {text} button stays disabled after the fill",
                     invalid=invalid[:5], empty=[r.get("label") for r in empty[:5]], blank=blank)
        missing = [r for r in invalid if r.get("reason") == "valueMissing"] + empty
        if missing:
            label = " ".join(str(missing[0].get("label") or "a field").split())[:80]
            raise _Parked("needs_human", f"required field without an answer: {label} (the "
                                         f"{text} button stays disabled after the fill)")
        if blank:
            # the page wants a box the plan left blank (SP6 review I4): the
            # unanswered fields are the evidence, in the policy's words
            more = f"; also blank: {_cap(', '.join(blank[1:]), 100)}" if blank[1:] else ""
            raise _Parked("needs_human", f"required field without an answer: {blank[0]} (the "
                                         f"{text} button stays disabled after the fill{more})")
        rows = [_invalid_words(r) for r in invalid[:2]]
        raise _Parked("needs_human", f"the {text} button stays disabled after the fill"
                                     + (f" ({_cap('; '.join(rows), 220)})" if rows else ""))

    # -- the same control after a repair (SP6 review I2) ---------------------------------------

    def _button_identity(self, digest: apply_form.FormDigest, n: int) -> dict[str, Any] | None:
        """Who button `n` is on the live page, read before a repair: its
        locator, its text, its identity (`apply_form.IDENT_FN_JS`: tag, type,
        id, name, aria-label, test attributes, then its label or its box's
        words), its form (`apply_form.form_index`) and, outside a form, the
        fields of the lowest box above it that holds any (`_BUTTON_HOME_JS`)."""
        b = next((x for x in digest.buttons if x.n == n), None)
        if b is None:
            return None
        who: dict[str, Any] = {"locator": tuple(b.locator), "text": " ".join(b.text.split()),
                               "ident": "", "form": None, "home": []}
        try:
            loc = apply_form.resolve(self.page, b.locator)
            if loc.count() == 1:
                who["ident"] = str(loc.first.evaluate(apply_form.IDENT_FN_JS, timeout=2_000))
                who["form"] = apply_form.form_index(self.page, [b.locator])[0]
                if who["form"][1] == -1:
                    who["home"] = list(loc.first.evaluate(_BUTTON_HOME_JS, timeout=2_000) or [])
        except Exception:       # noqa: BLE001  (a page double; `_same_button` finds none)
            pass
        return who

    def _same_button(self, digest: apply_form.FormDigest, who: dict[str, Any] | None) -> int | None:
        """The `n` of the button in `digest` that is the control `who`
        (`_button_identity`) names, read live: the same text, the same
        attributes (the identity without its label part: a form that writes
        its message into the button's own box changes those words, SP6
        review R2-I1), and the same form, or outside a form a box that holds
        a field of the one it sat in. Among several, the one whose label
        words still agree, then the one at the same locator. None when no
        button is that control: another form's button with the same words
        ("Submit" of a talent-community box) never is."""
        if who is None or not who.get("ident"):
            return None
        rows = [b for b in digest.buttons if " ".join(b.text.split()) == who["text"]]
        found = []
        for b in rows:
            live = self._button_identity(digest, b.n)
            if not (live and live["ident"]) or live["form"] != who["form"]:
                continue
            if _ident_attrs(live["ident"]) != _ident_attrs(who["ident"]):
                continue
            if who["form"][1] == -1 and who.get("home") \
                    and not set(live.get("home") or []) & set(who["home"]):
                continue
            found.append((not apply_form.same_ident(live["ident"], who["ident"]),
                          tuple(b.locator) != who["locator"], b.n))
        return min(found)[2] if found else None

    def _button_lost(self, who: dict[str, Any] | None) -> _Parked:
        """The park when the control a step clicks cannot be found again after
        a repair: nothing is clicked, never a look-alike."""
        text = _cap((who or {}).get("text") or "the button", 60)
        self._decide("button_lost", f"after the repair no button is the {text} the step "
                                    "clicks (the same identity in the same form); nothing "
                                    "was clicked")
        return _Parked("needs_human", f"the {text} button could not be found again after the "
                                      f"form's fields were repaired; nothing was clicked")

    # -- the form's refusals and their repair (ADV-02, ADV-06) -------------------------------

    def _form_state(self, digest: apply_form.FormDigest, n: int) -> dict[str, Any]:
        """The page just before a click on button `n`: its URL, its form's
        fields and the error texts it shows (a baseline for `_form_problems`)."""
        button = next((b for b in digest.buttons if b.n == n), None)
        try:
            errors = {e["text"] for e in apply_form.validity_report(
                self.page, button.locator if button is not None else None,
                self._filled_here)["errors"]}
        except Exception:       # noqa: BLE001  (a page double)
            errors = set()
        try:
            values = apply_form.box_values(self.page, self._typed_boxes())
        except Exception:       # noqa: BLE001  (a page double)
            values = []
        # the boxes' values stay in memory for the read after the click; they
        # are never written to the trace or the record
        return {"url": str(self.page.url), "fields": _fields_sig(digest), "errors": errors,
                "values": values}

    def _form_problems(self, digest: apply_form.FormDigest, n: int,
                       before: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Did the form refuse the click on button `n` (ADV-02): the page is
        the same form (the same URL and fields) and it reports a control that
        would not validate or marked invalid, or shows an error text it did
        not show before the click. Each problem: {label, message, reason,
        text, ident, name, kind} ("invalid" or "error"); [] when the page
        moved on or reports nothing."""
        try:
            if str(self.page.url) != str(before.get("url") or ""):
                return []
            fresh = self._drop_foreign_controls(self._extract())
        except Exception:       # noqa: BLE001  (a page mid-navigation: it moved on)
            return []
        if _fields_sig(fresh) != tuple(before.get("fields") or ()):
            return []
        if any(before.get("values") or []) and not self._holds_typed(before):
            # the same step back, emptied: the site took nothing and asks
            # nothing of a field (READ-04's "did not advance"), never a refusal
            return []
        button = next((b for b in digest.buttons if b.n == n), None)
        try:
            report = apply_form.validity_report(
                self.page, button.locator if button is not None else None, self._filled_here)
        except Exception:       # noqa: BLE001  (a page double)
            return []
        old = before.get("errors") or set()
        out = [{**r, "text": r.get("message") or "", "kind": "invalid"}
               for r in report["invalid"]]
        out += [{"label": "", "message": e["text"], "reason": "error", "text": e["text"],
                 "ident": e.get("ident") or "", "name": e.get("name") or "", "kind": "error"}
                for e in report["errors"] if e["text"] not in old]
        return out

    def _problem_fields(self, digest: apply_form.FormDigest, problems: list[dict[str, Any]],
                        actable: set[int] | None = None) -> dict[int, list[dict[str, Any]]]:
        """n -> the problems that name field `n`. In code first: the
        control's identity (`apply_form.same_ident`), its name or id, its
        label. The messages no control names go to the judge in one request
        (`apply_judge.error_questions`): a mapping at `FIELD_MAP_MIN_CONF` or
        above names its field; one under it, `none` or a dropped one names
        none, and the message is only evidence.

        A confident wrong mapping is possible (SP6 review M1), so: a field
        the judge named for a message on this form is never offered for it
        again (a later round, the form still showing the message, asks a
        fresh question without it); a message the first request maps to no
        field gets one second look, asked in other words; and a problem the
        judge mapped carries `mapped` (its confidence), which `_repair_named`
        never parks on alone. A message both looks leave unmapped names the
        field whose whole label its own words hold, when exactly one does
        (`field_named_in`): `by_label`, for a repair only, never a park. With `actable` (the fields a repair can put
        right), a judged field outside it (an upload made, a password) counts
        as no mapping: the second look asks without it (SP6 review R2)."""
        out: dict[int, list[dict[str, Any]]] = {}
        loose: list[dict[str, Any]] = []
        for p in problems:
            f = None
            if p.get("ident"):
                f = next((x for x in digest.fields if x.ident
                          and apply_form.same_ident(x.ident, str(p["ident"]))), None)
            if f is None and p.get("name"):
                f = next((x for x in digest.fields if x.id_or_name == p["name"]), None)
            if f is None and p.get("label") and p.get("shown", True):
                # by its label only for a control a person sees: a hidden box
                # behind a widget (a rich-text editor's textarea) is no field
                # the run can put right through the widget's label
                want = " ".join(str(p["label"]).split()).lower()
                f = next((x for x in digest.fields
                          if " ".join(x.label.split()).lower() == want), None)
            if f is not None:
                out.setdefault(f.n, []).append(p)
            elif p.get("text"):
                loose.append(p)
        if loose and digest.fields:
            sig = _fields_sig(digest)
            keys = [(sig, " ".join(str(p["text"]).split())) for p in loose]
            exclude = [{x.n for x in digest.fields
                        if _label_key(x.label) in self._error_tried.get(k, set())} for k in keys]
            named = self._map_messages(loose, digest, exclude)
            if actable is not None:
                for i in range(len(loose)):
                    n = self._field_mapped(digest, named.get(i), exclude[i])
                    if n is not None and n not in actable:
                        exclude[i] = exclude[i] | {n}
            unsure = [i for i in range(len(loose))
                      if self._field_mapped(digest, named.get(i), exclude[i]) is None]
            if unsure:
                # the second look (M1): the messages mapped to no field, asked
                # in other words, a fresh judgment
                more = self._map_messages([loose[i] for i in unsure], digest,
                                          [exclude[i] for i in unsure], again=True)
                for j, i in enumerate(unsure):
                    if j in more:
                        named[i] = more[j]
            fields: dict[int, int | None] = {}
            by_label: list[int] = []
            for i, p in enumerate(loose):
                n = self._field_mapped(digest, named.get(i), exclude[i])
                row = {**p, "mapped": round(named[i][1], 2)} if n is not None else None
                if n is None:
                    # both looks named no field: the message's own words, when
                    # they name exactly one field's whole label, for a repair
                    # only, never a park (`by_label`)
                    n = field_named_in(str(p.get("text") or ""), digest.fields)
                    if n is not None and (n in exclude[i]
                                          or (actable is not None and n not in actable)):
                        n = None
                    row = {**p, "mapped": 0.0, "by_label": True} if n is not None else None
                    if n is not None:
                        by_label.append(i)
                fields[i] = n
                if n is None:
                    continue
                out.setdefault(n, []).append(row)
                label = next(x.label for x in digest.fields if x.n == n)
                self._error_tried.setdefault(keys[i], set()).add(_label_key(label))
            self._decide("errors_mapped", f"{len(loose)} message(s) no control names, mapped by "
                                          "the judge",
                         messages=[_cap(p["text"], 80) for p in loose], fields=fields,
                         looked_again=len(unsure), by_label=by_label,
                         excluded={i: sorted(e) for i, e in enumerate(exclude) if e})
        return out

    def _map_messages(self, loose: list[dict[str, Any]], digest: apply_form.FormDigest,
                      exclude: list[set[int]], *,
                      again: bool = False) -> dict[int, tuple[int | None, float]]:
        """One request mapping the messages `loose` to the page's fields
        (`apply_judge.error_questions`): i -> (n or None, confidence)."""
        state, questions = apply_judge.error_questions([p["text"] for p in loose], digest.fields,
                                                       exclude=exclude, again=again)
        got = {k: v for k, v in self.r.jev.judge(state, questions).items() if k in questions}
        self.trace.add_answers(got)
        return apply_judge.read_error_fields(got, len(loose))

    @staticmethod
    def _field_mapped(digest: apply_form.FormDigest, got: tuple[int | None, float] | None,
                      excluded: set[int]) -> int | None:
        """The field a message's mapping names: one of the page's fields,
        not excluded for the message, at `FIELD_MAP_MIN_CONF` or above."""
        n, conf = got if got is not None else (None, 0.0)
        if n is None or conf < apply_judge.FIELD_MAP_MIN_CONF or n in excluded:
            return None
        return n if any(x.n == n for x in digest.fields) else None

    def _repair(self, digest: apply_form.FormDigest, plan: FillPlan,
                verification: list[VerifyResult], problems: list[dict[str, Any]], rec: dict, *,
                why: str = "") -> tuple[apply_form.FormDigest, FillPlan, list[VerifyResult]]:
        self._repaired = False
        return self._repair_named(digest, plan, verification, problems, rec, why=why)

    def _repair_named(self, digest: apply_form.FormDigest, plan: FillPlan,
                      verification: list[VerifyResult], problems: list[dict[str, Any]],
                      rec: dict, *, why: str = "",
                      depth: int = 0) -> tuple[apply_form.FormDigest, FillPlan, list[VerifyResult]]:
        """ADV-02's repair of the fields the form refused: first the page is
        read again (a field it revealed is filled, `_fill_revealed`); then
        each field a problem names (`_problem_fields`): one the plan left
        blank is asked again as required (its own mapping request, the
        option picks, the second look) and filled, or the job parks on it
        ("required field without an answer", with the form's words); one
        the fill put a value in is typed again in the shape the message asks
        (`apply_fill.repair`: bare digits, a date format, key by key) and
        verified. Returns the page, its plan and its verification.

        A blank field only the judge's reading of a message named (SP6
        review M1: the reading can be confidently wrong) is filled when it
        has an answer and never parked on at once when it has none: the
        messages that named it are mapped once more without it (`depth`
        1), and the job parks on it only when that finds nothing else on
        the page to act on."""
        fresh = self._drop_foreign_controls(self._extract())
        revealed = new_fields(digest, fresh)
        if revealed:
            digest, plan, verification = self._fill_revealed(digest, fresh, revealed, plan,
                                                             verification, rec)
        by_pf = {pf.n: pf for pf in plan.fields}
        named = self._problem_fields(digest, problems, actable={
            n for n, pf in by_pf.items()
            if pf.action not in ("upload", apply_judge.PASSWORD_ACTION)})
        blank = [n for n in named if n in by_pf and by_pf[n].action not in _ACTED
                 and by_pf[n].action != apply_judge.PASSWORD_ACTION]
        typed = [n for n in named if n in by_pf and by_pf[n].action in _ACTED
                 and by_pf[n].action != "upload"]
        # named by the judge's reading of a message alone (M1)
        soft = {n for n in blank if all("mapped" in p for p in named[n])}
        self._decide("repair", f"the form refused the step{f' ({why})' if why else ''}: "
                               f"{len(problems)} problem(s); {len(blank)} blank and {len(typed)} "
                               f"filled field(s) named",
                     problems=[_cap(p.get("text") or p.get("label") or "", 80) for p in problems],
                     blank=[by_pf[n].label for n in blank], typed=[by_pf[n].label for n in typed],
                     judged=[by_pf[n].label for n in sorted(soft)], look=depth + 1)
        if not blank and not typed and not revealed:
            return digest, plan, verification
        acted = bool(revealed) or bool(typed)
        missed: list[int] = []
        says: dict[int, str] = {}
        if blank:
            says = {n: _cap(named[n][0].get("text") or "", 100) for n in blank}
            sub = dataclasses.replace(digest, buttons=[], fields=[
                dataclasses.replace(f, required=True) for f in digest.fields if f.n in blank])
            answers = self._map(sub, {}, "application_form", discover=False)
            more = apply_judge.plan(sub, self.catalog, answers,
                                    generation_enabled=bool(self.r.settings["auto_apply_generate"]),
                                    company=self._company())
            more = self._complete_option_plan(sub, answers, more, rec)
            _spare_judged(more, soft)
            self._last_answers = {**self._last_answers, **answers}
            try:
                more_verification = self._fill_and_verify(sub, more, rec)
            except _Parked as p:
                n = next((pf.n for pf in more.fields if pf.label and pf.label in p.reason), None)
                said = f" (the form says: {says[n]})" if n in says and says[n] else ""
                raise _Parked(p.status, p.reason + said, p.tab_note) from None
            done = {pf.n: pf for pf in more.fields}
            plan = dataclasses.replace(plan, fields=[done.get(pf.n, pf) for pf in plan.fields])
            verification = [v for v in verification if v.n not in done] + more_verification
            acted = acted or any(pf.action in _ACTED for pf in more.fields)
            missed = [pf.n for pf in more.fields if pf.n in soft and pf.action not in _ACTED
                      and not all(p.get("by_label") for p in named[pf.n])]
        self._repaired = self._repaired or acted    # something on the page was acted on
        if typed:
            fixed = []
            for n in typed:
                pf = by_pf[n]
                hint = " ".join(str(p.get("text") or "") for p in named[n])
                fixed.append(apply_fill.repair(self.page, pf, hint))
            self._trace_fill(FillPlan(fields=[by_pf[n] for n in typed]), fixed, [], retry=True)
            again = {v.n: v for v in self._verify(fixed, _drafts(plan), _picks(plan),
                                                  _shaped(plan, digest))}
            self._last_filled.update({f.n: f for f in fixed})
            verification = [again.get(v.n, v) for v in verification]
            _record_verification(rec, list(again.values()))
        if missed and depth == 0:
            # M1: the judge named a blank field that has no answer; its
            # messages are mapped once more without it (a fresh question).
            # The field is kept: a message still shown when the rounds end
            # parks on it (R2-I4)
            for n in missed:
                for p in named[n]:
                    self._spared[_message_key(p.get("text") or "")] = (by_pf[n].label,
                                                                      says.get(n, ""))
            seen: set[str] = set()
            again_problems = []
            for n in missed:
                for p in named[n]:
                    if p["text"] not in seen:
                        seen.add(p["text"])
                        again_problems.append({k: v for k, v in p.items() if k != "mapped"})
            digest, plan, verification = self._repair_named(
                digest, plan, verification, again_problems, rec, why=why, depth=1)
            if not self._repaired:
                # nothing else on the page answers the message: the field the
                # judge named is the evidence, in the policy's words
                label = by_pf[missed[0]].label
                said = f" (the form says: {says[missed[0]]})" if says.get(missed[0]) else ""
                raise _Parked("needs_human", f"required field without an answer: {label}{said}")
        return digest, plan, verification

    def _spared_park(self, texts: list[str]) -> _Parked | None:
        """SP6 review R2-I4: the rounds are over and the form still shows a
        message whose field only the judge named and the sheet cannot answer
        (`_spared`): the park names that field, in the policy's words."""
        for text in texts:
            got = self._spared.get(_message_key(text or ""))
            if got:
                label, says = got
                self._decide("spared_field", f"the form still shows {_cap(text, 80)!r}; the "
                                             f"field the judge named for it, {label}, has no "
                                             "answer")
                said = f" (the form says: {says})" if says else ""
                return _Parked("needs_human", f"required field without an answer: {label}{said}")
        return None

    def _refused_words(self, problems: list[dict[str, Any]]) -> str:
        """A park's evidence after the repair rounds: each problem in the
        form's words (`_invalid_words`, or the error text)."""
        rows = []
        for p in problems[:3]:
            if p.get("kind") == "invalid":
                rows.append(_invalid_words(p))
            else:
                rows.append(f"the form says: {_cap(p.get('text') or '', 100)}")
        return _cap("; ".join(rows), 260)

    def _advance(self, digest: apply_form.FormDigest, plan: FillPlan,
                 verification: list[VerifyResult], rec: dict, n: int, conf: float) -> None:
        """Click the page's advance (`_click`) and read what the form said
        (ADV-02, ADV-06): a form that refused the step (`_form_problems`) is
        repaired (`_repair`) and the advance clicked once more, at most
        `REPAIR_ROUNDS` times, never a second time on a click the form
        refused without a repair; then the job parks naming each field and
        its message ("required field without an answer" when one was left
        empty)."""
        text = _button_text(digest, n)
        carried: set[str] = set()
        for round_no in range(REPAIR_ROUNDS + 1):
            who = self._button_identity(digest, n)
            before = self._form_state(digest, n)
            # the messages the last round's repair answered are no baseline:
            # one the form shows again after this click is its refusal still
            # (a banner a page writes the same words into, SP6 review M1)
            before["errors"] = set(before.get("errors") or set()) - carried
            problems: list[dict[str, Any]] = []

            def _check(d=digest, b=before, out=problems) -> bool:
                out[:] = self._form_problems(d, n, b)
                return bool(out)
            self._click(digest, n, "advance", rec, conf=conf, refused_by_form=_check)
            if not problems:
                return
            self._decide("form_refused", f"the form refused the {_cap(text, 40)} step "
                                         f"(round {round_no + 1})",
                         problems=[_cap(p.get("text") or p.get("label") or "", 80)
                                   for p in problems])
            if round_no == REPAIR_ROUNDS:
                break
            digest, plan, verification = self._repair(digest, plan, verification, problems, rec)
            if not self._repaired:
                # nothing the run can put right: never the same click again
                # here; on a first refusal the loop reads the page as it now
                # stands (its message too), as it did before SP6, and its own
                # "page did not advance" ends a step that comes back the same
                if round_no == 0:
                    return
                break
            carried = {str(p.get("text") or "") for p in problems if p.get("kind") == "error"}
            # the same control as the page now numbers it (SP6 review I2)
            n = self._same_button(digest, who)
            if n is None:
                raise self._button_lost(who)
        missing = [p for p in problems if p.get("reason") == "valueMissing"]
        if missing:
            label = " ".join(str(missing[0].get("label") or "a field").split())[:80]
            raise _Parked("needs_human", f"required field without an answer: {label} (the form "
                                         f"refused the {_cap(text, 40)} step)")
        spared = self._spared_park([str(p.get("text") or "") for p in problems])
        if spared is not None:
            raise spared
        raise _Parked("needs_human", f"the form refused the {_cap(text, 40)} step after "
                                     f"{REPAIR_ROUNDS} repair(s): {self._refused_words(problems)}")

    def _form_entry(self, digest: apply_form.FormDigest, plan: FillPlan, step: str,
                    button: tuple[int, float] | None) -> apply_form.Button | None:
        """The Apply entry a form step clicks instead of the gate (INV-01),
        or None. With nothing of the application on this page or before it,
        an Apply opens the form (Workday's "Apply Manually" in the start
        dialog, read as a form, its Apply judged the advance): the judged
        `apply_entry`, else an Apply-worded advance or gate button
        (`form_step_entry`). After a fill, an Apply-worded button apart from
        the fields is refused at the gate (`_apply_button_why`), never
        clicked as an entry: a form's own Apply laid out apart from its
        fields would send outside the gate."""
        if step == "advance":
            return None
        park_mode = not self.r.settings.get("auto_apply_submit", True)
        b = form_entry_choice(self.page, digest, plan, park_mode=park_mode,
                              filled=bool(self._filled_here or self._filled_any
                                          or self.form_filled))
        if b is not None:
            self._decide("entry_on_form_step", "nothing was filled on this page or an "
                                               "earlier one; its Apply opens the form",
                         button=b.n, text=b.text)
        return b

    def _complete_option_plan(self, digest: apply_form.FormDigest, answers: dict,
                              plan: FillPlan, rec: dict) -> FillPlan:
        """The option picks the first request could not carry, around the
        second look (`_reask`): a required field whose mapping came back
        dropped or weak is asked alone once more, then the picks, then a
        required field whose pick came back dropped or weak."""
        plan = self._reask(digest, answers, plan, rec, "source")
        s2, q2 = apply_judge.option_questions(digest, plan)
        if q2:
            picks = self.r.jev.judge(s2, q2)
            answers.update(picks)
            plan = apply_judge.plan(digest, self.catalog, answers,
                                    generation_enabled=bool(self.r.settings["auto_apply_generate"]),
                                    company=self._company())
            rec["flags"] = dict(plan.flags)
            self._trace("option_picks", answers=apply_trace.answers_json(picks),
                        plan=apply_trace.plan_json(plan))
        return self._reask(digest, answers, plan, rec, "pick")

    def _reask(self, digest: apply_form.FormDigest, answers: dict, plan: FillPlan, rec: dict,
               what: str) -> FillPlan:
        """The second look (SP5): the required fields the plan skipped for a
        mapping the first request dropped or left under its floor
        (`apply_judge.reask_targets`) are asked once more, all in one request
        (`apply_judge.reask_questions`, review M11); the answers replace the
        first look's and the plan is made again. A re-read of the same page
        (its URL path and its fields) reuses the second look's answers and
        makes no request. A field the data cannot answer still parks: the
        second look names `leave_blank` or `no_match` too; a consent tick
        read under its floor again still parks (review I1)."""
        targets = apply_judge.reask_targets(digest, self.catalog, answers, plan, what=what,
                                            company=self._company())
        if not targets:
            return plan
        key = (urlsplit(str(getattr(self.page, "url", "") or "")).path, _fields_sig(digest),
               what, tuple(targets))
        cached = key in self._reask_cache
        if cached:
            got = dict(self._reask_cache[key])
        else:
            s, q = apply_judge.reask_questions(digest, self.catalog, plan, targets, what=what,
                                               job=self.entry)
            got = {k: v for k, v in self.r.jev.judge(s, q).items() if k in q}
            self._reask_cache[key] = dict(got)
        answers.update(got)
        plan = apply_judge.plan(digest, self.catalog, answers,
                                generation_enabled=bool(self.r.settings["auto_apply_generate"]),
                                company=self._company())
        rec["flags"] = dict(plan.flags)
        labels = {f.n: f.label for f in digest.fields}
        self._decide("reask", f"asked {len(targets)} required field(s) once more ({what})"
                              f"{', the same page again: its answers reused' if cached else ''}: "
                              f"{_cap(', '.join(labels.get(n, '') for n in targets), 160)}",
                     fields=targets, answered=sorted(got), reused=cached)
        self._trace("reask", what=what, fields=targets, answers=apply_trace.answers_json(got),
                    reused=cached, plan=apply_trace.plan_json(plan))
        return plan

    def _fill_and_verify(self, digest: apply_form.FormDigest, plan: FillPlan,
                         rec: dict) -> list[VerifyResult]:
        self._resolve_generation(digest, plan, rec)
        for question, context in plan.missing:
            self._add_missing(question, context)
        if plan.park_reason:
            raise _Parked("needs_human", plan.park_reason)
        # the text boxes the plan leaves alone, as they read before the fill:
        # one the page writes into during the fill (a resume parser's guess)
        # is checked after it (`_page_writes`, FILL-03)
        idle = [pf for pf in plan.fields if pf.action not in _ACTED
                and pf.action != apply_judge.PASSWORD_ACTION
                and _typed_box(digest, pf.n)]
        try:
            idle_before = apply_form.box_values(self.page, [pf.locator for pf in idle])
        except Exception:       # noqa: BLE001  (a page double)
            idle_before = [None] * len(idle)
        self._idle += list(zip(idle, idle_before))
        errors: list[dict] = []
        outcomes: list[dict] = []
        filled = apply_fill.apply(self.page, plan, deadline=self.deadline, clock=self.r.clock,
                                  errors=errors, outcomes=outcomes)
        self._trace_fill(plan, filled, errors, outcomes=outcomes)
        rec.setdefault("fill_outcomes", []).extend(outcomes)
        locators = {pf.n: pf.locator for pf in plan.fields}
        self._filled_here += [locators[f.n] for f in filled
                              if f.n in locators and str(f.value or "").strip()]
        self._filled_any = self._filled_any or bool(self._filled_here)
        if _fills_the_application(digest, plan, filled):
            self.form_filled = True
        drafts = _drafts(plan)
        shaped = _shaped(plan, digest)
        verification = self._verify(filled, drafts, _picks(plan), shaped)
        verification = self._retry_failed(plan, filled, verification, drafts, shaped)
        self._last_filled.update({f.n: f for f in filled})
        self._record_fill(rec, digest, plan, filled, verification)
        self._trace("verify", results=[{"n": v.n, "label": v.label, "ok": v.ok,
                                        "p_correct": v.p_correct,
                                        "p_placeholder": v.p_placeholder}
                                       for v in verification])
        still = [v.label for v in verification if not v.ok
                 and any(pf.n == v.n and pf.required for pf in plan.fields)]
        if still:
            raise _Parked("needs_human", "could not verify: " + ", ".join(still))
        return verification

    def _after_fill(self, digest: apply_form.FormDigest, plan: FillPlan,
                    verification: list[VerifyResult],
                    rec: dict) -> tuple[apply_form.FormDigest, FillPlan, list[VerifyResult]]:
        """The page read again once the fill settles, before a button is
        chosen, up to `FILL_ROUNDS_MAX` times until it holds:

        - a value the page changed after the fill put it in (FILL-03: a
          resume parser, a profile lookup after the email) is put back once
          and verified again (`_page_changes`); a text box the plan left
          alone that the page wrote into during the fill is checked against
          the sheet and cleared when it is wrong (`_page_writes`);
        - a field the fill revealed (FILL-10: "Yes" opens "Please explain")
          is mapped, planned, filled and verified like the page's own
          (`_fill_revealed`);
        - buttons the fill enabled, revealed or renamed (ADV-01, study G10:
          a Next that waits for a privacy tick, a disabled Apply) are judged
          again (`_judge_buttons`); unchanged ones keep their roles and take
          the fresh read's locators.

        Returns the page as it now stands, its plan and its verification."""
        if self.page is None:           # a unit test's run with no page
            return digest, plan, verification
        url = str(self.page.url)
        for _ in range(FILL_ROUNDS_MAX):
            info = apply_fill.settle(self.page, CLICK_TIMEOUT_S)
            if str(self.page.url) != url:
                self._check_host(self.page.url)     # the fill took the page elsewhere
            fresh = self._drop_foreign_controls(self._extract())
            verification = self._page_changes(digest, plan, verification, rec)
            self._page_writes(rec)
            revealed = new_fields(digest, fresh)
            if revealed:
                self._decide("revealed", f"the fill revealed {len(revealed)} field(s): "
                                         f"{_cap(', '.join(f.label for f in revealed), 160)}; "
                                         + settled_words(info),
                             fields=[f.label for f in revealed])
                digest, plan, verification = self._fill_revealed(digest, fresh, revealed, plan,
                                                                 verification, rec)
                continue
            digest, plan = self._judge_buttons(digest, fresh, plan)
            break
        return digest, plan, verification

    def _page_changes(self, digest: apply_form.FormDigest, plan: FillPlan,
                      verification: list[VerifyResult], rec: dict) -> list[VerifyResult]:
        """FILL-03: every value this page's fill put in, read again; the ones
        the page changed since (an upload never counts: it is never sent
        twice) are put in once more and verified again. A field the page
        changes a second time keeps the page's value and its verification."""
        by_n = {pf.n: pf for pf in plan.fields}
        picks, shaped = _picks(plan), _shaped(plan, digest)

        def holds(n: int, value: str, was: str) -> bool:
            if n in picks:
                return pick_holds(value, *picks[n])
            if n in shaped:
                return shaped_holds(value, *shaped[n])
            return _same_text(value, was)
        changed = []
        for n, f in list(self._last_filled.items()):
            pf = by_n.get(n)
            if pf is None or pf.action == "upload" or n in self._refilled:
                continue
            if (n in picks or n in shaped) and not holds(n, f.value, f.value):
                continue        # it never held: the fill's own retry had its turn
            now = apply_fill.read_back(self.page, pf)
            if not holds(n, now, f.value):
                changed.append(pf)
        if not changed:
            return verification
        self._refilled |= {pf.n for pf in changed}
        self._decide("page_changed", f"the page changed {len(changed)} value(s) after the fill "
                                     f"({_cap(', '.join(pf.label for pf in changed), 160)}); "
                                     "put in once more", fields=[pf.label for pf in changed])
        errors: list[dict] = []
        again = apply_fill.apply(self.page, FillPlan(fields=changed), deadline=self.deadline,
                                 clock=self.r.clock, errors=errors)
        self._trace_fill(FillPlan(fields=changed), again, errors, retry=True)
        results = {v.n: v for v in self._verify(again, _drafts(plan), _picks(plan),
                                                _shaped(plan, digest))}
        self._last_filled.update({f.n: f for f in again})
        verification = [results.get(v.n, v) for v in verification]
        _record_verification(rec, list(results.values()))
        still = [v.label for v in verification if not v.ok
                 and any(pf.n == v.n and pf.required for pf in plan.fields)]
        if still:
            raise _Parked("needs_human", "could not verify: " + ", ".join(still)
                          + " (the page changed the value after the fill)")
        return verification

    def _page_writes(self, rec: dict) -> None:
        """FILL-03: a text box the plan left alone that the page wrote into
        during the fill (a resume parser's guess at a middle name or a past
        employer) is read against the sheet by the judge; one it reads as
        wrong is cleared. A value the box held before the fill (the site's
        own, an account's profile) is left as it is."""
        if not self._idle:
            return
        idle, self._idle = self._idle, []
        try:
            now = apply_form.box_values(self.page, [pf.locator for pf, _ in idle])
        except Exception:       # noqa: BLE001  (a page double)
            return
        wrote = [(pf, str(v)) for (pf, was), v in zip(idle, now)
                 if v is not None and str(v).strip() and str(v) != str(was or "")]
        if not wrote:
            return
        rows = [{"n": pf.n, "label": pf.label, "value": value} for pf, value in wrote]
        state, questions = apply_judge.verify_questions(rows, self.catalog.verification_excerpt())
        results = apply_judge.read_verification(rows, self.r.jev.judge(state, questions))
        wrong = [pf for (pf, _), v in zip(wrote, results) if not v.ok]
        self._decide("page_wrote", f"the page wrote into {len(wrote)} box(es) the plan left "
                                   f"alone; {len(wrong)} read as wrong and cleared",
                     fields=[pf.label for pf, _ in wrote], cleared=[pf.label for pf in wrong])
        for pf in wrong:
            try:
                apply_form.resolve(self.page, pf.locator).first.fill(
                    "", timeout=apply_fill.ACTION_TIMEOUT_MS)
                rec.setdefault("cleared", []).append(pf.label)
            except Exception as e:  # noqa: BLE001  (a box gone: nothing to clear)
                self._trace("error", step="page_writes.clear", error=type(e).__name__)

    def _fill_revealed(self, digest: apply_form.FormDigest, fresh: apply_form.FormDigest,
                       revealed: list[apply_form.Field], plan: FillPlan,
                       verification: list[VerifyResult],
                       rec: dict) -> tuple[apply_form.FormDigest, FillPlan, list[VerifyResult]]:
        """FILL-10: the fields the fill revealed, numbered after the page's
        own, mapped (their own request), planned (option picks and the
        second look), filled and verified. The page's buttons come from the
        fresh read (`_judge_buttons`)."""
        base = max([f.n for f in digest.fields] + [-1]) + 1
        extra = [dataclasses.replace(f, n=base + i) for i, f in enumerate(revealed)]
        sub = dataclasses.replace(fresh, fields=extra, buttons=[])
        answers = self._map(sub, {}, "application_form")
        more = apply_judge.plan(sub, self.catalog, answers,
                                generation_enabled=bool(self.r.settings["auto_apply_generate"]),
                                company=self._company())
        more = self._complete_option_plan(sub, answers, more, rec)
        self._last_answers = {**self._last_answers, **answers}
        more_verification = self._fill_and_verify(sub, more, rec)
        merged = dataclasses.replace(digest, fields=[*digest.fields, *extra])
        plan = dataclasses.replace(plan, fields=[*plan.fields, *more.fields],
                                   missing=[*plan.missing, *more.missing])
        merged, plan = self._judge_buttons(merged, fresh, plan)
        return merged, plan, [*verification, *more_verification]

    def _judge_buttons(self, digest: apply_form.FormDigest, fresh: apply_form.FormDigest,
                       plan: FillPlan) -> tuple[apply_form.FormDigest, FillPlan]:
        """The page's buttons after the fill (ADV-01, study G10). Unchanged
        (the same texts, flags and order): the fresh read's buttons, which
        carry the locators as they stand now, with the roles they had. Else
        they are judged again, in a request of their own, and the plan takes
        the new roles; the page's read keeps the new answers."""
        if not buttons_moved(digest, fresh):
            kept = [dataclasses.replace(new, n=old.n) for old, new in zip(digest.buttons,
                                                                        fresh.buttons)]
            return dataclasses.replace(digest, buttons=kept), plan
        old = {tuple(b.locator): b for b in digest.buttons}
        turned = [b for b in fresh.buttons if tuple(b.locator) in old
                  and _send_worded(b.text) and not _send_worded(old[tuple(b.locator)].text)]
        if turned:
            # a button the page read before now says it sends (INV-04): the
            # read the run judged stands, and the click's live check refuses
            # it; a new judgment would route it to the gate as a submit
            self._decide("button_turned_send", f"{_cap(turned[0].text, 40)} read "
                                               f"{_cap(old[tuple(turned[0].locator)].text, 40)} "
                                               "before the fill; its roles stand")
            return digest, plan
        sub = dataclasses.replace(fresh, fields=[])
        answers = self._map(sub, {}, "job_posting", discover=False)
        roles = apply_judge.plan(sub, self.catalog, answers, company=self._company())
        self._last_answers = {**self._last_answers, **answers}
        self._decide("buttons_after_fill", "the fill changed the page's buttons; judged again: "
                     + _cap("; ".join(f"{b.text} {'(disabled) ' if b.disabled else ''}"
                                      for b in fresh.buttons), 200),
                     roles={k: v[0] for k, v in roles.buttons.items()})
        return (dataclasses.replace(digest, buttons=list(fresh.buttons)),
                dataclasses.replace(plan, buttons=dict(roles.buttons)))

    def _trace_fill(self, plan: FillPlan, filled: list[apply_fill.Filled], errors: list[dict],
                    *, retry: bool = False, outcomes: list[dict] | None = None) -> None:
        """Which boxes took a value (never the value), how each was acted on
        (FILL-15) and the fill errors by their type."""
        actions = {pf.n: pf.action for pf in plan.fields}
        how = {o["n"]: o.get("how", "") for o in outcomes or []}
        self._trace("fill", retry=retry, errors=errors,
                    fields=[{"n": f.n, "label": f.label, "action": actions.get(f.n, ""),
                             "how": how.get(f.n, ""),
                             "holds_value": bool(str(f.value or "").strip())} for f in filled])

    def _review_page(self, digest: apply_form.FormDigest, answers: dict,
                     plan: FillPlan, rec: dict) -> None:
        """Fill and verify editable review controls, then the review's way on
        (`review_route`, READ-06): a confident advance with no submit is
        clicked (a wizard's middle step read as the review), anything else
        goes to the submit gate. Never on LinkedIn (`_no_form_on_linkedin`)."""
        self._no_form_on_linkedin("the review step")
        plan = self._complete_option_plan(digest, answers, plan, rec)
        with self._password_guard() as guard:
            verification = self._fill_and_verify(digest, plan, rec)
            self._fill_passwords(digest, plan, rec, guard)
            digest, plan, verification = self._after_fill(digest, plan, verification, rec)
            plan = self._own_submit(digest, plan)
            step, button, why = review_route(digest, plan,
                                             submit_apart=self._submit_apart(digest, plan),
                                             judged=self._judged_roles(digest))
            if step == "advance":
                self._decide("review_advance", "read as a review page with a confident advance "
                                               "and no submit button: a wizard step, its "
                                               "advance is clicked",
                             button=button[0], text=_button_text(digest, button[0]))
                digest, plan, verification, n = self._still_disabled(digest, plan, verification,
                                                                     button[0], rec)
                self._advance(digest, plan, verification, rec, n, button[1])
                return
            if step == "gate" and why:
                self._decide("to_gate", why, button=button[0],
                             text=_button_text(digest, button[0]))
                plan.buttons["submit"] = button
            self._submit_gate(digest, plan, verification, rec)

    @contextmanager
    def _password_guard(self):
        """The navigation guard (`_NavGuard`) for one form page. It is armed
        when `_fill_passwords` types the master password and removed when the
        page's step ends (a click, the submit gate, a park), before the
        window is left to the user. A navigation it stopped before any submit
        parks the job with the host it was headed for, and so does a form
        post it stopped after the submit click: that post was the send, and
        nothing went out. Any other navigation stopped after the submit click
        keeps the job submitted (a send may have gone out before the page
        moved on, and a second one must not) and says so."""
        guard = _NavGuard(self, self.page)
        try:
            yield guard
        except _Parked as p:
            if guard.blocked and self.submit_clicked and guard.posted:
                self.submit_clicked = False
                raise _Parked("needs_human", f"the form posts to {guard.blocked[0]}, outside "
                                             f"the allowed sites; the run stopped it and "
                                             f"nothing was sent", guard.before) from None
            if guard.blocked and not self.submit_clicked:
                raise _Parked("needs_human", f"left the allowed sites: {guard.blocked[0]}",
                              guard.before) from None
            if guard.blocked and p.status == "submitted":
                raise _Parked("submitted", f"submitted (unconfirmed): after the submit click "
                                           f"the run stopped the page going to "
                                           f"{guard.blocked[0]}; check that the application "
                                           f"went through", guard.before) from None
            raise
        finally:
            guard.stop()
        if guard.blocked and guard.posted:
            self.submit_clicked = False
            raise _Parked("needs_human", f"the form posts to {guard.blocked[0]}, outside the "
                                         f"allowed sites; the run stopped it and nothing was "
                                         f"sent", guard.before)
        if guard.blocked and not self.submit_clicked:
            raise _Parked("needs_human", f"left the allowed sites: {guard.blocked[0]}",
                          guard.before)

    def _fill_passwords(self, digest: apply_form.FormDigest, plan: FillPlan, rec: dict,
                        guard: _NavGuard) -> None:
        """The master password in the page's password boxes
        (`apply_judge.PASSWORD_ACTION`): an account made inside the
        application. The password is for job applications only (the user's
        rule, 2026-09-22), so it goes into a form as it does into a sign-up,
        and the submit gate still decides the send. It is typed last, once
        the other boxes are filled and verified, with `guard` armed, and only
        into a box that is a real password input, is named a password (not a
        passcode, a passport number or a security answer), sits on the
        application's site (`_password_ok` for the page and the box's frame)
        and belongs to no form that posts off the allowed sites. It never
        reaches the judge or the record. A required box that cannot take it
        parks the job; an optional one is left blank. It is typed on one form
        page per site: a second page asking for it parks, as the account
        step's second password screen does. A page that makes an account (a
        new-password box by its autocomplete or its label, or a second box to
        confirm it) puts it in the
        ledger under the host of the box's frame, unless one is there: once
        the page is sent, by the run or by the user, it exists."""
        boxes = [pf for pf in plan.fields if pf.action == apply_judge.PASSWORD_ACTION]
        if not boxes:
            return
        self.form_had_password = True
        host = digest.url_host or _host(self.page.url)
        if _site(host) in self.form_password_sites:
            # a second form page asking for the password on the same site is
            # the first one rejected (a wrong password): typing it again only
            # moves the account toward a lockout
            raise _Parked("needs_human", f"the form on {host} asked for the master password "
                                         "again", LOGIN_NOTE)
        fields = {f.n: f for f in digest.fields}
        frames = apply_form.frames(self.page)
        stored = ats_accounts.has_password()
        account_host = ""
        typed: list = []
        for pf in boxes:
            f = fields.get(pf.n)
            idx = int(pf.locator[0])
            box_host = _host(self._frame_url(frames, idx)) if 0 <= idx < len(frames) else ""
            outside = sorted(h for h in {_host(self.page.url), box_host or _host(self.page.url)}
                             if not self._password_ok(h))
            loc, kind, posts_to = None, "", ""
            try:
                loc = apply_form.resolve(self.page, pf.locator).first
                kind, action = loc.evaluate(
                    "el => [el.type || '', el.form && el.form.getAttribute('action') "
                    "? el.form.action : '']", timeout=5_000)
                if urlsplit(str(action or "")).scheme in ("http", "https"):
                    posts_to = _host(action)    # a `javascript:` action is no destination
            except Exception:       # noqa: BLE001  (a box or frame that went away takes nothing)
                loc = None
            if not stored:
                why = "the form asks for a password and no master password is stored"
            elif outside:
                why = f"a password box on {outside[0]}, outside the application site"
            elif loc is None:
                why = f"the password box ({pf.label}) went away"
            elif str(kind).lower() != "password" or f is None or not _names_password(f):
                why = f"{pf.label or 'a masked box'} is not a password box for an account"
            elif posts_to and not self._allowed_site(posts_to):
                why = f"the password box's form posts to {posts_to}, outside the allowed sites"
            else:
                if 0 <= idx < len(frames):
                    guard.frames.add(id(frames[idx]))
                guard.start()
                self._keep_secret_box(pf.locator)
                if ats_accounts.fill_password(self.page, loc):
                    account_host = account_host or box_host or host
                    typed.append(f)
                    self._filled_here.append(pf.locator)
                    self._filled_any = True
                    rec["filled"].append({"n": pf.n, "label": pf.label, "value": "",
                                          "type": "other", "id_or_name": "account_password",
                                          "upload": False, "hidden": True})
                    continue
                why = f"the password box ({pf.label}) did not take the master password"
            if pf.required:
                raise _Parked("needs_human", why)
            pf.action = "skip"
        if account_host:
            self.form_password_sites.add(_site(host))
            # a page that makes an account asks for a new password or its
            # confirmation; a sign-in read as the form makes none
            makes = len(typed) > 1 or any(
                str(f.autocomplete or "").lower() == "new-password"
                or _NEW_PASSWORD.search(f"{f.label or ''} {f.id_or_name or ''}") for f in typed)
            email = self.catalog.value("email")
            if makes and email and not ats_accounts.lookup(account_host):
                ats_accounts.record(account_host, email)

    def _resolve_generation(self, digest: apply_form.FormDigest, plan: FillPlan,
                            rec: dict | None = None) -> None:
        """Each `generate` field goes through the answergen hook once while the
        job's draft budget lasts. An accepted draft becomes a fill (the record
        marks it generated); a rejected or missing one leaves an optional field
        blank and flagged, and parks a required one with the hook's note."""
        by_n = {f.n: f for f in digest.fields}
        rows = rec.setdefault("generated", []) if rec is not None else []
        for pf in plan.fields:
            if pf.action != "generate":
                continue
            f = by_n.get(pf.n)
            text, note, record_note = None, "", ""
            key = _draft_key(f if f is not None else pf)
            if key in self._drafts_by_question:
                # FILL-12: the same question read again (a page the form sent
                # back, a re-read): its accepted draft, no second generation
                pf.action, pf.value = "fill", self._drafts_by_question[key]
                rows.append({"label": pf.label, "ok": True, "reused": True,
                             "note": "the draft already made for this question"})
                continue
            if self.gen_budget <= 0:
                note = "generation budget exhausted"
            elif f is not None:
                # every attempt spends a draft (a rejected one cost the same calls)
                self.gen_budget -= 1
                text = self.r.answergen.answer(f, self.catalog, self.r.jev,
                                               budget=self.gen_budget + 1)
                last = getattr(self.r.answergen, "last", None)
                if last is not None:
                    note = str(getattr(last, "note", "") or "")
                elif not text:
                    # a hook that keeps no attempt (NotConfigured, a bare injected
                    # hook) made no draft to reject; the record says so and the
                    # park reason stays the plain one
                    record_note = "no generator"
            if text:
                pf.action, pf.value = "fill", str(text)
                self._drafts_by_question[key] = str(text)
                rows.append({"label": pf.label, "ok": True, "note": note or "generated"})
                continue
            rows.append({"label": pf.label, "ok": False,
                         "note": note or record_note or "no draft"})
            pf.action = "skip"
            hint = (f.help or f.placeholder or f.type) if f is not None else ""
            plan.missing.append((pf.label, hint))
            if pf.required and not plan.park_reason:
                plan.park_reason = f"required field without an answer: {pf.label}"
                if note:
                    plan.park_reason += f"; {note}"

    def _add_missing(self, question: str, context: str) -> None:
        self.missing.append({"question": question, "context": context, "suggestion": ""})
        apply_queue.add_missing(self.job_id, question, context=context, path=self.r.queue_path)

    def _verify(self, filled: list[apply_fill.Filled],
                drafts: Mapping[int, str] | None = None,
                picks: Mapping[int, tuple[str, bool]] | None = None,
                shaped: Mapping[int, tuple[str, str]] | None = None) -> list[VerifyResult]:
        """The judge checks every typed fact against the sheet. A generated
        answer (`drafts`: n -> the accepted draft) is not on the sheet, and
        the grounding gate was its check; what is left is that the box holds
        the draft, which is a string comparison here, so no question carries
        the draft. A pick (`picks`: n -> the option planned: a select, a
        radio, a tick box) is checked in code too (FILL-13): the read-back
        shows the option (`pick_holds`). So is a value the page reshapes
        (`shaped`: a phone's digits, a date in the box's format, an
        upload's file name, `shaped_holds`: FILL-01, FILL-04, FILL-05).
        Results keep the fill order."""
        if not filled:
            return []
        drafts = drafts or {}
        picks = picks or {}
        shaped = shaped or {}
        by_n: dict[int, VerifyResult] = {}
        rows = []
        for f in filled:
            if f.n in drafts or f.n in picks or f.n in shaped:
                ok = (_same_text(f.value, drafts[f.n]) if f.n in drafts
                      else pick_holds(f.value, *picks[f.n]) if f.n in picks
                      else shaped_holds(f.value, *shaped[f.n]))
                by_n[f.n] = VerifyResult(n=f.n, label=f.label, ok=ok,
                                         p_correct=1.0 if ok else 0.0, p_placeholder=0.0)
            else:
                rows.append(f.to_dict())
        if rows:
            state, questions = apply_judge.verify_questions(
                rows, self.catalog.verification_excerpt())
            for v in apply_judge.read_verification(rows, self.r.jev.judge(state, questions)):
                by_n[v.n] = v
        return [by_n[f.n] for f in filled]

    def _retry_failed(self, plan: FillPlan, filled: list[apply_fill.Filled],
                      verification: list[VerifyResult],
                      drafts: Mapping[int, str] | None = None,
                      shaped: Mapping[int, tuple[str, str]] | None = None) -> list[VerifyResult]:
        """A required field that failed verification is filled once more with
        the same value (a read-back mismatch is usually widget timing)."""
        required = {pf.n for pf in plan.fields if pf.required}
        failed_ns = {v.n for v in verification if not v.ok and v.n in required}
        if not failed_ns:
            return verification
        self.log.info("job %s: retrying %d field(s) that failed verification",
                      self.job_id, len(failed_ns))
        retry = FillPlan(fields=[pf for pf in plan.fields if pf.n in failed_ns])
        errors: list[dict] = []
        refilled = apply_fill.apply(self.page, retry, deadline=self.deadline, clock=self.r.clock,
                                    errors=errors)
        self._trace_fill(retry, refilled, errors, retry=True)
        again = {v.n: v for v in self._verify(refilled, drafts, _picks(plan), shaped)}
        by_n = {f.n: f for f in refilled}
        for i, f in enumerate(filled):
            if f.n in by_n:
                filled[i] = by_n[f.n]
        return [again.get(v.n, v) for v in verification]

    def _record_fill(self, rec: dict, digest: apply_form.FormDigest, plan: FillPlan,
                     filled: list[apply_fill.Filled], verification: list[VerifyResult]) -> None:
        fields = {f.n: f for f in digest.fields}
        actions = {pf.n: pf.action for pf in plan.fields}
        generated = {pf.n for pf in plan.fields
                     if pf.fact_key == "needs_generation" and pf.action == "fill"}
        for f in filled:
            df = fields.get(f.n)
            rec["filled"].append({
                "n": f.n, "label": f.label, "value": f.value,
                "type": df.type if df else "", "id_or_name": df.id_or_name if df else "",
                "autocomplete": df.autocomplete if df else "",
                "upload": actions.get(f.n) == "upload",
                "generated": f.n in generated})
        _record_verification(rec, verification)

    def _click(self, digest: apply_form.FormDigest, n: int, role: str,
               rec: dict, *, conf: float | None = None,
               refused_by_form: Callable[[], bool] | None = None) -> apply_fill.ClickResult:
        """Click button `n` in role `role` (judged at `conf`). A submit is
        clicked once whatever the page showed: a quiet page is no proof the
        click failed and a second click could send twice, so a
        landed-but-quiet submit waits up to `SUBMIT_SETTLE_S` for the page
        instead. Any other role gets one retry of a quiet click that set
        nothing going (no navigation, no POST, PUT or PATCH: a click before
        the page's script was ready); a quiet click that did is waited for
        `STEP_SETTLE_S` more and never made again (ADV-06, SP6 review I3); a
        dead advance parks with the button, its role and the judge's
        confidence.
        A click that opens a new tab (NAV-05), right away or a moment after
        the click (the tabs are watched until the retry, which waits
        `POPUP_GRACE_S` for one first, review M5), is followed and never
        clicked again. `refused_by_form` (ADV-06): read after the first
        click, changed or quiet; when it says the form refused the click
        (its validation messages), the click is never made again here: the
        caller repairs the fields first. A page that shows a loading
        indicator after the click is waited on (ADV-07, `BUSY_WAIT_S`)."""
        button = next((b for b in digest.buttons if b.n == n), None)
        text = button.text if button else f"button {n}"
        rec["clicked"].append(f"{text} ({role})")
        self._last_click = (text, role)
        timeout = max(1.0, min(CLICK_TIMEOUT_S, self.deadline - self.r.clock()))
        check = self._live_check(role)
        # a loading indicator already up before the click is the page's own
        # (an ad's placeholder that never clears, M10): no wait for it after
        busy_before = role != "submit" and self._busy()
        with _popups(self.page) as opened:
            result = apply_fill.click(self.page, digest, n, timeout_s=timeout, check=check)
            if (role != "submit" and not opened and not result.changed
                    and not result.refused and result.clicked):
                # a tab that opens a moment after the click comes before any
                # second click
                self.page.wait_for_timeout(int(POPUP_GRACE_S * 1000))
        self._trace("click", n=n, text=text, role=role, confidence=conf,
                    clicked=result.clicked, changed=result.changed, url=str(self.page.url),
                    refused=result.refused, late=result.late, popups=len(opened),
                    overlay=result.overlay)
        if result.overlay:
            self._decide("overlay_cleared", f"{_cap(text, 40)} was covered ({result.overlay}); "
                                            "the cover was put away and the click made once "
                                            "more")
        if result.refused and role != "submit":
            self._refused_click(role, text, result.refused)
        if opened and not result.refused and (role == "submit" or not result.changed):
            # NAV-05: the click opened its next page in a new tab and left this
            # one as it was: the tab is the next page, and nothing is clicked
            # again; a submit's tab is read only when it is the thank-you
            # (the page the submit was made on keeps its own evidence)
            if self._adopt_click_popup(opened[0], text, role):
                return apply_fill.ClickResult(clicked=True, changed=True, late=result.late)
        if role == "submit":
            if result.clicked and not result.changed:
                self.log.info("job %s: the submit click changed nothing; waiting up to %s s",
                              self.job_id, SUBMIT_SETTLE_S)
                changed = apply_fill.wait_for_change(self.page, timeout_s=SUBMIT_SETTLE_S)
                self._trace("submit_settle", changed=changed, waited_s=SUBMIT_SETTLE_S)
                return apply_fill.ClickResult(clicked=True, changed=changed, late=result.late)
            return result
        if result.changed and not busy_before:
            self._wait_while_busy(text)
        if result.clicked and refused_by_form is not None and refused_by_form():
            return result
        if result.changed:
            return result
        went = [row for row in result.sent if not _tracking(row.split(" ", 1)[-1])
                and not _is_captcha_url(row.split(" ", 1)[-1])]
        if went:
            # ADV-06 (SP6 review I3): the click reached the page and set a
            # request going; a second click would make it twice (a step saved
            # twice, a send made twice). The page is waited for, never
            # clicked again.
            self.log.info("job %s: the %s click set %s going; waiting up to %s s for the page",
                          self.job_id, role, went[0], STEP_SETTLE_S)
            changed = apply_fill.wait_for_change(self.page, timeout_s=STEP_SETTLE_S)
            self._trace("step_settle", changed=changed, waited_s=STEP_SETTLE_S, sent=went[:3])
            if changed:
                self._wait_while_busy(text)
            if changed or (refused_by_form is not None and refused_by_form()):
                return apply_fill.ClickResult(clicked=True, changed=changed, late=result.late,
                                              sent=result.sent)
            if role == "advance":
                judged = f"judged {role} {conf:.2f}, " if conf is not None else ""
                raise _Parked("needs_human", f"the {role} button ({text}) did nothing ({judged}"
                                             f"its request left: {_cap(went[0], 100)}; it was "
                                             "not clicked again)")
            return result
        self.log.info("job %s: %s click changed nothing; retrying once", self.job_id, role)
        with _popups(self.page) as opened:
            result = apply_fill.click(self.page, digest, n, timeout_s=timeout, check=check)
        self._trace("click", n=n, text=text, role=role, confidence=conf, retry=True,
                    clicked=result.clicked, changed=result.changed, url=str(self.page.url),
                    refused=result.refused, popups=len(opened))
        if result.refused:
            self._refused_click(role, text, result.refused)
        if opened and not result.changed and self._adopt_click_popup(opened[0], text, role):
            return apply_fill.ClickResult(clicked=True, changed=True, late=result.late)
        if not result.changed and role == "advance":
            if self._human_check_showing():
                self._wait_for_human_check(f"a CAPTCHA challenge appeared after {text}")
                return apply_fill.ClickResult(clicked=True, changed=True)
            judged = f"judged {role} {conf:.2f}, " if conf is not None else ""
            ticked = ("; a CAPTCHA checkbox on the page is unticked: tick it, then Re-queue"
                      if self._human_check_showing(checkbox=True) else "")
            raise _Parked("needs_human", f"the {role} button ({text}) did nothing "
                                         f"({judged}clicked twice){ticked}")
        return result

    def _wait_while_busy(self, text: str) -> None:
        """ADV-07: after a click that changed the page, a loading indicator
        still in view (`aria-busy`, a skeleton: `apply_fill.ready_snapshot`)
        is waited on, up to `BUSY_WAIT_S` (a slow Workday or Taleo step can
        take 30 s), then the page settles; the trace says how long."""
        start = time.monotonic()
        while time.monotonic() - start < BUSY_WAIT_S:
            try:
                if not apply_fill.ready_snapshot(self.page)[1]:
                    break
            except Exception:       # noqa: BLE001  (a page double, a page mid-navigation)
                break
            self.page.wait_for_timeout(int(BUSY_POLL_S * 1000))
        waited = time.monotonic() - start
        if waited >= BUSY_POLL_S:
            info = apply_fill.settle(self.page, CLICK_TIMEOUT_S)
            self._decide("busy_after_click", f"a loading indicator showed after {_cap(text, 40)}; "
                                             f"waited {waited:.1f} s, then "
                                             + settled_words(info),
                         waited_s=round(waited, 1), capped=waited >= BUSY_WAIT_S)

    def _adopt_click_popup(self, popup, text: str, role: str) -> bool:
        """A click that opened a new tab (NAV-05): True when the tab is now
        the page. An advance's tab is the next page, followed like an Apply
        entry's (`_follow_popup`: its host admitted and checked). A submit's
        tab is the page the post-submit read reads (`_after_submit`, whose
        host check is its own) only when it shows received words; any other
        tab (a help page, an answer that says nothing, a browser error page)
        leaves the page the submit was made on, and what left, to decide."""
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=CLICK_TIMEOUT_S * 1000)
        except Exception:       # noqa: BLE001  (the tab is read as it is)
            pass
        url = str(getattr(popup, "url", ""))
        if role == "submit":
            try:
                received = confirmation_words(apply_fill.page_text(popup))
            except Exception:   # noqa: BLE001  (a tab mid-navigation, a closed tab)
                received = set()
            if not received:
                self._decide("click_popup", f"the submit click ({_cap(text, 60)}) opened a new "
                                            f"tab that shows no received words; the page the "
                                            f"submit was made on is read", url=_cap(url, 160))
                return False
            self._decide("click_popup", f"the submit click ({_cap(text, 60)}) opened a new tab "
                                        f"that shows {sorted(received)[0]!r}; it is read as the "
                                        f"page after the submit", url=_cap(url, 160))
            self.trace.nav(url)
            self._watch(popup)
            self.page = popup
            return True
        self._decide("click_popup", f"the {role} click ({_cap(text, 60)}) opened a new tab and "
                                    f"left the page as it was; the tab is the next page",
                     url=_cap(url, 160))
        self._follow_popup(popup, source_url=str(self.page.url))
        return True

    def _live_check(self, role: str, *, account: bool = False) -> Callable[[str, dict], str]:
        """`apply_fill.click`'s check for a click in `role`: `live_refusal`
        over the element as it reads just before the click."""
        def _check(expected: str, live: dict) -> str:
            return live_refusal(role, expected, live, account=account)
        return _check

    def _refused_click(self, role: str, text: str, why: str) -> None:
        """A click the live check stopped (INV-04): nothing was clicked, and
        the job waits for the person with the control's text then and now."""
        self._decide("live_refused", f"the {role} click on {text!r} was refused: {why}")
        raise _Parked("needs_human", f"the {role} button ({_cap(text, 60)}) changed before the "
                                     f"click: {why}; nothing was clicked")

    # -- the submit path ----------------------------------------------------------------------

    def _gate_read(self, digest: apply_form.FormDigest, plan: FillPlan) -> dict[str, Any]:
        """The page as the gate reads it just before the submit (`can_submit`'s
        `live`, INV-01 and INV-02): whether an application is on it (a field
        filled on this page, or the application filled on an earlier one),
        whether an Apply-worded submit is the form's own sending button
        (`_apply_button_why`), the submit's form's validity
        (`apply_form.validity_report`) and the required controls the
        extractor leaves out that are empty (`apply_form.control_scan`)."""
        out: dict[str, Any] = {}
        submit = plan.buttons.get("submit")
        if submit is None:
            return out
        button = next((b for b in digest.buttons if b.n == submit[0]), None)
        if not self._filled_here and not self.form_filled and not self._filled_any:
            out["no_application"] = "nothing was filled on this page or an earlier one"
        if button is None:
            return out
        if apply_judge.apply_worded(button.text):
            out["apply_button"] = self._apply_button_why(digest, button)
        try:
            out["invalid"] = apply_form.validity_report(self.page, button.locator,
                                                        self._filled_here)["invalid"]
            out["required_empty"] = [
                r for r in apply_form.control_scan(self.page, [int(button.locator[0])],
                                                   required_only=True)
                if r.get("required") and r.get("empty")]
        except Exception as e:      # noqa: BLE001  (a page double; a real page answers)
            self._trace("error", step="gate_read", error=type(e).__name__)
        return out

    def _apply_button_why(self, digest: apply_form.FormDigest, button: apply_form.Button) -> str:
        """"" when an Apply-worded button is the submit (INV-01): it sits in
        the same form, or the same box smaller than the page, as a field this
        run filled on this page (on a page with no control at all after the
        application was filled on earlier pages, the page's own button), and
        the judge says clicking it sends the finished application
        (`button_{n}_sends` at `BUTTON_SENDS_MIN`). Otherwise the reason it
        is no submit, as the gate's evidence."""
        sends = apply_judge.read_sends(self._last_answers, button.n)
        if self._filled_here:
            verdict, where = apply_form.same_scope(self.page, button.locator, self._filled_here)
            # "apart" never counts; "same", and "unclear" on a page this run
            # filled, count with the judge's word (the controller's ruling)
            same = verdict != "apart"
            rule = (f"it sits with the fields this page filled ({where})" if verdict == "same"
                    else f"its box is unclear ({where}); this page filled "
                         f"{len(self._filled_here)} field(s)")
        else:
            scan = []
            try:
                scan = apply_form.control_scan(self.page)
            except Exception:       # noqa: BLE001  (a page double)
                pass
            state, conf = apply_judge.read_page_state(self._last_answers)
            review = (state in ("review_page", "application_form")
                      and conf >= apply_judge.PAGE_STATE_MIN_CONF)
            same = ((self.form_filled or self._filled_any) and not digest.fields and not scan
                    and review)
            where = (f"a page with no control after the filled application, read as {state} "
                     f"{conf:.2f}" if same else
                     f"no field filled on this page (read as {state} {conf:.2f})")
            rule = f"a review page after the filled application (read as {state} {conf:.2f})"
        if same and sends >= apply_judge.BUTTON_SENDS_MIN:
            self._decide("apply_button_submit", f"the Apply ({_cap(button.text, 60)}) is the "
                                                f"submit: {rule}; the judge reads it as sending "
                                                f"at {sends:.2f}", button=button.n)
            return ""
        return (f"the Apply button ({_cap(button.text, 60)}) may open or start an application: "
                f"{where}; the judge reads it as sending the finished application at "
                f"{sends:.2f} (the gate needs {apply_judge.BUTTON_SENDS_MIN:.2f})")

    def _submit_gate(self, digest: apply_form.FormDigest, plan: FillPlan,
                     verification: list[VerifyResult], rec: dict) -> None:
        """The only place an application is sent. `can_submit` over the plan
        and the page as the gate reads it (`_gate_read`); in park mode a
        page that would pass parks `ready_to_submit` (a CAPTCHA checkbox on
        it is the person's to tick before their submit: the reason says so).
        In submit mode the requests that leave are watched from here on
        (`SendWatch`); a CAPTCHA checkbox still unticked waits for the person
        (study G11), and a page that moved on during that wait (the person
        sent it) is read as after a submit; a click that dispatched and then
        timed out on its navigation stays clicked (TERM-02); the page after
        it is read by `_after_submit`."""
        self._no_form_on_linkedin("the submit gate")
        live = self._gate_read(digest, plan)
        if live.get("invalid") and not live.get("no_application") \
                and self._gate_repairs < REPAIR_ROUNDS and plan.buttons.get("submit"):
            # ADV-02: the form reports a control that would not validate:
            # it is repaired before the gate decides, never sent as it is
            self._gate_repairs += 1
            submit = plan.buttons["submit"]
            text = _button_text(digest, submit[0])
            problems = [{**r, "text": r.get("message") or "", "kind": "invalid"}
                        for r in live["invalid"]]
            who = self._button_identity(digest, submit[0])
            digest, plan, verification = self._repair(digest, plan, verification, problems, rec,
                                                      why="read at the gate")
            if self._repaired:
                # the same control, never another form's with the same words
                n = self._same_button(digest, who)
                if n is None:
                    raise self._button_lost(who)
                plan.buttons["submit"] = (n, submit[1])
                self._submit_gate(digest, plan, verification, rec)
                return
        ok, why = can_submit(plan, verification, self.r.settings, live)
        submit = plan.buttons.get("submit")
        self._trace("gate", ok=ok, why=why, button=submit[0] if submit else None,
                    text=_button_text(digest, submit[0]) if submit else "",
                    confidence=submit[1] if submit else None,
                    live={k: v for k, v in live.items() if v})
        if not ok:
            forced = {**self.r.settings, "auto_apply_submit": True}
            ready, why_on = can_submit(plan, verification, forced, live)
            if ready:
                if self._human_check_showing(checkbox=True):
                    raise _Parked("ready_to_submit", f"{why}; {CHECKBOX_NOTE}",
                                  f"{CHECKBOX_NOTE}; {REVIEW_NOTE}")
                raise _Parked("ready_to_submit", why, REVIEW_NOTE)
            self._decide("gate_refused", why_on)
            if why_on == "no submit button":
                why_on += f" (buttons: {self._buttons_seen(digest)})"
            raise _Parked("needs_human", why_on)
        submit_n = plan.buttons["submit"][0]
        button = next((b for b in digest.buttons if b.n == submit_n), None)
        text = button.text if button else ""
        frame = None
        if button is not None:
            try:
                frame = apply_form.frames(self.page)[int(button.locator[0])]
            except Exception:       # noqa: BLE001  (a page double)
                frame = None
        self._submit_at = button.locator if button is not None else None
        who = self._button_identity(digest, submit_n)      # the control the gate clicks
        self._before_submit = self._submit_baseline(digest)
        watch = SendWatch(self, self.page, frame)
        self._send_watch = watch
        watch.start()
        if self._human_check_showing(checkbox=True):
            # the person ticks it; the run never does (study G11)
            self._decide("gate_captcha", "a CAPTCHA check is on the page before the submit")
            try:
                self._wait_for_human_check("a CAPTCHA check is on the form before the submit",
                                           checkbox=True)
            except _Parked as p:
                if not watch.any():
                    raise
                # a request left while the run waited: the person may have sent
                # it; the job never reads as unsent (R3)
                self.submit_clicked = True
                raise self._send_evidence(p, watch, when="during the wait") from None
            if watch.any() or self._moved_during_wait(digest):
                # the person may have sent it while the run waited
                self.submit_clicked = True
                self._decide("gate_moved", "the page moved on while the run waited for the "
                                           "CAPTCHA check; it is read as after a submit",
                             sent=watch.first())
                rec["clicked"].append("the page moved on during the CAPTCHA wait")
                self._after_submit(handoff=self.handed_off, during_wait=True)
                return
        self.log.info("job %s: clicking submit", self.job_id)
        self.submit_clicked = True    # set before the click so a crash after it reads as unconfirmed (no resend)
        result = self._click(digest, submit_n, "submit", rec, conf=plan.buttons["submit"][1])
        if result.refused:
            self.submit_clicked = False
            watch.stop()
            raise _Parked("needs_human", f"the submit button ({_cap(text, 60)}) changed before the "
                                         f"click: {result.refused}; nothing was clicked")
        if not result.clicked and not watch.any():
            # the click never landed: nothing was sent, the form is filled, the human submits
            self.submit_clicked = False
            watch.stop()
            rec["clicked"].append("submit did not register")
            self.log.info("job %s: the submit click did not register", self.job_id)
            raise _Parked("ready_to_submit", "submit did not register", SUBMIT_FAILED_NOTE)
        if result.late or not result.clicked:
            self._decide("submit_dispatched", f"the submit click was dispatched, then "
                                              f"{result.late or 'raised'}; it stays clicked "
                                              "(no second click)", sent=watch.first())
        self.log.info("job %s: SUBMIT CLICKED", self.job_id)
        rec["clicked"].append("SUBMIT CLICKED")
        typed = [pf for pf in plan.fields if pf.action == apply_judge.PASSWORD_ACTION]
        # an account page: the master password went in, and the page needed
        # it (a required box) or its button names the account; an optional
        # save-your-profile password on an application is no account page
        account = bool(typed) and (any(pf.required for pf in typed)
                                   or bool(_ACCOUNT_STEP_WORDS.search(text)
                                           or apply_judge.SIGN_IN_WORDS.search(text)))
        try:
            self._after_submit(account=account, handoff=self.handed_off)
        except _Refused as refused:
            # ADV-02: the form refused the send as typed and nothing left the
            # page (`_not_sent`): its fields are repaired once and the page
            # goes through the gate again, which decides as it did
            self._submit_repairs += 1
            submit = plan.buttons["submit"]
            digest, plan, verification = self._repair(digest, plan, verification,
                                                      refused.problems, rec,
                                                      why="the submit was refused")
            if not self._repaired:
                raise refused.park from None
            # the control the gate clicked, never another form's with the
            # same words (SP6 review I2)
            n = self._same_button(digest, who)
            if n is None:
                raise self._button_lost(who)
            plan.buttons["submit"] = (n, submit[1])
            rec["clicked"].append("the form refused the submit; repaired")
            self._submit_gate(digest, plan, verification, rec)

    def _moved_during_wait(self, digest: apply_form.FormDigest) -> bool:
        """After the gate's wait for the person: did the page move on (a new
        URL, received words it did not show before, or the submit button
        gone or hidden)? M8."""
        before = self._before_submit or {}
        if str(before.get("url") or "") != str(self.page.url):
            return True
        if new_confirmation(str(before.get("text") or ""), self._page_text()):
            return True
        loc = self._submit_at
        if not loc:
            return False
        try:
            found = apply_form.resolve(self.page, loc)
            return found.count() != 1 or not found.first.is_visible()
        except Exception:       # noqa: BLE001  (a frame gone: the page moved on)
            return True

    def _submit_baseline(self, digest: apply_form.FormDigest) -> dict[str, Any]:
        """The page just before the submit click, for the reads after it: its
        URL, its form, its visible text and error texts."""
        try:
            text = apply_fill.page_text(self.page)
        except Exception:       # noqa: BLE001  (a page double)
            text = ""
        try:
            errors = {e["text"] for e in apply_form.validity_report(self.page)["errors"]}
        except Exception:       # noqa: BLE001
            errors = set()
        try:
            values = apply_form.box_values(self.page, self._typed_boxes())
        except Exception:       # noqa: BLE001  (a page double)
            values = []
        # the boxes' values stay in memory for the post-submit read; they are
        # never written to the trace or the record
        return {"url": str(self.page.url), "fields": _fields_sig(digest), "text": text,
                "errors": errors, "values": values}

    def _typed_boxes(self) -> list[tuple[int, str]]:
        """The boxes this page's fill typed into, less the ones that took the
        master password or an emailed code (`_keep_secret_box`): their
        values are never read back, whatever type the box shows now (m4)."""
        secret = getattr(self, "_secret_locators", set())
        return [loc for loc in self._filled_here
                if (int(loc[0]), str(loc[1])) not in secret]

    def _holds_typed(self, before: Mapping[str, Any]) -> bool:
        """Does the form still hold what the run typed: at least half of the
        boxes that held a value before the click hold the same one? A
        server's validation answer keeps the values (a password or an upload
        it may drop, and those are not read); an emptied or reset form keeps
        none (R1)."""
        was = list(before.get("values") or [])
        try:
            now = apply_form.box_values(self.page, self._typed_boxes())
        except Exception:       # noqa: BLE001
            return False
        pairs = [(a, b) for a, b in zip(was, now) if a]
        kept = sum(1 for a, b in pairs if a == b)
        return bool(pairs) and kept >= max(1, (len(pairs) + 1) // 2)

    def _after_submit(self, *, account: bool = False, handoff: bool = False,
                      during_wait: bool = False) -> None:
        """Read what the submit click did before deciding (TERM-01), again
        every `POST_SUBMIT_POLL_S` while a request it sent is in flight or
        the page still moves, up to `POST_SUBMIT_WAIT_S`. Per look, in order:

        - a confirmation: received words new since the click
          (`new_confirmation`), or the judge's confirmation at
          `CONFIRMATION_MIN_CONF` on a page with no form field and no send
          button: `submitted`;
        - a bot-check challenge (a tall provider frame; a checkbox counts
          only before the click): the person solves it
          (`_wait_for_human_check`; headless parks), then the page is read
          again;
        - validation errors: on the form as it was, nothing sent yet: a
          control that would not validate, `aria-invalid`, an error text
          beside a field; once a request left (`SendWatch.any`), only
          `aria-invalid` and field error texts on the same form (its fields
          as before the click) count. Nothing was sent: `needs_human`
          (`submit_clicked` reset; the repair loop is SP6's);
        - an emailed-code screen: the code step, then the page is read
          again (the code screen back means the code was refused);
        - an error banner, or an error page: `needs_human` with its text;
        - nothing of these (`_inconclusive`), decided by what left.

        Every park here carries `CHECK_SENT_NOTE` and names the request when
        one left (`_send_evidence`), so a job is never re-queued over a send.

        `account`: the page was an account page (a "Create account and
        apply"). A confident form, sign-in or sign-up after that click
        (`_OPENED_BY_ACCOUNT`) may be the application the account opened, or
        a sent one's page reset for the signed-in user: the run can tell
        neither "submitted" nor "go on and send", so the job waits for the
        user with the page open. `handoff`: the page came from the account
        step (`_AsForm`), whose screen is a sign-up first; anything after it
        but a confirmation waits for the user the same way. `during_wait`:
        the page moved on while the run waited for the CAPTCHA check (the
        person may have sent it), so the reasons say so."""
        watch = self._send_watch or SendWatch(self, self.page)
        before = self._before_submit or {}
        self._sent_when = "during the CAPTCHA wait" if during_wait else "after the submit click"
        try:
            self._read_after_submit(watch, before, account, handoff)
        except _Parked as p:
            if p.status != "needs_human":
                raise
            raise self._send_evidence(p, watch, when="during the wait" if during_wait
                                      else "after the click") from None
        finally:
            watch.stop()

    @staticmethod
    def _send_evidence(p: _Parked, watch: SendWatch, when: str = "after the click") -> _Parked:
        """A park after the submit click (or a send the person made while the
        run waited): the "check whether" note, and the request that left
        named in the reason when one did."""
        reason = p.reason
        first = watch.first()
        if first and first not in reason:
            reason = f"{reason}; a request left {when}: {_cap(first, 120)}"
        return _Parked("needs_human", reason, CHECK_SENT_NOTE)

    def _read_after_submit(self, watch: SendWatch, before: Mapping[str, Any], account: bool,
                           handoff: bool) -> None:
        start = time.monotonic()
        last_seen, changed_at, judged = None, start, None
        reads = 0
        state, conf, answers = "other", 0.0, {}
        rec: dict = {}
        code_entered = False
        answers_seen, late_look = 0, False
        while True:
            # a request in flight as this look begins, and done by its end:
            # its answer may have changed the page after the look read it
            in_flight = bool(watch.pending)
            digest = self._post_submit_digest(watch)
            now = time.monotonic()
            seen = json.dumps(digest.to_dict(), sort_keys=True)
            held = seen == last_seen        # the page held since the last look
            if seen != last_seen:
                last_seen, changed_at = seen, now
            text = self._page_text()
            marker = new_confirmation(str(before.get("text") or ""), text)
            if seen != judged and (judged is None or held) and reads < POST_SUBMIT_READS:
                # the judge reads the page once it holds between two looks,
                # and at most `POST_SUBMIT_READS` times
                reads += 1
                answers = self._judge_page(digest)
                state, conf = apply_judge.read_page_state(answers)
                rec = self._new_page_record(state, conf, digest=digest, answers=answers)
                judged = seen
                self.log.info("job %s after submit: %s (%.2f)", self.job_id, state, conf)
            self._confirmed(marker, state, conf, digest, watch, code_entered,
                            judged=judged == seen)
            if self._human_check_showing():
                self._decide("after_submit", "a CAPTCHA challenge showed after the submit "
                                             "click; the person solves it", sent=watch.first())
                self._wait_for_human_check("a CAPTCHA challenge appeared after the submit "
                                           "click")
                last_seen, judged, changed_at = None, None, time.monotonic()
                continue
            same_form = tuple(before.get("fields") or ()) == _fields_sig(digest)
            as_before = str(before.get("url") or "") == str(self.page.url) and same_form
            report = self._post_submit_validity(as_before)
            old_errors = before.get("errors") or set()
            field_errors = [e for e in report["errors"]
                            if e.get("field") and e["text"] not in old_errors]
            banners = [e for e in report["errors"]
                       if not e.get("field") and e["text"] not in old_errors]
            invalid = report["invalid"]
            may_refuse = True
            if watch.any():
                # a request left: only what the site marked on a control
                # (`aria-invalid`, a message a control names) of the form as
                # the run typed it says the send was refused; an emptied or
                # reset form, a flash or a bare alert says nothing (I1, R1)
                invalid = [r for r in invalid if r.get("reason") == "aria-invalid"]
                field_errors = [e for e in field_errors if e.get("tied")]
                may_refuse = same_form and self._holds_typed(before)
            if digest.fields and (invalid or field_errors) and may_refuse:
                self._not_sent(invalid, field_errors, watch)
            sure = conf >= apply_judge.PAGE_STATE_MIN_CONF and judged == seen
            if account and state in _OPENED_BY_ACCOUNT and sure:
                raise self._maybe_only_the_account(state, conf)
            # a code screen the read is unsure of is the code screen when its
            # structure settles it (a code box, no password box)
            code_screen = state == "code_gate" and judged == seen and (
                sure or apply_judge.structural_kind(self._facts, strict=True) == "code_gate")
            if code_screen:
                if code_entered:
                    raise _Parked("needs_human", f"the emailed code was not accepted (the "
                                                 f"code screen came back, {conf:.2f}); "
                                                 f"{CHECK_SENT_REASON}")
                plan = apply_judge.plan(digest, self.catalog, answers, company=self._company())
                rec["flags"] = dict(plan.flags)
                self._code_gate(digest, plan, rec)
                code_entered = True
                last_seen, judged, changed_at = None, None, time.monotonic()
                self._submit_at = None
                continue
            if banners and not watch.pending:
                self._decide("after_submit", "an error banner after the submit click",
                             banner=banners[0]["text"], sent=watch.first())
                raise _Parked("needs_human", f"the site showed an error after the submit "
                                             f"click ({_cap(banners[0]['text'], 160)}); "
                                             f"{CHECK_SENT_REASON}")
            if state == "error_or_dead" and sure and not watch.pending:
                raise _Parked("needs_human", f"an error page after the submit click "
                                             f"({conf:.2f}; {_cap(digest.title, 80)}); "
                                             f"{CHECK_SENT_REASON}")
            busy = bool(watch.pending) or now - changed_at < POST_SUBMIT_QUIET_S
            if in_flight and not watch.pending and not late_look:
                # SP6 review R2-M2: the answer came during this look; the page
                # is read again (a confirmation that came with it) before any
                # ruling, the quiet window counted from the answer; past the
                # wait's end, once more only
                answers_seen += 1
                if answers_seen == 1:
                    self._decide("after_submit", "a request's answer came during the look; "
                                                 "the page is read once more")
                changed_at = time.monotonic()
                late_look = now - start >= POST_SUBMIT_WAIT_S
                continue
            if not busy or now - start >= POST_SUBMIT_WAIT_S:
                break
            self.page.wait_for_timeout(int(POST_SUBMIT_POLL_S * 1000))
        if judged != seen:
            # the last look was never judged (the budget ran out, or the page
            # moved on the last look): the requests decide on a fresh read
            answers = self._judge_page(digest)
            state, conf = apply_judge.read_page_state(answers)
            self._new_page_record(state, conf, digest=digest, answers=answers)
            self._decide("after_submit", f"the last read was stale; read once more "
                                         f"({state} {conf:.2f})")
            self._confirmed("", state, conf, digest, watch, code_entered, judged=True)
        self._inconclusive(state, conf, digest, watch, before, handoff)

    def _confirmed(self, marker: str, state: str, conf: float, digest: apply_form.FormDigest,
                   watch: SendWatch, code_entered: bool, *, judged: bool) -> None:
        """The confirmation test of a post-submit look: received words new
        since the click (`marker`), or the judge's confirmation of this very
        page (`judged`) at `CONFIRMATION_MIN_CONF` with no form field and no
        send button. Raises `submitted`."""
        send_button = any(apply_judge.SEND_WORDS.search(b.text) for b in digest.buttons)
        if not (marker or (state == "confirmation" and judged
                           and conf >= apply_judge.CONFIRMATION_MIN_CONF
                           and not digest.fields and not send_button)):
            return
        why = (f"the page shows {marker!r}, which it did not before the click"
               if marker else f"read as confirmation ({conf:.2f}) with no form field "
                              f"and no send button")
        self._decide("after_submit", f"confirmation: {why}", sent=watch.first())
        raise _Parked("submitted", "confirmation page after the emailed code"
                      if code_entered else "confirmation page")

    def _page_text(self) -> str:
        try:
            return apply_fill.page_text(self.page)
        except Exception:       # noqa: BLE001  (a page double, a page mid-navigation)
            return ""

    def _post_submit_validity(self, as_before: bool) -> dict[str, list]:
        """The validity of the submit's form when its button is still on the
        page (the same document, or a server's answer with the same form),
        and the error texts of every frame. A control that would not
        validate counts only on the form as it was before the click
        (`as_before`: a new form's empty boxes say nothing about the send);
        one the site marked `aria-invalid` counts on any page."""
        empty: dict[str, list] = {"invalid": [], "errors": []}
        try:
            report = apply_form.validity_report(self.page)
            locator = self._submit_locator()
            # the submit's form, or when its locator no longer names one
            # control (an inserted error summary shifts a path), the form
            # that held the filled fields
            report["invalid"] = apply_form.validity_report(
                self.page, locator, self._filled_here)["invalid"]
            if not as_before:
                report["invalid"] = [r for r in report["invalid"]
                                     if r.get("reason") == "aria-invalid"]
            return report
        except Exception:       # noqa: BLE001  (a page double, a page mid-navigation)
            return empty

    def _submit_locator(self) -> tuple[int, str] | None:
        """The submit button's locator when it still names one control."""
        loc = getattr(self, "_submit_at", None)
        if not loc:
            return None
        try:
            return loc if apply_form.resolve(self.page, loc).count() == 1 else None
        except Exception:       # noqa: BLE001
            return None

    def _not_sent(self, invalid: list, field_errors: list, watch: SendWatch) -> None:
        """Validation errors after the submit click: the form refused the
        send, so nothing went through (`submit_clicked` is reset); the job
        waits for the person with the messages and the fields. When nothing
        at all left the page (`SendWatch.any`), the form refused the send as
        typed: the gate repairs it once (`_Refused`, ADV-02)."""
        self.submit_clicked = False
        rows = [_invalid_words(r) for r in invalid[:3]]
        rows += [f"the form says: {_cap(e['text'], 100)}" for e in field_errors[:2]]
        self._decide("after_submit", "validation errors after the submit click; nothing was "
                                     "sent", invalid=invalid[:5],
                     errors=[e["text"] for e in field_errors[:5]], request=watch.first())
        park = _Parked("needs_human", f"{NOT_SENT_REASON}: validation errors "
                                      f"({_cap('; '.join(rows), 260)})")
        if not watch.any() and self._submit_repairs < 1:
            problems = [{**r, "text": r.get("message") or "", "kind": "invalid"} for r in invalid]
            problems += [{"label": "", "message": e["text"], "reason": "error", "text": e["text"],
                          "ident": e.get("ident") or "", "name": e.get("name") or "",
                          "kind": "error"} for e in field_errors]
            raise _Refused(problems, park)
        spared = self._spared_park([str(e.get("text") or "") for e in field_errors])
        raise spared if spared is not None else park

    def _inconclusive(self, state: str, conf: float, digest: apply_form.FormDigest,
                      watch: SendWatch, before: Mapping[str, Any], handoff: bool) -> None:
        """No confirmation, no error, no code screen within the wait: the
        requests that left decide. Nothing at all left and the form as it
        was: the submit did not go through (`submit_clicked` reset). Anything
        else the run cannot tell is the person's to check (never re-queued,
        so never sent twice): nothing seen leaving and a changed page; a
        request only to a host outside the application's sites; a request
        and the form again; a request and a sign-in (a session that
        expired: a sign-in read with its account boxes); a request and a
        confident form or review page with its own
        send button. A request to the application's sites and another page:
        "submitted (unconfirmed)"."""
        same = (str(before.get("url") or "") == str(self.page.url)
                and tuple(before.get("fields") or ()) == _fields_sig(digest))
        read = f"{state} {conf:.2f}"
        sure = conf >= apply_judge.PAGE_STATE_MIN_CONF
        when = self._sent_when
        if handoff:
            raise self._maybe_only_the_account(state, conf)
        if not watch.any():
            if same:
                self.submit_clicked = False
                self._decide("after_submit", "no request left and the form is as it was",
                             read=read)
                raise _Parked("needs_human", f"{NOT_SENT_REASON} (the form did not change after "
                                             f"the click and no request left; it reads as "
                                             f"{read})")
            self._decide("after_submit", "the page changed and no request was seen leaving",
                         read=read)
            raise _Parked("needs_human", f"{CHECK_SENT_REASON}: the page changed {when} and "
                                         f"reads as {read}; no request was seen leaving")
        first = _cap(watch.first(), 120)
        self._decide("send_observed", f"a request left {when} ({watch.first()})", read=read,
                     same_form=same, application_site=bool(watch.sent))
        if not watch.sent:
            where = ("from a new tab or a worker the run could not tie to the job's page"
                     if watch.first() in watch.unplaced_sends()
                     else "for a host outside the application's sites")
            raise _Parked("needs_human", f"{CHECK_SENT_REASON}: a request left {where} "
                                         f"({first}) and the page reads as {read}")
        if same:
            raise _Parked("needs_human", f"{CHECK_SENT_REASON}: a request left {when} ({first}) "
                                         f"and the page reads as the form again ({read})")
        if sure and state in ("login_wall", "signup_form") and _credential_form(digest):
            raise _Parked("needs_human", f"{CHECK_SENT_REASON}: {when} the page asks to sign in "
                                         f"({read}); the session may have expired before the "
                                         f"send")
        if sure and state in ("application_form", "review_page") and any(
                apply_judge.SEND_WORDS.search(b.text) for b in digest.buttons):
            raise _Parked("needs_human", f"{CHECK_SENT_REASON}: a request left {when} ({first}) "
                                         f"and the page is a form with its own send button "
                                         f"({read})")
        raise _Parked("submitted", f"submitted (unconfirmed): a request left {when} ({first}); "
                                   f"the page after reads as {read}")

    @staticmethod
    def _maybe_only_the_account(state: str, conf: float) -> _Parked:
        return _Parked("needs_human", f"after the account page's submit the page reads as "
                                      f"{state} ({conf:.2f}): the click may only have made "
                                      f"the account; check whether the application went "
                                      f"through, then Re-queue or Mark applied")

    def _post_submit_digest(self, watch: SendWatch | None = None) -> apply_form.FormDigest:
        """Validate a post-submit destination before reading or acting on it.

        A page that left the allowed sites after the submit click is read no
        further: "submitted (unconfirmed)" when a request to the
        application's sites left first, else the person checks. Either way
        the queue never sends it again."""
        try:
            self._check_host(self.page.url)
            return self._drop_foreign_controls(self._extract())
        except _Parked as p:
            if watch is not None and watch.sent:
                raise _Parked("submitted", f"submitted (unconfirmed): {p.reason} (after "
                                           f"{_cap(watch.first(), 120)})") from None
            raise _Parked("needs_human", f"{CHECK_SENT_REASON}: after the submit click "
                                         f"{p.reason}", CHECK_SENT_NOTE) from None

    def _code_gate(self, digest: apply_form.FormDigest, plan: FillPlan, rec: dict) -> None:
        self._no_form_on_linkedin("the code step")
        site = digest.url_host or _host(self.page.url)
        code = self.inbox.fetch_code(self.page, site, str(self.r.run_context().get("inbox_url") or ""))
        if not code:
            raise _Parked("needs_human", "emailed code needed", CODE_NOTE)
        target = _code_field(digest.fields)
        if target is None:
            raise _Parked("needs_human", "code gate without a code box", CODE_NOTE)
        self._keep_secret_box(target.locator)     # masked from here on, typed or not
        try:
            apply_form.resolve(self.page, target.locator).first.fill(str(code), timeout=5_000)
        except Exception as e:  # noqa: BLE001  (a fill exception may carry the private code)
            self._trace("error", step="code_gate.fill", error=type(e).__name__)
            raise _Parked("needs_human", "emailed code could not be filled", CODE_NOTE) from None
        self._trace("code", box=target.label)
        rec["filled"].append({"n": target.n, "label": target.label,
                              "value": HIDDEN,
                              "type": target.type, "id_or_name": target.id_or_name,
                              "upload": False, "hidden": True})
        advance = plan.buttons.get("advance")
        submit = plan.buttons.get("submit")
        if advance is not None and advance[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF:
            role, button, minimum = ("advance", advance,
                                     apply_judge.BUTTON_ADVANCE_MIN_CONF)
        elif submit is not None and submit[1] >= apply_judge.BUTTON_SUBMIT_MIN_CONF:
            role, button, minimum = ("submit", submit,
                                     apply_judge.BUTTON_SUBMIT_MIN_CONF)
        elif advance is not None:
            role, button, minimum = ("advance", advance,
                                     apply_judge.BUTTON_ADVANCE_MIN_CONF)
        elif submit is not None:
            role, button, minimum = ("submit", submit,
                                     apply_judge.BUTTON_SUBMIT_MIN_CONF)
        else:
            raise _Parked("needs_human", "code entered; no button to continue", CODE_NOTE)
        if button[1] < minimum:
            raise _Parked("needs_human",
                          f"code entered; {role} button confidence {button[1]:.2f} "
                          f"below {minimum:.2f}", CODE_NOTE)
        if not self.submit_clicked and _sends_application(digest, button[0],
                                                          account_only=False):
            # before the submit gate has let the application go, a code box
            # beside a "Submit application", a "Confirm" or a "Finish" is the
            # form's last step (INV-06)
            raise _Parked("needs_human", f"code entered; its button "
                                         f"({_cap(_button_text(digest, button[0]), 60)}) would "
                                         f"send the application", CODE_NOTE)
        result = self._click(digest, button[0], role, rec, conf=button[1])
        if result.refused:
            raise _Parked("needs_human", f"code entered; its button "
                                         f"({_cap(_button_text(digest, button[0]), 60)}) changed "
                                         f"before the click: {result.refused}", CODE_NOTE)
        if result.clicked or result.late:
            if role == "submit":
                # a landed click in the submit role may have sent: the job
                # never reads as unsent after it, so it is never sent twice
                # (INV-06)
                self.submit_clicked = True
            self._code_sent = True      # a code can finish a send the site held back

    # -- the end --------------------------------------------------------------------------------

    def _finish(self, status: str, reason: str, tab_note: str = "") -> Outcome:
        usage = _usage_delta(self.usage_before, jev.usage())
        usage["generated"] = generated_count(self.pages)
        self._stop_late_watch()
        self._flush_decisions()
        text = ""
        if self.page is not None:
            try:
                text = apply_fill.page_text(self.page)
            except Exception:       # noqa: BLE001  (the page is gone)
                text = ""
        self.trace.finish(status, reason, self.page,
                          extra_mask=self._secret_masks(self.page) if self.page is not None else [])
        record = ""
        if self.folder is not None:
            try:
                record = str(write_record(
                    self.folder, self.entry, status, reason, self.pages, usage, text,
                    missing=self.missing,
                    trace_dir=self.trace.rel_dir if self.trace.enabled else "",
                    attempt=self.trace.attempt if self.trace.enabled else 0))
            except Exception as e:      # noqa: BLE001  (a record failure must not lose the finish)
                self.log.warning("job %s: record not written: %s", self.job_id, e)
        if not tab_note and self.page is not None and status != "submitted":
            try:
                tab_note = f"{self.page.url} | {self.page.title()}"
            except Exception:       # noqa: BLE001
                tab_note = ""
        self._finish_entry(status, tab_note, record, reason)
        if self._send_watch is not None:
            self._send_watch.stop()
        if self.page is not None:
            if status == "submitted" and reason.startswith("confirmation page"):
                # only a confirmation closes the tab (TERM-03): an unconfirmed
                # send stays open for the person to check
                try:
                    self.page.close()
                except Exception:       # noqa: BLE001
                    pass
            else:
                self.r.parked_pages.append(self.page)
        self.log.info("job %s: %s (%s)", self.job_id, status, reason)
        return Outcome(job_id=self.job_id, status=status, reason=reason, record_path=record,
                       pages=len(self.pages), jev_usage=usage,
                       browser_closed=self.browser_closed)


    def _finish_entry(self, status: str, tab_note: str, record: str, reason: str) -> None:
        """`apply_queue.finish`, once more after `FINISH_RETRY_S` when it
        raises (a lock held by the dashboard), then an error naming the job;
        the drain goes on and the entry stays `in_progress` for the human."""
        for attempt in (1, 2):
            try:
                apply_queue.finish(self.job_id, status, tab_note=tab_note, record=record,
                                   notes=reason, path=self.r.queue_path)
                return
            except Exception as e:      # noqa: BLE001  (the queue write must not end the drain)
                if attempt == 1:
                    self.log.warning("job %s: queue finish failed (%s); retrying in %s s",
                                     self.job_id, e, FINISH_RETRY_S)
                    self.r.sleep(FINISH_RETRY_S)
                else:
                    self.log.error("job %s: queue finish failed twice (%s: %s); the entry "
                                   "stays in_progress with status %s unrecorded",
                                   self.job_id, type(e).__name__, e, status)


# --- the summary and the CLI ---------------------------------------------------------------

def summary_line(outcomes: list[Outcome]) -> str:
    counts = {s: 0 for s in ("submitted", "ready_to_submit", "needs_human", "failed")}
    requests = tokens = 0
    usd = 0.0
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
        requests += int(o.jev_usage.get("requests", 0))
        tokens += int(o.jev_usage.get("input_tokens", 0))
        usd += float(o.jev_usage.get("usd", 0.0))
    return (f"drained {len(outcomes)}: submitted {counts['submitted']}, "
            f"ready_to_submit {counts['ready_to_submit']}, needs_human {counts['needs_human']}, "
            f"failed {counts['failed']}; Jev {requests} requests, {tokens} tokens, ${usd:.4f}")


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE.parent / ".env")
    except Exception:       # noqa: BLE001  (python-dotenv absent: the environment is what it is)
        pass


def _settings_from_args(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_settings()
    if getattr(args, "no_submit", False):
        cfg["auto_apply_submit"] = False
    if getattr(args, "headless", False):
        cfg["auto_apply_headless"] = True
    if getattr(args, "jev", None):
        cfg["auto_apply_jev_mode"] = args.jev
    if getattr(args, "cap", None) is not None:
        cfg["auto_apply_batch_cap"] = int(args.cap)
    return cfg


def doctor(profile_dir: Path | None = None, out=None) -> int:
    """One line per auto-apply setup row plus the profile dir; 0 when every
    row passes (a missing profile is created by the first run)."""
    import setup_check
    out = out or sys.stdout
    profile = Path(profile_dir) if profile_dir else default_profile_dir()
    try:
        import settings
        stored = settings.load()
        mode = str(stored.get("auto_apply_jev_mode") or "typesafe").strip().lower()
        has_key = bool(settings.secret_status().get("TYPESAFE_API_KEY")) or bool(
            os.environ.get(jev.KEY_ENV, "").strip())
    except Exception:       # noqa: BLE001
        mode, has_key = "typesafe", bool(os.environ.get(jev.KEY_ENV, "").strip())
    sdk = setup_check.module_found("typesafe_sdk")
    playwright_found = setup_check.module_found("playwright")
    chrome = setup_check.chrome_installed()
    browser = playwright_found and (chrome or setup_check.chromium_installed())
    rows = [("TypeSafe API key", has_key or mode != "typesafe"),
            ("typesafe_sdk", sdk or mode != "typesafe"),
            ("playwright", playwright_found),
            ("browser: Google Chrome" if chrome else "chromium (Google Chrome not found)",
             browser)]
    chromium = browser
    for name, ok in rows:
        print(f"{'ok     ' if ok else 'MISSING'}  {name}", file=out)
    print(f"{'ok     ' if profile.is_dir() else 'absent '}  browser profile: {profile}"
          + ("" if profile.is_dir() else " (created by the first run or `login`)"), file=out)
    print(f"judge mode: {mode}", file=out)
    warnings = setup_check.auto_apply_warnings(has_key, mode, sdk, playwright_found, chromium)
    for w in warnings:
        print(f"  {w}", file=out)
    return 0 if not warnings else 2


def login(profile_dir: Path | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """Open the persistent profile at LinkedIn's login and the inbox in two
    tabs, headed, and wait for the user to close the window."""
    from playwright.sync_api import sync_playwright
    profile = Path(profile_dir) if profile_dir else default_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    inbox_url = str(apply_queue.build_context().get("inbox_url") or apply_queue.DEFAULT_INBOX_URL)
    with sync_playwright() as pw:
        ctx = launch_profile(pw, profile, headless=False)
        first = ctx.pages[0] if ctx.pages else ctx.new_page()
        first.goto(LINKEDIN_LOGIN_URL)
        ctx.new_page().goto(inbox_url)
        print("Sign in to both tabs, then close the window")
        hold_until_closed(ctx, sleep=sleep)
        try:
            ctx.close()
        except Exception:       # noqa: BLE001
            pass
    return 0


# --- the probe ------------------------------------------------------------------------------

def _one_line(text: str, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _probe_catalog():
    """An empty fact catalog: the probe asks about the page, never about a
    job's facts."""
    return apply_facts.FactCatalog([])


def _probe_linkedin(page) -> tuple[apply_linkedin.Decision | None, str]:
    """(the LinkedIn handler's decision on `page`, the line that says it); a
    job page gets the loop's wait for its top card."""
    kind = apply_linkedin.url_kind(str(page.url))
    if not kind:
        return None, "not on LinkedIn"
    if kind in ("signed_out", "redirector"):
        return None, str(linkedin_step(str(page.url), None))
    view, waited = linkedin_view(page, wait_s=LINKEDIN_READY_S if kind == "job" else 0)
    d = apply_linkedin.decide(view)
    step = linkedin_step(str(page.url), d)
    return d, (f"{step}; {d.why}; waited {waited} ms" if step
               else f"not taken ({d.why}; the judge reads this page)")


def _probe_page(page, n: int, judge: Any, out, *,
                park_mode: bool = False) -> tuple[apply_form.FormDigest, FillPlan | None,
                                                  apply_linkedin.Decision | None]:
    """Print page `n` as the run would read it: the digest (fields, buttons,
    the text's head), the judge's read when a judge is given, what the
    LinkedIn handler and the fieldless-posting fallback would do, and, with
    a judge, the step the loop would take (`loop_step`)."""
    linkedin, linkedin_line = _probe_linkedin(page)
    digest = apply_form.extract(page, content_site=lambda url: content_frame_site(
        url, str(page.url)))
    print(f"page {n}: {page.url}", file=out)
    print(f"  title: {_one_line(digest.title, 120)}", file=out)
    print(f"  fields ({len(digest.fields)}):", file=out)
    for f in digest.fields:
        extra = f" ({len(f.options)} options)" if f.options else ""
        frame = f" [frame {f.locator[0]}]" if f.locator[0] else ""
        print(f"  field [{f.n}] {f.type}{' required' if f.required else ''} "
              f"{_one_line(f.label, 80)!r}{extra}{frame}", file=out)
    print(f"  buttons ({len(digest.buttons)}):", file=out)
    for b in digest.buttons:
        frame = f" [frame {b.locator[0]}]" if b.locator[0] else ""
        print(f"  button [{b.n}] {_one_line(b.text, 60)!r} ({b.kind_hint or 'control'}){frame}",
              file=out)
    print(f"  text: {_one_line(digest.text, 400)}", file=out)
    plan = None
    if judge is not None:
        catalog = _probe_catalog()
        # the run's own read (`_JobRun._read`): the read request combined
        # with the page's structure, then the mapping (`_JobRun._map`)
        facts = apply_judge.page_facts(digest, str(page.url))
        state, questions = apply_judge.read_questions(digest, str(page.url))
        raw = dict(judge.judge(state, questions))
        combined = apply_judge.read_page(raw, facts)
        answers = {k: v for k, v in raw.items() if k in questions}
        if "page_state" in answers:
            answers["page_state_judged"] = answers["page_state"]
        answers["page_state"] = apply_judge.read_answer(combined)
        read, conf = combined.state, combined.conf
        with_fields = bool(digest.fields) or read != "job_posting"
        s2, q2 = apply_judge.page_questions(digest, catalog, {}, fields=with_fields)
        if q2:
            answers.update({k: v for k, v in judge.judge(s2, q2).items() if k in q2})
        print(f"  judge: page_state {read} {conf:.2f} "
              f"({apply_trace.page_state_reads(answers, top=5)}; the judge read "
              f"{combined.judged} {combined.judged_conf:.2f})", file=out)
        if combined.why:
            print(f"  judge: {combined.why}", file=out)
        for b in digest.buttons:
            role, rconf = apply_judge._choice_of(answers, f"button_{b.n}_role")
            print(f"  judge: button [{b.n}] {role} {rconf:.2f}", file=out)
        plan = apply_judge.plan(digest, catalog, answers)
        apart, unclassified, _ = posting_context(page, digest, plan)
        entry = form_entry_choice(page, digest, plan, park_mode=park_mode,
                                  filled=plan_fills(plan))
        step = loop_step(str(page.url), digest, plan, read, conf,
                         apply_trace.page_state_reads(
                             {"page_state": answers.get("page_state_judged")}),
                         park_mode=park_mode,
                         linkedin=linkedin, answers=answers, apart=apart,
                         unclassified=unclassified,
                         form_entry=entry.n if entry is not None else None, facts=facts)
    print(f"  linkedin handler: {linkedin_line}", file=out)
    n_fl = fieldless_apply_choice(digest)
    print("  fieldless posting: " + (f"would click [{n_fl}] {_button_text(digest, n_fl)!r}"
                                     if n_fl is not None else
                                     f"no ({len(digest.fields)} form field(s))" if digest.fields
                                     else "no button says apply"), file=out)
    print(f"  account screen: {'yes' if _credential_form(digest) else 'no'}", file=out)
    if judge is not None:
        print(f"  the loop would: {step}", file=out)
    return digest, plan, linkedin


def _probe_entry(page, digest: apply_form.FormDigest, plan: FillPlan | None,
                 linkedin: apply_linkedin.Decision | None = None) -> tuple[Any, str, str]:
    """(the locator of the Apply entry `--follow-apply` may click, its text,
    "") or (None, "", why not). On LinkedIn only the handler's offsite Apply,
    whatever else the page holds. Elsewhere never on a page with form fields
    (an Apply there may send the form), and only a plain Apply
    (`_PLAIN_APPLY`): an Easy, quick, one-click or third-party apply can send
    a stored profile at once on a signed-in profile."""
    if apply_linkedin.is_linkedin(str(page.url)):
        if linkedin is None or linkedin.kind != "offsite" or linkedin.control is None:
            return None, "", f"the LinkedIn handler takes no Apply here " \
                             f"({linkedin.why if linkedin else 'not a job page'})"
        c = linkedin.control
        return page.main_frame.locator(c.css), c.label, ""
    if digest.fields:
        return None, "", f"the page has {len(digest.fields)} form field(s); an Apply there " \
                         "may send the form"
    n = None
    if plan is not None:
        entry = plan.buttons.get("apply_entry")
        if entry is not None and entry[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF:
            n = entry[0]
    if n is None:
        n = fieldless_apply_choice(digest)
    if n is None:
        return None, "", "no Apply control"
    text = _button_text(digest, n)
    if not _PLAIN_APPLY.match(" ".join(text.split())):
        return None, "", f"[{n}] {text!r} is not a plain Apply entry"
    button = next(b for b in digest.buttons if b.n == n)
    return apply_form.resolve(page, button.locator), f"[{n}] {_one_line(text, 60)!r}", ""


def _probe(ctx, url: str, *, follow_apply: bool, judge: Any, out, settle_s: float,
           park_mode: bool = False) -> int:
    page = ctx.new_page()
    print(f"probe: {url}", file=out)
    try:
        # the run's own first load (`open_page`): domcontentloaded, one retry
        # of a dropped load, then the settle
        for row in open_page(page, url, timeout_ms=PROBE_GOTO_MS, settle_s=settle_s):
            print(f"  load: {row['why']}", file=out)
    except Exception as e:      # noqa: BLE001  (a dead or slow page is the answer)
        print(f"probe: the page did not load ({type(e).__name__})", file=out)
        return 1
    digest, plan, linkedin = _probe_page(page, 1, judge, out, park_mode=park_mode)
    if not follow_apply:
        return 0
    loc, label, why = _probe_entry(page, digest, plan, linkedin)
    if loc is None:
        print(f"follow-apply: nothing clicked ({why})", file=out)
        return 0
    popup, signal, _ = click_entry(page, loc)
    target, _info = await_destination(popup or page)
    print(f"follow-apply: clicked {label}; "
          f"{'a new tab' if popup or target is not page else 'the same tab'} at {target.url}",
          file=out)
    _probe_page(target, 2, judge, out, park_mode=park_mode)
    return 0


def probe(url: str, *, follow_apply: bool = False, judge: Any = None, headed: bool = False,
          profile_dir: Path | None = None, context: Any = None, out=None,
          settle_s: float = PROBE_SETTLE_S, park_mode: bool = False) -> int:
    """Read `url` the way the run would and print it: the digest, the judge's
    read (`judge`, asked once per page), what the LinkedIn shortcut and the
    fieldless-posting fallback would click, and with a judge the step the
    loop would take (in park mode with `park_mode`). `follow_apply` clicks only
    that Apply entry (never on a page with form fields) and prints the page
    it leads to. Nothing is typed, uploaded, ticked or submitted.

    The browser is a fresh temporary profile (headless unless `headed`), or
    the persistent profile at `profile_dir`; `context` injects one (tests).
    Exit 0, or 1 when the page did not load."""
    out = out or sys.stdout
    if context is not None:
        return _probe(context, url, follow_apply=follow_apply, judge=judge, out=out,
                      settle_s=settle_s, park_mode=park_mode)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = None
        if profile_dir is not None:
            ctx = launch_profile(pw, Path(profile_dir), headless=not headed)
        else:
            try:
                browser = pw.chromium.launch(channel=BROWSER_CHANNEL, headless=not headed)
            except Exception:       # noqa: BLE001  (Chrome absent: the bundled Chromium)
                browser = pw.chromium.launch(headless=not headed)
            ctx = browser.new_context(viewport=VIEWPORT)
        try:
            return _probe(ctx, url, follow_apply=follow_apply, judge=judge, out=out,
                          settle_s=settle_s, park_mode=park_mode)
        finally:
            try:
                ctx.close()
            except Exception:       # noqa: BLE001
                pass
            if browser is not None:
                browser.close()


def main(argv: list[str] | None = None) -> int:
    """Exit codes: 0 drained (or nothing queued), 1 unexpected error, 2 not
    configured (no judge, or the job id is not queued)."""
    ap = argparse.ArgumentParser(prog="apply_run",
                                 description="Jev-judged auto-apply: drain the queue.")
    sub = ap.add_subparsers(dest="verb", required=True)

    def _verbose(p):
        p.add_argument("--verbose", action="store_true", help="DEBUG logging")

    def _run_flags(p):
        _verbose(p)
        p.add_argument("--cap", type=int, default=None, help="jobs per drain")
        p.add_argument("--no-submit", action="store_true", dest="no_submit",
                       help="park every application at its review page")
        p.add_argument("--headless", action="store_true", help="no browser window")
        p.add_argument("--jev", choices=jev.MODES, default=None, help="judge mode")
        p.add_argument("--profile", default=None, help="browser profile dir")
        p.add_argument("--queue", default=None, help="queue file")

    _run_flags(sub.add_parser("drain", help="claim and run queued jobs until the queue is empty"))
    p = sub.add_parser("one", help="run one queued job by id")
    p.add_argument("job_id")
    _run_flags(p)
    p = sub.add_parser("login", help="sign in to LinkedIn and the inbox in the profile")
    _verbose(p)
    p.add_argument("--profile", default=None)
    p = sub.add_parser("doctor", help="check the auto-apply setup")
    _verbose(p)
    p.add_argument("--profile", default=None)
    p = sub.add_parser("probe", help="read one page as the run would; types and sends nothing")
    p.add_argument("url")
    _verbose(p)
    p.add_argument("--follow-apply", action="store_true", dest="follow_apply",
                   help="click only the Apply entry and read the page it opens")
    p.add_argument("--judge", action="store_true", help="ask the judge once per page")
    p.add_argument("--jev", choices=jev.MODES, default=None, help="judge mode for --judge")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--no-submit", action="store_true", dest="no_submit",
                   help="describe the loop's step in park mode")
    p.add_argument("--profile", default=None,
                   help="a persistent browser profile dir (default: a fresh temporary one)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    profile = Path(args.profile) if getattr(args, "profile", None) else None
    try:
        if args.verb == "doctor":
            return doctor(profile)
        if args.verb == "login":
            return login(profile)
        if args.verb == "probe":
            judge = None
            if args.judge:
                _load_env()
                mode = args.jev or load_settings()["auto_apply_jev_mode"]
                try:
                    judge = jev.get(mode)
                except (jev.JevUnavailable, ValueError) as e:
                    print(f"apply_run: {e}", file=sys.stderr)
                    return 2
            return probe(args.url, follow_apply=args.follow_apply, judge=judge,
                         headed=args.headed, profile_dir=profile, park_mode=args.no_submit)
        cfg = _settings_from_args(args)
        if cfg["auto_apply_jev_mode"] in ("fake", "replay"):
            print("apply_run: fake and replay judges are fixture-only; use typesafe for a "
                  "production queue", file=sys.stderr)
            return 2
        _load_env()
        try:
            judge = jev.get(cfg["auto_apply_jev_mode"])
        except (jev.JevUnavailable, ValueError) as e:
            print(f"apply_run: {e}", file=sys.stderr)
            return 2
        queue = Path(args.queue) if args.queue else None
        answergen = (apply_answergen.Generator() if cfg["auto_apply_generate"]
                     else NotConfigured())
        runner = Runner(jev=judge, queue_path=queue, profile_dir=profile, settings=cfg,
                        answergen=answergen)
        if args.verb == "one":
            entry = apply_queue.claim("apply_run", path=queue, job_id=args.job_id)
            if entry is None:
                print(f"apply_run: job {args.job_id} is not queued", file=sys.stderr)
                return 2
            outcomes = [runner.run_job(entry)]
        else:
            outcomes = runner.drain(cfg["auto_apply_batch_cap"])
        print(summary_line(outcomes))
        return 0
    except Exception as e:      # noqa: BLE001  (one line, documented exit 1)
        print(f"apply_run: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
