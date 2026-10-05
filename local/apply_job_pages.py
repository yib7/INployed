"""The page steps of a job's run: opening and reading a page, the busy and
loading waits, the consent banner, the LinkedIn and aggregator steps, the
account step and its park, the human check, the judged page, the job
posting and its entry click, the popups a click opens, and the guarded
click. `_PageSteps` is a base of `_JobRun`, and its methods run on a
`_JobRun`.

Split out of `apply_run`, which re-exports `_JobRun`.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping

import apply_account_flow
import apply_click
import apply_fill
import apply_form
import apply_judge
import apply_limits
import apply_linkedin
import apply_page
import apply_trace
from apply_judge import FillPlan
from apply_outcome import (ACCOUNT_PARK_REASONS, AGGREGATOR_NOTE, AGGREGATOR_REASON, _cap,
                           _closed_error, EASY_APPLY_NOTE, EASY_APPLY_REASON, FIELDS_MAX_REASON,
                           LINKEDIN_LOGIN_NOTE, LINKEDIN_RETURN_REASON, LOGIN_NOTE, MAILTO_NOTE,
                           MAILTO_REASON, _NotClicked, _Parked, SSO_NOTE, SSO_REASON)
from apply_sites import (_aggregator, company_site_control, _host, _is_captcha_url, link_targets,
                         _on_linkedin_redirector, _site, _tracking)
from apply_sendwatch import confirmation_words, LateWatch
from apply_page import (await_destination, mailto_address, open_page, _page_closed, _popups,
                        _settle_capped, _settled_ms, settled_words)
from apply_account_flow import _AsForm, password_step, sso_fallback_sites
from apply_route import (linkedin_view, _MAPPED_STATES, posting_context, posting_entry_choice,
                         unsure_step)
from apply_gate import live_refusal, submit_on


class _PageSteps:
    """`_JobRun`'s pages steps (see the module docstring)."""

    # -- reading a page ------------------------------------------------------------------

    def _open(self, url: str) -> None:
        """The first load (`open_page`), its decisions in the first page's
        trace."""
        for row in open_page(self.page, url):
            self._decide_next(**row)

    def _company(self) -> str:
        """The queue entry's company: the one name a routine consent's label
        may carry (`apply_judge.routine_consent`)."""
        return str((self.entry or {}).get("company") or "")

    def _busy(self) -> bool:
        """Whether a loading placeholder shows in the viewport (an `aria-busy`
        region, a skeleton: `apply_fill`'s readiness read)."""
        try:
            return bool(apply_click.ready_snapshot(self.page)[1])
        except Exception:       # noqa: BLE001  (a page double, a page mid-navigation)
            return False

    def _loading(self, digest: apply_form.FormDigest, busy: bool) -> bool:
        """A read with no field taken while a loading placeholder showed
        (`busy`: `_read_busy`'s look before or after the extract): a skeleton
        is no read of the page."""
        return busy and not digest.fields

    def _read_busy(self, look: bool) -> tuple[apply_form.FormDigest, bool]:
        """(the page's digest, whether a loading placeholder showed before or
        after it was read). With `look` off, no look. The look before the read
        catches a skeleton that clears between the read and a later look (a
        busy machine read the skeleton, the form came, and the look
        saw none); the look after it, when the read has no field, a skeleton
        painted between the first look and the read."""
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
        `EMPTY_READ_MAX_S` has passed (content arrives 0.3 to 1.7 s after
        `load` on SPA postings). A read that is only loading is read again
        every `EMPTY_READ_POLL_S` for at most `LOADING_WAIT_S`, once per step
        (an ad's or a widget's placeholder may never clear; the trace
        says when it stayed up). A step is its URL and the step before it
        (`last_sig`): a single-page wizard shows every step at one URL, each
        behind its own skeleton. The host is checked before every
        read again."""
        step = (str(self.page.url), self.last_sig)
        watch = step not in self._loading_waited
        digest, busy = self._read_busy(watch)
        loading = watch and self._loading(digest, busy)
        empty = apply_page._empty_read(digest)
        if not empty and not loading:
            return digest
        if loading:
            self._loading_waited.add(step)
        first = (f"{len(digest.fields)} field(s), {len(digest.buttons)} button(s), "
                 f"{len((digest.text or '').strip())} characters")
        what = "an empty read" if empty else "a loading placeholder"
        start = time.monotonic()
        last = json.dumps(digest.to_dict(), sort_keys=True)
        stable_since = start
        info = apply_fill.settle(self.page, apply_limits.CLICK_TIMEOUT_S) if empty else {"ms": 0}
        lingered = False
        while True:
            # the page may have moved on while it settled or between reads:
            # a page off the allowed sites is never read, let alone judged
            self._check_host(self.page.url)
            digest, busy = self._read_busy(loading)
            now = time.monotonic()
            empty = apply_page._empty_read(digest)
            loading = loading and self._loading(digest, busy)
            if loading and now - start >= apply_limits.LOADING_WAIT_S:
                # the placeholder had its one short wait: it no longer holds
                # the read (the empty-read rules still do)
                loading, lingered = False, True
            if not empty and not loading:
                break
            seen = json.dumps(digest.to_dict(), sort_keys=True)
            if seen != last:
                last, stable_since = seen, now
            if now - start >= apply_limits.EMPTY_READ_MAX_S or (
                    not loading and now - stable_since >= apply_limits.EMPTY_READ_STABLE_S):
                break
            self.page.wait_for_timeout(int(apply_limits.EMPTY_READ_POLL_S * 1000))
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
        further settle: a fresh extract and a fresh judge request (for
        a page read mid-render, an interstitial that clears itself)."""
        first, reads = f"{state} {conf:.2f}", self._reads(answers)
        info = apply_fill.settle(self.page, apply_limits.CLICK_TIMEOUT_S)
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
        read (on Teamtailor and bunq it took the Apply click): its
        reject, decline or necessary-only control, else its close; never an
        accept, allow or agree (`apply_form.consent_control`), and only in
        the page's own frames on the allowed sites, never a bot check's.
        Then the page settles and its host is checked again. At most
        `CONSENT_MAX` per job, so a banner that comes back cannot hold the
        run."""
        if self._consent_clicks >= apply_limits.CONSENT_MAX:
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
                self.page, lambda: loc.click(timeout=apply_click.ACTION_TIMEOUT_MS),
                timeout_s=apply_limits.CONSENT_WAIT_S)
        except Exception as e:      # noqa: BLE001  (the banner went away on its own)
            if _closed_error(e):
                raise
            error = type(e).__name__
            info = apply_fill.settle(self.page, apply_limits.CLICK_TIMEOUT_S)
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
            if dest is self.page and self._recover_error_page(self.page, transition=True):
                # Chrome's error page had its one retry: where the retry led
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
        view, waited = linkedin_view(self.page, wait_s=apply_limits.LINKEDIN_READY_S if kind == "job" else 0,
                                     job_title=str(self.entry.get("title") or ""),
                                     company=str(self.entry.get("company") or ""),
                                     clock=self.r.clock, deadline=self.deadline)
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
        if total > apply_limits.LINKEDIN_CLICKS_MAX:
            raise _Parked("needs_human", f"the offsite Apply ({control.label}) did not open the "
                                         f"company's site ({apply_limits.LINKEDIN_CLICKS_MAX} LinkedIn "
                                         f"Apply clicks in this job)")
        loc = self.page.main_frame.locator(control.css)
        self._click_entry(rec, loc, control.label, how="linkedin_handler")
        return True

    def _aggregator_step(self, digest: apply_form.FormDigest) -> bool:
        """A job board's posting: its company-site control
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
        said = password_step(digest)
        read = "signup" if state == "signup_form" else "signin"
        if said and said != read and not apply_account_flow.account_forms(self.page, digest):
            # the password boxes say the other step (a sign-up's two boxes or
            # its new-password box; a sign-in's current-password box, or its
            # one box beside "Forgot your password?"): the boxes decide, so a
            # misread never types the password into the other step's form
            self._decide("account_step_by_boxes", f"read as {state}; its password boxes say "
                                                  f"{said}", to=said)
            state = "signup_form" if said == "signup" else "login_wall"
        try:
            if state == "login_wall":
                if not self.accounts.login(self.page, digest, host):
                    raise self._account_park(digest, self._login_wall_reason(state, digest, host))
            elif not self.accounts.signup(self.page, digest, host):
                raise self._account_park(digest, f"account signup needed "
                                                 f"({self._account_evidence(state, digest)}"
                                                 f"{self._account_error()})")
        except _AsForm as form:
            self.log.info("job %s: the account screen carries the application; it is the "
                          "form", self.job_id)
            self.handed_off = True
            try:
                self._application_form(form.digest, form.answers, form.plan, self.pages[-1],
                                       completed=True)
            finally:
                self.handed_off = False

    def _account_error(self) -> str:
        """"; the account step failed: <type> at <step>" when the accounts
        hook's last step raised, else ""."""
        error = str(getattr(self.accounts, "last_error", "") or "")
        return f"; the account step failed: {error}" if error else ""

    def _login_wall_reason(self, state: str, digest: apply_form.FormDigest, host: str) -> str:
        """A sign-in the run could not pass: after the one sign-in the run
        tried without an account in the ledger, that no account on
        the site takes the master password; else the login wall and its
        evidence."""
        not_taken = getattr(self.accounts, "_not_taken", None)
        if not_taken is not None and _site(host) in getattr(self.accounts, "attempted", ()):
            return not_taken(_site(host), host, "login")
        return f"login wall ({self._account_evidence(state, digest)}{self._account_error()})"

    def _account_park(self, digest: apply_form.FormDigest, reason: str) -> _Parked:
        """The park for an account screen the run could not pass, `reason`:
        a "login wall" or "account signup needed" on a screen whose only way
        on is a sign-in with another site's account (`sso_fallback_sites`)
        parks as a dead end, with its reason and note, and the
        account step's own park kept in its words. A screen `sso_only` read
        as having another way on (a control it does not know) so still ends
        with the clear SSO reason. The main check's
        guards hold: never after the submit or a code step was clicked, nor
        on LinkedIn, and a CAPTCHA box or challenge
        waiting for the person keeps the account step's park, since it may
        be what held the screen. Only the park's words
        change: the run never clicks one of those sign-ins."""
        guarded = self.submit_clicked or self._code_sent or self._on_linkedin()
        sites = (sso_fallback_sites(digest)
                 if reason.startswith(ACCOUNT_PARK_REASONS) and not guarded else [])
        if not sites or self._human_check_showing(checkbox=True):
            return _Parked("needs_human", reason, LOGIN_NOTE)
        self._decide("sso_fallback", f"{_cap(reason, 200)}; the screen's only way on is a "
                                     f"sign-in with {', '.join(sites)}")
        return _Parked("needs_human", f"{SSO_REASON} ({', '.join(sites)}); the run never signs "
                                      "in with another site, and nothing else on the screen "
                                      f"took it on; the account step's park: {_cap(reason, 300)}",
                       SSO_NOTE)

    def _human_check_showing(self, *, checkbox: bool = False) -> bool:
        """Is a bot check waiting for the person on the page: a frame from a
        CAPTCHA provider that is visible and at least `HUMAN_CHECK_MIN_PX`
        tall (a challenge), or, with `checkbox`, a visible reCAPTCHA,
        hCaptcha or Turnstile checkbox (`size=normal`, whatever its
        height) whose response token is still empty
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
            if box and box["height"] >= apply_limits.HUMAN_CHECK_MIN_PX and box["y"] + box["height"] > 0:
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
        checkbox counts as the check (`_human_check_showing`). A caller
        with no `before` has just seen the challenge: one gone by now closed
        in between, and the check is done."""
        framed = self._human_check_showing(checkbox=checkbox)
        if not framed and before is None:
            self.last_sig = None
            self.log.info("job %s: the check closed before the wait; going on", self.job_id)
            apply_fill.settle(self.page, apply_limits.CLICK_TIMEOUT_S)
            return
        headless = bool(self.r.settings.get("auto_apply_headless"))
        clears_itself = (checkbox and framed and not self._human_check_showing()
                         and apply_form.unsolved_checkbox(self.page) == "turnstile")
        if headless and framed and not clears_itself:
            raise _Parked("needs_human", reason)
        limit = apply_limits.HUMAN_CHECK_AUTO_S if headless else apply_limits.HUMAN_CHECK_WAIT_S
        polls = int(limit / apply_limits.HUMAN_CHECK_POLL_S)
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
                                 "%d min)", self.job_id, reason, apply_limits.HUMAN_CHECK_WAIT_S // 60)
            self.r.sleep(apply_limits.HUMAN_CHECK_POLL_S)
        self.deadline += max(0.0, self.r.clock() - start)
        self.last_sig = None
        self.log.info("job %s: the check is done; going on", self.job_id)
        apply_fill.settle(self.page, apply_limits.CLICK_TIMEOUT_S)

    def _read(self, digest: apply_form.FormDigest) -> dict:
        """The page read: its own small request
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
        LinkedIn). A page with more than `apply_judge.FIELDS_MAX` fields
        parks: its mapping would cost the judge without end."""
        if state not in _MAPPED_STATES:
            return answers
        if len(digest.fields) > apply_judge.FIELDS_MAX:
            raise _Parked("needs_human", f"{FIELDS_MAX_REASON} (more than "
                                         f"{apply_judge.FIELDS_MAX})")
        with_fields = state != "job_posting" or bool(digest.fields)
        if with_fields and discover and not self._on_linkedin():
            self._discover_listbox_options(digest)
        # Sized to Jev's limits, split when a long form needs it
        requests = [(s, q) for s, q in apply_judge.page_requests(
            digest, self.catalog, self.entry, fields=with_fields) if q]
        sent = sum(len(s.get("buttons") or ()) for s, _ in requests)
        if len(requests) > 1 or (requests and sent < len(digest.buttons)):
            self._decide("mapping_sized", f"the mapping asked in {len(requests)} request(s) "
                                          f"with {sent} of {len(digest.buttons)} buttons, to "
                                          f"fit the judge's request limits",
                         fields=len(digest.fields))
        if requests:
            got = apply_judge.merge_answers([
                {k: v for k, v in self.r.jev.judge(s, q).items() if k in q}
                for s, q in requests])
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
            self._mask_sensitive(digest)
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
        park_mode = not submit_on(self.r.settings)
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
        # a control that cannot be read just before the click is never
        # clicked: what it would do is not known
        why = (live_refusal("apply_entry", text, live) if live
               else "its text could not be read just before the click")
        if why:
            self._refused_click("apply_entry", text, why)
        address = mailto_address(loc)
        if address:
            # The Apply opens an email to the employer; nothing to
            # click through (it would read as a page that did not advance)
            self._decide("mailto", f"the Apply ({_cap(text, 60)}) is an email address",
                         address=address)
            raise _Parked("needs_human", f"{MAILTO_REASON} to {address}", MAILTO_NOTE)
        rec["clicked"].append(f"{text} (apply_entry)")
        self._last_click = (text, "apply_entry")
        source_url = self.page.url
        popup, signal, waited = apply_page.click_entry(self.page, loc, on_popup=self._watch_popup)
        if popup is None:
            self._stop_late_watch()
            self._late_watch = LateWatch(self.page, signal, str(source_url))
            self._late_watch.start()
            dest, info = self._await_destination(self.page)
            self._trace("apply_entry", n=n, text=text, how=how, popup=False, signal=signal,
                        waited_ms=waited, destination=str(dest.url), **info)
            if dest is self.page and self._recover_error_page(self.page, transition=True):
                # Chrome's error page had its one retry: where the retry led
                dest, info = self._await_destination(self.page)
                self._trace("redirect", destination=str(dest.url), **info)
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
            popup.wait_for_load_state("domcontentloaded", timeout=apply_limits.CLICK_TIMEOUT_S * 1000)
        except Exception:       # noqa: BLE001
            pass
        self.log.info("job %s: Apply opened %s", self.job_id, popup.url)
        try:
            self._follow_popup(popup, source_url=source_url)
        finally:
            self._trace("apply_entry", n=n, text=text, how=how, popup=True, signal=signal,
                        waited_ms=waited, destination=str(self.page.url))

    def _watch_popup(self, popup) -> None:
        """The tab an Apply click opened, watched from its popup event: the
        address it opened at goes into the URL chain before the tab leaves it."""
        self.trace.nav(str(popup.url))
        self._watch(popup)

    def _follow_popup(self, popup, *, source_url: str | None = None) -> None:
        """Adopt the tab Apply opened once it has reached its destination:
        its host is admitted and checked after the redirects, never at the
        popup event, which on LinkedIn still shows the `/safety/go/` hop. A
        tab the safety interstitial's Continue opens is followed the same
        way."""
        source = source_url or self.page.url
        left = self.page
        watched: list = []

        def _watched(tab) -> None:
            if tab is not self.page and not any(tab is w for w in watched):
                self.trace.nav(str(getattr(tab, "url", "")))  # its first load came before the watch
                self._watch(tab)
                watched.append(tab)
        for _ in range(3):
            _watched(popup)
            dest, info = self._await_destination(popup)
            if info.get("trackers"):
                self._decide_next("tracker_hops", "waited out an ad tracker's hop to the "
                                                  "company's site", trackers=info["trackers"])
            if dest is not popup:
                popup = dest
                continue
            # Chrome's error page: its one retry, then the retry's destination
            if not self._recover_error_page(popup, transition=True):
                break
        _watched(popup)         # a third hop's tab is the job's too
        try:
            self._admit_ats_transition(popup.url, source)
            self._check_host(popup.url)
        except _Parked:
            # the park names the destination: that tab is the one kept open
            if popup is not left:
                self.page = popup
                self._leave(left, popup)
            raise
        self.page = popup
        self.last_sig = None
        self._leave(left, popup)

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
        `STEP_SETTLE_S` more and never made again; a
        dead advance parks with the button, its role and the judge's
        confidence.
        A click that opens a new tab, right away or a moment after
        the click (the tabs are watched until the retry, which waits
        `POPUP_GRACE_S` for one first), is followed and never
        clicked again. `refused_by_form`: read after the first
        click, changed or quiet; when it says the form refused the click
        (its validation messages), the click is never made again here: the
        caller repairs the fields first. A page that shows a loading
        indicator after the click is waited on (`BUSY_WAIT_S`)."""
        button = next((b for b in digest.buttons if b.n == n), None)
        text = button.text if button else f"button {n}"
        rec["clicked"].append(f"{text} ({role})")
        self._last_click = (text, role)
        timeout = max(1.0, min(apply_limits.CLICK_TIMEOUT_S, self.deadline - self.r.clock()))
        check = self._live_check(role)
        # a loading indicator already up before the click is the page's own
        # (an ad's placeholder that never clears): no wait for it after
        busy_before = role != "submit" and self._busy()
        with _popups(self.page) as opened:
            result = apply_fill.click(self.page, digest, n, timeout_s=timeout, check=check)
            if (role != "submit" and not opened and not result.changed
                    and not result.refused and result.clicked):
                # a tab that opens a moment after the click comes before any
                # second click
                self.page.wait_for_timeout(int(apply_limits.POPUP_GRACE_S * 1000))
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
            # The click opened its next page in a new tab and left this
            # one as it was: the tab is the next page, and nothing is clicked
            # again; a submit's tab is read only when it is the thank-you
            # (the page the submit was made on keeps its own evidence)
            if self._adopt_click_popup(opened[0], text, role):
                return apply_fill.ClickResult(clicked=True, changed=True, late=result.late)
        if role == "submit":
            if result.clicked and not result.changed:
                self.log.info("job %s: the submit click changed nothing; waiting up to %s s",
                              self.job_id, apply_limits.SUBMIT_SETTLE_S)
                changed = apply_fill.wait_for_change(self.page, timeout_s=apply_limits.SUBMIT_SETTLE_S)
                self._trace("submit_settle", changed=changed, waited_s=apply_limits.SUBMIT_SETTLE_S)
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
            # The click reached the page and set a
            # request going; a second click would make it twice (a step saved
            # twice, a send made twice). The page is waited for, never
            # clicked again.
            self.log.info("job %s: the %s click set %s going; waiting up to %s s for the page",
                          self.job_id, role, went[0], apply_limits.STEP_SETTLE_S)
            changed = apply_fill.wait_for_change(self.page, timeout_s=apply_limits.STEP_SETTLE_S)
            self._trace("step_settle", changed=changed, waited_s=apply_limits.STEP_SETTLE_S, sent=went[:3])
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
            self._refused_click(role, text, result.refused, retry=True)
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
        """After a click that changed the page, a loading indicator
        still in view (`aria-busy`, a skeleton: `apply_click.ready_snapshot`)
        is waited on, up to `BUSY_WAIT_S` (a slow Workday or Taleo step can
        take 30 s), then the page settles; the trace says how long."""
        start = time.monotonic()
        while time.monotonic() - start < apply_limits.BUSY_WAIT_S:
            try:
                if not apply_click.ready_snapshot(self.page)[1]:
                    break
            except Exception:       # noqa: BLE001  (a page double, a page mid-navigation)
                break
            self.page.wait_for_timeout(int(apply_limits.BUSY_POLL_S * 1000))
        waited = time.monotonic() - start
        if waited >= apply_limits.BUSY_POLL_S:
            info = apply_fill.settle(self.page, apply_limits.CLICK_TIMEOUT_S)
            self._decide("busy_after_click", f"a loading indicator showed after {_cap(text, 40)}; "
                                             f"waited {waited:.1f} s, then "
                                             + settled_words(info),
                         waited_s=round(waited, 1), capped=waited >= apply_limits.BUSY_WAIT_S)

    def _adopt_click_popup(self, popup, text: str, role: str) -> bool:
        """A click that opened a new tab: True when the tab is now
        the page. An advance's tab is the next page, followed like an Apply
        entry's (`_follow_popup`: its host admitted and checked). A submit's
        tab is the page the post-submit read reads (`_after_submit`, whose
        host check is its own) only when it shows received words; any other
        tab (a help page, an answer that says nothing, a browser error page)
        leaves the page the submit was made on, and what left, to decide."""
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=apply_limits.CLICK_TIMEOUT_S * 1000)
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

    def _refused_click(self, role: str, text: str, why: str, *, retry: bool = False) -> None:
        """A click the live check stopped: the job waits for the
        person with the control's text then and now. On the first click
        nothing was clicked (`_NotClicked`: a step's possible-send mark is
        taken back). On the retry the first click had landed and changed
        nothing, so a mark set before it stays."""
        self._decide("live_refused", f"the {role} click on {text!r} was refused: {why}",
                     retry=retry)
        if retry:
            raise _Parked("needs_human", f"the {role} button ({_cap(text, 60)}) changed before "
                                         f"its second click: {why}; the first click landed and "
                                         f"changed nothing")
        raise _NotClicked("needs_human", f"the {role} button ({_cap(text, 60)}) changed before "
                                         f"the click: {why}; nothing was clicked")
