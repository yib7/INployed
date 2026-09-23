"""The auto-apply drain loop: claim a queued job, drive a persistent Chromium
through the page state machine, submit behind the confidence gate or park,
write `apply_record.md`, finish the queue entry.

    python local/apply_run.py drain [--cap N] [--no-submit] [--headless]
                                    [--jev fake|replay|typesafe] [--profile DIR]
    python local/apply_run.py one <job_id> [same flags]
    python local/apply_run.py login      sign in to LinkedIn and the inbox once
    python local/apply_run.py doctor     key, SDK, Playwright, Chromium, profile

Per job (`Runner.run_job`): the fact catalog from the job folder's apply.md
(`apply_facts.build`), then one page at a time up to `apply_judge.MAX_PAGES`
and an eight-minute wall clock: `apply_form.extract` -> one Jev request
(`apply_judge.page_questions`) -> `read_page_state` -> the state table from
the design (section 3.5):

    job_posting            click the Apply entry, follow a popup
    application_form       plan, fill, verify, then advance, or the submit gate
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
field's value is never written) and calls `apply_queue.finish`.

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
        if self._email_first(page, digest) or _site(host) in self.email_sites:
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
        except _Parked:
            # the loop's own park (a host check, an unanswerable box): the
            # reason names what was refused and the loop ends the job with it
            raise
        except Exception:  # noqa: BLE001  (account details stay out of errors)
            return False

    def signup(self, page, digest, host: str) -> bool:
        return self._fill(page, digest, host, self._signup_email(), True)

    def _signup_email(self) -> str:
        return str(self.run.r.run_context().get("signup_email") or "")

    @staticmethod
    def _email_first(page, digest) -> bool:
        """An address box and no password box: the first screen of a two-step
        sign-in."""
        has_email = has_password = False
        for f in digest.fields:
            if _is_email_box(f):
                has_email = True
            elif apply_form.is_password_field(f.type, f.id_or_name, f.label, f.autocomplete):
                has_password = True
        return has_email and not has_password

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
        site = _site(host)
        self.steps[site] = self.steps.get(site, 0) + 1
        if self.steps[site] > self.MAX_STEPS_PER_SITE:
            return False
        blocked: list[str] = []
        frames = apply_form.frames(page)
        guarded: set[int] = {id(page.main_frame)}

        def _guard(route, request) -> None:
            """While the credentials are on the page, the page and the frames
            that hold them may not navigate off the application's sites: a
            sign-in form that posts there parks the job. Every other request
            goes through (a fetch, a popup, another frame), so the site's own
            bot check, sign-in API and scripts work as they would for a
            person; the password is only ever typed on the application's
            site (`_password_ok`), which is the protection that matters."""
            target_host = _host(request.url)
            try:
                frame_id = id(request.frame)
            except Exception:       # noqa: BLE001  (a service-worker request has no frame)
                frame_id = None
            if (target_host and request.is_navigation_request() and frame_id in guarded
                    and not self.run._allowed_site(target_host)):
                blocked.append(target_host)
                route.abort()
                return
            route.continue_()

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
            passwords, emails, others = [], [], []
            password_hosts = {_host(page.url)}
            for field in digest.fields:
                loc = apply_form.resolve(page, field.locator).first
                idx = int(field.locator[0])
                if loc.get_attribute("type") == "password":
                    passwords.append(loc)
                    if 0 <= idx < len(frames):
                        guarded.add(id(frames[idx]))
                        password_hosts.add(_host(self.run._frame_url(frames, idx)))
                elif _is_email_box(field):
                    emails.append(loc)
                    if 0 <= idx < len(frames):
                        guarded.add(id(frames[idx]))
                else:
                    pf = by_n.get(field.n)
                    if pf is not None and pf.action in ("fill", "select", "upload") \
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
            page.route("**/*", _guard)
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
                page.unroute("**/*", _guard)
            if signup and passwords and result.clicked:
                # the click landed, so the account may already exist whatever
                # the page did next; a ledger entry for an account that was
                # never created costs one failed login, a missing one costs a
                # second signup with the same address
                ats_accounts.record(host, email)
            if blocked:
                raise _Parked("needs_human", f"left the allowed sites: {blocked[0]}", before)
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
        except _Parked:
            raise
        except Exception:  # noqa: BLE001  (Playwright may include filled values)
            return False


class _Inbox:
    def __init__(self, run):
        self.run = run

    def fetch_code(self, page, site: str, inbox_url: str) -> str | None:
        self.run._check_host(inbox_url)
        entry = self.run.entry
        return apply_inbox.fetch_code(page, site, inbox_url, jev=self.run.r.jev,
                                      clock=self.run.r.clock, sleep=self.run.r.sleep,
                                      deadline=self.run.deadline,
                                      ats=str((entry.get("ats") or {}).get("system") or ""),
                                      company=str(entry.get("company") or ""))


# --- outcomes and the record ----------------------------------------------------------

@dataclass
class Outcome:
    job_id: str
    status: str
    reason: str
    record_path: str
    pages: int
    jev_usage: dict[str, Any] = field(default_factory=dict)


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


def write_record(folder: Path, entry: dict, outcome_status: str, reason: str,
                 pages: list[dict], jev_usage: dict, page_text: str, *,
                 missing: list[dict] | None = None) -> Path:
    """`apply_record.md` in the job folder. A value from a password field
    (`_is_password`) or a row marked `hidden` (the emailed code) is written
    as `<hidden>`."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    lines = [f"# Apply record: {entry.get('title', '')} at {entry.get('company', '')}", "",
             f"- Job id: {entry.get('job_posting_id', '')}",
             f"- Company: {entry.get('company', '')}",
             f"- Title: {entry.get('title', '')}",
             f"- Status: {outcome_status}",
             f"- Reason: {reason}",
             f"- Written: {datetime.now().isoformat(timespec='seconds')}",
             f"- Apply URL: {entry.get('apply_url', '')}", ""]
    for i, p in enumerate(pages, 1):
        lines.append(f"## Page {i}: {p.get('url', '')}")
        lines.append(f"- State: {p.get('state', '')} ({float(p.get('confidence', 0.0)):.2f})")
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
    path = folder / RECORD_NAME
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --- the submit gate ----------------------------------------------------------------------

def can_submit(plan: FillPlan, verification: list[VerifyResult],
               settings: dict) -> tuple[bool, str]:
    """(True, "") when the application may be sent, else (False, the first
    failing reason): the setting, the plan's park reason, a required field
    without an answer (any action other than fill / select / upload), a
    required field unverified, the submit button's confidence. The
    prohibited and captcha flags are recorded only (`apply_judge`'s rule)."""
    if not settings.get("auto_apply_submit", True):
        return False, "auto_apply_submit is off"
    if plan.park_reason:
        return False, plan.park_reason
    by_n = {v.n: v for v in verification}
    for pf in plan.fields:
        if not pf.required:
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
        `Outcome` per job."""
        limit = int(cap if cap is not None else self.settings["auto_apply_batch_cap"])

        def _work(ctx) -> list[Outcome]:
            outcomes: list[Outcome] = []
            while len(outcomes) < limit:
                entry = apply_queue.claim("apply_run", path=self.queue_path)
                if entry is None:
                    break
                outcomes.append(self._run_job(ctx, entry))
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

    def run(self) -> Outcome:
        self.log.info("job %s: start (%s)", self.job_id, self.entry.get("apply_url", ""))
        try:
            url = self._prepare()
            self._check_host(url)
            self.page = self.ctx.new_page()
            self.page.goto(url)
            self._loop()
            raise _Parked("needs_human", f"page budget exhausted ({apply_judge.MAX_PAGES} pages)")
        except _Parked as p:
            return self._finish(p.status, p.reason, p.tab_note)
        except Exception as e:      # noqa: BLE001  (the entry must leave in_progress)
            self.log.exception("job %s: unexpected error", self.job_id)
            if self.submit_clicked:
                return self._finish("submitted",
                                    f"submitted (unconfirmed): {type(e).__name__}: {e}")
            return self._finish("failed", f"{type(e).__name__}: {e}")

    def _loop(self) -> None:
        for page_no in range(apply_judge.MAX_PAGES):
            if self.r.clock() >= self.deadline:
                raise _Parked("needs_human", "time budget exhausted")
            self._check_host(self.page.url)
            if self._human_check_showing():
                self._wait_for_human_check("a CAPTCHA challenge is showing")
            digest = self._drop_foreign_controls(apply_form.extract(self.page))
            marker = self._page_marker()        # the page as judged: a bot check that
            self._discover_listbox_options(digest)  # clears itself shows as a change
            answers = self._judge_page(digest)
            state, conf = apply_judge.read_page_state(answers)
            sig = (state, self.page.url, json.dumps(digest.to_dict(), sort_keys=True))
            rec = self._new_page_record(state, conf)
            self.log.info("job %s page %d: %s (%.2f) at %s", self.job_id, page_no + 1,
                          state, conf, self.page.url)
            if sig == self.last_sig:
                raise _Parked("needs_human", "page did not advance")
            self.last_sig = sig
            if conf < apply_judge.PAGE_STATE_MIN_CONF:
                raise _Parked("needs_human", f"unsure what this page is ({state}, {conf:.2f})")
            plan = apply_judge.plan(digest, self.catalog, answers,
                                    generation_enabled=bool(self.r.settings["auto_apply_generate"]))
            rec["flags"] = dict(plan.flags)
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
                                                 f"({conf:.2f})")
                raise _Parked("submitted", "confirmation page")
            elif state == "captcha_or_bot_check":
                self._wait_for_human_check(
                    f"{_PARK_STATES[state]} (has_captcha p={plan.flags.get('has_captcha', 0.0):.2f})",
                    before=marker)
            else:
                reason = _PARK_STATES.get(state, state)
                if state == "payment_request":
                    reason += f" (asks_for_prohibited p={plan.flags.get('asks_for_prohibited', 0.0):.2f})"
                raise _Parked("needs_human", reason)

    # -- per state ------------------------------------------------------------------------

    def _account_step(self, state: str, digest: apply_form.FormDigest) -> None:
        """A sign-in or sign-up screen goes to the accounts hook when the
        master password may be typed on its site (`_password_ok`); LinkedIn
        signed out, or a sign-in on some other site, parks for the human."""
        host = digest.url_host or _host(self.page.url)
        if not self._password_ok(host):
            if _site(host) == _site(LINKEDIN_HOSTS[0]):
                raise _Parked("needs_human", "LinkedIn is signed out", LINKEDIN_LOGIN_NOTE)
            raise _Parked("needs_human", f"a sign-in on {host}, outside the application site",
                          LOGIN_NOTE)
        if state == "login_wall":
            if not self.accounts.login(self.page, digest, host):
                raise _Parked("needs_human", "login wall", LOGIN_NOTE)
        elif not self.accounts.signup(self.page, digest, host):
            raise _Parked("needs_human", "account signup needed", LOGIN_NOTE)

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

    def _new_page_record(self, state: str, conf: float) -> dict:
        rec = {"url": self.page.url, "state": state, "confidence": conf,
               "filled": [], "verification": [], "clicked": [], "flags": {},
               "generated": []}
        self.pages.append(rec)
        return rec

    def _job_posting(self, digest: apply_form.FormDigest, answers: dict, plan: FillPlan,
                     rec: dict) -> None:
        """Click the posting's Apply entry. A page that carries form fields
        is only clicked through a confident `apply_entry` role whose text is
        not submit-shaped; otherwise it is treated as the application form (a
        form's own Apply button is a submit, and clicking it before the fill
        would send an empty form). A fieldless posting keeps the text match."""
        n = None
        entry = plan.buttons.get("apply_entry")
        if entry is not None and entry[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF:
            if not (digest.fields and _submit_shaped(digest, entry[0])):
                n = entry[0]
        if n is None and not digest.fields:
            for b in digest.buttons:
                if "apply" in b.text.lower():
                    n = b.n
                    break
        if n is None:
            if digest.fields:
                self.log.info("job %s: posting with %d form field(s) and no confident Apply "
                              "entry; treating it as the application form",
                              self.job_id, len(digest.fields))
                self._application_form(digest, answers, plan, rec)
                return
            raise _Parked("needs_human", "no Apply button on the posting")
        button = next(b for b in digest.buttons if b.n == n)
        loc = apply_form.resolve(self.page, button.locator)
        self.pages[-1]["clicked"].append(f"{button.text} (apply_entry)")
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
            self._admit_ats_transition(self.page.url, source_url)
            self._check_host(self.page.url)
            return
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=CLICK_TIMEOUT_S * 1000)
        except Exception:       # noqa: BLE001
            pass
        self.log.info("job %s: Apply opened %s", self.job_id, popup.url)
        self._follow_popup(popup, source_url=source_url)

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
        """Settle `page` after the Apply click. On LinkedIn's `/safety/go/`
        hop, whose script sends the tab to the company's site a few seconds
        after it boots, wait for the tab to leave it and settle again. A hop
        that never moves on stays on LinkedIn and admits nothing."""
        apply_fill.settle(page, CLICK_TIMEOUT_S)
        if not _on_linkedin_redirector(page.url):
            return
        try:
            page.wait_for_url(lambda u: not _on_linkedin_redirector(u),
                              timeout=REDIRECT_TIMEOUT_S * 1000)
        except Exception as e:      # noqa: BLE001  (the loop reads whatever the tab shows)
            self.log.info("job %s: the LinkedIn redirect did not move on (%s)", self.job_id,
                          type(e).__name__)
            return
        apply_fill.settle(page, CLICK_TIMEOUT_S)

    def _application_form(self, digest: apply_form.FormDigest, answers: dict,
                          plan: FillPlan, rec: dict) -> None:
        plan = self._complete_option_plan(digest, answers, plan, rec)
        verification = self._fill_and_verify(digest, plan, rec)
        advance = plan.buttons.get("advance")
        submit = plan.buttons.get("submit")
        park_mode = not self.r.settings.get("auto_apply_submit", True)
        if advance is not None and (_submit_shaped(digest, advance[0])
                                    or (park_mode and _final_shaped(digest, advance[0]))):
            self.log.info("job %s: the advance button is submit-shaped; routing it "
                          "through the submit gate", self.job_id)
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
            submit = entry
            plan.buttons["submit"] = entry
        if submit is None and advance is not None \
                and advance[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF:
            self._click(digest, advance[0], "advance", rec)
            return
        if submit is not None:
            self._submit_gate(digest, plan, verification, rec)
            return
        raise _Parked("needs_human", "no way forward on this page")

    def _complete_option_plan(self, digest: apply_form.FormDigest, answers: dict,
                              plan: FillPlan, rec: dict) -> FillPlan:
        s2, q2 = apply_judge.option_questions(digest, plan)
        if q2:
            answers.update(self.r.jev.judge(s2, q2))
            plan = apply_judge.plan(digest, self.catalog, answers,
                                    generation_enabled=bool(self.r.settings["auto_apply_generate"]))
            rec["flags"] = dict(plan.flags)
        return plan

    def _fill_and_verify(self, digest: apply_form.FormDigest, plan: FillPlan,
                         rec: dict) -> list[VerifyResult]:
        self._resolve_generation(digest, plan, rec)
        for question, context in plan.missing:
            self._add_missing(question, context)
        if plan.park_reason:
            raise _Parked("needs_human", plan.park_reason)
        filled = apply_fill.apply(self.page, plan, deadline=self.deadline, clock=self.r.clock)
        drafts = _drafts(plan)
        verification = self._verify(filled, drafts)
        verification = self._retry_failed(plan, filled, verification, drafts)
        self._record_fill(rec, digest, plan, filled, verification)
        still = [v.label for v in verification if not v.ok
                 and any(pf.n == v.n and pf.required for pf in plan.fields)]
        if still:
            raise _Parked("needs_human", "could not verify: " + ", ".join(still))
        return verification

    def _review_page(self, digest: apply_form.FormDigest, answers: dict,
                     plan: FillPlan, rec: dict) -> None:
        """Fill and verify editable review controls before the submit gate."""
        plan = self._complete_option_plan(digest, answers, plan, rec)
        verification = self._fill_and_verify(digest, plan, rec)
        self._submit_gate(digest, plan, verification, rec)

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
        refilled = apply_fill.apply(self.page, retry, deadline=self.deadline, clock=self.r.clock)
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
               rec: dict) -> apply_fill.ClickResult:
        """Click button `n` in role `role`. A submit is clicked once whatever
        the page showed: a quiet page is no proof the click failed and a second
        click could send twice, so a landed-but-quiet submit waits up to
        `SUBMIT_SETTLE_S` for the page instead. Any other role gets one retry
        of a quiet click; a dead advance parks."""
        button = next((b for b in digest.buttons if b.n == n), None)
        text = button.text if button else f"button {n}"
        rec["clicked"].append(f"{text} ({role})")
        timeout = max(1.0, min(CLICK_TIMEOUT_S, self.deadline - self.r.clock()))
        result = apply_fill.click(self.page, digest, n, timeout_s=timeout)
        if role == "submit":
            if result.clicked and not result.changed:
                self.log.info("job %s: the submit click changed nothing; waiting up to %s s",
                              self.job_id, SUBMIT_SETTLE_S)
                changed = apply_fill.wait_for_change(self.page, timeout_s=SUBMIT_SETTLE_S)
                return apply_fill.ClickResult(clicked=True, changed=changed)
            return result
        if result.changed:
            return result
        self.log.info("job %s: %s click changed nothing; retrying once", self.job_id, role)
        result = apply_fill.click(self.page, digest, n, timeout_s=timeout)
        if not result.changed and role == "advance":
            if self._human_check_showing():
                self._wait_for_human_check(f"a CAPTCHA challenge appeared after {text}")
                return apply_fill.ClickResult(clicked=True, changed=True)
            raise _Parked("needs_human", f"the {role} button ({text}) did nothing")
        return result

    # -- the submit path ----------------------------------------------------------------------

    def _submit_gate(self, digest: apply_form.FormDigest, plan: FillPlan,
                     verification: list[VerifyResult], rec: dict) -> None:
        ok, why = can_submit(plan, verification, self.r.settings)
        if not ok:
            forced = {**self.r.settings, "auto_apply_submit": True}
            ready, why_on = can_submit(plan, verification, forced)
            if ready:
                raise _Parked("ready_to_submit", why, REVIEW_NOTE)
            raise _Parked("needs_human", why_on)
        submit_n = plan.buttons["submit"][0]
        self.log.info("job %s: clicking submit", self.job_id)
        self.submit_clicked = True    # set before the click so a crash after it reads as unconfirmed (no resend)
        result = self._click(digest, submit_n, "submit", rec)
        if not result.clicked:
            # the click never landed: nothing was sent, the form is filled, the human submits
            self.submit_clicked = False
            rec["clicked"].append("submit did not register")
            self.log.info("job %s: the submit click did not register", self.job_id)
            raise _Parked("ready_to_submit", "submit did not register", SUBMIT_FAILED_NOTE)
        self.log.info("job %s: SUBMIT CLICKED", self.job_id)
        rec["clicked"].append("SUBMIT CLICKED")
        self._after_submit()

    def _after_submit(self) -> None:
        digest = self._post_submit_digest()
        answers = self._judge_page(digest)
        state, conf = apply_judge.read_page_state(answers)
        rec = self._new_page_record(state, conf)
        self.log.info("job %s after submit: %s (%.2f)", self.job_id, state, conf)
        if state == "confirmation" and conf >= apply_judge.PAGE_STATE_MIN_CONF:
            raise _Parked("submitted", "confirmation page")
        if state == "code_gate" and conf >= apply_judge.PAGE_STATE_MIN_CONF:
            plan = apply_judge.plan(digest, self.catalog, answers)
            rec["flags"] = dict(plan.flags)
            self._code_gate(digest, plan, rec)
            digest = self._post_submit_digest()
            state, conf = apply_judge.read_page_state(self._judge_page(digest))
            self._new_page_record(state, conf)
            if state == "confirmation" and conf >= apply_judge.PAGE_STATE_MIN_CONF:
                raise _Parked("submitted", "confirmation page after the emailed code")
        raise _Parked("submitted", f"submitted (unconfirmed): the page after submit reads "
                                   f"as {state} ({conf:.2f})")

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
        except Exception:  # noqa: BLE001  (a fill exception may carry the private code)
            raise _Parked("needs_human", "emailed code could not be filled", CODE_NOTE) from None
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
        self._click(digest, button[0], role, rec)

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
        record = ""
        if self.folder is not None:
            try:
                record = str(write_record(self.folder, self.entry, status, reason, self.pages,
                                          usage, text, missing=self.missing))
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
                       pages=len(self.pages), jev_usage=usage)


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
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    profile = Path(args.profile) if getattr(args, "profile", None) else None
    try:
        if args.verb == "doctor":
            return doctor(profile)
        if args.verb == "login":
            return login(profile)
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
