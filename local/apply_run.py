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

Per job (`Runner.run_job`): the fact catalog from the job folder's apply.md
(`apply_facts.build`), then one page at a time up to `apply_judge.MAX_PAGES`
and the job's wall clock (`JOB_WALL_CLOCK_S`): `apply_form.extract` -> one Jev request
(`apply_judge.page_questions`) -> `read_page_state` -> the state table from
the design (section 3.5):

    job_posting            click the Apply entry, follow a popup
    application_form       plan, fill, verify, type the keyring password into a
                           password box, then advance, or the submit gate
    review_page            fill and verify editable controls, then submit gate
    login_wall / signup    fill the account email and hidden keyring password
    code_gate              read the emailed code in a separate inbox tab
    confirmation           finish submitted
    captcha / payment / error / other / low confidence
                           park needs_human

The submit gate is `can_submit(plan, verification, settings)`; it returns the
first failing reason. A submit click is recorded before the page is judged
again, so a crash after it finishes the entry `submitted` with an
"unconfirmed" note. Every terminal moment writes the record (fields, uploads,
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
import json
import logging
import os
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
import apply_queue  # noqa: E402
import apply_trace  # noqa: E402
import ats_accounts  # noqa: E402
import jev  # noqa: E402
from apply_judge import FillPlan, VerifyResult  # noqa: E402

log = logging.getLogger("apply_run")

JOB_WALL_CLOCK_S = 15 * 60         # per job, on the injectable clock (a solved CAPTCHA's wait is added back)
GENERATE_MAX = 3                   # generated answers per job (spec 3.7)
POPUP_TIMEOUT_MS = 5_000           # for the Apply entry to open a new tab
CLICK_TIMEOUT_S = 20               # click_button's wait for a change
SUBMIT_SETTLE_S = 10               # after a quiet submit click: wait this long for the page
HOLD_POLL_S = 1.0                  # while holding the window open
FINISH_RETRY_S = 1.0               # before the one retry of a failed queue finish
LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_HOSTS = ("linkedin.com", "www.linkedin.com")
LINKEDIN_REDIRECTOR = "/safety/go"  # the hop an off-site Apply link goes through
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
    "phenompeople.com", "trinethire.com"))
# Bot-check providers. Their frames' controls are never filled or clicked: a
# challenge is the user's to solve in the visible window.
CAPTCHA_SITES = frozenset(("hcaptcha.com", "recaptcha.net", "arkoselabs.com",
                           "funcaptcha.com", "geetest.com"))
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
CODE_NOTE = "enter the emailed code manually, then Re-queue"
REVIEW_NOTE = "review and submit"
SUBMIT_FAILED_NOTE = "submit did not register; review and submit"
CLOSED_REASON = "the browser window was closed"
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
# What a LinkedIn job page may still be read as over `_linkedin_apply`: signed
# out, a closed posting, and the other reads that park.
_LINKEDIN_POSTING_KEEPS = frozenset(("login_wall", "signup_form", "confirmation",
                                     "error_or_dead", "captcha_or_bot_check",
                                     "payment_request"))
# What the page after a submit click on an account page ("Create account and
# apply") may read as when the click only made the account and opened the
# application (`_JobRun._after_submit`): the job then waits for the user,
# since a sent application's page can look the same.
_OPENED_BY_ACCOUNT = frozenset(("application_form", "login_wall", "signup_form"))
_APPLY_WORD = re.compile(r"\bapply\b", re.I)
_SUBMIT_WORD = re.compile(r"\bsubmit\b", re.I)


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
_NEW_PASSWORD = re.compile(r"\b(create|new|choose|set|confirm|re-?enter|repeat|verify)\b"
                           r"|new[_-]?pass|confirm[_-]?pass", re.I)
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
            plan = apply_judge.plan(link_digest, self.run.catalog, self.run._judge_page(link_digest))
            if plan.buttons.get("advance", (None, 0))[1] < apply_judge.BUTTON_ADVANCE_MIN_CONF:
                return False
            page.goto(target, timeout=self._nav_timeout())
            self.run._check_host(page.url)
            fresh = self.run._drop_foreign_controls(apply_form.extract(page))
            state, confidence = apply_judge.read_page_state(self.run._judge_page(fresh))
            if state != "signup_form" or confidence < apply_judge.PAGE_STATE_MIN_CONF:
                return False
            return self.signup(page, fresh, fresh.url_host or _host(page.url))
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
        frames = apply_form.frames(page)
        guard = _NavGuard(self.run, page)
        try:
            answers = self.run._judge_page(digest)
            plan = apply_judge.plan(digest, self.run.catalog, answers)
            rec = self.run.pages[-1] if self.run.pages else {"flags": {}}
            plan = self.run._complete_option_plan(digest, answers, plan, rec)
            advance = plan.buttons.get("advance")
            if advance is None or advance[1] < apply_judge.BUTTON_ADVANCE_MIN_CONF:
                advance = plan.buttons.get("submit")
            if advance is None or advance[1] < apply_judge.BUTTON_ADVANCE_MIN_CONF:
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
                self._record(digest, email if emails else "", advance[0], len(passwords), filled)
                # an aborted navigation leaves the tab on a browser error page,
                # so the note for the human names the page before the click
                before = f"{page.url} | {page.title()}"
                result = apply_fill.click(page, digest, advance[0],
                                          timeout_s=self._timeout() / 1000)
            finally:
                guard.stop()
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


def _on_linkedin_redirector(url: str) -> bool:
    """Is `url` LinkedIn's `/safety/go/` hop to an off-site Apply page?"""
    parts = urlsplit(str(url or ""))
    return ((parts.hostname or "").lower() in LINKEDIN_HOSTS
            and parts.path.startswith(LINKEDIN_REDIRECTOR))


def _is_password(row: dict) -> bool:
    """A recorded row that came from a password-shaped control
    (`apply_form.is_password_field`, the one definition the planner and the
    accounts hook read too): its value is written as `<hidden>`."""
    return apply_form.is_password_field(str(row.get("type", "")),
                                        str(row.get("id_or_name", "")),
                                        str(row.get("label", "")),
                                        str(row.get("autocomplete", "")))


# the qualifier has to sit on the word "code": a Social Security Number box or
# a work authorization box carries the qualifier and is no place for the code
_CODE_WORDS = re.compile(
    r"(?:verification|security|one[- ]?time|auth\w*)[ _-]*code|\botp\b|passcode", re.I)
_NOT_CODE_WORDS = re.compile(
    r"zip|post\s*code|postal|country|promo|coupon|discount|referral|invite|area\s*code", re.I)


def _code_field(fields):
    """The box the emailed code goes in.

    A code gate can carry a postal code, a country code, a referral or a
    promo box as well, and all of them read as "code". A field whose label or
    id names a verification, security, one-time, auth or OTP code wins; the
    plain "code" match is the fallback, with the address, referral and promo
    words excluded. `autocomplete` `one-time-code` is the strongest signal the
    DOM offers."""
    def blob(f):
        return f"{f.label} {f.id_or_name}"

    for f in fields:
        if str(getattr(f, "autocomplete", "")).lower() == "one-time-code":
            return f
    for f in fields:
        text = blob(f)
        if _CODE_WORDS.search(text) and not _NOT_CODE_WORDS.search(text):
            return f
    for f in fields:
        text = blob(f)
        if "code" in text.lower() and not _NOT_CODE_WORDS.search(text):
            return f
    return None


def _button_text(digest: apply_form.FormDigest, n: int) -> str:
    return next((b.text for b in digest.buttons if b.n == n), "")


def _submit_shaped(digest: apply_form.FormDigest, n: int) -> bool:
    """Whether the text describes a final application submission.

    Native ``type=submit`` is only a form mechanic: multi-step wizards often
    use it for Continue/Next. Explicit submit/apply/send/finish text remains
    gated even if the judge labels that control as an advance.
    """
    button = next((b for b in digest.buttons if b.n == n), None)
    return bool(button) and bool(re.search(r"\b(submit|apply|send|finish)\b", button.text, re.I))


def _final_shaped(digest: apply_form.FormDigest, n: int) -> bool:
    """Words a last step's button also uses ("Complete", "Confirm",
    "Finalize", "Done"). In park mode such an advance goes through the submit
    gate, which parks it: a final button judged advance must not send the
    application the user asked to review. With submitting on it stays an
    advance, since "Complete profile" is a step too."""
    button = next((b for b in digest.buttons if b.n == n), None)
    return bool(button) and bool(re.search(r"\b(complete|confirm|finali[sz]e|done)\b",
                                           button.text, re.I))


_SIGN_IN_WORDS = re.compile(r"\b(sign|log)[\s-]*(in|on)\b|\blogin\b"
                            r"|\bsend\s+(me\s+)?(an?\s+|the\s+)?(verification\s+|sign[\s-]*in\s+)?"
                            r"(code|link)\b", re.I)
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
        return not (account_only and _SIGN_IN_WORDS.search(text))
    return _final_shaped(digest, n) and not _ACCOUNT_STEP_WORDS.search(text)


def linkedin_apply_choice(url: str, digest: apply_form.FormDigest, plan: FillPlan | None, *,
                          busy: bool = False) -> tuple[int | None, str]:
    """(the Apply control of a LinkedIn job page, why), or (None, why not):
    a `/jobs/view/` page on LinkedIn with no form field and no submit button,
    before any form was filled or submitted (`busy`). The judge's confident
    `apply_entry` wins when its text says apply, else the first control that
    does. Every queued job starts on one, and its site chrome and upsells
    have misread as a form (2026-09-22). The run and `probe` share it."""
    if busy:
        return None, "a form was filled or sent in this job"
    if _host(url) not in LINKEDIN_HOSTS:
        return None, "not on LinkedIn"
    if not urlsplit(str(url or "")).path.startswith("/jobs/view/"):
        return None, "not a /jobs/view/ page"
    if digest.fields:
        return None, f"the page has {len(digest.fields)} form field(s)"
    if any(_SUBMIT_WORD.search(b.text) for b in digest.buttons):
        return None, "a button says submit"
    says_apply = [b.n for b in digest.buttons if _APPLY_WORD.search(b.text)]
    entry = plan.buttons.get("apply_entry") if plan is not None else None
    if entry is not None and entry[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF \
            and entry[0] in says_apply:
        return entry[0], "the judged apply_entry"
    if says_apply:
        return says_apply[0], "the first control that says apply"
    return None, "no control says apply"


def fieldless_apply_choice(digest: apply_form.FormDigest) -> int | None:
    """A posting without form fields: its first control whose text says
    apply (the loop's fallback when no confident `apply_entry` was judged)."""
    if digest.fields:
        return None
    return next((b.n for b in digest.buttons if "apply" in b.text.lower()), None)


def await_destination(page, log: logging.Logger | None = None, job_id: str = "") -> None:
    """Settle `page` after an Apply click. On LinkedIn's `/safety/go/` hop,
    whose script sends the tab to the company's site a few seconds after it
    boots, wait for the tab to leave it and settle again. A hop that never
    moves on stays on LinkedIn and admits nothing."""
    logger = log or logging.getLogger("apply_run")
    apply_fill.settle(page, CLICK_TIMEOUT_S)
    if not _on_linkedin_redirector(page.url):
        return
    try:
        page.wait_for_url(lambda u: not _on_linkedin_redirector(u),
                          timeout=REDIRECT_TIMEOUT_S * 1000)
    except Exception as e:      # noqa: BLE001  (the loop reads whatever the tab shows)
        logger.info("job %s: the LinkedIn redirect did not move on (%s)", job_id,
                    type(e).__name__)
        return
    apply_fill.settle(page, CLICK_TIMEOUT_S)


def _usage_delta(before: dict, after: dict) -> dict[str, Any]:
    return {"requests": after["requests"] - before["requests"],
            "input_tokens": after["input_tokens"] - before["input_tokens"],
            "usd": after["usd"] - before["usd"]}


def generated_count(pages: list[dict]) -> int:
    """Accepted generated answers across the job's page records."""
    return sum(1 for p in pages for g in p.get("generated", []) if g.get("ok"))


def _drafts(plan: FillPlan) -> dict[int, str]:
    """n -> the accepted draft, for every field a generator filled."""
    return {pf.n: pf.value for pf in plan.fields
            if pf.fact_key == "needs_generation" and pf.action == "fill"}


def _same_text(a: str, b: str) -> bool:
    """Equal after whitespace runs collapse (a textarea normalises line ends)."""
    return " ".join(str(a).split()) == " ".join(str(b).split())


_TRACE_LINE = "- Trace: "


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
        try:
            (folder / trace_dir / RECORD_NAME).write_text(text, encoding="utf-8")
        except OSError as e:
            log.warning("the record copy in %s was not written: %s", trace_dir,
                        type(e).__name__)
    return path


# --- the submit gate ----------------------------------------------------------------------

def can_submit(plan: FillPlan, verification: list[VerifyResult],
               settings: dict) -> tuple[bool, str]:
    """(True, "") when the application may be sent, else (False, the first
    failing reason): the setting, the plan's park reason, a required field
    without an answer (any action other than fill / select / upload), a
    required field unverified, the submit button's confidence. The
    prohibited and captcha flags are recorded only (`apply_judge`'s rule).
    A password box still marked `PASSWORD_ACTION` holds the master password:
    `_JobRun._fill_passwords` typed it and checked its length in the page,
    and a required box it could not fill parked the job before the gate. The
    judge never sees it, so it has no verification row."""
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
    return True, ""


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
        self._last_dropped: dict[int, str] = {}      # frames `_drop_foreign_controls` left out
        self._last_click: tuple[str, str] | None = None     # (text, role) of the last click

    # -- the trace --------------------------------------------------------------------------

    def _trace(self, kind: str, **data: Any) -> None:
        """One step on the current page's trace (see `apply_trace`)."""
        self.trace.event(kind, **data)

    def _decide(self, what: str, why: str, **evidence: Any) -> None:
        """A decision the loop took and why: a shortcut, a remap, a guess
        acted on, a route to the submit gate."""
        self.trace.event("decision", what=what, why=why, **evidence)

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

    def _reads(self, answers: Mapping[str, Any] | None = None) -> str:
        """The page state's most probable reads, as a park reason's evidence."""
        return apply_trace.page_state_reads(self._last_answers if answers is None else answers)

    def _buttons_seen(self, digest: apply_form.FormDigest) -> str:
        """Each button of the page with the role and confidence the judge gave
        it, as a park reason's evidence."""
        rows = []
        for b in digest.buttons:
            role, conf = apply_judge._choice_of(self._last_answers, f"button_{b.n}_role")
            text = " ".join(b.text.split())[:40]
            rows.append(f"{text} {role} {conf:.2f}" if role else f"{text} (no role)")
        return "; ".join(rows) if rows else "none"

    def _window_closed(self) -> bool:
        return _context_gone(self.ctx)

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
        if ats_host and ats_host not in LINKEDIN_HOSTS:
            self.ats_host = ats_host
            self.ats_hosts.add(ats_host)
            self.ats_transition_used = True
        inbox_host = _host(str(self.r.run_context().get("inbox_url") or ""))
        if inbox_host:
            self.allowed.add(inbox_host)

    def _allowed_site(self, host: str) -> bool:
        """LinkedIn and the inbox by their exact hosts (their domains carry
        other people's content: `docs.google.com`, `forms.office.com`, a
        Google sign-in frame); the admitted ATS by its whole site
        (`login.icims.com` next to `careers-gtsx.icims.com`); a known ATS
        platform (`ATS_SITES`) anywhere."""
        host = _host(host)
        if host in self.allowed:
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
        return site in ATS_SITES or any(site == _site(h) for h in self.ats_hosts)

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
        bot-check provider (a CAPTCHA widget, a chat or cookie widget): they
        are never judged, filled or clicked, and the page goes on without
        them. The page text keeps every frame's words."""
        frames = list(self.page.frames)
        dropped: dict[int, str] = {}
        for idx in sorted({int(item.locator[0]) for item in (*digest.fields, *digest.buttons)}):
            if not 0 <= idx < len(frames):
                dropped[idx] = "gone"
                continue
            url = self._frame_url(frames, idx)
            host = _host(url)
            if _is_captcha_url(url) or (host and not self._allowed_site(host)):
                dropped[idx] = host
        self._last_dropped = dropped
        if not dropped:
            return digest
        self.log.info("job %s: ignoring the controls of frame(s) %s", self.job_id,
                      ", ".join(f"{i} ({h})" for i, h in sorted(dropped.items())))
        return apply_form.FormDigest(
            url_host=digest.url_host, title=digest.title, text=digest.text,
            fields=[f for f in digest.fields if int(f.locator[0]) not in dropped],
            buttons=[b for b in digest.buttons if int(b.locator[0]) not in dropped])

    def _discover_listbox_options(self, digest: apply_form.FormDigest) -> None:
        """Read choices rendered only after a listbox is opened, before planning."""
        for control in digest.fields:
            if control.type != "listbox" or control.options:
                continue
            try:
                control.options = apply_fill.open_listbox_options(self.page, control)
            except Exception as e:      # noqa: BLE001  (a widget may detach while opening)
                self.log.info("job %s: listbox %r did not expose options: %s",
                              self.job_id, control.label, e)

    def _admit_ats_transition(self, url: str, source_url: str) -> None:
        """Record where the application lives. A known ATS platform is
        admitted wherever the flow met it; any other site only as the one
        destination LinkedIn's Apply led to."""
        host = _host(url)
        if not host or host in LINKEDIN_HOSTS or _site(host) == _site(LINKEDIN_HOSTS[0]):
            return
        if any(_site(host) == _site(h) for h in self.ats_hosts):
            return
        if _site(host) not in ATS_SITES:
            if host in self.allowed:
                return
            if self.ats_transition_used or _host(source_url) not in LINKEDIN_HOSTS:
                self._check_host(url)
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
                url = self._prepare()
                self._trace("start", url=url, submit=bool(self.r.settings.get("auto_apply_submit",
                                                                              True)))
                self._check_host(url)
                self.page = self.ctx.new_page()
                self._watch(self.page)
                self.page.goto(url)
                self._loop()
                raise _Parked("needs_human",
                              f"page budget exhausted ({apply_judge.MAX_PAGES} pages)")
            except _Parked as p:
                self._trace("park", status=p.status, reason=p.reason)
                if p.status != "submitted" and self._window_closed():
                    # whatever the loop made of it, the window went away under it
                    return self._closed(f"the run had reached: {p.reason}")
                return self._finish(p.status, p.reason, p.tab_note)
            except Exception as e:      # noqa: BLE001  (the entry must leave in_progress)
                closed = _closed_error(e) or self._window_closed()
                self._trace("exception", error=type(e).__name__, closed=closed)
                if closed:
                    self.log.warning("job %s: the browser window closed (%s)", self.job_id,
                                     type(e).__name__)
                else:
                    self.log.exception("job %s: unexpected error", self.job_id)
                if self.submit_clicked:
                    self.browser_closed = closed
                    why = CLOSED_REASON if closed else f"{type(e).__name__}: {e}"
                    return self._finish("submitted", f"submitted (unconfirmed): {why}")
                if closed:
                    return self._closed(type(e).__name__)
                return self._finish("failed", f"{type(e).__name__}: {e}")
        finally:
            self.trace.close()

    def _closed(self, evidence: str) -> Outcome:
        """The window closed or the browser went away: the job waits for the
        user and the drain stops (`Outcome.browser_closed`)."""
        self.browser_closed = True
        self._trace("closed", evidence=evidence)
        return self._finish("needs_human", CLOSED_REASON)

    def _loop(self) -> None:
        for page_no in range(apply_judge.MAX_PAGES):
            if self.r.clock() >= self.deadline:
                raise _Parked("needs_human", "time budget exhausted")
            self._check_host(self.page.url)
            if self._human_check_showing():
                self._wait_for_human_check("a CAPTCHA challenge is showing")
            t0 = time.monotonic()
            digest = self._drop_foreign_controls(apply_form.extract(self.page))
            t1 = time.monotonic()
            marker = self._page_marker()        # the page as judged: a bot check that
            self._discover_listbox_options(digest)  # clears itself shows as a change
            answers = self._judge_page(digest)
            state, conf = apply_judge.read_page_state(answers)
            sig = (state, self.page.url, json.dumps(digest.to_dict(), sort_keys=True))
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
            plan = apply_judge.plan(digest, self.catalog, answers,
                                    generation_enabled=bool(self.r.settings["auto_apply_generate"]))
            rec["flags"] = dict(plan.flags)
            self._trace("plan", plan=apply_trace.plan_json(plan))
            unsure = conf < apply_judge.PAGE_STATE_MIN_CONF
            apply_n = self._linkedin_apply(digest, plan)
            if apply_n is not None and (unsure or state not in _LINKEDIN_POSTING_KEEPS):
                if state != "job_posting" or unsure:
                    self.log.info("job %s: a LinkedIn job page read as %s (%.2f); it is the "
                                  "posting", self.job_id, state, conf)
                self._decide("linkedin_shortcut", f"a LinkedIn job page read as {state} "
                                                  f"({conf:.2f}); it is the posting",
                             button=apply_n, text=_button_text(digest, apply_n))
                self._job_posting(digest, answers, plan, rec, apply_n=apply_n)
                continue
            if state in ("login_wall", "signup_form") and digest.fields \
                    and not _credential_form(digest) and not self._on_linkedin():
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
            if unsure:
                self._check_unsure(digest, state, conf)
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
            elif state == "confirmation":
                if not self.submit_clicked and conf < apply_judge.CONFIRMATION_MIN_CONF:
                    raise _Parked("needs_human", f"a confirmation-like page before any submit "
                                                 f"({conf:.2f})" + self._reads_suffix())
                raise _Parked("submitted", "confirmation page")
            elif state == "captcha_or_bot_check":
                self._wait_for_human_check(
                    f"{_PARK_STATES[state]} (has_captcha p={plan.flags.get('has_captcha', 0.0):.2f})",
                    before=marker)
            else:
                reason = _PARK_STATES.get(state, state)
                if state == "payment_request":
                    reason += f" (asks_for_prohibited p={plan.flags.get('asks_for_prohibited', 0.0):.2f})"
                elif state == "other":
                    reason += self._reads_suffix()
                raise _Parked("needs_human", reason)

    def _reads_suffix(self) -> str:
        reads = self._reads()
        return f"; reads: {reads}" if reads else ""

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
            if _site(host) == _site(LINKEDIN_HOSTS[0]):
                raise _Parked("needs_human", "LinkedIn is signed out", LINKEDIN_LOGIN_NOTE)
            raise _Parked("needs_human", f"a sign-in on {host}, outside the application site",
                          LOGIN_NOTE)
        try:
            if state == "login_wall":
                if not self.accounts.login(self.page, digest, host):
                    raise _Parked("needs_human", "login wall", LOGIN_NOTE)
            elif not self.accounts.signup(self.page, digest, host):
                raise _Parked("needs_human", "account signup needed", LOGIN_NOTE)
        except _AsForm as form:
            self.log.info("job %s: the account screen carries the application; it is the "
                          "form", self.job_id)
            self.handed_off = True
            try:
                self._application_form(form.digest, form.answers, form.plan, self.pages[-1],
                                       completed=True)
            finally:
                self.handed_off = False

    def _human_check_showing(self) -> bool:
        """Is a bot-check challenge open on the page: a frame from a CAPTCHA
        provider that is visible and at least `HUMAN_CHECK_MIN_PX` tall (the
        checkbox badge and an invisible widget are smaller or hidden)?"""
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
        return False

    def _page_marker(self) -> tuple[str, str]:
        try:
            text = apply_fill.page_text(self.page)[:2000]
        except Exception:       # noqa: BLE001
            text = ""
        return str(self.page.url), text

    def _wait_for_human_check(self, reason: str, *, before: tuple | None = None) -> None:
        """A CAPTCHA is the user's to solve; the run never touches it. The
        check is over when its challenge frame closes or, for a whole-page
        check, when the page differs from `before` (the page as it was judged;
        a check that clears itself, Cloudflare's "Just a moment", may already
        have). In a visible window the run waits up to `HUMAN_CHECK_WAIT_S`
        for that, then carries on, and the wait is added back to the job's
        clock. Headless, an open challenge parks at once and a whole-page
        check gets `HUMAN_CHECK_AUTO_S` to clear itself."""
        framed = self._human_check_showing()
        if not framed and before is None:
            before = self._page_marker()
        headless = bool(self.r.settings.get("auto_apply_headless"))
        if headless and framed:
            raise _Parked("needs_human", reason)
        limit = HUMAN_CHECK_AUTO_S if headless else HUMAN_CHECK_WAIT_S
        polls = int(limit / HUMAN_CHECK_POLL_S)
        start = self.r.clock()
        for i in range(polls + 1):
            if framed:
                done = not self._human_check_showing()
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

    def _judge_page(self, digest: apply_form.FormDigest) -> dict:
        state, questions = apply_judge.page_questions(digest, self.catalog, self.entry)
        return dict(self.r.jev.judge(state, questions))

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
            self.trace.screenshot(self.page, f"page-{n}")
        return rec

    def _check_unsure(self, digest: apply_form.FormDigest, state: str, conf: float) -> None:
        """A read below `PAGE_STATE_MIN_CONF` goes on as its guess when that is
        one of `_UNSURE_ACTS` and the page has that step's boxes (a code gate
        its code box; a sign-in read of form boxes is already the form, and
        the account step takes a screen of account boxes alone). Anything
        else parks."""
        if state not in _UNSURE_ACTS or (state == "code_gate"
                                         and _code_field(digest.fields) is None):
            raise _Parked("needs_human", f"unsure what this page is ({state}, {conf:.2f})"
                                         + self._reads_suffix())
        self.log.info("job %s: unsure of the page (%s, %.2f); going on with that read",
                      self.job_id, state, conf)
        self._decide("unsure_goes_on", f"unsure of the page ({state}, {conf:.2f}); going on "
                                       "with that read", reads=self._reads())

    def _on_linkedin(self) -> bool:
        return _host(str(self.page.url or "")) in LINKEDIN_HOSTS

    def _linkedin_apply(self, digest: apply_form.FormDigest, plan: FillPlan) -> int | None:
        """The Apply control of a LinkedIn job page (`linkedin_apply_choice`),
        else None; never once a form was filled or submitted."""
        n, _ = linkedin_apply_choice(str(self.page.url or ""), digest, plan,
                                     busy=self.submit_clicked or self.form_filled)
        return n

    def _job_posting(self, digest: apply_form.FormDigest, answers: dict, plan: FillPlan,
                     rec: dict, *, apply_n: int | None = None) -> None:
        """Click the posting's Apply entry: `apply_n` when the caller found
        it (`_linkedin_apply`). Otherwise a page that carries form fields
        is only clicked through a confident `apply_entry` role whose text is
        not submit-shaped; otherwise it is treated as the application form (a
        form's own Apply button is a submit, and clicking it before the fill
        would send an empty form). A fieldless posting keeps the text match.
        After a form was filled, an Apply button sends that form: the page
        goes the form's way, to the submit gate. In park mode so does one
        after a form page with a password box, typed or left blank: a page of
        a sign-up's boxes does not count as the filled application
        (`_fills_the_application`), and the Apply after it may be the review
        of an application that asked for no more."""
        park_mode = not self.r.settings.get("auto_apply_submit", True)
        if apply_n is None and (self.form_filled or (park_mode and self.form_had_password)):
            self.log.info("job %s: a posting read after a filled form; treating it as the "
                          "form's next page", self.job_id)
            self._decide("posting_as_form", "a posting read after a filled form (or a "
                                            "password page in park mode) is the form's next "
                                            "page")
            self._application_form(digest, answers, plan, rec)
            return
        n = apply_n
        how = "linkedin_shortcut" if n is not None else ""
        entry = plan.buttons.get("apply_entry")
        if n is None and entry is not None and entry[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF:
            if not (digest.fields and _submit_shaped(digest, entry[0])):
                n, how = entry[0], "judged_apply_entry"
        if n is None:
            n = fieldless_apply_choice(digest)
            how = "fieldless_text" if n is not None else ""
        if n is None:
            if digest.fields:
                self.log.info("job %s: posting with %d form field(s) and no confident Apply "
                              "entry; treating it as the application form",
                              self.job_id, len(digest.fields))
                self._decide("posting_as_form", f"a posting with {len(digest.fields)} form "
                                                "field(s) and no confident Apply entry")
                self._application_form(digest, answers, plan, rec)
                return
            raise _Parked("needs_human", f"no Apply button on the posting (buttons: "
                                         f"{self._buttons_seen(digest)})")
        button = next(b for b in digest.buttons if b.n == n)
        loc = apply_form.resolve(self.page, button.locator)
        self.pages[-1]["clicked"].append(f"{button.text} (apply_entry)")
        self._last_click = (button.text, "apply_entry")
        source_url = self.page.url
        popup = None
        try:
            with self.page.expect_popup(timeout=POPUP_TIMEOUT_MS) as info:
                loc.first.click(timeout=apply_fill.ACTION_TIMEOUT_MS)
            popup = info.value
        except Exception as e:      # noqa: BLE001  (no popup within the timeout)
            self.log.debug("job %s: no popup after Apply (%s)", self.job_id, e)
        if popup is None:
            self._await_destination(self.page)
            self._trace("apply_entry", n=n, text=button.text, how=how, popup=False,
                        destination=str(self.page.url))
            self._admit_ats_transition(self.page.url, source_url)
            self._check_host(self.page.url)
            return
        self.trace.nav(str(popup.url))      # its first load came before the watch
        self._watch(popup)
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=CLICK_TIMEOUT_S * 1000)
        except Exception:       # noqa: BLE001
            pass
        self.log.info("job %s: Apply opened %s", self.job_id, popup.url)
        try:
            self._follow_popup(popup, source_url=source_url)
        finally:
            self._trace("apply_entry", n=n, text=button.text, how=how, popup=True,
                        destination=str(popup.url))

    def _follow_popup(self, popup, *, source_url: str | None = None) -> None:
        """Adopt the tab Apply opened once it has reached its destination:
        its host is admitted and checked after the redirects, never at the
        popup event, which on LinkedIn still shows the `/safety/go/` hop."""
        source = source_url or self.page.url
        self._await_destination(popup)
        self._admit_ats_transition(popup.url, source)
        self._check_host(popup.url)
        self.page = popup
        self.last_sig = None

    def _await_destination(self, page) -> None:
        await_destination(page, self.log, self.job_id)

    def _application_form(self, digest: apply_form.FormDigest, answers: dict,
                          plan: FillPlan, rec: dict, *, completed: bool = False) -> None:
        """Fill and verify the page, then click its advance or go to the
        submit gate. `completed`: the plan already has its option picks."""
        if not completed:
            plan = self._complete_option_plan(digest, answers, plan, rec)
        with self._password_guard() as guard:
            verification = self._fill_and_verify(digest, plan, rec)
            self._fill_passwords(digest, plan, rec, guard)
            self._form_buttons(digest, plan, verification, rec)

    def _form_buttons(self, digest: apply_form.FormDigest, plan: FillPlan,
                      verification: list[VerifyResult], rec: dict) -> None:
        """The filled page's way on: a submit role, a submit-shaped advance
        (a final-shaped one in park mode) or a form's own Apply goes to the
        submit gate; otherwise a confident advance is clicked."""
        advance = plan.buttons.get("advance")
        submit = plan.buttons.get("submit")
        park_mode = not self.r.settings.get("auto_apply_submit", True)
        if advance is not None and (_submit_shaped(digest, advance[0])
                                    or (park_mode and _final_shaped(digest, advance[0]))):
            self.log.info("job %s: the advance button is submit-shaped; routing it "
                          "through the submit gate", self.job_id)
            self._decide("advance_to_gate", "the advance button is submit-shaped",
                         button=advance[0], text=_button_text(digest, advance[0]))
            if submit is None:
                submit = advance
                plan.buttons["submit"] = advance
            advance = None
        entry = plan.buttons.get("apply_entry")
        if submit is None and entry is not None and _submit_shaped(digest, entry[0]):
            # a form's own "Apply" button judged apply_entry sends the form:
            # it is the submit and goes through the gate like any other
            self.log.info("job %s: the apply_entry button on a form is submit-shaped; "
                          "routing it through the submit gate", self.job_id)
            self._decide("entry_to_gate", "the apply_entry button on a form is submit-shaped",
                         button=entry[0], text=_button_text(digest, entry[0]))
            submit = entry
            plan.buttons["submit"] = entry
        if submit is None and advance is not None \
                and advance[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF:
            self._click(digest, advance[0], "advance", rec, conf=advance[1])
            return
        if submit is not None:
            self._submit_gate(digest, plan, verification, rec)
            return
        raise _Parked("needs_human", f"no way forward on this page (buttons: "
                                     f"{self._buttons_seen(digest)})")

    def _complete_option_plan(self, digest: apply_form.FormDigest, answers: dict,
                              plan: FillPlan, rec: dict) -> FillPlan:
        s2, q2 = apply_judge.option_questions(digest, plan)
        if q2:
            picks = self.r.jev.judge(s2, q2)
            answers.update(picks)
            plan = apply_judge.plan(digest, self.catalog, answers,
                                    generation_enabled=bool(self.r.settings["auto_apply_generate"]))
            rec["flags"] = dict(plan.flags)
            self._trace("option_picks", answers=apply_trace.answers_json(picks),
                        plan=apply_trace.plan_json(plan))
        return plan

    def _fill_and_verify(self, digest: apply_form.FormDigest, plan: FillPlan,
                         rec: dict) -> list[VerifyResult]:
        self._resolve_generation(digest, plan, rec)
        for question, context in plan.missing:
            self._add_missing(question, context)
        if plan.park_reason:
            raise _Parked("needs_human", plan.park_reason)
        errors: list[dict] = []
        filled = apply_fill.apply(self.page, plan, deadline=self.deadline, clock=self.r.clock,
                                  errors=errors)
        self._trace_fill(plan, filled, errors)
        if _fills_the_application(digest, plan, filled):
            self.form_filled = True
        drafts = _drafts(plan)
        verification = self._verify(filled, drafts)
        verification = self._retry_failed(plan, filled, verification, drafts)
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

    def _trace_fill(self, plan: FillPlan, filled: list[apply_fill.Filled], errors: list[dict],
                    *, retry: bool = False) -> None:
        """Which boxes took a value (never the value) and the fill errors by
        their type."""
        actions = {pf.n: pf.action for pf in plan.fields}
        self._trace("fill", retry=retry, errors=errors,
                    fields=[{"n": f.n, "label": f.label, "action": actions.get(f.n, ""),
                             "holds_value": bool(str(f.value or "").strip())} for f in filled])

    def _review_page(self, digest: apply_form.FormDigest, answers: dict,
                     plan: FillPlan, rec: dict) -> None:
        """Fill and verify editable review controls before the submit gate."""
        plan = self._complete_option_plan(digest, answers, plan, rec)
        with self._password_guard() as guard:
            verification = self._fill_and_verify(digest, plan, rec)
            self._fill_passwords(digest, plan, rec, guard)
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
                if ats_accounts.fill_password(self.page, loc):
                    account_host = account_host or box_host or host
                    typed.append(f)
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
                drafts: Mapping[int, str] | None = None) -> list[VerifyResult]:
        """The judge checks every typed fact against the sheet. A generated
        answer (`drafts`: n -> the accepted draft) is not on the sheet, and
        the grounding gate was its check; what is left is that the box holds
        the draft, which is a string comparison here, so no question carries
        the draft. Results keep the fill order."""
        if not filled:
            return []
        drafts = drafts or {}
        by_n: dict[int, VerifyResult] = {}
        rows = []
        for f in filled:
            if f.n in drafts:
                ok = _same_text(f.value, drafts[f.n])
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
                      drafts: Mapping[int, str] | None = None) -> list[VerifyResult]:
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
        again = {v.n: v for v in self._verify(refilled, drafts)}
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
        rec["verification"] = [{"n": v.n, "label": v.label, "ok": v.ok,
                                "p_correct": v.p_correct, "p_placeholder": v.p_placeholder}
                               for v in verification]

    def _click(self, digest: apply_form.FormDigest, n: int, role: str,
               rec: dict, *, conf: float | None = None) -> apply_fill.ClickResult:
        """Click button `n` in role `role` (judged at `conf`). A submit is
        clicked once whatever the page showed: a quiet page is no proof the
        click failed and a second click could send twice, so a
        landed-but-quiet submit waits up to `SUBMIT_SETTLE_S` for the page
        instead. Any other role gets one retry of a quiet click; a dead
        advance parks with the button, its role and the judge's confidence."""
        button = next((b for b in digest.buttons if b.n == n), None)
        text = button.text if button else f"button {n}"
        rec["clicked"].append(f"{text} ({role})")
        self._last_click = (text, role)
        timeout = max(1.0, min(CLICK_TIMEOUT_S, self.deadline - self.r.clock()))
        result = apply_fill.click(self.page, digest, n, timeout_s=timeout)
        self._trace("click", n=n, text=text, role=role, confidence=conf,
                    clicked=result.clicked, changed=result.changed, url=str(self.page.url))
        if role == "submit":
            if result.clicked and not result.changed:
                self.log.info("job %s: the submit click changed nothing; waiting up to %s s",
                              self.job_id, SUBMIT_SETTLE_S)
                changed = apply_fill.wait_for_change(self.page, timeout_s=SUBMIT_SETTLE_S)
                self._trace("submit_settle", changed=changed, waited_s=SUBMIT_SETTLE_S)
                return apply_fill.ClickResult(clicked=True, changed=changed)
            return result
        if result.changed:
            return result
        self.log.info("job %s: %s click changed nothing; retrying once", self.job_id, role)
        result = apply_fill.click(self.page, digest, n, timeout_s=timeout)
        self._trace("click", n=n, text=text, role=role, confidence=conf, retry=True,
                    clicked=result.clicked, changed=result.changed, url=str(self.page.url))
        if not result.changed and role == "advance":
            if self._human_check_showing():
                self._wait_for_human_check(f"a CAPTCHA challenge appeared after {text}")
                return apply_fill.ClickResult(clicked=True, changed=True)
            judged = f"judged {role} {conf:.2f}, " if conf is not None else ""
            raise _Parked("needs_human", f"the {role} button ({text}) did nothing "
                                         f"({judged}clicked twice)")
        return result

    # -- the submit path ----------------------------------------------------------------------

    def _submit_gate(self, digest: apply_form.FormDigest, plan: FillPlan,
                     verification: list[VerifyResult], rec: dict) -> None:
        ok, why = can_submit(plan, verification, self.r.settings)
        submit = plan.buttons.get("submit")
        self._trace("gate", ok=ok, why=why, button=submit[0] if submit else None,
                    text=_button_text(digest, submit[0]) if submit else "",
                    confidence=submit[1] if submit else None)
        if not ok:
            forced = {**self.r.settings, "auto_apply_submit": True}
            ready, why_on = can_submit(plan, verification, forced)
            if ready:
                raise _Parked("ready_to_submit", why, REVIEW_NOTE)
            raise _Parked("needs_human", why_on)
        submit_n = plan.buttons["submit"][0]
        self.log.info("job %s: clicking submit", self.job_id)
        self.submit_clicked = True    # set before the click so a crash after it reads as unconfirmed (no resend)
        result = self._click(digest, submit_n, "submit", rec, conf=plan.buttons["submit"][1])
        if not result.clicked:
            # the click never landed: nothing was sent, the form is filled, the human submits
            self.submit_clicked = False
            rec["clicked"].append("submit did not register")
            self.log.info("job %s: the submit click did not register", self.job_id)
            raise _Parked("ready_to_submit", "submit did not register", SUBMIT_FAILED_NOTE)
        self.log.info("job %s: SUBMIT CLICKED", self.job_id)
        rec["clicked"].append("SUBMIT CLICKED")
        text = next((b.text for b in digest.buttons if b.n == submit_n), "")
        typed = [pf for pf in plan.fields if pf.action == apply_judge.PASSWORD_ACTION]
        # an account page: the master password went in, and the page needed
        # it (a required box) or its button names the account; an optional
        # save-your-profile password on an application is no account page
        account = bool(typed) and (any(pf.required for pf in typed)
                                   or bool(_ACCOUNT_STEP_WORDS.search(text)
                                           or _SIGN_IN_WORDS.search(text)))
        self._after_submit(account=account, handoff=self.handed_off)

    def _after_submit(self, *, account: bool = False, handoff: bool = False) -> None:
        """Read the page after the submit click: a confirmation (or a code
        gate, then a confirmation) finishes the job submitted, and anything
        else submitted and unconfirmed, so the queue never sends it twice.

        `account`: the page was an account page (a "Create account and
        apply"). A confident form, sign-in or sign-up after that click
        (`_OPENED_BY_ACCOUNT`) may be the application the account opened, or
        a sent one's page reset for the signed-in user: the run can tell
        neither "submitted" nor "go on and send", so the job waits for the
        user with the page open. `handoff`: the page came from the account
        step (`_AsForm`), whose screen is a sign-up first; anything after it
        but a confirmation waits for the user the same way."""
        digest = self._post_submit_digest()
        answers = self._judge_page(digest)
        state, conf = apply_judge.read_page_state(answers)
        rec = self._new_page_record(state, conf, digest=digest, answers=answers)
        self.log.info("job %s after submit: %s (%.2f)", self.job_id, state, conf)
        if state == "confirmation" and conf >= apply_judge.PAGE_STATE_MIN_CONF:
            raise _Parked("submitted", "confirmation page")
        sure = conf >= apply_judge.PAGE_STATE_MIN_CONF
        if account and state in _OPENED_BY_ACCOUNT and sure:
            raise self._maybe_only_the_account(state, conf)
        if state == "code_gate" and sure:
            plan = apply_judge.plan(digest, self.catalog, answers)
            rec["flags"] = dict(plan.flags)
            self._code_gate(digest, plan, rec)
            digest = self._post_submit_digest()
            answers = self._judge_page(digest)
            state, conf = apply_judge.read_page_state(answers)
            self._new_page_record(state, conf, digest=digest, answers=answers)
            if state == "confirmation" and conf >= apply_judge.PAGE_STATE_MIN_CONF:
                raise _Parked("submitted", "confirmation page after the emailed code")
        if handoff:
            raise self._maybe_only_the_account(state, conf)
        raise _Parked("submitted", f"submitted (unconfirmed): the page after submit reads "
                                   f"as {state} ({conf:.2f})")

    @staticmethod
    def _maybe_only_the_account(state: str, conf: float) -> _Parked:
        return _Parked("needs_human", f"after the account page's submit the page reads as "
                                      f"{state} ({conf:.2f}): the click may only have made "
                                      f"the account; check whether the application went "
                                      f"through, then Re-queue or Mark applied")

    def _post_submit_digest(self) -> apply_form.FormDigest:
        """Validate a post-submit destination before reading or acting on it.

        Once the submit click landed, a boundary violation is still a submitted
        (unconfirmed) terminal outcome so the queue never resends the form.
        """
        try:
            self._check_host(self.page.url)
            return self._drop_foreign_controls(apply_form.extract(self.page))
        except _Parked as p:
            raise _Parked("submitted", f"submitted (unconfirmed): {p.reason}") from None

    def _code_gate(self, digest: apply_form.FormDigest, plan: FillPlan, rec: dict) -> None:
        site = digest.url_host or _host(self.page.url)
        code = self.inbox.fetch_code(self.page, site, str(self.r.run_context().get("inbox_url") or ""))
        if not code:
            raise _Parked("needs_human", "emailed code needed", CODE_NOTE)
        target = _code_field(digest.fields)
        if target is None:
            raise _Parked("needs_human", "code gate without a code box", CODE_NOTE)
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
        if not self.submit_clicked and _submit_shaped(digest, button[0]):
            # before the submit gate has let the application go, a code box
            # beside a "Submit application" is the form's last step
            raise _Parked("needs_human", "code entered; its button would send the "
                                         "application", CODE_NOTE)
        self._click(digest, button[0], role, rec, conf=button[1])

    # -- the end --------------------------------------------------------------------------------

    def _finish(self, status: str, reason: str, tab_note: str = "") -> Outcome:
        usage = _usage_delta(self.usage_before, jev.usage())
        usage["generated"] = generated_count(self.pages)
        text = ""
        if self.page is not None:
            try:
                text = apply_fill.page_text(self.page)
            except Exception:       # noqa: BLE001  (the page is gone)
                text = ""
        self.trace.finish(status, reason, self.page)
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
        if self.page is not None:
            if status == "submitted":
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


def _probe_page(page, n: int, judge: Any, out) -> tuple[apply_form.FormDigest, FillPlan | None]:
    """Print page `n` as the run would read it: the digest (fields, buttons,
    the text's head), the judge's read when a judge is given, and what the
    LinkedIn shortcut and the fieldless-posting fallback would click."""
    digest = apply_form.extract(page)
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
        state, questions = apply_judge.page_questions(digest, catalog, {})
        answers = dict(judge.judge(state, questions))
        read, conf = apply_judge.read_page_state(answers)
        print(f"  judge: page_state {read} {conf:.2f} "
              f"({apply_trace.page_state_reads(answers, top=5)})", file=out)
        for b in digest.buttons:
            role, rconf = apply_judge._choice_of(answers, f"button_{b.n}_role")
            print(f"  judge: button [{b.n}] {role} {rconf:.2f}", file=out)
        plan = apply_judge.plan(digest, catalog, answers)
    n_li, why = linkedin_apply_choice(str(page.url), digest, plan)
    print("  linkedin shortcut: " + (f"would click [{n_li}] {_button_text(digest, n_li)!r}"
                                     if n_li is not None else f"not taken ({why})"), file=out)
    n_fl = fieldless_apply_choice(digest)
    print("  fieldless posting: " + (f"would click [{n_fl}] {_button_text(digest, n_fl)!r}"
                                     if n_fl is not None else
                                     f"no ({len(digest.fields)} form field(s))" if digest.fields
                                     else "no button says apply"), file=out)
    print(f"  account screen: {'yes' if _credential_form(digest) else 'no'}", file=out)
    return digest, plan


def _probe_entry(page, digest: apply_form.FormDigest,
                 plan: FillPlan | None) -> tuple[int | None, str]:
    """The Apply entry `--follow-apply` may click, or (None, why not). Never
    on a page with form fields (an Apply there may send the form), never a
    control that says submit or Easy Apply."""
    if digest.fields:
        return None, f"the page has {len(digest.fields)} form field(s); an Apply there may " \
                     "send the form"
    n, _ = linkedin_apply_choice(str(page.url), digest, plan)
    if n is None and plan is not None:
        entry = plan.buttons.get("apply_entry")
        if entry is not None and entry[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF:
            n = entry[0]
    if n is None:
        n = fieldless_apply_choice(digest)
    if n is None:
        return None, "no Apply control"
    text = _button_text(digest, n)
    if _SUBMIT_WORD.search(text) or re.search(r"easy\s*apply", text, re.I):
        return None, f"[{n}] {text!r} is not an Apply entry the probe follows"
    return n, ""


def _probe(ctx, url: str, *, follow_apply: bool, judge: Any, out, settle_s: float) -> int:
    page = ctx.new_page()
    print(f"probe: {url}", file=out)
    try:
        page.goto(url, timeout=PROBE_GOTO_MS)
    except Exception as e:      # noqa: BLE001  (a dead or slow page is the answer)
        print(f"probe: the page did not load ({type(e).__name__})", file=out)
        return 1
    apply_fill.settle(page, settle_s)
    digest, plan = _probe_page(page, 1, judge, out)
    if not follow_apply:
        return 0
    n, why = _probe_entry(page, digest, plan)
    if n is None:
        print(f"follow-apply: nothing clicked ({why})", file=out)
        return 0
    button = next(b for b in digest.buttons if b.n == n)
    popup = None
    try:
        with page.expect_popup(timeout=POPUP_TIMEOUT_MS) as info:
            apply_form.resolve(page, button.locator).first.click(
                timeout=apply_fill.ACTION_TIMEOUT_MS)
        popup = info.value
    except Exception:       # noqa: BLE001  (no popup: the tab itself moved on, or nothing did)
        popup = None
    target = popup or page
    await_destination(target)
    print(f"follow-apply: clicked [{n}] {_one_line(button.text, 60)!r}; "
          f"{'a new tab' if popup else 'the same tab'} at {target.url}", file=out)
    _probe_page(target, 2, judge, out)
    return 0


def probe(url: str, *, follow_apply: bool = False, judge: Any = None, headed: bool = False,
          profile_dir: Path | None = None, context: Any = None, out=None,
          settle_s: float = PROBE_SETTLE_S) -> int:
    """Read `url` the way the run would and print it: the digest, the judge's
    read (`judge`, asked once per page), and what the LinkedIn shortcut and
    the fieldless-posting fallback would click. `follow_apply` clicks only
    that Apply entry (never on a page with form fields) and prints the page
    it leads to. Nothing is typed, uploaded, ticked or submitted.

    The browser is a fresh temporary profile (headless unless `headed`), or
    the persistent profile at `profile_dir`; `context` injects one (tests).
    Exit 0, or 1 when the page did not load."""
    out = out or sys.stdout
    if context is not None:
        return _probe(context, url, follow_apply=follow_apply, judge=judge, out=out,
                      settle_s=settle_s)
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
                          settle_s=settle_s)
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
                         headed=args.headed, profile_dir=profile)
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
