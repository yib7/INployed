"""The form steps of a job's run: the application form and its buttons, the
form's problems and their repair, the advance to the next step, the
option plan and the re-ask, the fill and its verification, the review
page, the password boxes, the generated answers, the option ties and the
pauses. `_FormSteps` is a base of `_JobRun`, and its methods run on a
`_JobRun`.

Split out of `apply_run`, which re-exports `_JobRun`.
"""
from __future__ import annotations

import dataclasses
import time
from contextlib import contextmanager
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

import apply_click
import apply_facts
import apply_fill
import apply_form
import apply_judge
import apply_limits
import apply_pause
import apply_queue
import apply_sendwatch
import apply_trace
import ats_accounts
from apply_judge import FillPlan, VerifyResult
from apply_outcome import (_cap, CHECK_SENT_NOTE, CHECK_SENT_REASON, CLOSED_REASON, LOGIN_NOTE,
                           _NotClicked, OPTION_TIE_WORDS, OPTIONS_UNREAD_WORDS, _Parked,
                           PAUSE_UNANSWERED_REASON, _PauseClosed, _SentSeen, TAB_CLOSED_REASON,
                           _Unsent)
from apply_send_words import _final_shaped, _send_worded
from apply_sites import _host, _insecure, _site
from apply_sendwatch import new_confirmation
from apply_page import (_BUTTON_HOME_JS, _button_text, field_named_in, _fields_sig, _ident_attrs,
                        _label_key, _message_key, _record_verification, settled_words,
                        _spare_judged)
from apply_account_flow import (_fills_the_application, _names_password, _NEW_PASSWORD,
                                page_problem, _page_words)
from apply_route import (_ACTED, buttons_moved, _draft_key, _drafts, form_entry_choice, form_route,
                         new_fields, _park_on_stuck, pick_holds, _picks, place_words, review_route,
                         _same_text, _shaped, shaped_holds, _typed_box)
from apply_gate import _control_words, _invalid_words, submit_on


class _FormSteps:
    """`_JobRun`'s form steps (see the module docstring)."""

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
        park_mode = not submit_on(self.r.settings)
        plan = self._own_submit(digest, plan)
        step, button, why = form_route(digest, plan, park_mode=park_mode,
                                       submit_apart=self._submit_apart(digest, plan),
                                       judged=self._judged_roles(digest))
        b = self._form_entry(digest, plan, step)
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
        """The controls of the way on's frame the run cannot read
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
            # before one hides it
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
        """With both a judged advance and a judged submit, does the
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
        """Of the buttons judged submit at `BUTTON_SUBMIT_MIN_CONF`
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
        """The way on the filled page chose: when its button is
        still disabled once the fill settled, and stays so for
        `DISABLED_WAIT_S`, the fields the form reports as invalid are
        repaired (`_repair`, up to `REPAIR_ROUNDS`); a button that stays
        disabled parks the job with the fields that keep it so as the
        evidence: the form's own report (a control that would not validate,
        a required control left empty) and the boxes the plan left blank.
        Nothing is clicked. Returns the page, its plan, its verification and
        the button's `n` as the page now numbers it (the same control,
        `_same_button`).

        A CAPTCHA checkbox on the page comes first: a way on
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
        for round_no in range(apply_limits.REPAIR_ROUNDS + 1):
            loc = apply_form.resolve(self.page, button.locator)
            deadline = time.monotonic() + apply_limits.DISABLED_WAIT_S
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
            if invalid and round_no < apply_limits.REPAIR_ROUNDS:
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
            park = _Parked("needs_human", f"required field without an answer: {label} (the "
                                          f"{text} button stays disabled after the fill)")
        elif blank:
            # the page wants a box the plan left blank: the
            # unanswered fields are the evidence, in the policy's words
            more = f"; also blank: {_cap(', '.join(blank[1:]), 100)}" if blank[1:] else ""
            park = _Parked("needs_human", f"required field without an answer: {blank[0]} (the "
                                          f"{text} button stays disabled after the fill{more})")
        else:
            rows = [_invalid_words(r) for r in invalid[:2]]
            park = _Parked("needs_human", f"the {text} button stays disabled after the fill"
                                          + (f" ({_cap('; '.join(rows), 220)})" if rows else ""))
        # The person fixes the page in the browser (or answers its blank
        # fields in the card) and the page is read again; no answer parks
        self.pause.at_disabled(digest, plan, park.reason)
        raise park

    # -- the same control after a repair ---------------------------------------

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
        its message into the button's own box changes those words), and
        the same form, or outside a form a box that holds
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

    # -- the form's refusals and their repair -------------------------------

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
        """Did the form refuse the click on button `n`: the page is
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
            # nothing of a field (a "did not advance"), never a refusal
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

        A confident wrong mapping is possible, so: a field
        the judge named for a message on this form is never offered for it
        again (a later round, the form still showing the message, asks a
        fresh question without it); a message the first request maps to no
        field gets one second look, asked in other words; and a problem the
        judge mapped carries `mapped` (its confidence), which `_repair_named`
        never parks on alone. A message both looks leave unmapped names the
        field whose whole label its own words hold, when exactly one does
        (`field_named_in`): `by_label`, for a repair only, never a park. With `actable` (the fields a repair can put
        right), a judged field outside it (an upload made, a password) counts
        as no mapping: the second look asks without it."""
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
                # the second look: the messages mapped to no field, asked
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
        """The repair of the fields the form refused: first the page is
        read again (a field it revealed is filled, `_fill_revealed`); then
        each field a problem names (`_problem_fields`): one the plan left
        blank is asked again as required (its own mapping request, the
        option picks, the second look) and filled, or the job parks on it
        ("required field without an answer", with the form's words); one
        the fill put a value in is typed again in the shape the message asks
        (`apply_fill.repair`: bare digits, a date format, key by key) and
        verified. Returns the page, its plan and its verification.

        A blank field only the judge's reading of a message named (the
        reading can be confidently wrong) is filled when it
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
        # named by the judge's reading of a message alone
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
            labels = [f.label for f in digest.fields]
            says = {n: _page_words(named[n][0].get("text"), 100, labels) for n in blank}
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
            except _PauseClosed:
                raise
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
            again = {v.n: v for v in self._verify(fixed, _drafts(plan, digest), _picks(plan),
                                                  _shaped(plan, digest))}
            self._last_filled.update({f.n: f for f in fixed})
            verification = [again.get(v.n, v) for v in verification]
            _record_verification(rec, list(again.values()))
            _park_on_stuck(self._clear_wrong_optional(
                FillPlan(fields=[by_pf[n] for n in typed]), list(again.values()), rec))
        if missed and depth == 0:
            # the judge named a blank field that has no answer; its
            # messages are mapped once more without it (a fresh question).
            # The field is kept: a message still shown when the rounds end
            # parks on it
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
        """The rounds are over and the form still shows a
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

    def _refused_words(self, problems: list[dict[str, Any]],
                       labels: Iterable[object] = ()) -> str:
        """A park's evidence after the repair rounds: each problem in the
        form's words (`_invalid_words`, or the error text, a quoted field
        `labels` kept)."""
        labels = list(labels)
        rows = []
        for p in problems[:3]:
            if p.get("kind") == "invalid":
                rows.append(_invalid_words(p))
            else:
                rows.append(f"the form says: {_page_words(p.get('text'), 100, labels)}")
        return _cap("; ".join(rows), 260)

    def _advance(self, digest: apply_form.FormDigest, plan: FillPlan,
                 verification: list[VerifyResult], rec: dict, n: int, conf: float) -> None:
        """Click the page's advance (`_click`) and read what the form
        said: a form that refused the step (`_form_problems`) is
        repaired (`_repair`) and the advance clicked once more, at most
        `REPAIR_ROUNDS` times, never a second time on a click the form
        refused without a repair; then the job parks naming each field and
        its message ("required field without an answer" when one was left
        empty)."""
        text = _button_text(digest, n)
        carried: set[str] = set()
        for round_no in range(apply_limits.REPAIR_ROUNDS + 1):
            # submit mode clicks a final-worded advance ("Confirm",
            # "Complete", "Done") as a step (park mode sends it to the gate);
            # once the application's answers are on the site it may send, so
            # the gate's live checks run first (`_final_step_checks`) and it
            # is marked before the click. Before them there is nothing to
            # send: an address screen's "Confirm"
            final_worded = submit_on(self.r.settings) and self.form_filled \
                and _final_shaped(digest, n)
            if final_worded:
                digest, plan, verification, n = self._final_step_checks(
                    digest, plan, verification, rec, n)
                text = _button_text(digest, n)
            who = self._button_identity(digest, n)
            before = self._form_state(digest, n)
            # the messages the last round's repair answered are no baseline:
            # one the form shows again after this click is its refusal still
            # (a banner a page writes the same words into)
            before["errors"] = set(before.get("errors") or set()) - carried
            problems: list[dict[str, Any]] = []

            def _check(d=digest, b=before, out=problems) -> bool:
                out[:] = self._form_problems(d, n, b)
                return bool(out)
            final = final_worded and not self._final_advance
            if final:
                self._final_advance = True
            try:
                result = self._click(digest, n, "advance", rec, conf=conf,
                                     refused_by_form=_check)
            except _NotClicked:
                if final:
                    self._final_advance = False     # the live check stopped the click
                raise
            if final and (result.refused or not (result.clicked or result.late)):
                self._final_advance = False     # nothing was clicked
            if not problems:
                return
            self._decide("form_refused", f"the form refused the {_cap(text, 40)} step "
                                         f"(round {round_no + 1})",
                         problems=[_cap(p.get("text") or p.get("label") or "", 80)
                                   for p in problems])
            if round_no == apply_limits.REPAIR_ROUNDS:
                break
            digest, plan, verification = self._repair(digest, plan, verification, problems, rec)
            if not self._repaired:
                # nothing the run can put right: never the same click again
                # here; on a first refusal the loop reads the page as it now
                # stands (its message too), as it did before repairs, and its own
                # "page did not advance" ends a step that comes back the same
                if round_no == 0:
                    return
                break
            carried = {str(p.get("text") or "") for p in problems if p.get("kind") == "error"}
            # the same control as the page now numbers it
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
        said = self._refused_words(problems, [f.label for f in digest.fields])
        raise _Parked("needs_human", f"the form refused the {_cap(text, 40)} step after "
                                     f"{apply_limits.REPAIR_ROUNDS} repair(s): {said}")

    def _form_entry(self, digest: apply_form.FormDigest, plan: FillPlan,
                    step: str) -> apply_form.Button | None:
        """The Apply entry a form step clicks instead of the gate,
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
        park_mode = not submit_on(self.r.settings)
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
        required field whose pick came back dropped or weak, then the saved
        answers the own-question gate held back (`_reworded`)."""
        plan = self._reask(digest, answers, plan, rec, "source")
        s2, q2 = apply_judge.option_questions(digest, plan, catalog=self.catalog,
                                              company=self._company())
        if q2:
            picks = self.r.jev.judge(s2, q2)
            answers.update(picks)
            plan = apply_judge.plan(digest, self.catalog, answers,
                                    generation_enabled=bool(self.r.settings["auto_apply_generate"]),
                                    company=self._company())
            rec["flags"] = dict(plan.flags)
            self._trace("option_picks", answers=apply_trace.answers_json(picks),
                        plan=apply_trace.plan_json(plan))
        plan = self._reask(digest, answers, plan, rec, "pick")
        return self._reworded(digest, answers, plan, rec)

    def _reworded(self, digest: apply_form.FormDigest, answers: dict, plan: FillPlan,
                rec: dict) -> FillPlan:
        """The saved answers the own-question gate held back, asked in one
        request whether each settles its field's question as worded there
        (`apply_judge.settle_questions`); the plan made again fills the sure
        ones (`apply_judge.settled_pick`)."""
        s, q = apply_judge.settle_questions(digest, plan, answers, self.catalog,
                                            company=self._company())
        if not q:
            return plan
        got = {k: v for k, v in self.r.jev.judge(s, q).items() if k in q}
        answers.update(got)
        plan = apply_judge.plan(digest, self.catalog, answers,
                                generation_enabled=bool(self.r.settings["auto_apply_generate"]),
                                company=self._company())
        rec["flags"] = dict(plan.flags)
        asked = sorted(int(k.split("_")[1]) for k in q)
        filled = [pf.n for pf in plan.fields if pf.n in asked and pf.action == "select"]
        labels = {f.n: f.label for f in digest.fields}
        self._decide("reworded", f"asked whether {len(asked)} saved answer(s) settle a question "
                               f"worded another way, {len(filled)} sure: "
                               f"{_cap(', '.join(labels.get(n, '') for n in asked), 160)}",
                     fields=asked, filled=filled)
        self._trace("reworded", answers=apply_trace.answers_json(got),
                    plan=apply_trace.plan_json(plan))
        return plan

    def _reask(self, digest: apply_form.FormDigest, answers: dict, plan: FillPlan, rec: dict,
               what: str) -> FillPlan:
        """The second look: the required fields the plan skipped for a
        mapping the first request dropped or left under its floor
        (`apply_judge.reask_targets`) are asked once more, all in one request
        (`apply_judge.reask_questions`); the answers replace the
        first look's and the plan is made again. A re-read of the same page
        (its URL path and its fields) reuses the second look's answers and
        makes no request. A field the data cannot answer still parks: the
        second look names `leave_blank` or `no_match` too; a consent tick
        read under its floor again still parks."""
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
        # The person's answers kept for this page go in first; a park the
        # person can answer pauses the job (`apply_pause.Pauser`), and parks
        # as before when no answer comes
        self.pause.apply_pending(digest, plan)
        self._resolve_generation(digest, plan, rec)
        if plan.park_reason:
            self.pause.at_plan(digest, plan)
        for question, context in plan.missing:
            self._add_missing(question, context, digest)
        if plan.park_reason:
            raise _Parked("needs_human", plan.park_reason)
        # the text boxes the plan leaves alone, as they read before the fill:
        # one the page writes into during the fill (a resume parser's guess)
        # is checked after it (`_page_writes`); a box holding the
        # person's own value (`apply_pause.KEPT`) is theirs
        idle = [pf for pf in plan.fields if pf.action not in _ACTED
                and pf.action not in (apply_judge.PASSWORD_ACTION, apply_pause.KEPT)
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
        tied = self._option_ties(plan, errors, ask_required=False)
        filled = [f for f in filled if f.n not in tied]
        # A required field left with no option pauses for the person's pick
        filled += self.pause.at_tie(digest, plan, tied)
        locators = {pf.n: pf.locator for pf in plan.fields}
        self._filled_here += [locators[f.n] for f in filled
                              if f.n in locators and str(f.value or "").strip()]
        self._filled_here += apply_pause.kept_locators(plan)
        for locator in apply_pause.kept_locators(plan):
            self._mask_box(locator)     # the person's own value stays out of the screenshots
        self._filled_any = self._filled_any or bool(self._filled_here)
        if _fills_the_application(digest, plan, filled):
            self.form_filled = True
        for pf in plan.fields:
            if pf.n in tied and pf.required:
                self._add_missing(pf.label, tied[pf.n], digest)
        self._park_a_required_tie(plan, tied)
        drafts = _drafts(plan, digest)
        shaped = _shaped(plan, digest)
        verification = self._verify(filled, drafts, _picks(plan), shaped)
        verification = self._retry_failed(plan, filled, verification, drafts, shaped)
        self._last_filled.update({f.n: f for f in filled})
        stuck = self._clear_wrong_optional(plan, verification, rec)
        self._record_fill(rec, digest, plan, filled, verification)
        self._trace("verify", results=[{"n": v.n, "label": v.label, "ok": v.ok,
                                        "p_correct": v.p_correct,
                                        "p_placeholder": v.p_placeholder}
                                       for v in verification])
        still = [v.label for v in verification if not v.ok
                 and any(pf.n == v.n and pf.required for pf in plan.fields)]
        if still:
            raise _Parked("needs_human", "could not verify: " + ", ".join(still))
        _park_on_stuck(stuck)
        return verification

    def _after_fill(self, digest: apply_form.FormDigest, plan: FillPlan,
                    verification: list[VerifyResult],
                    rec: dict) -> tuple[apply_form.FormDigest, FillPlan, list[VerifyResult]]:
        """The page read again once the fill settles, before a button is
        chosen, up to `FILL_ROUNDS_MAX` times until it holds:

        - a value the page changed after the fill put it in (a
          resume parser, a profile lookup after the email) is put back once
          and verified again (`_page_changes`); a text box the plan left
          alone that the page wrote into during the fill is checked against
          the sheet and cleared when it is wrong (`_page_writes`);
        - a field the fill revealed ("Yes" opens "Please explain")
          is mapped, planned, filled and verified like the page's own
          (`_fill_revealed`);
        - buttons the fill enabled, revealed or renamed (such as
          a Next that waits for a privacy tick, a disabled Apply) are judged
          again (`_judge_buttons`); unchanged ones keep their roles and take
          the fresh read's locators.

        Returns the page as it now stands, its plan and its verification."""
        if self.page is None:           # a unit test's run with no page
            return digest, plan, verification
        url = str(self.page.url)
        for _ in range(apply_limits.FILL_ROUNDS_MAX):
            info = apply_fill.settle(self.page, apply_limits.CLICK_TIMEOUT_S)
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
        """Every value this page's fill put in, read again; the ones
        the page changed since (an upload never counts: it is never sent
        twice) are put in once more and verified again. A field the page
        changes a second time keeps the page's value and its verification."""
        by_n = {pf.n: pf for pf in plan.fields}
        picks, shaped = _picks(plan), _shaped(plan, digest)

        def holds(n: int, value: str, was: str) -> bool:
            if n in picks:
                return pick_holds(value, *picks[n])
            if n in shaped and shaped[n][0] != "suggestion":
                return shaped_holds(value, *shaped[n])
            return _same_text(value, was)     # a match the fill took: kept as it was
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
        results = {v.n: v for v in self._verify(again, _drafts(plan, digest), _picks(plan),
                                                _shaped(plan, digest))}
        self._last_filled.update({f.n: f for f in again})
        verification = [results.get(v.n, v) for v in verification]
        _record_verification(rec, list(results.values()))
        stuck = self._clear_wrong_optional(FillPlan(fields=changed), list(results.values()), rec)
        still = [v.label for v in verification if not v.ok
                 and any(pf.n == v.n and pf.required for pf in plan.fields)]
        if still:
            raise _Parked("needs_human", "could not verify: " + ", ".join(still)
                          + " (the page changed the value after the fill)")
        _park_on_stuck(stuck)
        return verification

    def _page_writes(self, rec: dict) -> None:
        """A text box the plan left alone that the page wrote into
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
                    "", timeout=apply_click.ACTION_TIMEOUT_MS)
                rec.setdefault("cleared", []).append(pf.label)
            except Exception as e:  # noqa: BLE001  (a box gone: nothing to clear)
                self._trace("error", step="page_writes.clear", error=type(e).__name__)

    def _fill_revealed(self, digest: apply_form.FormDigest, fresh: apply_form.FormDigest,
                       revealed: list[apply_form.Field], plan: FillPlan,
                       verification: list[VerifyResult],
                       rec: dict) -> tuple[apply_form.FormDigest, FillPlan, list[VerifyResult]]:
        """The fields the fill revealed, numbered after the page's
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
        """The page's buttons after the fill. Unchanged
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
            # a button the page read before now says it sends: the
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
        and the fill errors by their type."""
        actions = {pf.n: pf.action for pf in plan.fields}
        how = {o["n"]: o.get("how", "") for o in outcomes or []}
        self._trace("fill", retry=retry, errors=errors,
                    fields=[{"n": f.n, "label": f.label, "action": actions.get(f.n, ""),
                             "how": how.get(f.n, ""),
                             "holds_value": bool(str(f.value or "").strip())} for f in filled])

    def _review_page(self, digest: apply_form.FormDigest, answers: dict,
                     plan: FillPlan, rec: dict) -> None:
        """Fill and verify editable review controls, then the review's way on
        (`review_route`): a confident advance with no submit is
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
        window is left to the user. A navigation it stopped before anything
        may have been sent (`_maybe_sent`) parks the job with the host it was
        headed for. A form post it stopped after the submit click was the
        send, and nothing went out, only when no request but that post left
        and the step's own read claims no confirmation (`_stopped_post`);
        otherwise the step's end stands and names the
        stopped post. Any other navigation stopped after a possible send
        keeps the step's end (a send may have gone out before the page moved
        on, and a second one must not), and a step that went on after one
        asks the person to check. `_maybe_sent`, never `submit_clicked`
        alone: validation errors reset the click after a request left."""
        guard = apply_sendwatch._NavGuard(self, self.page)
        try:
            yield guard
        except _Parked as p:
            if guard.blocked and self.submit_clicked and guard.posted:
                raise self._stopped_post(guard, p) from None
            if guard.blocked and not self._maybe_sent():
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
            raise self._stopped_post(guard, None)
        if guard.blocked and not self._maybe_sent():
            raise _Parked("needs_human", f"left the allowed sites: {guard.blocked[0]}",
                          guard.before)
        if guard.blocked and not self.submit_clicked:
            # a step that may have sent (a code, a link, a final-worded
            # advance, a request the submit's watch saw) went on, and the
            # page it led to was stopped
            raise _Parked("needs_human", f"{CHECK_SENT_REASON}: after {self._sent_step()} the "
                                         f"run stopped the page going to {guard.blocked[0]}",
                          CHECK_SENT_NOTE)

    def _stopped_post(self, guard: apply_sendwatch._NavGuard, p: _Parked | None) -> _Parked:
        """The end of a step whose form post the guard stopped (`p`: the
        step's own park, or None when the step went on). Nothing was sent
        when the step claims no confirmation and no request but the stopped
        post left (`SendWatch`'s rows, the stopped one aside): the job is no
        possible send (`_Unsent`). A "submitted (unconfirmed)" end that rests
        on the watch's rows alone (`_SentSeen`) claims none: its row may be
        the stopped post, which the watch counts as sent when it was headed
        for an admitted job board. Received words do
        claim one. Otherwise the step's end stands and names the stopped
        post; a step that went on after a request left asks the person to
        check."""
        host = guard.post_host() or (guard.blocked[0] if guard.blocked else "")
        watch = self._send_watch
        seen = ([*watch.sent, *watch.possible, *watch.unplaced_sends()]
                if watch is not None and self.submit_clicked else [])
        others = [row for row in seen if row not in guard.posts]
        claims = p is not None and p.status == "submitted" and not isinstance(p, _SentSeen)
        if not claims and not others:
            self.submit_clicked = False
            self._unsent = True
            return _Unsent("needs_human", f"the form posts to {host}, outside the allowed "
                                          f"sites; the run stopped it and nothing was sent",
                           guard.before)
        if p is not None:
            return _Parked(p.status, f"{p.reason}; the run stopped a post to {host}",
                           p.tab_note)
        return _Parked("needs_human", f"{CHECK_SENT_REASON}: a request left after the submit "
                                      f"click ({_cap(others[0], 120)}); the run stopped a post "
                                      f"to {host}", CHECK_SENT_NOTE)

    def _fill_passwords(self, digest: apply_form.FormDigest, plan: FillPlan, rec: dict,
                        guard: apply_sendwatch._NavGuard) -> None:
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
        fields = {f.n: f for f in digest.fields}
        # the page makes the password (an account made inside the
        # application) rather than signs in with it
        making = len(boxes) > 1 or any(
            str(f.autocomplete or "").lower() == "new-password"
            or _NEW_PASSWORD.search(f"{f.label or ''} {f.id_or_name or ''}")
            for f in (fields.get(pf.n) for pf in boxes) if f is not None)
        if _site(host) in self.form_password_sites \
                and not (making and self._form_retype(host, digest, boxes)):
            # a second form page asking for the password on the same site is
            # the first one rejected (a wrong password): typing it again only
            # moves the account toward a lockout. A form that makes the
            # account, shown again with its password emptied, is no sign-in
            # and takes it once more
            says = page_problem(digest)
            raise _Parked("needs_human", f"the form on {host} asked for the master password "
                                         "again" + (f" (the page says {says!r})" if says else ""),
                          LOGIN_NOTE)
        if making and ats_accounts.has_password():
            self._check_password_rules(digest, host)       # before anything is typed
        frames = apply_form.frames(self.page)
        stored = ats_accounts.has_password()
        account_host = ""
        typed: list = []
        for pf in boxes:
            f = fields.get(pf.n)
            # the frame the fill acts in (`resolve`'s), so the host checked
            # is the host the password is typed on
            frame = self._box_frame(self.page, pf.locator, frames)
            box_url = self._frame_address(frame) if frame is not None else ""
            box_host = _host(box_url)
            loc, kind, posts_to, action = None, "", "", ""
            try:
                loc = apply_form.resolve(self.page, pf.locator).first
                kind, action = loc.evaluate(
                    "el => [el.type || '', el.form && el.form.getAttribute('action') "
                    "? el.form.action : '']", timeout=5_000)
                if urlsplit(str(action or "")).scheme in ("http", "https"):
                    posts_to = _host(action)    # a `javascript:` action is no destination
                else:
                    action = ""
            except Exception:       # noqa: BLE001  (a box or frame that went away takes nothing)
                loc = None
            # the page, the box's frame and the form's action: an unencrypted
            # address first, then a page or frame off the application
            plain = self._secret_refusal(u for u in (str(self.page.url), box_url, action)
                                         if _insecure(u))
            outside = self._secret_refusal([str(self.page.url), box_url or str(self.page.url)])
            if not stored:
                why = "the form asks for a password and no master password is stored"
            elif plain or outside:
                why = plain or outside
            elif loc is None:
                why = f"the password box ({pf.label}) went away"
            elif str(kind).lower() != "password" or f is None or not _names_password(f):
                why = f"{pf.label or 'a masked box'} is not a password box for an account"
            elif posts_to and not self._password_ok(posts_to):
                # the application's own site only: LinkedIn, the inbox and a
                # job board are allowed to load and never take the password
                why = f"the password box's form posts to {posts_to}, outside the allowed sites"
            else:
                if frame is not None:
                    guard.frames.add(id(frame))
                guard.start()
                self._keep_secret_box(pf.locator)
                moved = self._moved_box(self.page, frame)
                if moved:
                    raise _Parked("needs_human", moved)
                if ats_accounts.fill_password(self.page, loc, host_ok=self._password_frame_ok):
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
            self._form_password_sigs[_site(host)] = _fields_sig(digest)
            # a page that makes an account asks for a new password or its
            # confirmation; a sign-in read as the form makes none
            makes = len(typed) > 1 or any(
                str(f.autocomplete or "").lower() == "new-password"
                or _NEW_PASSWORD.search(f"{f.label or ''} {f.id_or_name or ''}") for f in typed)
            email = self.catalog.value("email")
            if makes and email and not self._account_for(account_host):
                self._record_account(account_host, email)

    def _form_retype(self, host: str, digest: apply_form.FormDigest, boxes: list) -> bool:
        """The form page that made an account with the master
        password (the caller's check: a new-password box, or a second box to
        confirm it; a sign-in's box never), shown again with the same boxes
        and its password boxes emptied (the site cleared them after an error
        elsewhere), takes it once more, once per site."""
        site = _site(host)
        if site in self._form_retyped or self._form_password_sigs.get(site) != _fields_sig(digest):
            return False
        try:
            empty = all(int(apply_form.resolve(self.page, pf.locator).first.evaluate(
                "el => (el.value || '').length", timeout=5_000)) == 0 for pf in boxes)
        except Exception:       # noqa: BLE001  (a box that cannot be read is no emptied box)
            empty = False
        if not empty:
            return False
        self._form_retyped.add(site)
        self.form_password_sites.discard(site)
        says = page_problem(digest)
        self._decide("password_retyped", "the form came back with its password boxes emptied; "
                                         "the master password is typed once more"
                                         + (f" (the page says {says!r})" if says else ""))
        return True

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
            if key is not None and key in self._drafts_by_question:
                # The same question read again (a page the form sent
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
                    extra = getattr(last, "calls", 1)
                    if isinstance(extra, int) and extra > 1:
                        # A retried draft call spends a draft too, so
                        # a job never makes more than GENERATE_MAX calls
                        self.gen_budget = max(0, self.gen_budget - (extra - 1))
                elif not text:
                    # a hook that keeps no attempt (NotConfigured, a bare injected
                    # hook) made no draft to reject; the record says so and the
                    # park reason stays the plain one
                    record_note = "no generator"
            if text:
                pf.action, pf.value = "fill", str(text)
                if key is not None:
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

    def _option_ties(self, plan: FillPlan, errors: list[dict], *,
                     ask_required: bool = True) -> dict[int, str]:
        """The fields left with no option chosen, each with the words for
        why: the options tie on the planned answer (`apply_fill.OptionTie`:
        the options that hold it differ in meaning), or
        a list whose options were never read ahead holds no option code
        matches to the answer (`apply_fill.OptionsUnread`).
        Each is an open question for the person; the caller parks a required
        one and leaves an optional one blank, out of the verification.
        `ask_required` off leaves a required one's missing entry to the caller
        (the form's fill asks the person first)."""
        why = {apply_fill.OptionTie.__name__: OPTION_TIE_WORDS,
               apply_fill.OptionsUnread.__name__: OPTIONS_UNREAD_WORDS}
        tied = {e.get("n"): why[e.get("error")] for e in errors if e.get("error") in why}
        for pf in plan.fields:
            if pf.n not in tied:
                continue
            if tied[pf.n] == OPTION_TIE_WORDS:
                self._decide("option_tie", f"the options that hold the answer for {pf.label!r} "
                                           f"tie and differ in meaning: none was chosen",
                             fields=[pf.label])
            else:
                self._decide("options_unread", f"the options of {pf.label!r} could not be "
                                               f"read and none is the answer: none was chosen",
                             fields=[pf.label])
            if ask_required or not pf.required:
                self._add_missing(pf.label, tied[pf.n])
        return tied

    def _clear_wrong_optional(self, plan: FillPlan, verification: list[VerifyResult],
                              rec: dict) -> list[str]:
        """An optional field whose answer failed its check has the answer
        taken out (`apply_fill.clear`): the form goes with
        the field blank. Each one cleared is
        traced (`cleared_optional`), recorded on the page (`cleared`) and
        dropped from the values the page is read against. Returns the
        labels of the ones whose answer stays (a radio group keeps its
        choice): the caller parks on the first (`_park_on_stuck`)."""
        failed = {v.n for v in verification if not v.ok}
        cleared: list[str] = []
        stuck: list[str] = []
        for pf in plan.fields:
            if pf.n not in failed or pf.required or pf.action not in _ACTED:
                continue
            got = self._last_filled.get(pf.n)
            if got is None or not str(got.value or "").strip():
                continue        # the read-back holds nothing to take out
            if apply_fill.clear(self.page, pf):
                self._last_filled.pop(pf.n, None)
                cleared.append(pf.label)
            else:
                stuck.append(pf.label)
        if cleared:
            rec.setdefault("cleared", []).extend(cleared)
            self._decide("cleared_optional",
                         f"the answer of {len(cleared)} optional field(s) failed its check and "
                         f"was taken out: {_cap(', '.join(cleared), 160)}", fields=cleared)
        return stuck

    @staticmethod
    def _park_a_required_tie(plan: FillPlan, tied: Mapping[int, str]) -> None:
        """A required field among `tied` (`_option_ties`) parks the job: its
        answer is the person's to pick. The form and an account screen park
        the same way; an optional one stays blank."""
        required = [pf for pf in plan.fields if pf.n in tied and pf.required]
        if required:
            raise _Parked("needs_human", f"required field without an answer: "
                                         f"{required[0].label} ({tied[required[0].n]})")

    def _add_missing(self, question: str, context: str,
                     digest: apply_form.FormDigest | None = None) -> None:
        """One missing answer for the queue entry; with `digest`, the field's
        help, live options and answer type go with it, so Answer now
        opens Add answer prefilled with them."""
        f = next((x for x in digest.fields if x.label == question), None) \
            if digest is not None else None
        extra: dict[str, Any] = {}
        if f is not None:
            extra = {"help": f.help or "", "options": [str(o) for o in f.options],
                     "type": apply_facts.answer_type(f.type, f.options)}
        self.missing.append({"question": question, "context": context, "suggestion": "",
                             **extra})
        # a queue lock held past its wait leaves the question in the record
        # and the drain report; the job goes on
        self.r._queue_write(self.job_id, "missing answer", lambda: apply_queue.add_missing(
            self.job_id, question, context=context, path=self.r.queue_path, **extra),
            "the question is in the record only")

    def _pause_moved(self, before: tuple | None, text: str, reason: str,
                     buttons: tuple = ()) -> _Parked | None:
        """After a pause's wait: did the page move on
        while the run waited? `before` is the pause's print of the page
        (URL, fields), `buttons` its visible (text, locator) pairs.

        - A received phrase it did not show before (`new_confirmation`)
          reads as sent on any page.
        - A page with a send-worded button (`_send_worded`) reads as sent
          when its address changed, when no button with that text is on the
          page now, when none of its labelled fields is left, or when it can
          no longer be read: the two signals the gate's own wait uses
          (`_moved_during_wait`), and the form gone.
        - A page with no send-worded button (a wizard's Next)
          that moved on (its address changed, or any of its labelled fields
          is gone: a same-address single-page app's next step) is read and
          planned again: the person went on to the next step, and nothing on
          the page they left could send.

        Read as sent, the job may have been sent (`_maybe_sent`), is never
        handed back to the queue, and parks with the "check whether" note.
        None when the page is the form it paused on, or a step it moved on
        to from a page that could not send."""
        word = new_confirmation(text, self._page_text())
        what = f"it shows {word!r}" if word else ""
        if not word and before is not None:
            after, now = self.pause._read()
            was = {row for row in before[1] if row[0]}
            url_moved = after is not None and str(after[0]) != str(before[0])
            form_gone = bool(was) and after is not None and not was & set(after[1])
            # any labelled field gone: a same-address single-page app's next
            # step (`Pauser._moved_on`)
            form_changed = after is not None and not was <= set(after[1])
            sends = [t for t, _loc in buttons if _send_worded(t)]
            if sends:
                there = {" ".join(str(t).split()).lower() for t, _loc in now}
                gone = [t for t in sends if " ".join(str(t).split()).lower() not in there]
                if after is None:
                    what = "it could not be read"
                elif url_moved:
                    what = "its address changed"
                elif len(gone) == len(sends):
                    what = f"its {_cap(' '.join(str(gone[0]).split()), 60)!r} button is gone"
                elif form_gone:
                    what = "the form it paused on is gone"
            elif url_moved or form_changed:
                self._person_moved_on = True
                self._decide("pause_moved_on", "the page moved on to another step while the "
                                               "run waited for you, from a page with no send "
                                               "button: it is read and planned again")
                return None
        if not what:
            return None
        self._pause_sent = True
        self._decide("pause_moved", f"the page moved on while the run waited for you ({what}): "
                                    "the application may have been sent in the browser")
        return _Parked("needs_human", f"{CHECK_SENT_REASON}: the page moved on during the pause "
                                      f"({what}); the run had reached: {_cap(reason, 200)}",
                       CHECK_SENT_NOTE)

    def _pause_closed(self, buttons: tuple = (), reason: str = "") -> _Parked:
        """The window or the tab closed during a pause's wait. The person had
        the browser, and every application step has a way on: they may have
        clicked through and sent it before the close, from any page. The job
        may have been sent (`_pause_sent`), and the park returned already
        carries the check-whether reason and note (`_PauseClosed`), so it is
        never offered a Re-queue whatever the run's
        handler finds of the window. `buttons` (the pause's read) only words
        the decision; `reason` is what the pause asked about."""
        sends = [t for t, _loc in buttons if _send_worded(t)]
        self._pause_sent = True
        what = (f"its {_cap(' '.join(str(sends[0]).split()), 60)!r} button was on the page"
                if sends else "you had gone on to another step" if self._person_moved_on
                else "you had the browser and may have gone on in it")
        self._decide("pause_closed", f"the browser closed during the pause ({what}): the "
                                     "application may have been sent in the browser")
        window = self._window_closed()
        why = (CLOSED_REASON if window else TAB_CLOSED_REASON if self._tab_closed()
               else PAUSE_UNANSWERED_REASON)
        return _PauseClosed("needs_human", f"{CHECK_SENT_REASON}: the run stopped after the "
                                           f"pause ({why}); {what}; the run had reached: "
                                           f"{_cap(reason, 200)}", CHECK_SENT_NOTE,
                            window=window)

    def _pause_reload(self) -> None:
        """After every resume from a pause (an answer the
        person saved or added in the Apply Answers tab meanwhile): the store
        read again and the facts rebuilt from it, the entry's own PDFs kept. A store that no
        longer reads keeps the answers the run had."""
        from resume_tailor import apply_answers
        try:
            answers = apply_answers.load()
        except apply_answers.AnswerStoreError as e:
            self.log.warning("job %s: the answer store did not read after the save (%s)",
                             self.job_id, type(e).__name__)
            return
        self.r.answers = answers
        old = self.catalog
        if self.folder is None or old is None:
            return
        self.catalog = apply_facts.build(self.folder, answers=answers)
        for key in ("resume_file", "cover_letter_file"):
            if key in old.facts and key in self.catalog.facts:
                self.catalog.facts[key] = old.facts[key]

    def _verify(self, filled: list[apply_fill.Filled],
                drafts: Mapping[int, str] | None = None,
                picks: Mapping[int, tuple[str, bool]] | None = None,
                shaped: Mapping[int, tuple[str, str]] | None = None) -> list[VerifyResult]:
        """The judge checks every typed fact against the sheet. A generated
        answer (`drafts`: n -> the accepted draft) is not on the sheet, and
        the grounding gate was its check; what is left is that the box holds
        the draft, which is a string comparison here, so no question carries
        the draft. A pick (`picks`: n -> the option planned: a select, a
        radio, a tick box) is checked in code too: the read-back
        shows the option (`pick_holds`). So is a value the page reshapes
        (`shaped`: a phone's digits, a date in the box's format, an
        upload's file name, the cover letter's words, `shaped_holds`). A
        search box's match that does not name the value
        typed (`suggestion_holds`) is the judge's to read. Results keep the
        fill order."""
        if not filled:
            return []
        drafts = drafts or {}
        picks = picks or {}
        shaped = shaped or {}
        places = (place_words(self.catalog) if self.catalog is not None
                  and any(kind == "suggestion" for kind, _ in shaped.values()) else frozenset())
        by_n: dict[int, VerifyResult] = {}
        rows = []
        for f in filled:
            if f.n in shaped and shaped[f.n][0] == "suggestion" \
                    and not shaped_holds(f.value, *shaped[f.n], places):
                rows.append(f.to_dict())
            elif f.n in drafts or f.n in picks or f.n in shaped:
                ok = (_same_text(f.value, drafts[f.n]) if f.n in drafts
                      else pick_holds(f.value, *picks[f.n]) if f.n in picks
                      else shaped_holds(f.value, *shaped[f.n], places))
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
