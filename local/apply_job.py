"""`_JobRun`, the state machine for one queue entry: its set-up, the trace
and decisions, the masks and the allowed hosts, the account ledger, the
run and its loop, and every terminal path through `_finish`. Its steps
live in the bases `_PageSteps` (`apply_job_pages`), `_FormSteps`
(`apply_job_form`) and `_SubmitSteps` (`apply_job_submit`).

Split out of `apply_run`, which re-exports `_JobRun`.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TYPE_CHECKING
from urllib.parse import urlsplit

import apply_click
import apply_facts
import apply_fill
import apply_form
import apply_judge
import apply_limits
import apply_linkedin
import apply_page
import apply_pause
import apply_queue
import apply_route
import apply_send_words
import apply_sendwatch
import apply_trace
import ats_accounts
import jev
from apply_outcome import (AGGREGATOR_NOTE, AGGREGATOR_REASON, ALREADY_APPLIED_NOTE,
                           ALREADY_APPLIED_REASON, _cap, CHECK_SENT_NOTE, CHECK_SENT_REASON,
                           _closed_error, CLOSED_POSTING_REASON, CLOSED_REASON, _context_gone,
                           EASY_APPLY_NOTE, EASY_APPLY_REASON, JUDGE_DOWN_REASON, KEY_REFUSED_NOTE,
                           _no_connection, Outcome, _Parked, PASSWORD_HTTP_REASON,
                           PASSWORD_RULE_NOTE, PASSWORD_RULE_REASON, _PauseClosed, REQUEUED_NOTE,
                           SSO_NOTE, SSO_REASON, TAB_CLOSED_REASON, TENANT_REASON, _Unsent)
from apply_sites import (_aggregator, AGGREGATOR_BOARDS_MAX, AGGREGATOR_SITES, ATS_SITES,
                         _ats_tenant, content_frame_site, _easy_apply, FRONT_END_SITES, _host,
                         _insecure, _is_captcha_url, LINKEDIN_HOSTS, NAV_ATS_MAX, _platform, _site,
                         _tracker, TRACKER_SITES)
from apply_sendwatch import LateWatch, new_confirmation
from apply_page import (entry_problem, error_frames, _error_page, error_step, MALFORMED_REASON,
                        _page_closed, page_signature)
from apply_account_flow import (_Accounts, _email_first, _Inbox, _is_email_box, password_rules)
from apply_route import (ERROR_PAGE_REASON, generated_count, _PARK_STATES, UNSENT_NOTE, _usage_delta)
from apply_gate import submit_on
from apply_record import write_record
from apply_job_submit import _SubmitSteps
from apply_job_form import _FormSteps
from apply_job_pages import _PageSteps

if TYPE_CHECKING:
    from apply_run import Runner


class _JobRun(_PageSteps, _FormSteps, _SubmitSteps):
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
        self._form_password_sigs: dict[str, tuple] = {}     # that form's boxes, per site
        self._form_retyped: set[str] = set()        # sites whose form took it twice
        self.form_had_password = False  # a form page carried a password box, typed or not
        self.handed_off = False         # the page at the gate came from the account step
        self.gen_budget = apply_limits.GENERATE_MAX
        # the job's pauses for the person: a question it can ask waits
        # for the answer in place of a park
        self.pause = apply_pause.Pauser(self, _Parked)
        self.catalog: apply_facts.FactCatalog | None = None
        self.allowed: set[str] = set()
        self.ats_host = ""
        self.ats_hosts: set[str] = set()       # every admitted ATS host; matched by site
        self.ats_transition_used = False
        self._aggregator_host = ""        # the job board the tab is on
        self._aggregator_left = False     # its company-site link was followed
        self._boards: list[str] = []      # the boards read in this job, in order
        self.last_sig: tuple | None = None
        self.usage_before = jev.total_usage()
        self.start = runner.clock()
        # the wall-clock start: mail from before it is never the job's
        self.started_at = datetime.now()
        self.deadline = self.start + apply_limits.JOB_WALL_CLOCK_S
        # a malformed entry's paths are never used: `run` ends it
        self.folder = None if entry_problem(entry) else self._folder()
        self.accounts = runner.accounts if runner.accounts is not None else _Accounts(self)
        self.inbox = runner.inbox if runner.inbox is not None else _Inbox(self)
        # the trace (`apply_trace`); off until `run` starts it, so a test that
        # drives one step of a `_JobRun` needs no folder
        self.trace = apply_trace.Trace.off()
        self.browser_closed = False
        self._last_answers: Mapping[str, Any] = {}   # the page read the loop acts on
        self._facts = apply_judge.PageFacts()        # the last read page's structure
        # steps whose placeholder was waited on: (URL, the step before it's
        # signature), so a single-page wizard's steps at one URL each get one
        # wait
        self._loading_waited: set[tuple[str, tuple | None]] = set()
        # the second look's answers per page (its URL path and fields) and kind:
        # a re-read of the same page reuses them, never asks again
        self._reask_cache: dict[tuple, dict[str, Any]] = {}
        self._last_dropped: dict[int, str] = {}      # frames `_drop_foreign_controls` left out
        self._last_click: tuple[str, str] | None = None     # (text, role) of the last click
        # (page, frame, locator) of every box the master password or an
        # emailed code went into: every later screenshot masks them
        self._secret_boxes: list[tuple[Any, Any, Any]] = []
        self._secret_locators: set[tuple[int, str]] = set()     # the same boxes' digest locators
        # the sensitive boxes and the person's own, masked in the screenshots
        self._masked_boxes: list[tuple[Any, Any, Any]] = []
        self._masked_keys: set[tuple[int, int, str]] = set()
        # decisions taken before the page they belong to is recorded (a
        # settle, a consent banner, a re-read): `_new_page_record` writes them
        self._pending: list[dict[str, Any]] = []
        self._consent_clicks = 0
        self._linkedin_clicks: dict[str, int] = {}    # a LinkedIn job id -> the handler's clicks
        self._late_watch: LateWatch | None = None     # tabs the last entry click opens late
        self._job_pages: list = []      # the job's own tabs, in the order they opened
        self._watched: list = []        # the tabs `_watch` listens on
        # (a tab the run left for another it opened, its print then): the
        # page a closed tab's flow may go on in
        self._left_pages: list[tuple[Any, str, str]] = []   # (the tab, its print, its text)
        self._adopted = 0               # tabs taken over after the site closed the job's
        self._turn = 0                  # the state loop's page turn, across a takeover
        # the locators this page's fill put a value in (the gate's evidence
        # that an application is on the page)
        self._filled_here: list[tuple[int, str]] = []
        self._filled_any = False        # a value went on a page of this job (the account step too)
        # this page's fill as read back (n -> `apply_fill.Filled`), and the text
        # boxes it left alone with their values before it: the re-read after
        # the fill compares against both
        self._last_filled: dict[int, apply_fill.Filled] = {}
        self._idle: list[tuple[Any, str | None]] = []
        self._refilled: set[int] = set()    # fields put back once after the page changed them
        self._drafts_by_question: dict[str, str] = {}   # accepted drafts, this job
        self._options_seen: dict[tuple, list[str]] = {}  # A page's listbox options
        self._repaired = False              # the last `_repair` acted on the page
        self._submit_repairs = 0            # repairs after the form refused the submit
        self._gate_repairs = 0              # repairs of what the gate read invalid, this page
        # (the form's fields, a message no control names) -> the fields the
        # judge named for it on that form: never offered for it again
        self._error_tried: dict[tuple, set[str]] = {}
        # a message's words -> (the field only the judge named for it, which
        # had no answer; the form's words): the park when the rounds end with
        # the message still shown; this page's
        self._spared: dict[str, tuple[str, str]] = {}
        self._code_sent = False         # the code step clicked on (a code can finish a send)
        # the code step clicked on after the submit click or once the
        # application's answers went on a page: a code the site may have held
        # the application for (an account's own code sends none of it)
        self._code_may_send = False
        # submit mode clicked an advance whose words a last step uses
        # ("Confirm", "Complete", "Done"): it may have sent
        self._final_advance = False
        # submit mode read an emailed link's page once the application's
        # answers went on the site or after the submit click: the link may be
        # the step that sends it
        self._link_may_send = False
        # the page moved on while the run waited for the person: they may
        # have sent it in the browser (`_pause_moved`)
        self._pause_sent = False
        # the person went on from a page with no send button during a pause
        # (`_pause_moved`): they may have sent it on a later
        # step, so a judge down afterwards never hands the job back to the
        # queue (`_requeue_unless_moved_on`). Read nowhere else
        self._person_moved_on = False
        # a send that never reached the site and nothing else left (`_Unsent`):
        # what the watch saw is no possible send
        self._unsent = False
        self._links_followed: set[str] = set()     # sites whose emailed link was opened
        self._send_watch: apply_sendwatch.SendWatch | None = None     # the requests after the submit click
        self._sent_when = "after the submit click"      # or "during the CAPTCHA wait" (m5)
        self._before_submit: dict[str, Any] | None = None   # the page just before it
        self._submit_at: tuple[int, str] | None = None      # the submit button's locator
        # a tab's last main-frame load the network dropped, (address, method,
        # error), by the tab (held with the tab); a new tab's first load,
        # which Playwright names no tab for yet, in `_unplaced_loads` with its
        # request (Chrome's error page; `_held_load`)
        self._failed_loads: dict[int, tuple[Any, tuple[str, str, str]]] = {}
        self._unplaced_loads: list[tuple[Any, tuple[str, str, str]]] = []
        self._load_listener: Callable[[Any], None] | None = None
        # the ATS hosts a tab's main frame was sent to, in order, redirects
        # the run never saw land included (`_pin_first_tenant`)
        self._nav_ats: list[str] = []
        self._nav_listener: Callable[[Any], None] | None = None
        self._error_retried: set[str] = set()      # addresses loaded once more after it

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
        trace's URL chain, and `page` among the job's tabs. A page already
        watched keeps the listeners it has."""
        if not any(page is p for p in self._job_pages):
            self._job_pages.append(page)
        if any(page is p for p in self._watched):
            return
        self._watched.append(page)
        try:
            main = page.main_frame
            page.on("framenavigated",
                    lambda frame: frame == main and frame.url != "about:blank"
                    and self.trace.nav(frame.url))
        except Exception:       # noqa: BLE001  (a page double)
            pass
        # the page's own sends from its first request: a click's evidence
        # leaves them out
        apply_click.watch_requests(page)

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

    def _mask_box(self, locator: tuple[int, str]) -> None:
        """Note a box the screenshots mask that is no secret of the run's:
        a sensitive question (a government ID, a birthdate, bank or card
        details), or a box the person filled in themselves."""
        key = (id(self.page), int(locator[0]), str(locator[1]))
        if key in self._masked_keys:
            return
        try:
            frame = apply_form.frames(self.page)[int(locator[0])]
            self._masked_boxes.append((self.page, frame, frame.locator(str(locator[1]))))
            self._masked_keys.add(key)
        except Exception:       # noqa: BLE001  (a page double, a frame that went away)
            pass

    def _mask_sensitive(self, digest: apply_form.FormDigest) -> None:
        """Every box of `digest` that asks a sensitive question
        (`apply_judge.is_sensitive_field`), for the screenshots' masks:
        whatever the page or the person put in it stays out of them."""
        for f in digest.fields:
            if apply_judge.is_sensitive_field(f.label, f.id_or_name):
                self._mask_box(f.locator)

    def _secret_masks(self, page) -> list:
        """The noted secret and masked boxes on `page` whose frames are still
        there."""
        out = []
        for owner, frame, loc in [*self._secret_boxes, *self._masked_boxes]:
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
        """LinkedIn (any `*.linkedin.com` host, the country subdomains
        too) and the inbox by its exact host (its domain carries other
        people's content: `docs.google.com`, `forms.office.com`, a Google
        sign-in frame); the admitted ATS by its whole site (`login.icims.com`
        next to `careers-gtsx.icims.com`); a known ATS platform
        (`ATS_SITES`) anywhere, until the job's own account on one is known
        (`_tenant_departure`). The master password never goes to LinkedIn
        (`_password_ok`)."""
        host = _host(host)
        if host in self.allowed or apply_linkedin.is_linkedin(host):
            return True
        if self._tenant_departure(host):
            return False
        site = _site(host)
        return site in ATS_SITES or any(site == _site(h) for h in self.ats_hosts)

    def _pinned_hosts(self) -> list[str]:
        """The job's own hosts on an ATS platform: its company's account there,
        from the queue entry, from `_admit_ats_transition`, or the first one
        the job's page landed on (`_pin_first_tenant`). A career-site front
        end (`FRONT_END_SITES`) is never one: its Apply hands the job on."""
        return sorted(h for h in self.ats_hosts
                      if _site(h) in ATS_SITES and _site(h) not in FRONT_END_SITES)

    def _tenant_departure(self, url_or_host: str) -> str:
        """The job's own ATS host that `url_or_host` departs from, "" when it
        does not. Once the job's account on an ATS platform is known
        (`_pinned_hosts`), a host on a platform is the application's only
        when it is one of the job's hosts, a host every company on the
        job's platform shares (`login.icims.com`, `wd5.myworkdaysite.com`),
        or a host naming the same company there (`careers-gtsx.icims.com`
        beside `gtsx.icims.com`). Another company's account on the same
        platform, or another platform, departs from it."""
        host = _host(url_or_host)
        site = _site(host)
        if site not in ATS_SITES or host in self.ats_hosts:
            return ""
        pinned = self._pinned_hosts()
        if not pinned:
            return ""
        platform = _platform(site)
        mine = [h for h in pinned if _site(h) in platform]
        if not mine:
            return pinned[0]
        tenant = _ats_tenant(host)
        ours = {_ats_tenant(h) for h in mine} - {""}
        return mine[0] if tenant and ours and tenant not in ours else ""

    def _tenant_park(self, host: str, pinned: str) -> _Parked:
        return _Parked("needs_human", f"{TENANT_REASON}: the page moved from {pinned} to "
                                      f"{host}")

    def _pin_first_tenant(self, url: str) -> None:
        """The first ATS host the job's tab was sent to (`_nav_ats`, a
        redirect it passed through too) or else `url`'s, when no step named
        the job's account on a platform yet, becomes it: another company's
        account met later, a redirect onward included, departs from it
        (`_tenant_departure`). A career-site front end met before then is
        kept as one of the job's hosts and pins nothing."""
        if self._pinned_hosts():
            return
        for host in [*self._nav_ats, _host(url)]:
            if not host or _site(host) not in ATS_SITES:
                continue
            self.ats_hosts.add(host)
            if _site(host) not in FRONT_END_SITES:
                break
        else:
            return
        self._decide("tenant_pinned", f"the application's platform account is on {host}")

    def _check_host(self, url: str) -> None:
        if _error_page(url):
            # Chrome's error page is a load the network dropped, never a site
            # the flow left for: the job's tab gets its one retry
            page = self.page
            if page is None or not _error_page(str(getattr(page, "url", ""))):
                raise _Parked("needs_human", f"{ERROR_PAGE_REASON}: a tab shows Chrome's "
                                             f"error page (a load the network dropped)")
            self._recover_error_page(page)
            url = str(page.url)
        host = _host(url)
        pinned = self._tenant_departure(host) if host else ""
        if pinned:
            raise self._tenant_park(host, pinned)
        if host and not self._allowed_site(host):
            raise _Parked("needs_human", f"left the allowed sites: {host}")

    # -- Chrome's error page ----------------------------------------------------------

    def _listen_loads(self) -> None:
        """Note every main-frame load of the job's context that the network
        dropped (its address, method and error): Chrome's error page, which
        the tab shows then, names none of them."""
        def _failed(request) -> None:
            try:
                if not request.is_navigation_request():
                    return
                failure = str(request.failure or "")
                if "ERR_ABORTED" in failure:
                    return          # a load another one replaced: no error page follows
                row = (str(request.url), str(request.method).upper(), failure)
                try:
                    frame = request.frame
                except Exception:   # noqa: BLE001  (a new tab's first load: no frame yet)
                    self._unplaced_loads.append((request, row))
                    return
                if frame.parent_frame is None:
                    self._failed_loads[id(frame.page)] = (frame.page, row)
            except Exception:       # noqa: BLE001  (a request that cannot be read)
                pass
        try:
            self.ctx.on("requestfailed", _failed)
            self._load_listener = _failed
        except Exception:       # noqa: BLE001  (a context double)
            self._load_listener = None

        def _sent(request) -> None:
            try:
                if not request.is_navigation_request() or request.frame.parent_frame is not None:
                    return
                host = _host(str(request.url))
                if (host and _site(host) in ATS_SITES and host not in self._nav_ats
                        and len(self._nav_ats) < NAV_ATS_MAX):
                    self._nav_ats.append(host)
            except Exception:       # noqa: BLE001  (a new tab's first load names no frame yet)
                pass
        try:
            self.ctx.on("request", _sent)
            self._nav_listener = _sent
        except Exception:       # noqa: BLE001  (a context double)
            self._nav_listener = None

    def _unlisten_loads(self) -> None:
        for event, attr in (("requestfailed", "_load_listener"), ("request", "_nav_listener")):
            fn = getattr(self, attr)
            setattr(self, attr, None)
            if fn is not None:
                try:
                    self.ctx.remove_listener(event, fn)
                except Exception:   # noqa: BLE001  (the context is gone)
                    pass

    def _held_load(self, page) -> tuple[str, str, str] | None:
        """The load that failed in `page`, taken from what is held. A new
        tab's first load names no tab when it
        fails; by the tab's error page Playwright ties that load to it, so
        each held first load is placed now: `page`'s own is taken (the
        newest), one of a tab since closed is dropped, and one of another
        open tab goes to that tab. A load still tied to no tab belongs to a
        tab never reported (one closed at once), so it stays held and is
        never taken: a stale address is never loaded in `page`. Rows of
        closed tabs are dropped. A load that failed once the tab was known
        is newer than its first and wins. None when no load is known for
        `page`."""
        for key, (tab, _) in list(self._failed_loads.items()):
            if _page_closed(tab):
                del self._failed_loads[key]
        own = self._failed_loads.pop(id(page), None)
        later = own[1] if own is not None and own[0] is page else None
        try:
            main = page.main_frame
        except Exception:       # noqa: BLE001  (a closed tab)
            main = None
        first = None
        held: list[tuple[Any, tuple[str, str, str]]] = []
        for request, row in self._unplaced_loads:
            try:
                frame = request.frame
                tab = frame.page
            except Exception:   # noqa: BLE001  (its tab not reported yet, or never)
                held.append((request, row))
                continue
            if main is not None and frame is main:
                first = row                 # the newest held for this tab wins
            elif not _page_closed(tab) and getattr(frame, "parent_frame", None) is None:
                self._failed_loads.setdefault(id(tab), (tab, row))
        self._unplaced_loads = held
        return later if later is not None else first

    def _recover_error_page(self, page, *, transition: bool = False) -> bool:
        """A tab on Chrome's own error page (`chrome-error://chromewebdata/`)
        reads as a load the network dropped, never as a site the flow
        left for: the address that failed is loaded once more after
        `GOTO_RETRY_S` when it is a GET on the allowed sites. A POST, PUT or
        PATCH is never sent again, and after the submit click neither is a
        GET that may have carried the send (`SendWatch.carried_get`): at
        most one send per job, so a GET the click caused (up to and with its
        first navigation) or one to the submit form's action is never loaded
        again, and a later GET only when the answer of a send to the
        application's sites that came back led to it (`SendWatch.led_on`:
        its HTTP redirect, or an address with no query). A send that never reached the site
        (`_no_connection`) and was the one request seen parks as nothing
        sent (`_Unsent`). A retry that lands on the error page again parks, as does an
        error page whose address is unknown (`_held_load`). An address off
        the allowed sites parks as the site it names, but with `transition`:
        the page an Apply or a redirect led to, which `_admit_ats_transition`
        judges once it has loaded. True when the address was loaded again,
        False when the tab shows no error page."""
        try:
            now = str(page.url)
        except Exception:       # noqa: BLE001  (a closed tab: the caller's handling)
            return False
        if not _error_page(now):
            return False
        failed = self._held_load(page)
        if failed is None:
            raise _Parked("needs_human", f"{ERROR_PAGE_REASON}: the tab shows Chrome's error "
                                         f"page and the address that failed is not known")
        url, method, failure = failed
        host = _host(url)
        if host and not transition and not self._allowed_site(host):
            raise _Parked("needs_human", f"left the allowed sites: {host}")
        bare = apply_sendwatch.SendWatch._bare(url)
        what = f"{method} {_cap(bare, 120)} failed on the network ({_cap(failure, 60)})"
        watch = self._send_watch
        row = f"{method} {bare}"
        action = str((self._before_submit or {}).get("action") or "")
        carried = watch is not None and (method != "GET" or watch.carried_get(row, action, url))
        if self.submit_clicked and carried and _no_connection(failure) and watch.only(row):
            # the send never reached the site and nothing else left: the job
            # is no possible send, and the load is still never made again
            self.submit_clicked = False
            self._unsent = True
            self._decide("after_submit", f"{what}; no connection was made and no other request "
                                         f"left, so nothing was sent")
            raise _Unsent("needs_human", f"{ERROR_PAGE_REASON}: {what}; no connection was "
                                         f"made, so nothing was sent", UNSENT_NOTE)
        if method != "GET":
            raise _Parked("needs_human", f"{ERROR_PAGE_REASON}: {what}; a {method} is never "
                                         f"sent again")
        if self.submit_clicked and carried:
            raise _Parked("needs_human", f"{ERROR_PAGE_REASON}: {what}; that load carried the "
                                         f"send, so it is never loaded again")
        if not self.submit_clicked and (self._final_advance or self._code_may_send
                                        or self._link_may_send or self._pause_sent):
            # a last-worded step, a code or link the site may have held the
            # application for, or a pause the person may have sent it in:
            # the load may carry that send, so it is never made again
            raise _Parked("needs_human", f"{CHECK_SENT_REASON}: {what} after a step that may "
                                         f"have sent the application; it is never loaded "
                                         f"again", CHECK_SENT_NOTE)
        if url in self._error_retried:
            raise _Parked("needs_human", f"{ERROR_PAGE_REASON}: {what} again after one retry")
        self._error_retried.add(url)
        self._decide_next("error_page_retry", f"{what}; the tab showed Chrome's error page; "
                                              f"one retry of the GET", url=_cap(bare, 160))
        self.log.info("job %s: Chrome's error page after %s; one retry", self.job_id, what)
        apply_page._error_page_up(page, apply_limits.GOTO_ERROR_PAGE_S)
        page.wait_for_timeout(int(apply_limits.GOTO_RETRY_S * 1000))
        left_ms = int(max(1.0, self.deadline - self.r.clock()) * 1000)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=min(apply_limits.GOTO_TIMEOUT_MS, left_ms))
        except Exception as e:      # noqa: BLE001  (Playwright's Error and TimeoutError)
            if _closed_error(e):
                raise
            # a retry the network dropped again shows as the error page below
        if _error_page(str(page.url)):
            self._failed_loads.pop(id(page), None)
            raise _Parked("needs_human", f"{ERROR_PAGE_REASON}: {what}, and again after one "
                                         f"retry")
        return True

    def _related_hosts(self, host: str) -> list[str]:
        """The job's own ATS hosts on `host`'s site, other than `host`: a
        shared sign-in host (`login.icims.com`) finds the tenant's account
        by them."""
        site, own = _site(host), _host(host)
        return sorted(h for h in self.ats_hosts if _site(h) == site and _host(h) != own)

    def _account_for(self, host: str) -> dict | None:
        """The ledger's account for `host`: by the host, by its tenant, or
        by the job's own hosts on its site (`ats_accounts.lookup`)."""
        return ats_accounts.lookup(host, related=self._related_hosts(host))

    def _record_account(self, host: str, email: str, **extra: Any) -> None:
        """The account made or signed in to on `host`, in the ledger: under
        `host`, or, when `host` names no tenant and a job host on its site
        does, under that host (a sign-in host every tenant shares must never
        hand one company's account to another). A ledger that cannot be
        read or kept aside (`ats_accounts.record` raises OSError) is logged
        and the application goes on: the account itself was made."""
        target = host
        if not ats_accounts.tenant_key(host):
            named = [h for h in self._related_hosts(host) if ats_accounts.tenant_key(h)]
            if named:
                target = named[0]
        try:
            ats_accounts.record(target, email, **extra)
        except OSError as e:
            self.log.warning("job %s: the account on %s was not written to the ledger (%s: %s)",
                             self.job_id, target, type(e).__name__, _cap(str(e), 160))

    def _password_frame_ok(self, frame_url: str) -> bool:
        """`ats_accounts.fill_password`'s check of the frame holding the box,
        at the moment of the fill: `_password_ok` on its URL. A blank or
        srcdoc frame (`about:`) has no address of its own and was checked
        through its parent's (`_frame_address`)."""
        url = str(frame_url or "")
        if url.startswith("about:"):
            return True
        return self._password_ok(url)

    def _check_password_rules(self, digest: apply_form.FormDigest, host: str) -> None:
        """Before the master password makes an account, the rules
        the screen states (`password_rules`) against the stored password,
        counted in `ats_accounts` (never the value here): one it misses
        parks, nothing typed. The park and the trace give how many rules it
        misses, never which: those would describe the stored password."""
        rules, said = password_rules(digest)
        if not rules:
            return
        unmet = ats_accounts.unmet_rules(rules) or []
        self._decide("password_rules", f"the screen's password rules: {_cap(said, 160)}",
                     rules=sorted(rules), unmet=len(unmet))
        if unmet:
            raise _Parked("needs_human", f"{PASSWORD_RULE_REASON} on {host}: it misses "
                                         f"{len(unmet)} of the rules the site states (the site "
                                         f"asks: {_cap(said, 160)})", PASSWORD_RULE_NOTE)

    def _password_ok(self, url_or_host: str) -> bool:
        """May the master password be typed on `url_or_host`? Only on the
        application itself: the admitted ATS site or a known ATS platform
        (the job's own account there once it is known, `_tenant_departure`),
        never on LinkedIn's or the inbox provider's domain, and never at an
        address that reaches its site unencrypted (`_insecure`: an `http`
        URL off this machine). The callers pass the page's, the frame's and
        the form's URLs, so the scheme is checked with the host."""
        if _insecure(url_or_host):
            return False
        host = _host(url_or_host)
        site = _site(host)
        if not site or site == _site(LINKEDIN_HOSTS[0]):
            return False
        if site == _site(str(self.r.run_context().get("inbox_url") or "")):
            return False
        if site in TRACKER_SITES or site in AGGREGATOR_SITES:
            return False            # A job board or a tracker is never the application
        if self._tenant_departure(host):
            return False
        return site in ATS_SITES or any(site == _site(h) for h in self.ats_hosts)

    def _extract(self, page=None) -> apply_form.FormDigest:
        """`apply_form.extract` of `page` (the job's page by default), a child
        frame read first only when it is the page's site, an ATS platform or
        the admitted application (`content_frame_site`)."""
        page = page if page is not None else self.page
        apply_click.watch_requests(page)
        return apply_form.extract(page, content_site=lambda url: content_frame_site(
            url, str(page.url), self.ats_hosts))

    def _frame_url(self, frames: list, idx: int) -> str:
        """The URL a frame's controls answer to; a blank or srcdoc frame takes
        its parent's."""
        return self._frame_address(frames[idx])

    def _box_frame(self, page, locator, frames: list):
        """The frame a box's locator acts in (`apply_form.resolve_frame`: by
        the URL the frame had at the read, then by its index), so the host
        checked is the host typed on; the frame at its index when that
        cannot be told, None when it is gone."""
        try:
            return apply_form.resolve_frame(page, locator)
        except Exception:       # noqa: BLE001  (a page double, a frame gone)
            idx = int(locator[0])
            return frames[idx] if 0 <= idx < len(frames) else None

    def _secret_refusal(self, urls: Iterable[str]) -> str:
        """Why the master password may not go to a box answering to `urls`
        (the page's, the box's frame's, its form's action), "" when it may:
        an unencrypted address (`_insecure`) first, then a host off the
        application (`_password_ok`)."""
        urls = [str(u) for u in urls if str(u or "").strip()]
        plain = next((u for u in urls if _insecure(u)), "")
        if plain:
            return f"{PASSWORD_HTTP_REASON} ({_host(plain)})"
        outside = sorted({_host(u) for u in urls if _host(u) and not self._password_ok(u)})
        return f"a password box on {outside[0]}, outside the application site" if outside \
            else ""

    def _moved_box(self, page, frame) -> str:
        """Read just before the master password is typed: why the page or the
        box's frame (where the fill acts) may no longer take it, now that
        either may have moved since the check (`_secret_refusal`); "" when
        both still may."""
        urls = [str(getattr(page, "url", "") or "")]
        if frame is not None:
            urls.append(self._frame_address(frame))
        why = self._secret_refusal(urls)
        return f"the password box moved before the password was typed: {why}" if why else ""

    def _frame_address(self, frame) -> str:
        """The URL `frame`'s controls answer to; a blank or srcdoc frame takes
        its parent's."""
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
        widget): they are never judged, filled or clicked, and the
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
        return dataclasses.replace(       # the rest as read: its dialog too
            digest, fields=[f for f in digest.fields if int(f.locator[0]) not in dropped],
            buttons=[b for b in digest.buttons if int(b.locator[0]) not in dropped])

    def _discover_listbox_options(self, digest: apply_form.FormDigest) -> None:
        """Read choices rendered only after a listbox is opened, before
        planning. A page read again (a step the form sent back) takes the
        options its listboxes showed before, by the page's path, the
        control's locator and its label, and opens none of them again."""
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
            except apply_send_words.PopupRefused as e:
                # its own words send: never opened, left unanswered
                control.refused = str(e)
                self._decide("popup_refused", f"a popup was left unopened: {e}",
                             field=control.n)
            except Exception as e:      # noqa: BLE001  (a widget may detach while opening)
                self.log.info("job %s: listbox %r did not expose options: %s",
                              self.job_id, control.label, type(e).__name__)

    def _admit_ats_transition(self, url: str, source_url: str) -> None:
        """Record where the application lives. A known ATS platform is
        admitted wherever the flow met it, until the job's own account on one
        is known (`_tenant_departure`): from then on another company's
        account, or another platform, is admitted only as where LinkedIn's
        Apply or a job board's company link led, and any other step that
        lands there parks. Any other site is admitted only as the one
        destination LinkedIn's Apply led to. Chrome's error page is never
        one: the caller's retry (`_recover_error_page`) comes first."""
        if _error_page(url):
            raise _Parked("needs_human", f"{ERROR_PAGE_REASON}: a tab shows Chrome's error "
                                         f"page (a load the network dropped)")
        host = _host(url)
        if not host or host in LINKEDIN_HOSTS or _site(host) == _site(LINKEDIN_HOSTS[0]):
            return
        if _tracker(host):
            # a hop that never moved on: never the destination
            raise _Parked("needs_human", f"the tracker hop ({host}) did not move on to the "
                                         f"company's site")
        from_board = bool(self._aggregator_host and not self._aggregator_left
                          and _host(source_url) == self._aggregator_host)
        pinned = self._tenant_departure(host)
        if pinned:
            if not (from_board or apply_linkedin.is_linkedin(source_url)):
                raise self._tenant_park(host, pinned)
        elif any(_site(host) == _site(h) for h in self.ats_hosts):
            return
        if _site(host) not in ATS_SITES:
            if (from_board and _aggregator(host) and _site(host) != _site(self._aggregator_host)
                    and any(_site(host) == _site(b) for b in self._boards)):
                # a board's company link back to a board already read: the
                # boards link to each other and to no company
                # site, and reading them again would only loop
                chain = " -> ".join([*self._boards, host])
                raise _Parked("needs_human", f"{AGGREGATOR_REASON} on {host}: the job boards "
                                             f"link to each other ({chain}) and to no company "
                                             f"site", AGGREGATOR_NOTE)
            if host in self.allowed:
                return
            from_linkedin = not self.ats_transition_used and apply_linkedin.is_linkedin(source_url)
            if _aggregator(host) and from_linkedin:
                # a job board LinkedIn's Apply led to: the tab may
                # stay there and follow its company-site link once; nothing
                # is ever filled or signed in on it (`_password_ok`)
                self.allowed.add(host)
                self._aggregator_host = host
                self._boards = [host]
                self.ats_transition_used = True
                self._decide_next("aggregator", f"LinkedIn's Apply led to a job board ({host})")
                return
            if _aggregator(host) and from_board:
                # a board's company link that lands on another board: that
                # board is read the same way, for its own company
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
        # a queue lock held past its wait, or an entry deleted meanwhile,
        # costs the queue its note of the site; the job goes on
        self.r._queue_write(self.job_id, "ats update", lambda: apply_queue.update(
            self.job_id, path=self.r.queue_path,
            ats={"domain": host, "system": inferred["system"]}),
            "the entry keeps its earlier site")
        ats = self.entry.get("ats") or {}
        self.entry["ats"] = {**ats, "domain": host, "system": inferred["system"]}
        self.allowed.add(host)
        self.ats_hosts.add(host)
        self.ats_host = host
        self.ats_transition_used = True

    # -- the run ----------------------------------------------------------------------------

    def _prepare(self) -> str:
        """The sheet's answers refreshed from the store, the catalog, the
        PDFs from the entry's artifacts, the allowlist; returns the apply
        URL. Raises `_Parked("failed", ...)` when the sheet or the URL is
        missing."""
        if self.folder is None or not (self.folder / "apply.md").exists():
            raise _Parked("failed", "no apply.md")
        answers = self.r.answers if self.r.answers is not None else self.r.load_answers()
        self._refresh_sheet(answers)
        self.catalog = apply_facts.build(self.folder, answers=answers)
        self._apply_artifacts()
        self._build_allowlist()
        url = str(self.entry.get("apply_url") or "")
        if not url:
            raise _Parked("failed", "no apply_url")
        return url

    def _refresh_sheet(self, answers: list[dict]) -> None:
        """The sheet's Standard answers and Address re-rendered from the
        store before the facts are read (FL-2), so the sheet a person opens
        shows what this run may fill. A refresh that fails, or a sheet
        without those sections, is logged and the job goes on: the facts
        come from the store either way."""
        try:
            from resume_tailor import apply_data
            if not apply_data.refresh_answer_sections(self.folder, answers):
                self.log.warning("job %s: the sheet's answer sections were not refreshed "
                                 "(no apply.md, or its Standard answers or signature "
                                 "heading is missing)", self.job_id)
        except Exception as e:      # noqa: BLE001  (the sheet is a view; the job goes on)
            self.log.warning("job %s: the apply.md answers were not refreshed (%s)",
                             self.job_id, type(e).__name__)

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
                problem = entry_problem(self.entry)
                if problem:
                    # The job ends here and the drain goes on; no
                    # trace or record goes where a malformed path points
                    raise _Parked("failed", f"{MALFORMED_REASON}: {problem}")
                self._start_trace()
                self._listen_loads()
                self.log.info("job %s: start (%s)", self.job_id, self.entry.get("apply_url", ""))
                if _easy_apply(self.entry):
                    # the user applies to an Easy Apply job on LinkedIn in
                    # person: no page is opened, nothing is read or clicked
                    self._decide("easy_apply", "the queue entry is an Easy Apply job")
                    raise _Parked("needs_human", EASY_APPLY_REASON, EASY_APPLY_NOTE)
                url = self._prepare()
                self._trace("start", url=url, submit=submit_on(self.r.settings))
                self._check_host(url)
                self.page = self.ctx.new_page()
                self._watch(self.page)
                self._open(url)
                self._drive()
                raise _Parked("needs_human",
                              f"page budget exhausted ({apply_judge.MAX_PAGES} pages"
                              f"{self._last_states()})")
            except _Parked as p:
                self._trace("park", status=p.status, reason=p.reason)
                if not p.reason.startswith("confirmation page"):
                    # the site closed the job's tab after the send: the tab
                    # it handed back to may show the confirmation
                    done = self._confirmed_elsewhere()
                    if done is not None:
                        return done
                if isinstance(p, _PauseClosed):
                    # a close during a pause's wait is its own check-whether
                    # end, whatever is found of the window now; a closed window still stops the drain
                    self.browser_closed = p.window or self._window_closed()
                    return self._finish(p.status, p.reason, p.tab_note)
                if p.status != "submitted":
                    # whatever the loop made of it, the window or the tab went
                    # away under it
                    closed = self._window_closed()
                    gone = not closed and self._tab_closed()
                    if (closed or gone) and self._maybe_sent():
                        # Something may have been sent; the
                        # job keeps the check-sent end and its note, never the
                        # closed one whose note is a Re-queue
                        return self._stopped_after_send(
                            CLOSED_REASON if closed else TAB_CLOSED_REASON,
                            f"; the run had reached: {_cap(p.reason, 200)}", window=closed)
                    if closed:
                        return self._closed(f"the run had reached: {p.reason}")
                    if gone:
                        return self._tab_gone(f"the run had reached: {p.reason}")
                    if self._judge_down() and not self._maybe_sent():
                        # a park reached after the judge went down (a step
                        # that noted the error and went on) is no answer
                        return self._requeue_unless_moved_on("")
                return self._finish(p.status, p.reason, p.tab_note)
            except Exception as e:      # noqa: BLE001  (the entry must leave in_progress)
                # the context or the browser gone is a closed window; a closed
                # page with the context alive (the user closed the job's tab,
                # the site closed its own popup) ends this job only
                closed = self._window_closed()
                tab = not closed and (_closed_error(e) or self._tab_closed())
                # The reason names the error's type and the run's step,
                # never its message (a Playwright call log carries selectors,
                # the page's words and the values typed); the traceback's
                # frames go to the trace and the job's log, both local
                step = error_step(e)
                frames = error_frames(e)
                self._trace("exception", error=type(e).__name__, step=step, closed=closed,
                            tab_closed=tab, frames=frames)
                if closed or tab:
                    self.log.warning("job %s: the browser %s closed (%s)", self.job_id,
                                     "window" if closed else "tab", type(e).__name__)
                elif not isinstance(e, jev.JudgeOutage):
                    self.log.error("job %s: unexpected error %s at %s; traceback (the message "
                                   "left out):\n  %s", self.job_id, type(e).__name__, step,
                                   "\n  ".join(frames))
                down = "" if closed or tab else self._judge_down()
                if down and not self._maybe_sent():
                    return self._requeue_unless_moved_on(step)
                done = self._confirmed_elsewhere() if tab else None
                if done is not None:
                    return done
                done = self._confirmed_here() if down and self.submit_clicked else None
                if done is not None:
                    return done
                if self._maybe_sent():
                    # the submit click, or a code or link step the site may
                    # have held the application for: the
                    # job is never handed back to the queue
                    why = (CLOSED_REASON if closed else TAB_CLOSED_REASON if tab
                           else f"{JUDGE_DOWN_REASON}: {down} at {step}" if down
                           else f"{type(e).__name__} at {step}")
                    watch = self._send_watch
                    if self.submit_clicked and watch is not None and watch.sent:
                        self.browser_closed = closed
                        return self._finish("submitted", f"submitted (unconfirmed): {why} "
                                                         f"(after {_cap(watch.first(), 120)})")
                    # no request to the application's sites was seen: the
                    # run claims no send, and the job is never re-queued on
                    # its own. A code, link or final-worded step before the
                    # submit click runs with no request watch, so nothing
                    # seen says nothing there
                    left = (f"; a request left: {_cap(watch.first(), 120)}"
                            if watch is not None and watch.any()
                            else "; no request was seen leaving" if watch is not None
                            else "; the run was not watching requests at this step")
                    return self._stopped_after_send(why, left, window=closed)
                if closed:
                    return self._closed(type(e).__name__)
                if tab:
                    return self._tab_gone(type(e).__name__)
                # a service's status stays in (`jev.error_kind`): a request the
                # judge rejected (a 400, one too large) ends this job only
                return self._finish("failed", f"{jev.error_kind(e)} at {step} "
                                              f"(page {len(self.pages)})")
            except BaseException as e:
                # Ctrl+C in a drain, or a queue write that
                # failed twice. The entry leaves in_progress: a possible send
                # waits for the person to check it; anything else waits for
                # the person too, and the drain stops (re-raised)
                self._trace("exception", error=type(e).__name__, step=error_step(e))
                try:
                    if self._maybe_sent():
                        self._stopped_after_send(f"{type(e).__name__} at {error_step(e)}",
                                                 "; the run was interrupted")
                    else:
                        self._finish("needs_human", f"the run was interrupted "
                                                    f"({type(e).__name__} at {error_step(e)})")
                except BaseException:   # noqa: BLE001  (the first interruption is the one raised)
                    self.log.warning("job %s: the interrupted job's end could not be written",
                                     self.job_id)
                raise
        finally:
            self._unlisten_loads()
            self.trace.close()

    def _stopped_after_send(self, why: str, more: str = "", *, window: bool = False) -> Outcome:
        """The run stopped (`why`: the window or the tab closed, the judge
        down, an error) after a step that may have sent the application
        (`_maybe_sent`): the person checks it, and the job is never handed
        back to the queue. `more` follows the parenthesis (what left, what
        the run had reached); a closed window still stops the drain."""
        if window:
            self.browser_closed = True
        return self._finish("needs_human", f"{CHECK_SENT_REASON}: the run stopped after "
                                           f"{self._sent_step()} ({_cap(why, 160)}){more}",
                            CHECK_SENT_NOTE)

    def _sent_step(self) -> str:
        """The step `_maybe_sent` stands on, as the park names it."""
        watch = self._send_watch
        if self.submit_clicked or (watch is not None and not self._unsent and watch.any()):
            return "the submit click"
        if self._link_may_send:
            return "the link step"
        if self._code_may_send:
            return "the code step"
        if self._pause_sent and not self._final_advance:
            return "the pause"
        return "the final-worded step"

    def _judge_down(self) -> str:
        """The open breaker's error class and status (`jev.Guarded.down`), or ""."""
        down = getattr(self.r.jev, "down", "")
        return down if isinstance(down, str) else ""

    def _maybe_sent(self) -> bool:
        """The submit click landed, a code step went on once the application
        may have been held for it (`_code_may_send`), submit mode read an
        emailed link's page at such a point (`_link_may_send`), submit mode
        clicked a final-worded advance once the answers were on the site
        (`_final_advance`), or the submit's watch saw a
        request leave that `_Unsent` did not rule out (a reset
        `submit_clicked` after validation errors):
        something may have been sent, so the job is never handed back to the
        queue. An account's own code before any of the application's answers
        went on a page sends none of it. A page that moved
        on while the run waited for the person (`_pause_sent`) may have been
        sent in the browser."""
        if self.submit_clicked or self._code_may_send or self._link_may_send \
                or self._final_advance or self._pause_sent:
            return True
        watch = self._send_watch
        return bool(watch is not None and not self._unsent and watch.any())

    def _requeue_unless_moved_on(self, step: str) -> Outcome:
        """The judge went down with nothing the run knows of sent: the job
        goes back to the queue (`_requeued`), unless the person went on from
        a page with no send button during a pause (`_person_moved_on`).
        They may have sent it on a later step, so the job parks
        with the check-whether note and is never re-queued on its own. The
        judge is still down, so the drain stops (`Outcome.judge_down`, set
        here whatever `_finish` reads of the breaker)."""
        if not self._person_moved_on:
            return self._requeued(step)
        at = f" at {step}" if step else ""
        out = self._finish("needs_human", f"{CHECK_SENT_REASON}: the run stopped after the "
                                          f"pause ({JUDGE_DOWN_REASON}: {self._judge_down()}"
                                          f"{at}); you went on to another step in the "
                                          "browser during the pause", CHECK_SENT_NOTE)
        out.judge_down = True
        return out

    def _requeued(self, step: str) -> Outcome:
        """The judge went down under the job before anything could
        have been sent. The entry goes back to `queued`
        (`apply_queue.unclaim`), the job's tabs close, and the drain stops
        (`Outcome.judge_down`), behind the others, so the next drain starts
        on another job. After an error the service may get over (a busy
        status, a 5xx, a timeout, a dropped connection) the attempt the claim
        counted is taken back; the outage counts only when the judge
        answered earlier in the drain (`jev.Guarded.answers`), so an outage
        for every job never does, and only for an error
        the job's request may have caused (`jev.Guarded.request_fault`: a
        5xx other than 503 and 529 or a timeout; never a busy or overloaded
        service, a long Retry-After or a dropped connection, most
        often the network's). The job's
        `OUTAGES_MAX`th counted outage parks it instead (`OUTAGES_PARKED`,
        inside the policy), so a failure its own request causes never holds
        the queue's head. After a refused key (`jev.Guarded.refused`) the
        attempt stays counted and no outage is. No record
        is written for a re-queue (the job has not ended); the trace ends
        with the reason."""
        at = f" at {step}" if step else ""
        answers = getattr(self.r.jev, "answers", 0)
        answers = answers if isinstance(answers, int) else 0
        after = f" after {answers} answer{'' if answers == 1 else 's'} in this drain" \
            if answers else ""
        down = f"{JUDGE_DOWN_REASON}: {self._judge_down()}{at}{after}"
        refused = getattr(self.r.jev, "refused", False) is True
        fault = getattr(self.r.jev, "request_fault", False) is True
        counted = not refused and answers > 0 and fault
        if counted and apply_queue.outages(self.entry) + 1 >= apply_limits.OUTAGES_MAX:
            return self._finish("needs_human", f"{down}; {apply_limits.OUTAGES_PARKED}", apply_limits.OUTAGES_NOTE)
        reason = f"{down}; {KEY_REFUSED_NOTE if refused else REQUEUED_NOTE}"
        usage = _usage_delta(self.usage_before, jev.total_usage())
        usage["generated"] = generated_count(self.pages)
        self._stop_late_watch()
        self._flush_decisions()
        self.trace.finish("queued", reason, self.page,
                          extra_mask=self._secret_masks(self.page) if self.page is not None else [])
        self.r._queue_write(self.job_id, "unclaim", lambda: apply_queue.unclaim(
            self.job_id, notes=reason, give_back=not refused, outage=counted,
            path=self.r.queue_path))
        self._close_job_pages()
        self.log.warning("job %s: %s", self.job_id, reason)
        return Outcome(job_id=self.job_id, status="queued", reason=reason, record_path="",
                       pages=len(self.pages), jev_usage=usage, judge_down=True,
                       trace_dir=self._trace_dir())

    def _trace_dir(self) -> str:
        """This attempt's trace folder, or "" when no trace was written."""
        return str(self.trace.dir) if self.trace.enabled and self.trace.dir else ""

    def _close_job_pages(self, keep=None) -> None:
        """Close the job's tabs but `keep` (one tab per job)."""
        for page in [*self._job_pages, self.page]:
            if page is None or page is keep or _page_closed(page):
                continue
            try:
                page.close()
            except Exception:       # noqa: BLE001  (closed under the run)
                pass

    def _parked_tab(self) -> Any:
        """The tab a job that ends keeps open: the one it ended on, else the
        job's last tab still open (None when none is)."""
        if self.page is not None and not _page_closed(self.page):
            return self.page
        return next((p for p in reversed(self._job_pages) if not _page_closed(p)), None)

    def _drive(self) -> None:
        """The state loop (`_loop`). When the site closed the job's tab
        before anything could have been sent (a `window.close()` that hands
        the flow back to the page that opened it), the loop goes on once in
        the tab the flow moved on in (`_take_over`); the page budget
        counts on across it."""
        start = 0
        while True:
            try:
                self._loop(start)
                return
            except Exception as e:      # noqa: BLE001  (re-raised unless a tab is taken over)
                if not self._take_over(e):
                    raise
                start = self._turn + 1

    def _take_over(self, e: BaseException) -> bool:
        """Whether the run goes on in another tab of the job after
        `e` left the loop. Only when the job's tab closed with the window
        open, nothing could have been sent (after the submit click, or a code
        step the application may have been held for, the run only reads that
        tab: `_maybe_sent`, `_confirmed_elsewhere`), the
        judge is up, no tab was taken over before in this job, and a tab the
        run left for another has moved on since (`_moved_on`): the flow went
        on there. A tab as the run left it (the user closed the job's tab; a
        popup that closed itself over an unchanged page) is no way on, and
        the job ends as a closed tab."""
        if isinstance(e, _Parked) and e.status == "submitted":
            return False
        if not self._tab_closed() or self._window_closed():
            return False
        if self._adopted or self._maybe_sent() or self._judge_down():
            return False
        moved = self._moved_on()
        if moved is None:
            return False
        page = moved[0]
        self._adopted += 1
        closed = str(getattr(self.page, "url", ""))
        url = str(getattr(page, "url", ""))
        self._trace("tab_taken_over", closed=closed, url=url, error=type(e).__name__)
        self._decide_next("tab_taken_over", "the site closed the job's tab; the flow went on "
                                            "in the tab that opened it, which the run takes over",
                          closed=_cap(closed, 160), url=_cap(url, 160))
        self.log.info("job %s: the site closed the job's tab; going on at %s", self.job_id, url)
        self._left_pages = [row for row in self._left_pages if row[0] is not page]
        self.page = page
        self.last_sig = None
        return True

    def _moved_on(self) -> tuple[Any, str, str] | None:
        """(the last tab the run left for another (`_leave`) that is still
        open, on the allowed sites and read as other than the run left it;
        its text then; its text now), looked at every `TAKEOVER_POLL_S` for
        up to `TAKEOVER_WAIT_S` (the closing tab's script may have set it
        going the moment before), else None. A tab that could not be read
        either time is never taken, nor is a LinkedIn tab, which is never
        the company's flow."""
        rows = [row for row in reversed(self._left_pages)
                if row[1] and not _page_closed(row[0])]
        if not rows:
            return None
        end = time.monotonic() + apply_limits.TAKEOVER_WAIT_S
        while True:
            for page, before, then in rows:
                url = str(getattr(page, "url", ""))
                host = _host(url)
                if _page_closed(page) or not host or _error_page(url) \
                        or apply_linkedin.is_linkedin(host) or not self._allowed_site(host):
                    continue
                now, text = apply_page._page_print(page)
                if now and now != before:
                    return page, then, text
            if time.monotonic() >= end:
                return None
            try:
                rows[0][0].wait_for_timeout(apply_limits.TAKEOVER_POLL_S * 1000)
            except Exception:       # noqa: BLE001  (the tab closed too)
                time.sleep(apply_limits.TAKEOVER_POLL_S)

    def _confirmed_elsewhere(self) -> Outcome | None:
        """After a send: the job's tab closed after the submit click
        or a code step that may have sent (`_maybe_sent`), and a tab the run
        left for it moved on
        (`_moved_on`, a form in a popup that hands back to its opener as it
        closes). That tab is only read, never clicked or judged (at most one
        send per job): received words it did not show when the run left it
        end the job `submitted` on a confirmation; anything else leaves the
        ending to the closed tab's rules (None)."""
        if not self._maybe_sent() or not self._tab_closed() or self._window_closed():
            return None
        moved = self._moved_on()
        if moved is None:
            return None
        page, then, text = moved
        marker = new_confirmation(then, text)
        if not marker:
            return None
        url = str(getattr(page, "url", ""))
        self._decide("after_submit", f"confirmation: the job's tab closed after the submit; "
                                     f"the tab that opened it shows {marker!r}, which it did "
                                     f"not before", url=_cap(url, 160))
        self.page = page
        return self._finish("submitted", f"confirmation page (in the tab that opened the job's "
                                         f"closed tab: {marker!r})")

    def _confirmed_here(self) -> Outcome | None:
        """The judge went down after the submit click: the
        job's page is still read for received words it did not show before
        the click (`new_confirmation`, which needs no judge), and they end
        the job `submitted`. None when it shows none."""
        before = self._before_submit or {}
        marker = new_confirmation(str(before.get("text") or ""), self._page_text())
        if not marker:
            return None
        self._decide("after_submit", f"confirmation: the page shows {marker!r}, which it did "
                                     f"not before the click; read while the judge was down")
        return self._finish("submitted", f"confirmation page ({marker!r}, read while the judge "
                                         f"was down)")

    def _leave(self, source, popup) -> None:
        """The run left `source` for `popup`. A LinkedIn tab closes (one
        tab per job; LinkedIn's part is done once the company's tab is
        adopted); any other is kept with its print, as the page a flow may
        hand back to when its popup closes itself."""
        if source is None or source is popup or _page_closed(source):
            return
        url = str(getattr(source, "url", ""))
        if apply_linkedin.is_linkedin(_host(url)):
            self._decide_next("source_tab_closed", "the LinkedIn tab the Apply left is closed "
                                                   "once the company's tab is adopted",
                              url=_cap(url, 160))
            try:
                source.close()
            except Exception:       # noqa: BLE001  (closed under the run)
                pass
            return
        self._left_pages.append((source, *apply_page._page_print(source)))

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

    def _loop(self, start: int = 0) -> None:
        """One page per turn from turn `start`: the consent banner out of the
        way, a read that waits for the page to render (`_read_digest`),
        LinkedIn's pages by the handler (`_linkedin_step`, no judge), every
        other page judged (an unsure read taken once more after a settle,
        `_reread`) and handled by its state."""
        for page_no in range(start, apply_judge.MAX_PAGES):
            self._turn = page_no
            if self.r.clock() >= self.deadline:
                raise _Parked("needs_human", f"time budget exhausted "
                                             f"({apply_limits.JOB_WALL_CLOCK_S // 60} min; {len(self.pages)} "
                                             f"page(s){self._last_states()})")
            self._take_late_popup()
            self._pin_first_tenant(self.page.url)
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
            turn = apply_route.route_turn(
                str(self.page.url), digest, state, conf, answers=answers, facts=facts,
                submit_clicked=self.submit_clicked, code_sent=self._code_sent,
                before=str((self._before_submit or {}).get("text") or "")
                if self.submit_clicked else None)
            state = self._take_turn(turn, digest, facts)
            if state == "application_form" and _email_first(digest):
                # the address screen of a two-step sign-in taken as a form:
                # its site takes the password screen after it all the same
                sites = getattr(self.accounts, "email_sites", None)
                if sites is not None:
                    sites.add(_site(digest.url_host or _host(self.page.url)))
            if state in ("application_form", "review_page", "confirmation"):
                # a sign-in tried without an account in the ledger led on:
                # its account goes in the ledger
                confirm = getattr(self.accounts, "confirm_sign_ins", None)
                if confirm is not None:
                    confirm()
            self._map(digest, answers, state)
            plan = apply_judge.plan(digest, self.catalog, answers,
                                    generation_enabled=bool(self.r.settings["auto_apply_generate"]),
                                    company=self._company())
            rec["flags"] = dict(plan.flags)
            self._trace("plan", plan=apply_trace.plan_json(plan))
            try:
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
                        # A closed posting has its own reason
                        raise _Parked("needs_human", f"{CLOSED_POSTING_REASON} ({_cap(closed, 160)})",
                                      apply_linkedin.CLOSED_NOTE)
                    reason = _PARK_STATES.get(state, state)
                    if state == "payment_request":
                        reason += (f" (page_payment p="
                                   f"{apply_judge.noul_of(answers, 'page_payment'):.2f})")
                    elif state == "other":
                        reason += self._reads_suffix()
                    raise _Parked("needs_human", reason)
            except apply_pause.Replan as why:
                # The person answered a pause in the browser, or the page
                # changed during it: the page is read and planned again, the
                # answers kept for it (`apply_pause.Pauser.apply_pending`)
                self.last_sig = None
                self._decide("replan", f"the page is read and planned again ({why})")
                continue

    def _take_turn(self, turn: apply_route.Turn, digest: apply_form.FormDigest,
                   facts: apply_judge.PageFacts) -> str:
        """Acts on `apply_route.route_turn`'s verdict for this page: each
        change of the read is logged and decided in the order it was made,
        then the job parks, ends, or goes on as the state returned."""
        for step in turn.steps:
            kind = step[0]
            if kind == "remap":
                _, read, conf = step
                # a page of form boxes is the form, whatever it was read as:
                # the account step would type the facts in and click its button.
                # An address screen with another box (a country) goes this way
                # too, so its site takes the password screen after it.
                self.log.info("job %s: read as %s (%.2f) with no account boxes; it is the "
                              "form", self.job_id, read, conf)
                self._decide("remap", f"read as {read} ({conf:.2f}) with no account boxes; "
                                      "it is the form", to="application_form")
                if any(_is_email_box(f) for f in digest.fields):
                    self.accounts.email_sites.add(_site(digest.url_host or _host(self.page.url)))
            elif kind == "confirmation_contradicted":
                _, conf, read, then = step
                self._decide("confirmation_contradicted",
                             f"read as confirmation ({conf:.2f}) on a page with a form field "
                             f"or a submit button and no received words; going on as the "
                             f"next read ({read} {then:.2f})", to=read)
            elif kind == "structural_fallback":
                _, read, conf, to = step
                self.log.info("job %s: unsure of the page (%s, %.2f); its structure reads %s",
                              self.job_id, read, conf, to)
                self._decide("structural_fallback", f"unsure of the page ({read}, {conf:.2f}); "
                                                    f"its structure reads it as {to}",
                             reads=self._reads(), facts=facts.to_dict(), to=to)
            elif kind == "unsure_goes_on":
                _, read, conf = step
                self.log.info("job %s: unsure of the page (%s, %.2f); going on with that read",
                              self.job_id, read, conf)
                self._decide("unsure_goes_on", f"unsure of the page ({read}, {conf:.2f}); going "
                                               "on with that read", reads=self._reads())
            elif kind == "structure_over_other":
                _, conf, to = step
                # `other` is none of the listed kinds; a page whose
                # structure settles one of them is that one
                self._decide("structure_over_other", f"read as other ({conf:.2f}); its "
                                                     f"structure reads it as {to}",
                             facts=facts.to_dict(), to=to)
            elif kind == "link_sent":
                _, read, conf, said = step
                # A page with no box that says a verification link was
                # emailed is the account check, whatever else it was read as;
                # its way on is the link in the email
                self._decide("remap", f"read as {read} ({conf:.2f}); the page says "
                                      f"{said!r} and has no box to fill: an account "
                                      "check by an emailed link", to="code_gate")
        state, conf = turn.state, turn.conf
        if turn.action == "act":
            return state
        if turn.action == "linkedin_form":
            self._no_form_on_linkedin(turn.detail)
            return state
        if turn.action == "submitted":
            raise _Parked("submitted", turn.detail, "")
        why, evidence = turn.detail
        if why == "already_applied":
            # A job the site says was applied to
            # before is never applied to again
            self._decide("already_applied", evidence)
            raise _Parked("needs_human", f"{ALREADY_APPLIED_REASON} ({_cap(evidence, 160)}; "
                                         f"{_cap(digest.url_host or _host(self.page.url), 60)})",
                          ALREADY_APPLIED_NOTE)
        if why == "confirmation":
            raise _Parked("needs_human", evidence, CHECK_SENT_NOTE)
        if why == "sso_only":
            # The only way on is a sign-in with another site's
            # account, which the run never uses: a dead end
            self._decide("sso_only", f"read as {state} ({conf:.2f}); its only way on "
                                     f"signs in with {', '.join(evidence)}")
            raise _Parked("needs_human", f"{SSO_REASON} ({', '.join(evidence)}); the run "
                                         "never signs in with another site", SSO_NOTE)
        raise _Parked("needs_human", f"unsure what this page is ({state}, {conf:.2f})"
                                     + self._reads_suffix())

    def _reads_suffix(self) -> str:
        reads = self._reads()
        return f"; reads: {reads}" if reads else ""

    # -- the end --------------------------------------------------------------------------------

    def _finish(self, status: str, reason: str, tab_note: str = "") -> Outcome:
        # every address in the reason and the tab note without its query: a
        # page a method=get form reached carries the answers there, and the
        # reason goes to the queue, the record and the trace
        reason = apply_trace.scrub_urls(reason)
        usage = _usage_delta(self.usage_before, jev.total_usage())
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
                # the error's type, never its message
                self.log.warning("job %s: record not written (%s)", self.job_id,
                                 type(e).__name__)
        if not tab_note and self.page is not None and status != "submitted":
            try:
                tab_note = f"{self.page.url} | {self.page.title()}"
            except Exception:       # noqa: BLE001
                tab_note = ""
        self._finish_entry(status, apply_trace.scrub_urls(tab_note), record, reason)
        if self._send_watch is not None:
            self._send_watch.stop()
        confirmed = status == "submitted" and reason.startswith("confirmation page")
        if self.page is not None:
            if confirmed:
                # only a confirmation closes the tab: an unconfirmed
                # send stays open for the person to check
                try:
                    self.page.close()
                except Exception:       # noqa: BLE001
                    pass
            else:
                self.r.parked_pages.append(self.page)
        # One tab per job stays, the one the job ended on (the job's
        # last open tab when the site or the user closed that one)
        self._close_job_pages(keep=None if confirmed else self._parked_tab())
        self.log.info("job %s: %s (%s)", self.job_id, status, reason)
        return Outcome(job_id=self.job_id, status=status, reason=reason, record_path=record,
                       pages=len(self.pages), jev_usage=usage,
                       browser_closed=self.browser_closed, judge_down=bool(self._judge_down()),
                       trace_dir=self._trace_dir())


    def _finish_entry(self, status: str, tab_note: str, record: str, reason: str) -> None:
        """`apply_queue.finish` through the runner's retrying queue write
        (`Runner._queue_write`): the drain goes on, and an entry it could not
        write stays `in_progress` for the human."""
        self.r._queue_write(self.job_id, "finish", lambda: apply_queue.finish(
            self.job_id, status, tab_note=tab_note, record=record, notes=reason,
            path=self.r.queue_path),
            f"the entry stays in_progress with status {status} unrecorded")
