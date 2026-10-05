"""The submit steps of a job's run: the gate read and the final step
checks, the submit gate, what the page shows after the submit click and
whether it was sent, the code gate and the one-time code, and the
verification link with its sender check. `_SubmitSteps` is a base of
`_JobRun`, and its methods run on a `_JobRun`.

Split out of `apply_run`, which re-exports `_JobRun`.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

import apply_click
import apply_fill
import apply_form
import apply_inbox
import apply_judge
import apply_limits
import apply_linkedin
import apply_queue
import apply_send_words
import apply_sendwatch
import apply_verify
import ats_accounts
from apply_judge import FillPlan, VerifyResult
from apply_outcome import (_cap, CHECK_SENT_NOTE, CHECK_SENT_REASON, CHECKBOX_NOTE, _closed_error,
                           CODE_NOTE, LINK_BOT_NOTE, LINK_BOT_WORDS, LINK_FAILED_WORDS,
                           LINK_HELD_NOTE, LINK_MOVES_MAX, LINK_NOTE, LINK_REASON,
                           LINK_SUBMIT_NOTE, LINK_USED_NOTE, NOT_SENT_REASON, _NotClicked, _Parked,
                           _Refused, REVIEW_NOTE, _SentSeen, SUBMIT_FAILED_NOTE, _Unsent)
from apply_send_words import _ACCOUNT_STEP_WORDS, _sends_application, step_only
from apply_sites import (AGGREGATOR_SITES, ATS_SITES, _host, _IDENTITY_SITES,
                         _INBOX_PROVIDER_SITES, _link_challenge, _link_check_text, LINKEDIN_HOSTS,
                         _platform, _sender_site, _site, TRACKER_SITES)
from apply_sendwatch import ACTION_READ_MS, new_confirmation
from apply_page import _button_text, _code_field, _fields_sig, _settled_ms
from apply_account_flow import code_advance, _credential_form, _FORM_ACTION_JS, _page_words
from apply_route import _LINK_REMAPS, _OPENED_BY_ACCOUNT
from apply_gate import _ACCOUNT_OWN_WORDS, can_submit, _control_words, _invalid_words, submit_on
from apply_record import HIDDEN


class _SubmitSteps:
    """`_JobRun`'s submit steps (see the module docstring)."""

    # -- the submit path ----------------------------------------------------------------------

    def _gate_read(self, digest: apply_form.FormDigest, plan: FillPlan) -> dict[str, Any]:
        """The page as the gate reads it just before the submit (`can_submit`'s
        `live`): whether an application is on it (a field
        filled on this page, or the application filled on an earlier one),
        whether an Apply-worded submit is the form's own sending button
        (`_apply_button_why`), whether a submit in submit mode names only a
        step (`step_only`), the submit's form's validity
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
        account = self.handed_off \
            or any(pf.action == apply_judge.PASSWORD_ACTION for pf in plan.fields)
        if submit_on(self.r.settings) and step_only(button.text) \
                and not (account and _ACCOUNT_OWN_WORDS.search(button.text)):
            # In submit mode a submit whose
            # words are only a step's is never clicked as the send. An
            # account screen that carries the application (its password
            # typed here, or handed back by the account step) keeps its
            # account button ("Create account", "Sign in") as the send, and
            # only that one: its "Next" is a step as anywhere
            out["step_button"] = (f"the submit button ({_cap(button.text, 60)}) holds only a "
                                  f"step's words; it is never clicked as the send")
        out.update(self._form_live(button))
        return out

    def _form_live(self, button: apply_form.Button) -> dict[str, Any]:
        """The checks of `button`'s form the gate reads just before a send:
        `invalid` (a control that would not validate, or one marked
        `aria-invalid`, `apply_form.validity_report`) and `required_empty`
        (a required control the extractor leaves out, still empty,
        `apply_form.control_scan`). A page that cannot answer them gives
        `unreadable`, which fails the gate: a form never reads as valid
        because it could not be read."""
        try:
            invalid = apply_form.validity_report(self.page, button.locator,
                                                 self._filled_here)["invalid"]
            empty = [r for r in apply_form.control_scan(self.page, [int(button.locator[0])],
                                                        required_only=True)
                     if r.get("required") and r.get("empty")]
        except Exception as e:      # noqa: BLE001  (a page mid-navigation, a closed frame)
            self._trace("error", step="gate_read", error=type(e).__name__)
            return {"unreadable": (f"the form's validity could not be read before "
                                   f"{_cap(button.text, 60)} ({type(e).__name__})")}
        return {"invalid": invalid, "required_empty": empty}

    def _final_step_checks(self, digest: apply_form.FormDigest, plan: FillPlan,
                           verification: list[VerifyResult], rec: dict, n: int
                           ) -> tuple[apply_form.FormDigest, FillPlan, list[VerifyResult], int]:
        """The gate's live checks before a final-worded advance ("Confirm",
        "Complete", "Done") in submit mode once the application is filled,
        since that click may be the send: a control that would not validate
        is repaired as at the gate (`REPAIR_ROUNDS` in all), and one still
        invalid, a required control still empty, a form that cannot be read
        or an unticked CAPTCHA checkbox parks the job before the click.
        Returns the page and the button's number as they stand after any
        repair."""
        text = _button_text(digest, n)
        while True:
            button = next((b for b in digest.buttons if b.n == n), None)
            if button is None:
                return digest, plan, verification, n
            live = self._form_live(button)
            invalid = live.get("invalid") or []
            if not invalid or self._gate_repairs >= apply_limits.REPAIR_ROUNDS:
                break
            self._gate_repairs += 1
            who = self._button_identity(digest, n)
            problems = [{**r, "text": r.get("message") or "", "kind": "invalid"}
                        for r in invalid]
            digest, plan, verification = self._repair(
                digest, plan, verification, problems, rec,
                why=f"read before the {_cap(text, 40)} step")
            if not self._repaired:
                break
            same = self._same_button(digest, who)
            if same is None:
                raise self._button_lost(who)
            n = same
        why = live.get("unreadable") or ""
        for row in invalid:
            why = why or _invalid_words(row)
        for row in live.get("required_empty") or []:
            why = why or _control_words(row)
        if not why and self._human_check_showing(checkbox=True):
            why = "a CAPTCHA checkbox on the page is unticked: tick it, then Re-queue"
        if why:
            self._decide("final_step_refused", f"the {_cap(text, 40)} step may send the "
                                               f"application and the gate's checks refuse it: "
                                               f"{why}")
            raise _Parked("needs_human", f"{why} (before the {_cap(text, 40)} step, which may "
                                         f"send the application; nothing was clicked)")
        return digest, plan, verification, n

    def _apply_button_why(self, digest: apply_form.FormDigest, button: apply_form.Button) -> str:
        """"" when an Apply-worded button is the submit: it sits in
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
        (`SendWatch`); a CAPTCHA checkbox still unticked waits for the
        person, and a page that moved on during that wait (the person
        sent it) is read as after a submit; a click that dispatched and then
        timed out on its navigation stays clicked; the page after
        it is read by `_after_submit`."""
        self._no_form_on_linkedin("the submit gate")
        live = self._gate_read(digest, plan)
        if live.get("invalid") and not live.get("no_application") \
                and self._gate_repairs < apply_limits.REPAIR_ROUNDS and plan.buttons.get("submit"):
            # The form reports a control that would not validate:
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
        watch = apply_sendwatch.SendWatch(self, self.page, frame)
        self._send_watch = watch
        watch.start()
        if self._human_check_showing(checkbox=True):
            # the person ticks it; the run never does
            self._decide("gate_captcha", "a CAPTCHA check is on the page before the submit")
            try:
                self._wait_for_human_check("a CAPTCHA check is on the form before the submit",
                                           checkbox=True)
            except _Parked as p:
                if not watch.any():
                    raise
                # a request left while the run waited: the person may have sent
                # it; the job never reads as unsent
                self.submit_clicked = True
                raise self._send_evidence(p, watch, when="during the wait") from None
            if watch.any() or self._moved_during_wait():
                # the person may have sent it while the run waited
                self.submit_clicked = True
                self._decide("gate_moved", "the page moved on while the run waited for the "
                                           "CAPTCHA check; it is read as after a submit",
                             sent=watch.first())
                rec["clicked"].append("the page moved on during the CAPTCHA wait")
                try:
                    self._after_submit(handoff=self.handed_off, during_wait=True)
                except _Refused as refused:
                    # the form refused it as typed and nothing left: the
                    # run made no click to repair after, so the refusal parks
                    raise refused.park from None
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
                                           or apply_send_words.SIGN_IN_WORDS.search(text)))
        try:
            self._after_submit(account=account, handoff=self.handed_off)
        except _Refused as refused:
            # The form refused the send as typed and nothing left the
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
            # same words
            n = self._same_button(digest, who)
            if n is None:
                raise self._button_lost(who)
            plan.buttons["submit"] = (n, submit[1])
            rec["clicked"].append("the form refused the submit; repaired")
            self._submit_gate(digest, plan, verification, rec)

    def _moved_during_wait(self) -> bool:
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
        URL, its form, its visible text and error texts, and the address the
        submit button's form sends to (`_FORM_ACTION_JS`, "" for a button in
        no form: the GET that carries a send)."""
        try:
            text = apply_fill.page_text(self.page)
        except Exception:       # noqa: BLE001  (a page double)
            text = ""
        action = ""
        if self._submit_at:
            try:
                action = str(apply_form.resolve(self.page, self._submit_at).first
                             .evaluate(_FORM_ACTION_JS, timeout=ACTION_READ_MS) or "")
            except Exception:       # noqa: BLE001  (a page double, a button gone)
                action = ""
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
                "errors": errors, "values": values, "action": action}

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
        none."""
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
        """Read what the submit click did before deciding, again
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
          (`submit_clicked` reset; the repair loop handles it);
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
        watch = self._send_watch or apply_sendwatch.SendWatch(self, self.page)
        before = self._before_submit or {}
        self._sent_when = "during the CAPTCHA wait" if during_wait else "after the submit click"
        try:
            self._read_after_submit(watch, before, account, handoff)
        except _Parked as p:
            if p.status != "needs_human" or isinstance(p, _Unsent):
                raise
            raise self._send_evidence(p, watch, when="during the wait" if during_wait
                                      else "after the click") from None
        finally:
            watch.stop()

    @staticmethod
    def _send_evidence(p: _Parked, watch: apply_sendwatch.SendWatch, when: str = "after the click") -> _Parked:
        """A park after the submit click (or a send the person made while the
        run waited): the "check whether" note, and the request that left
        named in the reason when one did."""
        reason = p.reason
        first = watch.first()
        if first and first not in reason:
            reason = f"{reason}; a request left {when}: {_cap(first, 120)}"
        return _Parked("needs_human", reason, CHECK_SENT_NOTE)

    def _read_after_submit(self, watch: apply_sendwatch.SendWatch, before: Mapping[str, Any], account: bool,
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
            if seen != judged and (judged is None or held) and reads < apply_limits.POST_SUBMIT_READS:
                # the judge reads the page once it holds between two looks,
                # and at most `POST_SUBMIT_READS` times
                reads += 1
                answers = self._judge_page(digest)
                state, conf = apply_judge.read_page_state(answers)
                rec = self._new_page_record(state, conf, digest=digest, answers=answers)
                judged = seen
                self.log.info("job %s after submit: %s (%.2f)", self.job_id, state, conf)
                said = apply_judge.link_sent(digest)
                if state in _LINK_REMAPS and said:
                    # after the submit too: a page with no box that
                    # says a link was emailed, and no received words, waits
                    # for that link, a confirmation read included
                    self._decide("remap", f"read as {state} ({conf:.2f}) after the submit "
                                          f"click; the page says {said!r} and has no box to "
                                          "fill: the emailed link", to="code_gate")
                    state = "code_gate"
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
                # reset form, a flash or a bare alert says nothing
                invalid = [r for r in invalid if r.get("reason") == "aria-invalid"]
                field_errors = [e for e in field_errors if e.get("tied")]
                may_refuse = same_form and self._holds_typed(before)
            if digest.fields and (invalid or field_errors) and may_refuse:
                self._not_sent(invalid, field_errors, watch, [f.label for f in digest.fields])
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
                                             f"click ({_page_words(banners[0]['text'], 160)}); "
                                             f"{CHECK_SENT_REASON}")
            if state == "error_or_dead" and sure and not watch.pending:
                raise _Parked("needs_human", f"an error page after the submit click "
                                             f"({conf:.2f}; {_cap(digest.title, 80)}); "
                                             f"{CHECK_SENT_REASON}")
            busy = bool(watch.pending) or now - changed_at < apply_limits.POST_SUBMIT_QUIET_S
            if in_flight and not watch.pending and not late_look:
                # The answer came during this look; the page
                # is read again (a confirmation that came with it) before any
                # ruling, the quiet window counted from the answer; past the
                # wait's end, once more only
                answers_seen += 1
                if answers_seen == 1:
                    self._decide("after_submit", "a request's answer came during the look; "
                                                 "the page is read once more")
                changed_at = time.monotonic()
                late_look = now - start >= apply_limits.POST_SUBMIT_WAIT_S
                continue
            if not busy or now - start >= apply_limits.POST_SUBMIT_WAIT_S:
                break
            self.page.wait_for_timeout(int(apply_limits.POST_SUBMIT_POLL_S * 1000))
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
                   watch: apply_sendwatch.SendWatch, code_entered: bool, *, judged: bool) -> None:
        """The confirmation test of a post-submit look: received words new
        since the click (`marker`), or the judge's confirmation of this very
        page (`judged`) at `CONFIRMATION_MIN_CONF` with no form field and no
        send button. Raises `submitted`."""
        send_button = any(apply_send_words.SEND_WORDS.search(b.text) for b in digest.buttons)
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

    def _not_sent(self, invalid: list, field_errors: list, watch: apply_sendwatch.SendWatch,
                  labels: Iterable[object] = ()) -> None:
        """Validation errors after the submit click: the form refused the
        send, so nothing went through (`submit_clicked` is reset); the job
        waits for the person with the messages and the fields (a message's
        quote of one of the page's field `labels` kept). When nothing at all
        left the page (`SendWatch.any`), the form refused the send as typed:
        the gate repairs it once (`_Refused`)."""
        self.submit_clicked = False
        labels = list(labels)
        rows = [_invalid_words(r) for r in invalid[:3]]
        rows += [f"the form says: {_page_words(e['text'], 100, labels)}"
                 for e in field_errors[:2]]
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
                      watch: apply_sendwatch.SendWatch, before: Mapping[str, Any], handoff: bool) -> None:
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
                apply_send_words.SEND_WORDS.search(b.text) for b in digest.buttons):
            raise _Parked("needs_human", f"{CHECK_SENT_REASON}: a request left {when} ({first}) "
                                         f"and the page is a form with its own send button "
                                         f"({read})")
        raise _SentSeen("submitted", f"submitted (unconfirmed): a request left {when} ({first}); "
                                     f"the page after reads as {read}")

    @staticmethod
    def _maybe_only_the_account(state: str, conf: float) -> _Parked:
        return _Parked("needs_human", f"after the account page's submit the page reads as "
                                      f"{state} ({conf:.2f}): the click may only have made "
                                      f"the account; check whether the application went "
                                      f"through, then Re-queue or Mark applied")

    def _post_submit_digest(self, watch: apply_sendwatch.SendWatch | None = None) -> apply_form.FormDigest:
        """Validate a post-submit destination before reading or acting on it.

        A page that left the allowed sites after the submit click is read no
        further: "submitted (unconfirmed)" when a request to the
        application's sites left first, else the person checks. Either way
        the queue never sends it again. A send that never reached the site
        (`_Unsent`) stands as raised."""
        try:
            self._check_host(self.page.url)
            return self._drop_foreign_controls(self._extract())
        except _Unsent:
            raise
        except _Parked as p:
            if watch is not None and watch.sent:
                raise _SentSeen("submitted", f"submitted (unconfirmed): {p.reason} (after "
                                             f"{_cap(watch.first(), 120)})") from None
            raise _Parked("needs_human", f"{CHECK_SENT_REASON}: after the submit click "
                                         f"{p.reason}", CHECK_SENT_NOTE) from None

    def _code_gate(self, digest: apply_form.FormDigest, plan: FillPlan, rec: dict) -> None:
        self._no_form_on_linkedin("the code step")
        site = digest.url_host or _host(self.page.url)
        target = _code_field(digest.fields)
        if target is None and apply_judge.link_sent(digest):
            # The account check is a link in the email
            self._verify_link(digest, rec)
            return
        code = self.inbox.fetch_code(self.page, site, str(self.r.run_context().get("inbox_url") or ""))
        if not code:
            raise _Parked("needs_human", "emailed code needed", CODE_NOTE)
        if target is None:
            raise _Parked("needs_human", "code gate without a code box", CODE_NOTE)
        self._keep_secret_box(target.locator)     # masked from here on, typed or not
        if target.widget == "otp":
            for css in target.option_locators:
                self._keep_secret_box((int(target.locator[0]), str(css)))
        try:
            if target.widget == "otp":
                self._fill_otp(target, str(code))
            else:
                apply_form.resolve(self.page, target.locator).first.fill(str(code), timeout=5_000)
        except _Parked:
            raise
        except Exception as e:  # noqa: BLE001  (a fill exception may carry the private code)
            self._trace("error", step="code_gate.fill", error=type(e).__name__)
            raise _Parked("needs_human", "emailed code could not be filled", CODE_NOTE) from None
        self._trace("code", box=target.label, boxes=len(target.option_locators) or 1)
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
        elif (own := code_advance(digest)) is not None:
            # no button judged the way on: the step's one own button that
            # says it verifies or goes on ("Verify", "Continue")
            self._decide("code_advance", f"no button was judged the way on; the code step's "
                                         f"own {_cap(_button_text(digest, own), 40)!r} is")
            role, button, minimum = ("advance", (own, apply_judge.BUTTON_ADVANCE_MIN_CONF),
                                     apply_judge.BUTTON_ADVANCE_MIN_CONF)
        else:
            raise _Parked("needs_human", "code entered; no button to continue", CODE_NOTE)
        text = _cap(_button_text(digest, button[0]), 60)
        if not submit_on(self.r.settings) and self.form_filled \
                and not self.submit_clicked:
            # The application's
            # answers are on the site, so the code may finish a send the site
            # held for it, whatever role the judge gave the button. Park mode
            # ends here as its submit end: the code typed, the button left
            # for the person
            self._decide("code_step_park", f"the code is entered; its {text!r} ({role}, "
                                           f"{button[1]:.2f}) may send the application, and "
                                           "park mode clicks no send",
                         button=button[0], confidence=button[1])
            raise _Parked("ready_to_submit", f"auto_apply_submit is off; the emailed code is "
                                             f"entered and its button ({text}) is the step that "
                                             f"may send the application", REVIEW_NOTE)
        if button[1] < minimum:
            raise _Parked("needs_human",
                          f"code entered; {role} button confidence {button[1]:.2f} "
                          f"below {minimum:.2f}", CODE_NOTE)
        if not self.submit_clicked and _sends_application(digest, button[0],
                                                          account_only=False):
            # before the submit gate has let the application go, a code box
            # beside a "Submit application", a "Confirm" or a "Finish" is the
            # form's last step
            raise _Parked("needs_human", f"code entered; its button "
                                         f"({_cap(_button_text(digest, button[0]), 60)}) would "
                                         f"send the application", CODE_NOTE)
        if role == "submit" and not self.submit_clicked:
            # the submit gate is the only send: a code step's
            # button read as the submit before the gate has let the
            # application go never marks the job clicked, so a "verified"
            # page after it is confirmed by received words alone
            # (`confirmation_step`'s code_sent rule)
            if not self.form_filled:
                # none of the application is on the site yet: the button is
                # a step control of the account's check (an email "Verify")
                self._decide("code_step_control", f"the code step's {text!r} was read as the "
                                                  f"submit ({button[1]:.2f}) before any of the "
                                                  "application went on a page; it is clicked "
                                                  "as a step control",
                             button=button[0], confidence=button[1])
                role = "advance"
            else:
                # it may send what the site held for the code: clicked once,
                # never twice (the submit role's click), and the job is never
                # handed back (`_code_may_send`)
                self._decide("code_step_may_send", f"the code step's {text!r} was read as the "
                                                   f"submit ({button[1]:.2f}) after the "
                                                   "application's answers went on a page; it is "
                                                   "clicked once, and only received words "
                                                   "confirm a send after it",
                             button=button[0], confidence=button[1])
        # marked before the click: a click that lands and
        # then raises (the tab closed under it) leaves the job a possible
        # send, never taken over (`_take_over`) or handed back to the queue.
        # The site may have held the application for this code; an account's
        # own code, before the application's answers went on a page, sends
        # none of it
        code_sent, may_send = self._code_sent, self._code_may_send
        self._code_sent = True          # a code can finish a send the site held back
        self._code_may_send = may_send or bool(self.submit_clicked or self.form_filled)
        try:
            result = self._click(digest, button[0], role, rec, conf=button[1])
        except _NotClicked:
            # an advance the live check stopped: nothing went
            self._code_sent, self._code_may_send = code_sent, may_send
            raise
        if result.refused or not (result.clicked or result.late):
            # the button changed before the click, or the click never landed:
            # nothing went
            self._code_sent, self._code_may_send = code_sent, may_send
        if result.refused:
            raise _Parked("needs_human", f"code entered; its button ({text}) changed before "
                                         f"the click: {result.refused}", CODE_NOTE)

    def _fill_otp(self, target: apply_form.Field, code: str) -> None:
        """A code in one-character boxes (`widget` "otp"): typed from
        the first box (`apply_verify.fill_code`: a click, then key by key, so
        a widget that moves the focus on takes each character), read back as
        the boxes joined, and when that is not the code, put in box by box.
        The read-back is only compared with the code, never logged or
        recorded."""
        frame = apply_form.frames(self.page)[int(target.locator[0])]
        boxes = [frame.locator(css).first for css in target.option_locators]
        if len(code) != len(boxes):
            raise _Parked("needs_human", f"emailed code could not be filled (the code has "
                                         f"{len(code)} characters and the page {len(boxes)} "
                                         f"boxes)", CODE_NOTE)

        def _holds() -> bool:
            joined = "".join(b.input_value(timeout=apply_click.ACTION_TIMEOUT_MS) for b in boxes)
            return joined == code
        apply_verify.fill_code(self.page, boxes[0], code)
        if _holds():
            return
        self._decide("otp_box_by_box", "the code typed from the first box did not fill the "
                                       "boxes; each box takes its own character")
        for box, char in zip(boxes, code):
            box.fill(char, timeout=apply_click.ACTION_TIMEOUT_MS)
        if not _holds():
            raise _Parked("needs_human", "emailed code could not be filled (the boxes did not "
                                         "keep it)", CODE_NOTE)

    def _job_ats_sites(self) -> set[str]:
        """The ATS sites the job's application runs on: the platform of each
        of its own ATS hosts, and of the system its queue entry names."""
        sites: set[str] = set()
        for h in self._pinned_hosts():
            sites |= _platform(_site(h))
        system = str((self.entry.get("ats") or {}).get("system") or "").strip().lower()
        for site, name in apply_queue.ATS_FAMILY_SITES.items():
            if name == system and system != "linkedin":
                sites |= _platform(site)
        return sites

    def _sender_refused(self, sender: str) -> bool:
        """Is a listed message's sender one the job's code or link never
        comes from, whatever the judge answers? LinkedIn (by its address, or
        by its name on a row that shows a name alone); the inbox provider;
        an identity provider (`_IDENTITY_SITES`) unless it is the job's own
        site; another ATS than the job's when the job's is known
        (`_job_ats_sites`). A page that set off another site's code (a
        LinkedIn sign-in, a Workday account of another company) never gets
        it typed in."""
        site = _sender_site(sender)
        if not site:
            return bool(re.search(r"\blinkedin\b", str(sender or ""), re.I))
        if site == _site(LINKEDIN_HOSTS[0]):
            return True
        inbox_url = str(self.r.run_context().get("inbox_url") or "")
        provider = apply_inbox.provider_for(inbox_url) or ""
        if site in _INBOX_PROVIDER_SITES.get(provider, ()) or site == _site(inbox_url):
            return True
        own = {_site(h) for h in self.ats_hosts}
        page_host = _host(str(getattr(self.page, "url", "") or ""))
        if page_host:
            own.add(_site(page_host))
        if site in _IDENTITY_SITES and site not in own:
            return True
        if site in ATS_SITES and site not in own:
            mine = self._job_ats_sites()
            return bool(mine) and site not in mine
        return False

    def _link_ok(self, host: str) -> bool:
        """May a verification link from the inbox be opened? Only
        on the application's own site (an admitted ATS host's or the job's
        page's) or a known ATS platform (`ATS_SITES`), and once the job's own
        account on a platform is known only that account's hosts
        (`_tenant_departure`, `ats_accounts.tenant_key`); never on
        LinkedIn, a job board, a tracker, or the inbox provider's site
        (unless the job's page itself is served from the inbox's host)."""
        host = _host(host)
        site = _site(host)
        if not site or apply_linkedin.is_linkedin(host) or site in TRACKER_SITES \
                or site in AGGREGATOR_SITES:
            return False
        page_host = _host(str(getattr(self.page, "url", "") or ""))
        inbox_host = _host(str(self.r.run_context().get("inbox_url") or ""))
        if inbox_host and site == _site(inbox_host) and _site(page_host) != site:
            return False
        own = {_site(h) for h in self.ats_hosts} | ({_site(page_host)} if page_host else set())
        if site not in own and site not in ATS_SITES:
            return False
        if self._tenant_departure(host):
            # another company's account, or another platform, than the job's
            return False
        tenant = ats_accounts.tenant_key(host)
        ours = {ats_accounts.tenant_key(h) for h in [*self.ats_hosts, page_host]
                if _site(h) == site} - {""}
        return not (tenant and ours and tenant not in ours)

    def _verify_link(self, digest: apply_form.FormDigest, rec: dict) -> None:
        """A page that says a verification link was emailed. The
        link comes from the inbox (`_Inbox.fetch_link`: the site's message,
        a link on an allowed host, `_link_ok`), opens in a tab of its own,
        guarded onto the allowed hosts, and closes once it settled; the
        job's tab is then loaded again from its own URL by a GET (never a
        reload, which sends again the POST the page came from) and the
        loop reads what the site shows now (a sign-in, the
        application). A hash-routed page's URL is loaded without its
        fragment first: a load of the same URL with one moves inside the
        document and loads nothing. The fragment then goes back on as a move
        inside the new document. Once per site: a
        second link page after the link was followed parks. A link on any
        other host is never opened, and the park names its host.

        Once the application's answers are on the site,
        the link may be the step that sends it. Park mode ends before the
        inbox is read, as its submit end. Submit mode marks the job a
        possible send before the inbox is read (`_link_may_send`), and every
        park from there on carries the check-sent reason and no Re-queue
        advice (`_link_park`). After the link opens
        the job's tab is never loaded again: the link's own page is read,
        its received words end the job submitted, and anything else asks
        the person to check."""
        host = digest.url_host or _host(self.page.url)
        site = _site(host)
        if not submit_on(self.r.settings) and self.form_filled \
                and not self.submit_clicked:
            self._decide("link_step_park", f"the page on {host} says a link was emailed after "
                                           "the application's answers went on the site; the "
                                           "link may send the application, and park mode opens "
                                           "no send")
            raise _Parked("ready_to_submit", f"auto_apply_submit is off; the emailed link from "
                                             f"{host} is the step that may send the application; "
                                             f"it was not opened", LINK_SUBMIT_NOTE)
        if self.form_filled or self.submit_clicked or self._maybe_sent():
            # the site may hold the application for this link: from here on
            # the job is never taken over or handed back to the queue, and no
            # park asks for a Re-queue
            self._link_may_send = True
        if site in self._links_followed:
            raise self._link_park(f"the emailed link was opened and {host} still asks for it",
                                  LINK_NOTE, used=False)
        fetch = getattr(self.inbox, "fetch_link", None)
        inbox_url = str(self.r.run_context().get("inbox_url") or "")
        link = fetch(self.page, host, inbox_url) if fetch is not None else None
        if not link:
            refused = list(getattr(self.inbox, "refused", None) or [])
            if refused:
                raise self._link_park(f"the email's link goes to {refused[0]}, outside the "
                                      "application's sites; it was never opened", LINK_NOTE,
                                      used=False)
            raise self._link_park(f"no verification link from {host} in the inbox", LINK_NOTE,
                                  used=False)
        to = _host(link)
        if not self._link_ok(to):
            raise self._link_park(f"the email's link goes to {to}, outside the application's "
                                  "sites; it was never opened", LINK_NOTE, used=False)
        shown = self._open_link(link)
        self._links_followed.add(site)
        rec["clicked"].append("the emailed verification link (opened in a tab of its own)")
        if self._maybe_sent():
            # the job's tab is the answer to what may have sent the
            # application: loading it again would send that again
            marker = new_confirmation("", shown)
            self._decide("verify_link", f"the emailed link on {to} was opened in a tab of its own "
                                        f"after a possible send; its page is read, and the "
                                        f"job's tab is not loaded again", host=to,
                         shown=_cap(shown, 120), received=marker)
            if marker:
                raise _Parked("submitted", f"submitted (unconfirmed): the emailed link's page on "
                                           f"{to} says {marker!r}; the job's tab was not loaded "
                                           f"again after the send")
            raise _Parked("needs_human", f"{CHECK_SENT_REASON}: the emailed link on {to} was "
                                         f"opened after the application's answers went on the "
                                         f"site, and its page shows no received words; the "
                                         f"job's tab was not loaded again", CHECK_SENT_NOTE)
        self._decide("verify_link", f"the account check's link in the email, on {to}, was "
                                    f"opened in a tab of its own and closed; the job's tab is "
                                    f"loaded again from its URL", host=to, shown=_cap(shown, 120))
        url = str(self.page.url)
        bare = url.split("#", 1)[0]
        try:
            # a GET of the URL with no fragment is a new document; with the
            # fragment it would be a move inside this one
            self.page.goto(bare, wait_until="domcontentloaded", timeout=apply_limits.GOTO_TIMEOUT_MS)
            if bare != url:
                self.page.goto(url, wait_until="domcontentloaded", timeout=apply_limits.GOTO_TIMEOUT_MS)
        except Exception as e:      # noqa: BLE001  (the page is read as it is)
            if _closed_error(e):
                raise
            self._trace("error", step="verify_link.goto", error=type(e).__name__)
        info = apply_fill.settle(self.page, apply_limits.CLICK_TIMEOUT_S)
        self._decide_next("settled", f"settled {_settled_ms(info)} ms after the page was loaded "
                                     f"again")
        self._check_host(self.page.url)
        self.last_sig = None            # the same page again is the site's next step

    def _link_park(self, why: str, note: str, *, used: bool = True) -> _Parked:
        """A park at an emailed link (`why`, `note`: its words before any
        send). Once the link may send the application (`_maybe_sent`, set
        before the inbox is read) the park carries the
        check-sent reason and never a Re-queue: opening the link and then
        re-queueing would apply a second time. A link the run never used
        (`used` False: not in the inbox, never opened, held on its own
        address) is the person's to open, then Mark applied
        (`LINK_HELD_NOTE`); one whose address answered may have sent it
        (`CHECK_SENT_NOTE`)."""
        if not self._maybe_sent():
            return _Parked("needs_human", f"{LINK_REASON}: {why}", note)
        return _Parked("needs_human", f"{CHECK_SENT_REASON}: {LINK_REASON}, and the link may "
                                      f"send the application: {why}",
                       CHECK_SENT_NOTE if used else LINK_HELD_NOTE)

    def _open_link(self, link: str) -> str:
        """The emailed verification link in a new tab of the job's context,
        every main-frame navigation of it held to `_link_ok` before it is
        sent. The tab's route fetches each one itself with no redirect
        followed (the browser follows a server's redirect with no route
        seeing the hop): a redirect to another host is never asked for,
        and one to an allowed host goes to the page as a script's move, so
        the route sees that hop too. A script's or a meta refresh's move
        is held the same way, and so is the URL the tab settled on. Once
        the tab is headed for any other host nothing more of it loads and
        nothing of it is read. A navigation answered by the site's bot
        check (`_link_challenge`) goes no further: the check's page never
        runs, and the park asks the person to open the link. A 403, 429 or 503 that is no check goes no further either,
        and its park names the status. A settled page
        parks as a check when its main frame says so (`LINK_BOT_WORDS`) on a
        page with no box to fill (`_link_check_text`). On the link's own
        address the link is not used; an answer held on a later hop comes
        after that address answered, so the park says the link may have
        been used and asks for a Re-queue first. A page
        the tab opens (a popup) loads nothing and is closed. The tab's text
        once it settled (a park when it was refused, left the allowed hosts,
        asked for a bot check, was held by its status or did not load), and
        the tab closed. Each park goes through `_link_park`: once the link
        may send the application it asks the person to check, never to
        Re-queue."""
        context = self.page.context
        known = list(context.pages)
        tab = context.new_page()
        stopped: list[str] = []
        moves: list[str] = []           # the redirects handed to the page as a script's move
        landed = [False]                # a page that is no redirect was handed to the tab
        broken: list[str] = []          # why the route could not answer a navigation
        # what held a navigation's answer (`_link_challenge`: a bot check or a
        # status), and whether a hop of the link had answered before it (the
        # link may have been used)
        challenged: list[tuple[str, str, bool]] = []

        def main_frame(request) -> bool:
            try:
                return request.frame.parent_frame is None
            except Exception:       # noqa: BLE001  (a frame gone: the tab's own)
                return True

        def left(url: str) -> bool:
            """Is `url` off the allowed hosts? Where it goes joins `stopped`:
            its host, or its scheme when it has none (a `javascript:` or a
            `data:` address)."""
            host = _host(url)
            if self._link_ok(host):
                return False
            scheme = urlsplit(str(url or "")).scheme.lower()
            where = host or (f"a {scheme}: address" if scheme else "an address with no host")
            if where not in stopped:
                stopped.append(where)
            return True

        def guard(route) -> None:
            request = route.request
            if stopped:
                route.abort()       # the tab left the allowed hosts: nothing more of it loads
                return
            if not (request.is_navigation_request() and main_frame(request)):
                route.fallback()
                return
            if left(request.url):
                route.abort()
                return
            if len(moves) >= LINK_MOVES_MAX:
                broken.append("TooManyRedirects")
                route.abort()
                return
            try:
                answer = route.fetch(max_redirects=0, timeout=apply_limits.GOTO_TIMEOUT_MS)
            except Exception as e:  # noqa: BLE001  (a network error may quote the link's token)
                broken.append(type(e).__name__)
                route.abort()
                return
            kind, what = _link_challenge(answer)
            if kind:
                challenged.append((kind, what, bool(moves)))
                route.abort()       # the answer's page never runs (on the link's own
                return              # address, the link is not used)
            where = answer.headers.get("location", "") if 300 <= answer.status < 400 else ""
            if not where:
                landed[0] = True
                route.fulfill(response=answer)
                return
            where = urljoin(request.url, where)
            if left(where):
                route.abort()       # the other host is never asked
                return
            moves.append(where)
            landed[0] = False
            # the response's cookies are already the context's: the route's
            # fetch shares the context's cookie jar
            route.fulfill(status=200, content_type="text/html", body=(
                "<!doctype html><script>location.replace("
                + json.dumps(where).replace("<", "\\u003c") + ")</script>"))

        def hop(request) -> None:
            # a server's redirect the route did not fetch itself (none is
            # expected) is still checked here
            if request.is_navigation_request() and main_frame(request):
                left(request.url)

        def fresh(route) -> None:
            # a page made while the link is open is the tab's popup: nothing
            # of it loads (a popup's first request comes before its page)
            request = route.request
            if request.is_navigation_request():
                try:
                    page = request.frame.page
                except Exception:   # noqa: BLE001  (a page still being made)
                    page = None
                if page is None or (page is not tab and page not in known):
                    route.abort()
                    return
            route.fallback()

        def close(page) -> None:
            try:
                page.close()
            except Exception:       # noqa: BLE001
                pass
        error, text, check = "", "", ""
        try:
            context.route("**/*", fresh)
            tab.on("popup", close)
            tab.on("request", hop)
            tab.route("**/*", guard)
            tab.goto(link, wait_until="domcontentloaded", timeout=apply_limits.GOTO_TIMEOUT_MS)
            end = time.monotonic() + apply_limits.GOTO_TIMEOUT_MS / 1000
            while moves and not landed[0] and not stopped and not broken and not challenged \
                    and time.monotonic() < end:
                tab.wait_for_timeout(100)   # the script's move to the redirect's target
            if broken and not stopped:
                raise RuntimeError(broken[0])
            if not stopped and not challenged and not left(tab.url):
                apply_fill.settle(tab, apply_limits.CLICK_TIMEOUT_S)
            if not stopped and not challenged and not left(tab.url):
                text = apply_fill.page_text(tab)
                check = _link_check_text(tab)
                left(tab.url)       # a redirect while it was read: the text is dropped
        except Exception as e:      # noqa: BLE001  (an error may quote the link's token)
            error = broken[0] if broken else type(e).__name__
        finally:
            for page in [p for p in context.pages if p is not tab and p not in known]:
                close(page)         # the tab's popups, whatever the event saw
            close(tab)
            try:
                context.unroute("**/*", fresh)
            except Exception:       # noqa: BLE001
                pass
        if stopped:
            raise self._link_park(f"the emailed link went on to {stopped[0]}, outside the "
                                  "application's sites; the run stopped it", LINK_NOTE)
        bot = LINK_BOT_WORDS.search(check)
        if bot and not challenged:
            challenged.append(("check", f"the page says {' '.join(bot.group(0).split())!r}",
                               bool(moves)))
        if challenged:
            kind, what, hopped = challenged[0]
            said = f"asked for a bot check ({what})" if kind == "check" else f"answered {what}"
            if hopped:
                raise self._link_park(f"the emailed link's page {said} after the link's own "
                                      "address had answered, so the link may have been used",
                                      LINK_USED_NOTE)
            # on the link's own address the link is not used
            raise self._link_park(f"the emailed link's page {said}",
                                  LINK_BOT_NOTE if kind == "check" else LINK_NOTE, used=False)
        if error:
            raise self._link_park(f"the emailed link did not open ({error})", LINK_NOTE)
        refused = LINK_FAILED_WORDS.search(text or "")
        if refused:
            raise self._link_park(f"the site refused the emailed link "
                                  f"({' '.join(refused.group(0).split())!r})", LINK_NOTE)
        return " ".join((text or "").split())
