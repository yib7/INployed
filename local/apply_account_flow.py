"""Sign-in and sign-up screens: the account form readers, `_Accounts` (the
account address and the keyring password typed on an account screen),
`_Inbox`, the password rules a page states, single sign-on, and the account
and code step advances. Named apart from `ats_accounts`, the credential
ledger.

Split out of `apply_run`, which re-exports these names. It writes to the
run's own log, `apply_run` (one of `apply_trace.LOGGERS`).
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

import apply_click
import apply_facts
import apply_fill
import apply_form
import apply_inbox
import apply_judge
import apply_limits
import apply_send_words
import apply_sendwatch
import ats_accounts
import jev
from apply_judge import FillPlan
from apply_send_words import _send_worded, _sends_application
from apply_outcome import ACCOUNT_EXISTS_REASON, _cap, LOGIN_NOTE, _Parked, PASSWORD_HTTP_REASON
from apply_sites import (_host, _is_captcha_url, _MARK_LINK_JS, _MARKED_LINK, _scripted_link,
                         _site, _tracking)
from apply_page import _chrome, _settled_ms


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
    return [f for f in digest.fields if apply_form.password_box(f)]


def _fills_the_application(digest, plan: FillPlan, filled) -> bool:
    """Did this page's fill put the application's own answers on it? A page
    with a password box whose boxes a sign-up asks for (a name, the address,
    a phone: `_ACCOUNT_FACTS`) makes an account; an application is filled
    only once a page carries something else (a resume, a profile link, a
    written answer). A page without a password box counts whatever it held,
    but an address box alone (`_email_first`): the start of a sign-in or of
    an application, whose code step comes before any answer."""
    if not filled or _email_first(digest):
        return False
    if not _password_boxes(digest):
        return True
    done = {f.n for f in filled}
    return any(pf.n in done and pf.fact_key not in _ACCOUNT_FACTS for pf in plan.fields)


def account_forms(page, digest) -> list:
    """A sign-in and a sign-up side by side (Taleo, SuccessFactors):
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
        if at[1] >= 0 and apply_form.password_box(f) and at not in pw_forms:
            pw_forms.append(at)
    if len(pw_forms) < 2:
        return []
    return [dataclasses.replace(
        digest, fields=[f for f, at in zip(digest.fields, fields_at) if at == form],
        buttons=[b for b, at in zip(digest.buttons, buttons_at) if at == form])
        for form in pw_forms]


# The address the submit button's form sends to, before the click: the
# button's own `formaction`, else its form's `action`
# (read as the attribute: a control named "action" shadows the property),
# resolved against the page; "" for a button in no form
_FORM_ACTION_JS = """el => {
  const form = el.form || el.closest('form');
  if (!form) return '';
  const raw = el.getAttribute('formaction') ?? form.getAttribute('action') ?? '';
  try { return new URL(raw, document.baseURI).href; } catch (e) { return ''; }
}"""


def _form_action(loc) -> str:
    """The absolute action URL of the form a box sits in; "" for a box with
    no form or no action, a script action (`javascript:`), or a box that
    cannot be read."""
    try:
        action = str(loc.evaluate(_FORM_ACTION_JS, timeout=5_000) or "")
    except Exception:       # noqa: BLE001  (a box or frame that went away)
        return ""
    return action if urlsplit(action).scheme.lower() in ("http", "https") else ""


def _credential_form(digest) -> bool:
    """A sign-in or sign-up screen by its boxes: a password box, or the
    address box of a two-step sign-in, and no file box (an account screen
    never takes a resume; one that does is the application form creating an
    account). A page of other boxes is a form, whatever it was read as; only
    a screen of this shape reaches `_Accounts`' typing and clicking."""
    if any(f.type == "file" for f in digest.fields):
        return False
    return bool(_password_boxes(digest)) or _email_first(digest)


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
    the facts cannot answer parks the job with its question, like any form.

    With no account in the ledger, a sign-in screen's way to a sign-up
    is taken, a link or a button; with none, one sign-in with the
    master password is tried. A sign-up that says the address has
    an account signs in instead; the site's password rules are
    checked before the password makes an account; a sign-up shown
    again with its password boxes emptied takes it once more; a
    click that set a request going is waited on; a step that
    raised names its error in the park."""

    MAX_STEPS_PER_SITE = 4      # account screens handled per site before the job parks

    def __init__(self, run):
        self.run = run
        self.email_sites: set[str] = set()      # sites where the address went in this job
        self.steps: dict[str, int] = {}
        # (site, "login" | "signup") where the password went in this job: a
        # second password screen of the same kind is a rejected password, and
        # typing it again only moves the account toward a lockout
        self.password_typed: set[tuple[str, str]] = set()
        self.attempted: set[str] = set()    # sites of the one sign-in without an account
        self.exists: set[str] = set()       # sites whose sign-up said the account exists
        self.retyped: set[str] = set()      # sites whose sign-up took the password twice
        self.pending: dict[str, tuple[str, str]] = {}   # site -> (host, email) of that sign-in
        self.last_error = ""                # the account step's exception

    def login(self, page, digest, host: str) -> bool:
        self.last_error = ""
        account = self.run._account_for(host)
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
            # A sign-up beside the sign-in, and no account in the
            # ledger for the site: the sign-up's form is the step
            return self._fill(page, digest, host, self._signup_email(), True)
        try:
            went = self._signup_link(page, digest, host)
            if went is None:
                went = self._signup_button(page, digest)
        except (_Parked, _AsForm, jev.JudgeOutage, jev.JevUnavailable):
            raise
        except jev.RequestRejected as e:
            raise self._judge_refused("accounts.login", e) from None
        except Exception as e:  # noqa: BLE001  (account details stay out of errors)
            self._failed("accounts.login", e)
            return False
        if went is not None:
            return went
        return self._attempt_sign_in(page, digest, host)

    def _not_taken(self, site: str, host: str, kind: str, digest=None) -> str:
        """Why a second password screen of the same kind parks: after the
        one sign-in tried without an account in the ledger, no
        account on the site takes the master password; after a sign-up that
        said the address has an account, that account has another
        password; else the step did not take it, with what the page says
        about it."""
        if kind == "login" and site in self.exists:
            return (f"{ACCOUNT_EXISTS_REASON} on {host} with another password: reset it to the "
                    f"master password, then Re-queue")
        if kind == "login" and site in self.attempted:
            return (f"no account on {host} took the master password (one sign-in was tried "
                    f"with the sign-up address) and the screen offers no sign-up: create the "
                    f"account or reset its password, then Re-queue")
        says = page_problem(digest) if digest is not None else ""
        return (f"the {kind} on {host} did not take the master password"
                + (f" (the page says {says!r})" if says else ""))

    def _retype(self, site: str, passwords: list, digest) -> bool:
        """The same create-account screen shown again with its
        password boxes emptied (the site cleared them after an error in
        another box) takes the master password once more, once per site;
        a screen that kept them, or a third showing, never does."""
        if site in self.retyped or not passwords or password_step(digest) == "signin":
            # a sign-in's box that came back is a rejected password, never
            # typed again (a lockout)
            return False
        try:
            empty = all(int(loc.evaluate("el => (el.value || '').length")) == 0
                        for loc in passwords)
        except Exception:       # noqa: BLE001  (a box that cannot be read is no emptied box)
            empty = False
        if not empty:
            return False
        self.retyped.add(site)
        says = page_problem(digest)
        self.run._decide("password_retyped", "the sign-up came back with its password boxes "
                                             "emptied; the master password is typed once more"
                                             + (f" (the page says {says!r})" if says else ""))
        return True

    def _to_sign_in(self, page, digest, host: str, exists: str) -> bool:
        """From a sign-up that says the address has an account, the
        screen's own way to the sign-in (the first button or link that says
        sign in, none in the header, none with another site, none for job
        alerts, none that sends), clicked through the live click guard; with none, the job
        parks naming the account."""
        own: dict[str, apply_form.Button] = {}
        for b in digest.buttons:
            text = b.text or ""
            if b.chrome or b.disabled or not _SIGN_IN_ONLY.search(text) \
                    or _THIRD_PARTY.search(text) or _NOT_ACCOUNT_STEP.search(text) \
                    or _NOT_AN_ACCOUNT.search(text) or _send_worded(text, account=True):
                continue
            own.setdefault(" ".join(text.lower().split()), b)
        if not own:
            raise _Parked("needs_human", f"{ACCOUNT_EXISTS_REASON} on {host} ({exists!r}) and "
                                         f"the page offers no way to sign in: sign in, then "
                                         f"Re-queue", LOGIN_NOTE)
        # every one of them leads to the sign-in ("Sign in instead" in the
        # message, "Already have an account? Sign in" below the form): the
        # first on the page
        button = next(iter(own.values()))
        rec = self.run.pages[-1] if self.run.pages else None
        if rec is not None:
            rec["clicked"].append(f"{button.text} (to the sign-in)")
        self.run._last_click = (button.text, "advance")
        result = self._click(page, digest, button.n)
        if result.refused:
            raise _Parked("needs_human", f"the account step's button ({_cap(button.text, 60)}) "
                                         f"changed before the click: {result.refused}; nothing "
                                         f"was clicked", LOGIN_NOTE)
        self.run._check_host(page.url)
        return bool(result.changed)

    def _click(self, page, digest, n: int) -> apply_fill.ClickResult:
        """An account step's click, through the live click guard: its wait
        is the loop's click budget (`CLICK_TIMEOUT_S`), cut to the time the
        job has left and never under one second; a quiet click that set a
        request going (a slow sign-up) is waited on `STEP_SETTLE_S` more,
        never made again; a loading indicator after it is waited on
        (`_wait_while_busy`)."""
        timeout = max(1.0, min(apply_limits.CLICK_TIMEOUT_S, self.run.deadline - self.run.r.clock()))
        text = next((b.text for b in digest.buttons if b.n == n), "")
        result = apply_fill.click(page, digest, n, timeout_s=timeout,
                                  check=self.run._live_check("advance", account=True))
        self.run._trace("click", n=n, text=text, role="advance", account=True,
                        clicked=result.clicked, changed=result.changed, url=str(page.url),
                        refused=result.refused, late=result.late)
        if result.refused or not result.clicked:
            return result
        if not result.changed:
            went = [row for row in result.sent if not _tracking(row.split(" ", 1)[-1])
                    and not _is_captcha_url(row.split(" ", 1)[-1])]
            if not went:
                return result
            self.run.log.info("job %s: the account click set %s going; waiting up to %s s",
                              self.run.job_id, went[0], apply_limits.STEP_SETTLE_S)
            changed = apply_fill.wait_for_change(page, timeout_s=apply_limits.STEP_SETTLE_S)
            self.run._trace("step_settle", changed=changed, waited_s=apply_limits.STEP_SETTLE_S,
                            sent=went[:3])
            if not changed:
                return result
            result = dataclasses.replace(result, changed=True)
        if page is self.run.page:
            self.run._wait_while_busy(text)
        return result

    def _judge_refused(self, step: str, e: BaseException) -> _Parked:
        """The judge refused the account screen's request whatever the moment
        (`jev.RequestRejected`, a choice with too many options): the job
        parks with that reason, never as a login wall."""
        self._failed(step, e)
        return _Parked("needs_human", f"the judge refused to read the account screen "
                                      f"({type(e).__name__} at {step})", LOGIN_NOTE)

    def _failed(self, step: str, e: BaseException) -> None:
        """An account step that raised: its step and the exception's
        type in the trace and in `last_error`, which the park's reason
        carries; never its message (it may quote a filled value)."""
        self.last_error = f"{type(e).__name__} at {step}"
        self.run._trace("error", step=step, error=type(e).__name__)

    def _signup_button(self, page, digest) -> bool | None:
        """A sign-in screen's one create-account BUTTON (Workday's
        `createAccountLink`, SuccessFactors, Oracle), never one in the
        site's header, a disabled one, a sign-up with another site, or one
        whose words send the application ("Create account and apply"):
        clicked through the live click guard, and the loop reads the screen
        it shows. None when the screen has no such button (or several that
        say different things); False when the click was refused or changed
        nothing."""
        own: dict[str, apply_form.Button] = {}
        for b in digest.buttons:
            if b.chrome or b.disabled or not _CREATE_ACCOUNT.search(b.text or "") \
                    or _THIRD_PARTY.search(b.text or "") or _NOT_AN_ACCOUNT.search(b.text or "") \
                    or apply_judge.DECLINE_WORDS.search(b.text or ""):
                continue
            own.setdefault(" ".join(b.text.lower().split()), b)
        if len(own) != 1:
            return None
        button = next(iter(own.values()))
        if _sends_application(digest, button.n, account_only=False):
            self.run._decide("signup_button", f"the sign-in screen's create-account button "
                                              f"({_cap(button.text, 60)}) reads as sending the "
                                              "application; it is not clicked")
            return False
        self.run._decide("signup_button", "the sign-in screen's one create-account button",
                         text=button.text)
        rec = self.run.pages[-1] if self.run.pages else None
        if rec is not None:
            rec["clicked"].append(f"{button.text} (to the sign-up)")
        self.run._last_click = (button.text, "advance")
        result = self._click(page, digest, button.n)
        if result.refused:
            raise _Parked("needs_human", f"the account step's button ({_cap(button.text, 60)}) "
                                         f"changed before the click: {result.refused}; nothing "
                                         f"was clicked", LOGIN_NOTE)
        self.run._check_host(page.url)
        return bool(result.changed)

    def _attempt_sign_in(self, page, digest, host: str) -> bool:
        """No account in the ledger for the site, and no way to make
        one on this screen (the user may have made it by hand with the
        master password): one sign-in with the sign-up address and the
        master password, on a screen of the address and the password. A
        sign-in that leads on puts the account in the ledger
        (`confirm_sign_ins`); one that comes back to a sign-in parks."""
        if not _password_boxes(digest) or not any(_is_email_box(f) for f in digest.fields):
            return False
        email = self._signup_email()
        site = _site(host)
        if not email or site in self.attempted:
            return False
        self.attempted.add(site)
        self.run._decide("sign_in_attempt", "no account in the ledger and no sign-up on the "
                                            "screen: one sign-in with the master password")
        if not self._fill(page, digest, host, email, False):
            return False
        self.pending[site] = (host, email)
        return True

    def confirm_sign_ins(self) -> None:
        """The attempted sign-ins that led past the account
        screens: their accounts go in the ledger."""
        for site, (host, email) in list(self.pending.items()):
            self.run._record_account(host, email, note="signed in with the master password")
            self.run._decide("account_recorded", f"the sign-in on {host} led on; the account is "
                                                 "in the ledger")
            del self.pending[site]

    def _signup_link(self, page, digest, host: str) -> bool | None:
        """The sign-in page's one create-account link, taken once the judge
        rates it a way on (the page it leads to is read and signed up on
        here). A link to job alerts, a newsletter or a talent network, a
        sign-up with another site or a decline is none, and a link
        in the site's header, nav or footer counts only when the page's body
        has none. None when the page has no such link, several that lead to
        different pages, or one the judge does not rate a way on: the
        screen's create-account button and the one sign-in
        come next."""
        # Expose account-creation links as buttons to the same role judge.
        links = page.get_by_role("link").filter(has_text=re.compile(r"create.*account|sign up|register", re.I))
        # (text, target, in the site's chrome, the link's index)
        found: list[tuple[str, str, bool, int]] = []
        for i in range(links.count()):
            link = links.nth(i)
            text = " ".join((link.inner_text(timeout=apply_click.ACTION_TIMEOUT_MS) or "").split())
            href = link.get_attribute("href", timeout=apply_click.ACTION_TIMEOUT_MS)
            if not href or _NOT_AN_ACCOUNT.search(text) or _THIRD_PARTY.search(text) \
                    or apply_judge.DECLINE_WORDS.search(text):
                continue
            found.append((text, urljoin(page.url, href), bool(link.evaluate(_LINK_CHROME_JS)),
                          i))
        found = [f for f in found if not f[2]] or found
        if not found:
            return None
        # a header link and a body link that point at the same page are one
        # offer; two different destinations are a choice nobody made. The
        # hrefs are resolved against the page first, so an absolute link
        # and a relative one to the same target count once.
        targets = {f[1] for f in found}
        if len(targets) != 1:
            self.run._decide("signup_link", f"the sign-in page's create-account links lead to "
                                            f"{len(targets)} pages; none is followed")
            return None
        target = targets.pop()
        # "#", "#signup" or "javascript:void(0)": the page's own script opens
        # the sign-up, so the link is clicked through the live click guard
        # where a real address is loaded
        scripted = _scripted_link(target, str(page.url))
        if not scripted:
            self.run._check_host(target)
        link_digest = apply_form.FormDigest(
            url_host=host, title=digest.title, text=digest.text,
            buttons=[apply_form.Button(0, (0, "a"), found[0][0], "")])
        plan = apply_judge.plan(link_digest, self.run.catalog,
                                self.run._map(link_digest, {}, "job_posting", discover=False,
                                              own_page=False), company=self.run._company())
        advance_conf = plan.buttons.get("advance", (None, 0))[1]
        self.run._decide("signup_link", "the sign-in page's one create-account link",
                         target=target, advance=advance_conf)
        if advance_conf < apply_judge.BUTTON_ADVANCE_MIN_CONF:
            return None
        if scripted:
            if not self._click_link(page, links.nth(found[0][3]), found[0][0]):
                return False
        else:
            page.goto(target, timeout=self._nav_timeout())
        self.run._check_host(page.url)
        # the sign-up page renders like any other: it is read once it
        # holds still
        info = apply_fill.settle(page, apply_limits.CLICK_TIMEOUT_S)
        self.run._decide_next("settled", f"settled {_settled_ms(info)} ms after the "
                                         "create-account link")
        fresh = self.run._drop_foreign_controls(self.run._extract(page))
        # the loop's own read: the judge with the page's
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
            raise self.run._account_park(fresh, f"login wall (the create-account link led to "
                                                f"{_cap(page.url, 120)}: "
                                                f"{self.run._account_evidence(state, fresh)})")
        if not self.signup(page, fresh, fresh.url_host or _host(page.url)):
            raise self.run._account_park(fresh, f"account signup needed (the create-account "
                                                f"link led to {_cap(page.url, 120)}: "
                                                f"{self.run._account_evidence(state, fresh)})")
        return True

    def _click_link(self, page, link, text: str) -> bool:
        """A create-account link whose address the page's script handles
        (`_scripted_link`), clicked through the account step's guarded
        click (`_click`): True when the page changed. A click the live
        check refused parks; one that changed nothing is False."""
        link.evaluate(_MARK_LINK_JS)
        one = apply_form.FormDigest(url_host=_host(page.url), title="", text="",
                                    buttons=[apply_form.Button(0, (0, _MARKED_LINK), text,
                                                               "link")])
        rec = self.run.pages[-1] if self.run.pages else None
        if rec is not None:
            rec["clicked"].append(f"{text} (to the sign-up)")
        self.run._last_click = (text, "advance")
        result = self._click(page, one, 0)
        if result.refused:
            raise _Parked("needs_human", f"the account step's link ({_cap(text, 60)}) changed "
                                         f"before the click: {result.refused}; nothing was "
                                         f"clicked", LOGIN_NOTE)
        return bool(result.clicked and result.changed)

    def signup(self, page, digest, host: str) -> bool:
        self.last_error = ""
        return self._fill(page, digest, host, self._signup_email(), True)

    def _signup_email(self) -> str:
        return str(self.run.r.run_context().get("signup_email") or "")

    def _timeout(self) -> int:
        """Milliseconds for one action on the page, inside the job's clock."""
        return max(1, int(min(5, self.run.deadline - self.run.r.clock()) * 1000))

    def _nav_timeout(self) -> int:
        """Milliseconds for a page load, which takes longer than an action and
        gets the loop's own click budget (`CLICK_TIMEOUT_S`)."""
        return max(1, int(min(apply_limits.CLICK_TIMEOUT_S,
                              self.run.deadline - self.run.r.clock()) * 1000))

    def _record(self, digest, email: str, advance_n: int, passwords: int,
                others: list | None = None) -> None:
        """Write the account step into the current page's record:
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
        exists = account_exists(digest) if signup and (site, "signup") in self.password_typed \
            else ""
        if exists:
            # The sign-up says the address has an account: one
            # sign-in with the master password instead, never a second sign-up
            self.exists.add(site)
            self.run._decide("account_exists", f"the sign-up says {exists!r}; the sign-in is "
                                               "the step")
            return self._to_sign_in(page, digest, host, _page_words(exists, 120))
        groups = account_forms(page, digest)
        if groups:
            # A sign-in and a sign-up side by side: the sign-in when
            # the ledger knows the account, else the sign-up; its own form's
            # boxes and button alone are acted on
            want = "signin" if self.run._account_for(host) else "signup"
            chosen = next((g for g in groups if password_step(g) == want), None)
            if chosen is not None:
                self.run._decide("account_form", f"a sign-in and a sign-up side by side; the "
                                                 f"{'sign-in' if want == 'signin' else 'sign-up'} "
                                                 "form is the step",
                                 fields=[f.label for f in chosen.fields])
                digest, signup = chosen, want == "signup"
        frames = apply_form.frames(page)
        guard = apply_sendwatch._NavGuard(self.run, page)
        try:
            answers = self.run._map(digest, {}, "signup_form" if signup else "login_wall",
                                    discover=False)
            plan = apply_judge.plan(
                digest, self.run.catalog, answers,
                generation_enabled=bool(self.run.r.settings["auto_apply_generate"]),
                company=self.run._company())
            rec = self.run.pages[-1] if self.run.pages else {"flags": {}}
            plan = self.run._complete_option_plan(digest, answers, plan, rec)
            advance = account_advance(digest, plan, signup=signup)
            if advance is None:
                return False
            by_n = {pf.n: pf for pf in plan.fields}
            passwords, emails, others, written = [], [], [], []
            # the addresses the password would answer to: the page's, each
            # password box's frame's (the frame the fill acts in) and its
            # form's action
            password_urls = [str(page.url)]
            action_urls: list[str] = []
            box_frames: list = []
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
                    frame = self.run._box_frame(page, field.locator, frames)
                    box_frames.append(frame)
                    if frame is not None:
                        guard.frames.add(id(frame))
                        password_urls.append(self.run._frame_address(frame))
                    action_urls.append(_form_action(loc))
                elif _is_email_box(field):
                    emails.append(loc)
                    if 0 <= idx < len(frames):
                        guard.frames.add(id(frames[idx]))
                else:
                    pf = by_n.get(field.n)
                    if pf is not None and pf.action == "generate":
                        written.append(pf)
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
                    elif pf is not None and pf.fact_key == "needs_generation":
                        # an optional open-ended question stays blank
                        # (`apply_judge.plan`), and only an application asks it
                        written.append(pf)
            if not passwords and not emails:
                return False
            # A screen that asks what only an application asks (a profile
            # link, work authorization, a written answer) is the form: the
            # form step writes and verifies the answers, types the password,
            # and clicks its way on or stops at the submit gate.
            carries = bool(written) or any(pf.fact_key not in _ACCOUNT_FACTS for pf in others)
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
                    and site not in self.email_sites and not self.run._account_for(host):
                return False
            if passwords:
                # the last word before the password is typed: the page and
                # every frame holding a password box are on the application's
                # site, whichever path (a sign-up link, a redirect) led here
                why = self.run._secret_refusal([*password_urls, *action_urls])
                if why.startswith(PASSWORD_HTTP_REASON):
                    raise _Parked("needs_human", why, LOGIN_NOTE)
                outside = sorted({_host(u) for u in password_urls
                                  if _host(u) and not self.run._password_ok(u)})
                if outside:
                    raise _Parked("needs_human",
                                  f"a sign-in on {outside[0]}, outside the application site",
                                  LOGIN_NOTE)
                if signup or password_step(digest) == "signup":
                    self.run._check_password_rules(digest, host)
                kind = "signup" if signup else "login"
                if (site, kind) in self.password_typed:
                    if not (signup and self._retype(site, passwords, digest)):
                        raise _Parked("needs_human", self._not_taken(site, host, kind, digest),
                                      LOGIN_NOTE)
            guard.start()
            try:
                for loc in emails:
                    try:
                        current = str(loc.input_value(timeout=self._timeout()) or "")
                    except Exception:       # noqa: BLE001  (a box that cannot be read is filled)
                        current = ""
                    if current.strip().lower() != email.strip().lower():
                        loc.fill(email, timeout=self._timeout())
                # a box whose options tie on the answer is left blank and
                # traced; a required one parks before the password is typed
                # (the form's rule on the account screen too)
                errors: list[dict] = []
                typed = FillPlan(fields=others)
                filled = apply_fill.apply(page, typed, deadline=self.run.deadline,
                                          clock=self.run.r.clock, errors=errors)
                tied = self.run._option_ties(typed, errors)
                filled = [f for f in filled if f.n not in tied]
                self.run._park_a_required_tie(typed, tied)
                for loc, frame in zip(passwords, box_frames):
                    # the box's frame where the fill acts, once more just
                    # before it: a frame that moved since the check above
                    # never takes the password
                    moved = self.run._moved_box(page, frame)
                    if moved:
                        raise _Parked("needs_human", moved, LOGIN_NOTE)
                    if not ats_accounts.fill_password(page, loc,
                                                      host_ok=self.run._password_frame_ok):
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
                result = self._click(page, digest, advance[0])
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
                    self.run._record_account(host, email)
                challenge = (not guard.blocked and not result.changed
                             and self.run._human_check_showing())
                if challenge:
                    # the credentials are still on the page while the person
                    # solves the challenge, and the page may post them once it
                    # clears: the guard stays on through the wait
                    try:
                        self.run._wait_for_human_check("a CAPTCHA challenge appeared at sign-in")
                    except _Parked:
                        if not guard.blocked:
                            raise
            finally:
                guard.stop()
            if guard.blocked:
                raise _Parked("needs_human", f"left the allowed sites: {guard.blocked[0]}", before)
            if emails:
                self.email_sites.add(site)
            if not result.changed and not challenge:
                return False
            self.run._check_host(page.url)
            # the loop judges whatever comes next: the password screen, the
            # form, a code gate, or the same screen with an error (which the
            # per-site step cap ends)
            return True
        except (_Parked, _AsForm, jev.JudgeOutage, jev.JevUnavailable):
            raise
        except jev.RequestRejected as e:
            raise self._judge_refused("accounts.fill", e) from None
        except Exception as e:  # noqa: BLE001  (Playwright may include filled values)
            self._failed("accounts.fill", e)
            return False


class _Inbox:
    """The job's verification mail (`apply_inbox`), read in a tab of its own:
    a code, never one older than the job or one the job used already, or an
    account check's link on an allowed host; never from a sender the job
    refuses (`_JobRun._sender_refused`)."""

    def __init__(self, run):
        self.run = run
        self.used: set[str] = set()         # the codes handed to the run, by `code_hash`
        self.refused: list[str] = []        # hosts of verification links never opened

    def _entry_words(self) -> dict[str, str]:
        entry = self.run.entry
        return {"ats": str((entry.get("ats") or {}).get("system") or ""),
                "company": str(entry.get("company") or "")}

    def fetch_code(self, page, site: str, inbox_url: str) -> str | None:
        self.run._check_host(inbox_url)
        errors: list[str] = []
        code = apply_inbox.fetch_code(page, site, inbox_url, jev=self.run.r.jev,
                                      clock=self.run.r.clock, sleep=self.run.r.sleep,
                                      deadline=self.run.deadline, errors=errors,
                                      since=self.run.started_at, used=frozenset(self.used),
                                      refuse=self.run._sender_refused, **self._entry_words())
        if code:
            self.used.add(apply_inbox.code_hash(code))
        self.run._trace("inbox", found=bool(code), errors=errors)
        return code

    def fetch_link(self, page, site: str, inbox_url: str) -> str | None:
        self.run._check_host(inbox_url)
        errors: list[str] = []
        refused: list[str] = []
        link = apply_inbox.fetch_link(page, site, inbox_url, jev=self.run.r.jev,
                                      allowed=self.run._link_ok, clock=self.run.r.clock,
                                      sleep=self.run.r.sleep, deadline=self.run.deadline,
                                      errors=errors, since=self.run.started_at, refused=refused,
                                      refuse=self.run._sender_refused, **self._entry_words())
        self.refused = refused
        # the link's host alone: its path and query carry the account's token
        self.run._trace("inbox_link", found=bool(link), host=_host(link or ""),
                        refused=refused, errors=errors)
        return link


# an account screen's own button by its words: sign in, log in, create an
# account, register, sign up, continue, next
_ACCOUNT_BUTTON = re.compile(r"\b(sign|log)[\s-]*(in|on|up)\b|\blogin\b|\bregister\b"
                             r"|\bcreate\b.*\baccount\b|\b(continue|next)\b", re.I)


_SIGN_UP_WORDS = re.compile(r"\bcreate\b|\bregister\b|\bsign[\s-]*up\b|\bjoin\b", re.I)
# a control that goes to the sign-up from a sign-in screen, never a
# sign-up for job alerts, a newsletter or a talent network
_CREATE_ACCOUNT = re.compile(r"\bcreate\s+(?:an?\s+|your\s+|new\s+)?(?:\w+\s+)?account\b"
                             r"|\bsign[\s-]*up\b|\bregister\b|\bnew\s+(?:user|candidate)\b", re.I)
_NOT_AN_ACCOUNT = re.compile(r"\balerts?\b|\bnewsletter|\btalent\s+(?:community|network|pool)"
                             r"|\bupdates\b|\bevents?\b", re.I)
# a link in the site's header, nav or footer (a form's or a dialog's own
# header is the form's)
_LINK_CHROME_JS = """el => {
  const c = el.closest('header, footer, nav, [role=banner], [role=contentinfo], [role=navigation]');
  return !!c && !(c.parentElement && c.parentElement.closest('form, dialog, [role=dialog]'));
}"""
_SIGN_IN_ONLY = re.compile(r"\b(sign|log)[\s-]*(in|on)\b|\blogin\b", re.I)


def password_step(digest: apply_form.FormDigest) -> str:
    """What an account screen's password boxes say it is:
    "signup" for a `new-password` box or two boxes (a password and its
    confirmation), "signin" for `current-password` alone, or for one box
    that names neither beside a "Forgot your password?" control (a sign-in's
    own tie-break); "" (the read decides) with no box,
    one box that names neither and nothing else to go on (a one-box sign-up
    looks like a sign-in), or boxes that say both (a
    change of password)."""
    boxes = [f for f in digest.fields if apply_form.password_box(f)]
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

# A sign-up's statement that the address has an account already
# ("An account with this email already exists"; never the question "Already
# have an account? Sign in")
ACCOUNT_EXISTS_WORDS = re.compile(
    r"\b(?:account|user|profile|e-?mail(?:\s+address)?|username)\b[^.!?\n]{0,60}?\b(?:already\s+"
    r"(?:exists?|registered|in\s+use|taken|been\s+(?:registered|used|taken)|associated)"
    r"|is\s+already\s+(?:registered|in\s+use|taken))"
    r"|\balready\s+(?:have|has)\s+an\s+account\s+with\s+(?:this|that|the)\b", re.I)


_PROBLEM_WORDS = re.compile(r"\berror\b|\binvalid\b|\bincorrect\b|\bwrong\b|\bnot\s+match"
                            r"|\btry\s+again\b|\bfailed\b|\bunable\b|\balready\b|\bnot\s+valid\b",
                            re.I)

# A page's line can quote what the person typed ("jane@x.com
# is already registered", "'Jane Q' does not match"). A reason reaches the
# queue row and the drain's report, which the person may send on, so a line
# enters it without its quoted spans and its email- or phone-shaped tokens;
# the local trace keeps the whole line
_QUOTED_SPAN = re.compile(r"\"[^\"\n]{0,200}\"|“[^”\n]{0,200}”"
                          r"|‘[^’\n]{0,200}’|(?<!\w)'[^'\n]{0,200}'(?!\w)")
_EMAIL_SHAPED = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_SHAPED = re.compile(r"\+?\(?\d[\d\s().-]{5,}\d")


def _quote_key(text: object) -> str:
    """A field's label as a quote of it is compared: lowercased, spaces
    joined, the required mark and a trailing colon dropped."""
    return " ".join(str(text or "").replace("*", " ").split()).strip(" :").lower()


def _page_words(text: object, n: int, labels: Iterable[object] = ()) -> str:
    """`text` (a page's line) for a reason: quoted spans, email-shaped tokens
    and phone-shaped runs (seven digits or more) replaced, spaces joined,
    capped at `n`. A quoted span that is one of the page's field
    `labels` stays ("Please complete the 'Start date' field"): a label is
    the form's own words, never what the person typed."""
    own = {k for k in map(_quote_key, labels) if k}

    def _quoted(m: re.Match) -> str:
        return m.group(0) if own and _quote_key(m.group(0)[1:-1]) in own else "[quoted]"
    line = _QUOTED_SPAN.sub(_quoted, str(text or ""))
    line = _EMAIL_SHAPED.sub("[email]", line)
    line = _PHONE_SHAPED.sub(
        lambda m: "[number]" if sum(c.isdigit() for c in m.group(0)) >= 7 else m.group(0), line)
    return _cap(" ".join(line.split()), n)


def page_problem(digest: apply_form.FormDigest | None) -> str:
    """The first line of a page's text that reads as a problem (an error, a
    refusal, a mismatch, an address in use), capped: never a field's label
    or a question ("Already have an account?"); "" with none. The
    line comes without what it may quote of the person's data
    (`_page_words`)."""
    if digest is None:
        return ""
    labels = {" ".join((f.label or "").lower().split()) for f in digest.fields}
    for line in (digest.text or "").splitlines():
        line = " ".join(line.split())
        if line and "?" not in line and line.lower() not in labels \
                and _PROBLEM_WORDS.search(line):
            return _page_words(line, 120)
    return ""


def account_exists(digest: apply_form.FormDigest) -> str:
    """The words a page states that the address has an account already,
    or ""; a question or a condition never counts
    (`apply_judge.statement_words`)."""
    return apply_judge.statement_words(ACCOUNT_EXISTS_WORDS, digest.text or "",
                                       [f.label for f in digest.fields])


# The password rules a sign-up states, read by code
_RULE_LINE = re.compile(r"character|letter|number|digit|numeric|symbol|upper|lower|special|length"
                        r"|\blong\b|at least|minimum|maximum|must|contain|include", re.I)
_RULE_MIN = (
    re.compile(r"(?:at\s+least|a\s+minimum\s+of|minimum(?:\s+of)?|min\.?|no\s+(?:fewer|less)\s+than)"
               r"\s+(\d{1,2})\s*(?:characters|chars)\b", re.I),
    re.compile(r"\b(\d{1,2})\s*(?:or\s+more|\+)\s*(?:characters|chars)\b", re.I),
    re.compile(r"\b(\d{1,2})\s+characters\s+(?:or\s+(?:more|longer)|minimum)\b", re.I))
_RULE_RANGE = re.compile(r"\b(?:between\s+)?(\d{1,2})\s*(?:-|to|and|\u2013)\s*(\d{1,3})\s*"
                         r"(?:characters|chars)\b", re.I)
_RULE_MAX = re.compile(r"(?:at\s+most|a\s+maximum\s+of|maximum(?:\s+of)?|max\.?|no\s+more\s+than"
                       r"|up\s+to|not\s+(?:exceed|be\s+longer\s+than))\s+(\d{1,3})\s*"
                       r"(?:characters|chars)\b", re.I)
_RULE_CLASSES = {
    "upper": re.compile(r"upper\s*-?\s*case|capital\s+letter", re.I),
    "lower": re.compile(r"lower\s*-?\s*case", re.I),
    "digit": re.compile(r"\b(?:number|numeral|digit|numeric)", re.I),
    "special": re.compile(r"special\s+character|\bsymbol|non-?\s*alpha|punctuation", re.I),
}
_RULE_FORBIDDEN = re.compile(
    r"(?:cannot|can't|must\s+not|may\s+not|should\s+not|do\s+not)\s+(?:contain|include|use)\s+"
    r"(?:the\s+)?(?:following\s+)?(?:characters?|symbols?)?\s*:?\s*"
    r"((?:(?:[^\w\s]|\b(?:and|or)\b)\s*){1,30})", re.I)
# "at least 3 of the following", "three of these", "3 out of 4": a count
# of the classes the rules name
_RULE_SOME = re.compile(r"\b([1-4]|one|two|three|four)\s+(?:out\s+)?of\s+(?:the\s+)?"
                        r"(?:following|these|those|(?:\d|two|three|four|five)\b)", re.I)
_RULE_COUNTS = {"one": 1, "two": 2, "three": 3, "four": 4}
_PASSWORD_WORD = re.compile(r"\bpassword", re.I)
# a prohibition, to the end of its clause: the classes it names ("must not
# contain your phone number", "no spaces or special characters") are no
# class the password needs
_RULE_NOT = re.compile(
    r"(?:\b(?:cannot|can\s+not|must\s+not|may\s+not|should\s+not|do\s+not|does\s+not|never)"
    r"|\b(?:can|don|doesn)['\u2019]t|\bnot\s+(?:allowed|permitted)|\bno)\b"
    r"[^.;!]*?(?=\band\s+(?:must|should|needs?\s+to|has\s+to|contains?|includes?)\b|[.;!]|$)",
    re.I)
_RULE_CLASS_WORD = (r"(?:" + "|".join(f"(?:{p.pattern})" for p in _RULE_CLASSES.values())
                    + r")\w*(?:\s+(?:letters?|characters?))?")
# classes joined by "or" ("a number or special character", "a number, a
# symbol or an uppercase letter"): any one of them will do
_RULE_EITHER = re.compile(
    _RULE_CLASS_WORD + r"(?:\s*,\s*(?:an?\s+|one\s+)?" + _RULE_CLASS_WORD + r")*"
    r"\s*,?\s+or\s+(?:an?\s+|one\s+)?" + _RULE_CLASS_WORD, re.I)
# a rule's words the reader cannot read with certainty:
# advice ("avoid", "recommended", "should") is no rule at all, and a choice
# ("or", "and/or", "a mix of", "any") that `_RULE_EITHER` does not read
# whole ("a digit (0-9) or a special character") makes its sentence's
# classes no class the password needs. "8 or more" is a count, no choice
_RULE_ADVICE = re.compile(r"\b(?:avoid\w*|recommend\w*|suggest\w*|should|ideally|prefer\w*"
                          r"|encourag\w*|consider\w*|optional\w*|tips?)\b", re.I)
_RULE_CHOICE = re.compile(r"\bor\b|\b(?:mix|mixture|combination|variety)\s+of\b|\bany\b", re.I)
_RULE_OR_COUNT = re.compile(r"\bor\s+(?:more|longer|greater|fewer|less|above|higher)\b", re.I)
# a line's head before "(" or ":" that is no rule ("Nickname (up to 20
# characters)", "Bio: at least 50 characters"): another field's label
_RULE_HEAD = re.compile(r"^([^(:]*)[(:]")
_RULE_HEADING = re.compile(r"requirement|\brules?\b|polic|criteria|strength", re.I)
# a rule line about another name the screen asks for, never the password's
_RULE_OTHER_NAME = re.compile(r"\buser\s*-?\s*names?\b|\bnick\s*names?\b|\b(?:display|screen)\s+names?\b",
                              re.I)


def _bare_label(text: str) -> str:
    """A label's words as a line of the page shows them: lower case, the
    required mark ("*", "(required)") left off."""
    text = " ".join(str(text or "").lower().split())
    return re.sub(r"\s*(?:\*|\(required\)|\(optional\))$", "", text)


def _forbidden_chars(listed: str) -> str:
    """The characters a forbidden list names, its separators left out: a
    comma, a semicolon, a slash, a full stop, "and", "or"; a character in
    quotes is the character. A separator the site forbids is left for the
    site to judge."""
    chars: set[str] = set()
    for token in listed.split():
        if token.lower() in ("and", "or"):
            continue
        token = re.sub(r"[,;/.]", "", token)
        if len(token) == 3 and token[0] == token[2] and token[0] in "\"'`":
            token = token[1]
        chars.update(token)
    return "".join(sorted(chars))


def _rule_lines(digest: apply_form.FormDigest, boxes: list) -> list[str]:
    """The words a screen ties to its password boxes: each box's own help
    (its aria-describedby, its maxlength) and a hint in it that reads as a
    rule, and the rules list under a line that names the password (its
    label, "Password requirements:"): the lines that follow it while they
    read as rules. A line that reads as no rule, or another field's label
    (alone, or with its own hint: "Nickname (up to 20 characters)"), ends
    the list; a line that names the password starts one of its own; a rule
    for the username, outside a prohibition, is left out."""
    parts = [" ".join(str(f.help or "").split()) for f in boxes if f.help]
    parts += [" ".join(f.placeholder.split()) for f in boxes
              if f.placeholder and _RULE_LINE.search(f.placeholder)]
    labels = {_bare_label(f.label) for f in digest.fields if f.label}
    lines = [" ".join(line.split()) for line in (digest.text or "").splitlines()]
    for i, line in enumerate(lines):
        if not _PASSWORD_WORD.search(line):
            continue
        for j, near in enumerate(lines[i:i + 9]):
            if not near:
                continue
            named = _PASSWORD_WORD.search(near)
            if _bare_label(near) in labels:
                if named:
                    continue        # a password box's own label ("Confirm password")
                break               # another field's label: the password's block ends
            head = _RULE_HEAD.match(near)
            if head and head.group(1).strip() and not named and not _RULE_LINE.search(
                    head.group(1)) and not _RULE_HEADING.search(head.group(1)):
                break               # another field's label with its own hint
            if not named and _RULE_OTHER_NAME.search(_RULE_NOT.sub(" ", near)):
                continue            # the username's rule ("at least 3 characters in the username")
            if _RULE_LINE.search(near):
                parts.append(near)
            elif not (j == 0 or named):
                break               # a line that is no rule ends the list
    return parts


def _class_rules(parts: list[str]) -> dict[str, Any]:
    """The character classes the rules' words ask for, their prohibitions
    already left out, and only the ones read with certainty: a sentence of advice (`_RULE_ADVICE`) is no rule. With a count
    ("3 of the following"), the classes named and `classes_needed`; else
    each sentence's classes, where classes joined by "or" are any one of
    them (`classes_needed` 1 when that one choice is all the rules ask). A
    sentence with a choice `_RULE_EITHER` did not read whole ("and/or", "a
    mix of", "any", "(0-9) or": `_RULE_CHOICE`) needs none of its classes.
    A choice beside other classes, or two choices, is unclear and no rule:
    the site judges it."""
    sentences = [s for p in parts for s in re.split(r"[.;!](?:\s+|$)", p)
                 if s.strip() and not _RULE_ADVICE.search(s)]
    said = " ".join(sentences)
    named = [key for key, pattern in _RULE_CLASSES.items() if pattern.search(said)]
    m = _RULE_SOME.search(said)
    if m:
        need = int(m.group(1)) if m.group(1).isdigit() else _RULE_COUNTS[m.group(1).lower()]
        if need > len(named):
            return {}       # fewer classes named than the count: the list was not read whole
        return {**dict.fromkeys(named, True),
                **({"classes_needed": need} if need < len(named) else {})}
    required: set[str] = set()
    choices: list[set[str]] = []
    for sentence in sentences:
        here = {key for key, pattern in _RULE_CLASSES.items() if pattern.search(sentence)}
        joined = {key for e in _RULE_EITHER.finditer(sentence)
                  for key, pattern in _RULE_CLASSES.items() if pattern.search(e.group(0))}
        unread = _RULE_EITHER.sub(" ", _RULE_OR_COUNT.sub(" ", sentence))
        if not _RULE_CHOICE.search(unread):
            required |= here - joined if len(joined) > 1 else here
        if len(joined) > 1:
            choices.append(joined)
    if required:
        return dict.fromkeys((key for key in _RULE_CLASSES if key in required), True)
    if len(choices) == 1:
        return {**dict.fromkeys((key for key in _RULE_CLASSES if key in choices[0]), True),
                "classes_needed": 1}
    return {}


def password_rules(digest: apply_form.FormDigest) -> tuple[dict[str, Any], str]:
    """(the password rules an account screen states, the words
    they were read from). The words are the ones the screen ties to its
    password boxes (`_rule_lines`), never another field's hint ("Up to 20
    characters" under a name) or a line further down: a length (at least,
    at most, a range), an uppercase letter, a lowercase letter, a digit, a
    special character, a count of those ("3 of the following", "a number
    or special character": `classes_needed`, `_class_rules`), characters it
    must not hold. A class a prohibition names ("must not contain your
    phone number") is no class the password needs. A rule the words leave
    unclear is not read: the master password is typed and the site judges
    it."""
    boxes = _password_boxes(digest)
    if not boxes:
        return {}, ""
    parts = list(dict.fromkeys(p for p in _rule_lines(digest, boxes) if p))
    said = " ".join(parts)
    rules: dict[str, Any] = {}
    m = _RULE_RANGE.search(said)
    if m and int(m.group(1)) <= int(m.group(2)):
        rules["min_length"], rules["max_length"] = int(m.group(1)), int(m.group(2))
    for pattern in _RULE_MIN:
        m = pattern.search(said)
        if m:
            rules["min_length"] = max(int(m.group(1)), int(rules.get("min_length") or 0))
            break
    m = _RULE_MAX.search(said)
    if m:
        rules["max_length"] = int(m.group(1))
    if rules.get("min_length", 0) > rules.get("max_length", 1000):
        rules.pop("min_length")         # lengths that cannot both hold: unclear
        rules.pop("max_length")
    rules.update(_class_rules([_RULE_NOT.sub(" ", p) for p in parts]))
    m = _RULE_FORBIDDEN.search(said)
    banned = _forbidden_chars(m.group(1)) if m else ""
    if banned:
        rules["forbidden"] = banned
    return rules, said


# A sign-in with another site's account ("Sign in with Google",
# "Continue with Microsoft"): the run never takes one
_SSO_SIGN_IN = re.compile(r"\b(?:sign|log)[\s-]*(?:in|on|up)\b|\bcontinue\b|\bregister\b"
                          r"|\bsign[\s-]*up\b", re.I)
_SSO_NAME = re.compile(r"\b(?:with|using|via|through)\s+(?:your\s+)?([a-z]+)\b", re.I)
_SSO_NAMES = {"linkedin": "LinkedIn", "github": "GitHub", "sso": "SSO", "x": "X"}


# The page chrome a screen of sign-ins with other sites may hold beside
# them, none a way on. Any other control
# may be a way on, so the screen is not SSO-only: a screen misread so ends
# at the account step, whose park falls back to the SSO one
# (`_JobRun._account_park`), where a screen misread the other way would lose
# a job the run could apply to.
# - a word of help, a way back, a cancel, a close, the site's privacy,
#   terms and cookie notices, read after the words of a way on
_SSO_ASIDE = re.compile(r"\bhelp\b|\bback\b|\bcancel\b|\bclose\b|\bdismiss\b|\bprivacy\b"
                        r"|\bterms\b|\bcookies?\b", re.I)
# - a cookie banner's choice ("Accept all" reads as an advance otherwise)
_COOKIE_CHOICE = re.compile(r"\bcookies?\b|^\s*(?:accept|allow|reject|refuse|deny|decline)\s+all\b",
                            re.I)
# - a control whose whole words are chrome, read before the words of a way
#   on ("Email us" holds "email", "Forgot password?" a password): a page
#   about the site, a way to reach it, a language, an account's recovery
_SSO_LANGUAGE = (r"english|fran[c\u00e7]ais|deutsch|espa[n\u00f1]ol|italiano|portugu[e\u00ea]s"
                 r"|nederlands|polski|svenska|dansk|norsk|suomi|t\u00fcrk\u00e7e|magyar"
                 r"|\u0440\u0443\u0441\u0441\u043a\u0438\u0439|\u65e5\u672c\u8a9e"
                 r"|(?:\u7b80\u4f53|\u7e41\u9ad4)?\u4e2d\u6587|\ud55c\uad6d\uc5b4"
                 r"|(?:en|fr|de|es|pt|nl|pl|sv|da|fi|ja|zh|ko)(?:[-_][a-z]{2})?")
_SSO_CHROME = re.compile(
    r"learn\s+more(?:\s+about\b.{0,40})?|read\s+more|about\s+us"
    r"|contact(?:\s+(?:us|support))?|(?:e-?mail|phone|call)\s+(?:us|support)|support"
    r"|faqs?|frequently\s+asked\s+questions|accessibility(?:\s+statement)?"
    r"|terms(?:\s+of\s+(?:use|service)|\s+(?:and|&)\s+conditions)?|code\s+of\s+conduct"
    r"|(?:(?:select|change|choose)\s+(?:your\s+)?)?languages?(?:\s*:\s*(?:" + _SSO_LANGUAGE + r"))?"
    r"|(?:" + _SSO_LANGUAGE + r")(?:\s*\([^)]{1,30}\))?"
    r"|forgot(?:\s+(?:your|my))?\s+(?:password|user\s*name|e-?mail(?:\s+address)?|login"
    r"|sign[\s-]*in(?:\s+details)?)|(?:reset|recover)\s+(?:your\s+|my\s+)?(?:password|account)"
    r"|(?:having\s+)?trouble\s+(?:signing|logging)\s+in|(?:can['\u2019]?t|cannot)\s+(?:sign|log)"
    r"\s+in|account\s+recovery|need\s+help\s+signing\s+in", re.I)
# a control that offers another way to sign in or apply than another site's
# account, whatever aside word it also holds ("Go back and use email")
_SSO_WAY_ON = re.compile(
    r"\b(?:more|other|another|different|alternative)\s+(?:sign[\s-]*in\s+|log[\s-]*in\s+)?"
    r"(?:ways?|methods?|options?)\b|\bshow\s+(?:more|all)\b|\be-?mail\b|\bpassword\b"
    r"|\busername\b|\bphone\b|\bcode\b|\b(?:magic|sign[\s-]*in|login)\s+link\b|\bjoin\b"
    r"|\bnew\s+(?:user|candidate|account)\b|\bguest\b", re.I)


def _sso_site(text: str) -> str | None:
    """The site a control signs in with ("Sign in with Google": Google),
    None for a control that is no sign-in with another site's account."""
    if not (_THIRD_PARTY.search(text) and _SSO_SIGN_IN.search(text)):
        return None
    m = _SSO_NAME.search(text)
    word = (m.group(1) if m else "another site").lower()
    return _SSO_NAMES.get(word, word.capitalize())


def _sso_chrome(text: str) -> bool:
    """Is a control page chrome beside sign-ins with other sites
    (`_COOKIE_CHOICE`, `_SSO_CHROME` over its whole words)?"""
    core = re.sub(r"^[^\w(]+|[^\w)]+$", "", text)
    return bool(_COOKIE_CHOICE.search(text) or _SSO_CHROME.fullmatch(core))


def sso_sites(digest: apply_form.FormDigest) -> list[str]:
    """The sites a screen with no box to fill but tick boxes offers to sign
    in with (`THIRD_PARTY`), whatever else it holds; [] for a screen with a
    box to fill. The site's header never counts."""
    if any(f.type != "checkbox" for f in digest.fields):
        return []
    names: list[str] = []
    for b in digest.buttons:
        name = None if b.chrome or b.disabled else _sso_site(" ".join((b.text or "").split()))
        if name and name not in names:
            names.append(name)
    return names


def sso_only(digest: apply_form.FormDigest) -> list[str]:
    """The sites a screen offers to sign in with (`sso_sites`), when
    that is its only way on: beside them only known page chrome (help, a
    way back, a cancel, a close, a notice, a cookie banner's choice, "Learn
    more", "Contact us", "Email us", a language, "Forgot password?"). Any
    other control means []: an Apply, a Next, a sign-in or a sign-up of its
    own, a send, a control that may show one ("More options"), and one the
    run does not know ("Skip", "Get started", "Upload resume")."""
    names = sso_sites(digest)
    if not names:
        return []
    for b in digest.buttons:
        if b.chrome or b.disabled:
            continue
        text = " ".join((b.text or "").split())
        if _THIRD_PARTY.search(text):
            continue            # another site's control, a sign-in or not
        if _sso_chrome(text):
            continue
        if apply_judge.entry_worded(text) or apply_judge.ADVANCE_WORDS.search(text) \
                or _ACCOUNT_BUTTON.search(text) or apply_send_words.SEND_WORDS.search(text) \
                or _SSO_WAY_ON.search(text) or not _SSO_ASIDE.search(text):
            return []
    return names


def sso_fallback_sites(digest: apply_form.FormDigest) -> list[str]:
    """The sites a screen the account step could not pass offers to sign in
    with (`sso_sites`), when none of its other controls is a way on of its
    own: beside them only page chrome, the site's own sign-in or sign-up,
    and controls the run does not know ("Skip for now"). An Apply, a Next
    or a Continue, a send, and another way to sign in (`_SSO_WAY_ON`: an
    email, a phone, a code) mean []."""
    names = sso_sites(digest)
    if not names:
        return []
    for b in digest.buttons:
        if b.chrome or b.disabled:
            continue
        text = " ".join((b.text or "").split())
        if _THIRD_PARTY.search(text) or _sso_chrome(text):
            continue
        if apply_judge.entry_worded(text) or apply_judge.ADVANCE_WORDS.search(text) \
                or apply_send_words.SEND_WORDS.search(text) or _SSO_WAY_ON.search(text):
            return []
    return names


# an account screen's controls that are no way on for its step: a password
# reset, a resend, a cancel, a way back, help
_NOT_ACCOUNT_STEP = re.compile(r"\bforgot|\breset\b|\bresend\b|\bcancel\b|\bback\b|\bhelp\b"
                               r"|\btrouble\b", re.I)


def account_advance(digest: apply_form.FormDigest, plan: FillPlan, *,
                    signup: bool = False) -> tuple[int, float] | None:
    """The button an account screen's step clicks: the judged advance, else
    the judged submit, at `BUTTON_ADVANCE_MIN_CONF` or above, never one of
    the site's header (a header's "Sign In" on a sign-up screen), never a
    sign-in with another site ("Continue with Google"), and never one whose words name the other step while the screen
    has its own button that names this one and its password boxes say no
    other step than the read (`password_step`: a sign-up screen's "Already
    have an account? Sign In" beside its "Create Account"; a screen whose only way
    on says "Sign in" is a sign-in whatever it was read as; a sign-in read
    as a sign-up keeps its judged Sign In). With neither: the
    screen's own buttons whose words name the account step ("Create
    Account", "Sign in", "Continue"), one per text (Workday draws "Create
    Account" twice, a click filter over the real button); a sign-up takes the
    one that makes the account, a sign-in the one that signs in,
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
                or _THIRD_PARTY.search(b.text) or _NOT_AN_ACCOUNT.search(b.text):
            continue
        own.setdefault(" ".join(b.text.lower().split()), b)
    buttons = list(own.values())
    fitting = [b for b in buttons if fits.search(b.text)]
    text = {b.n: b.text for b in digest.buttons}
    for role in ("advance", "submit"):
        held = plan.buttons.get(role)
        if held is None or held[1] < apply_judge.BUTTON_ADVANCE_MIN_CONF \
                or _chrome(digest, held[0]) or _THIRD_PARTY.search(text.get(held[0], "")) \
                or _NOT_ACCOUNT_STEP.search(text.get(held[0], "")) \
                or _NOT_AN_ACCOUNT.search(text.get(held[0], "")):
            # never a header's button, a sign-in with another site, or a
            # control beside the step: a password reset (it mails a reset
            # link), a resend, a cancel, a way back, a sign-up for job
            # alerts, a newsletter or a talent network
            continue
        words = text.get(held[0], "")
        if step == read and fitting and other.search(words) and not fits.search(words):
            # the judge rated the screen's own "Sign In" the advance above
            # "Create Account", and the sign-up clicked it and landed on the
            # sign-in screen (seen on Workday)
            continue
        return held
    if len(buttons) > 1:
        buttons = fitting
    if len(buttons) == 1:
        return buttons[0].n, apply_judge.BUTTON_ADVANCE_MIN_CONF
    return None


_CODE_WAY_ON = re.compile(r"\b(verify|continue|next)\b", re.I)


def code_advance(digest: apply_form.FormDigest) -> int | None:
    """A code step's own way on when no button was judged one: its one
    button (none in the header, none disabled, none with another site,
    none that declines or sends) whose words verify or go on ("Verify",
    "Verify and continue", "Next"); None with none or several."""
    own = {" ".join(b.text.lower().split()): b.n for b in digest.buttons
           if not b.chrome and not b.disabled and _CODE_WAY_ON.search(b.text)
           and not _THIRD_PARTY.search(b.text) and not apply_judge.DECLINE_WORDS.search(b.text)
           and not _send_worded(b.text)}
    return next(iter(own.values())) if len(own) == 1 else None


# a sign-in or a profile from another site: never a form's way on
_THIRD_PARTY = apply_judge.THIRD_PARTY
