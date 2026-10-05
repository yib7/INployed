"""The auto-apply drain loop: claim a queued job, drive a persistent Chromium
through the page state machine, submit behind the confidence gate or park,
write `apply_record.md`, finish the queue entry.

    python local/apply_run.py drain [--cap N] [--no-submit] [--headless]
                                    [--jev fake|replay|typesafe] [--profile DIR]
    python local/apply_run.py one <job_id> [same flags]
    python local/apply_run.py login      sign in to LinkedIn and the inbox once
    python local/apply_run.py doctor     judge mode, Jev switch, key, SDK, Playwright, Chromium, profile
    python local/apply_run.py probe <url> [--follow-apply] [--judge] [--headed]
                                         read one page as the run would; changes nothing

Per job (`Runner.run_job`): an Easy Apply entry (`is_easy_apply`) ends at
once, `needs_human` with `EASY_APPLY_REASON`, and no page is opened. Else the
sheet's Standard answers and Address refreshed from the answer store the
drain read once (`Runner.load_answers`), the fact catalog from the job
folder's apply.md and that store (`apply_facts.build`), the first
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
                           password box, read the page again (fields the
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
refuses one that turned into a send. Only a confirmation closes the
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
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import apply_answergen  # noqa: E402
import apply_facts  # noqa: E402
import apply_click  # noqa: E402
import apply_fill  # noqa: E402
import apply_form  # noqa: E402
import apply_judge  # noqa: E402
import apply_inbox  # noqa: E402, F401
import apply_linkedin  # noqa: E402
import apply_pause  # noqa: E402
import apply_queue  # noqa: E402
import apply_send_words  # noqa: E402
import apply_trace  # noqa: E402
import ats_accounts  # noqa: E402
import jev  # noqa: E402
import jev_switch  # noqa: E402
import profile_lock  # noqa: E402
from apply_judge import FillPlan, VerifyResult  # noqa: E402
from apply_send_words import _final_shaped, _send_worded  # noqa: E402

# The run's lower layers, split out of this file; an import only goes down:
# limits, outcome and send words, then sites, sendwatch, page, account flow,
# route, and gate and record. The names below are re-exported for this file
# and for readers in tests, scripts/ and apply_assess.py; a test patches the
# module that defines a name (tests/test_apply_run_facade.py checks it).
import apply_limits  # noqa: E402
import apply_sendwatch  # noqa: E402
import apply_page  # noqa: E402
import apply_account_flow  # noqa: E402
import apply_route  # noqa: E402
from apply_outcome import (ACCOUNT_PARK_REASONS, AGGREGATOR_NOTE, AGGREGATOR_REASON,  # noqa: E402, F401
                           ALREADY_APPLIED_NOTE, ALREADY_APPLIED_REASON, _cap,
                           CHECK_SENT_NOTE, CHECK_SENT_REASON, CHECKBOX_NOTE,
                           _closed_error, CLOSED_POSTING_REASON, CLOSED_REASON, CODE_NOTE,
                           _context_gone, EASY_APPLY_NOTE, EASY_APPLY_REASON,
                           FIELDS_MAX_REASON, JUDGE_DOWN_REASON, KEY_REFUSED_NOTE,
                           LINK_BOT_NOTE, LINK_BOT_WORDS, LINK_FAILED_WORDS,
                           LINK_HELD_NOTE, LINK_MOVES_MAX, LINK_NOTE, LINK_REASON,
                           LINK_SUBMIT_NOTE, LINK_USED_NOTE, LINKEDIN_LOGIN_NOTE,
                           LINKEDIN_RETURN_REASON, LOGIN_NOTE, MAILTO_NOTE, MAILTO_REASON,
                           _no_connection, NOT_SENT_REASON, _NotClicked, OPTION_TIE_WORDS,
                           OPTIONS_UNREAD_WORDS, Outcome, _Parked, PASSWORD_HTTP_REASON,
                           PASSWORD_RULE_NOTE, PASSWORD_RULE_REASON,
                           PAUSE_UNANSWERED_REASON, _PauseClosed, _Refused, REQUEUED_NOTE,
                           REVIEW_NOTE, _SentSeen, SSO_NOTE, SSO_REASON,
                           SUBMIT_FAILED_NOTE, TAB_CLOSED_REASON, TENANT_REASON, _Unsent)
from apply_sites import (_aggregator, AGGREGATOR_BOARDS_MAX, AGGREGATOR_SITES, ATS_SITES,  # noqa: E402, F401
                         _ats_tenant, company_site_control, content_frame_site,
                         _easy_apply, FRONT_END_SITES, _host, _IDENTITY_SITES,
                         _INBOX_PROVIDER_SITES, _insecure, _is_captcha_url,
                         _link_challenge, _link_check_text, link_targets, LINKEDIN_HOSTS,
                         LINKEDIN_LOGIN_URL, NAV_ATS_MAX, _on_linkedin_redirector,
                         _platform, _scripted_link, _sender_site, _site, _tracker,
                         TRACKER_SITES, _tracking)
from apply_sendwatch import (ACTION_READ_MS, confirmation_words, LateWatch,  # noqa: E402, F401
                             new_confirmation)
from apply_page import (await_destination, _BUTTON_HOME_JS, _button_text, _code_field,  # noqa: E402, F401
                        entry_problem, error_frames, _error_page, error_step,
                        field_named_in, _fields_sig, _ident_attrs, _is_password,
                        _label_key, mailto_address, MALFORMED_REASON, _message_key,
                        open_page, _page_closed, page_signature, _past_trackers, _popups,
                        _record_verification, _settle_capped, _settled_ms, settled_words,
                        _spare_judged)
from apply_account_flow import (account_advance, account_exists, _Accounts, _AsForm,  # noqa: E402, F401
                                code_advance, _CREATE_ACCOUNT, _credential_form,
                                _email_first, _fills_the_application, _FORM_ACTION_JS,
                                _Inbox, _is_email_box, _names_password, _NEW_PASSWORD,
                                page_problem, _page_words, password_rules, password_step,
                                sso_fallback_sites, sso_only, _THIRD_PARTY)
from apply_route import (_ACTED, buttons_moved, _draft_key, _drafts, ERROR_PAGE_REASON,  # noqa: E402, F401
                         fieldless_apply_choice, form_entry_choice, form_route,
                         generated_count, _LINK_REMAPS, _LINKEDIN_FORM_STATES,
                         linkedin_step, linkedin_view, loop_step, _MAPPED_STATES,
                         new_fields, _NEXT_WORDS, _OPENED_BY_ACCOUNT, other_step,
                         _park_on_stuck, _PARK_STATES, pick_holds, _picks, place_words,
                         _PLAIN_APPLY, plan_fills, posting_context, posting_entry_choice,
                         remaps_to_form, review_route, _same_text, _shaped, shaped_holds,
                         step_position, suggestion_holds, _typed_box, UNSENT_NOTE,
                         unsure_acts, unsure_step, _usage_delta)
from apply_gate import (_ACCOUNT_OWN_WORDS, can_submit, _control_words, guard_submit,  # noqa: E402, F401
                        _invalid_words, live_refusal, PARK_MODE_KEY, SUBMIT_KEY, submit_on)
from apply_record import (_cell, drain_report_dir, DRAIN_REPORT_PREFIX, drain_table,  # noqa: E402, F401
                          HIDDEN, REASON_CELL_MAX, RECORD_NAME, summary_line,
                          write_drain_report, write_record)
from apply_job_submit import _SubmitSteps  # noqa: E402

log = logging.getLogger("apply_run")

VIEWPORT = {"width": 1400, "height": 1000}
# Every browser the runner opens keeps Chrome's renderer sandbox on
# (Playwright turns it off unless asked) and takes no downloads: an
# application's pages are a stranger's, in a profile that holds the
# sign-ins and the typed password.
LAUNCH_HARDENING = {"chromium_sandbox": True, "accept_downloads": False}
BROWSER_CHANNEL = "chrome"         # the installed Google Chrome; the bundled Chromium is the fallback

DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_apply_submit": True,
    "auto_apply_headless": False,
    "auto_apply_jev_mode": "typesafe",
    "auto_apply_batch_cap": 10,
    "auto_apply_generate": True,
    "auto_apply_pause_minutes": 10,     # the pause's wait for the user
    "auto_apply_check_parallel": 10,    # difficulty checks at once (assess_pool)
}


def launch_profile(pw, profile_dir: Path, *, headless: bool, log=None):
    """Open the auto-apply profile in the installed Google Chrome, or in the
    bundled Playwright Chromium when Chrome will not start.

    The profile is its own directory, apart from the user's everyday Chrome
    profile: Chrome refuses automation on its default profile and locks a
    profile to one running browser. The logins made once through `login`
    stay in this directory for every later run.

    One browser at a time: the profile's sentinel
    (`profile_lock.hold`) is taken first and kept until the context closes,
    so the difficulty check and the Auto-apply panel see every browser
    opened here, the bundled one too, which leaves no Chrome lock. A taken
    sentinel, or a Chrome that fails while Chrome's own lock is held, raises
    `profile_lock.ProfileBusy` before the bundled build could open a
    profile another browser holds.

    On the real profile, with its sentinel held, the difficulty check's
    leftover profile copies are swept first (`_sweep_check_copies`)."""
    log = log or logging.getLogger("apply_run")
    guard = profile_lock.hold(profile_dir)
    if guard is None:
        raise profile_lock.ProfileBusy(profile_lock.RUN_BUSY)
    try:
        _sweep_check_copies(profile_dir, log)
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(profile_dir), channel=BROWSER_CHANNEL, headless=headless, viewport=VIEWPORT,
                **LAUNCH_HARDENING)
        except Exception as e:      # noqa: BLE001  (Chrome absent or broken: the bundled build)
            if profile_lock.chrome_holds(profile_dir):
                raise profile_lock.ProfileBusy(profile_lock.RUN_BUSY) from None
            first = str(e).strip().splitlines()[0][:200] if str(e).strip() else ""
            log.warning("Google Chrome did not start (%s: %s); using the bundled Chromium",
                        type(e).__name__, first)
            ctx = pw.chromium.launch_persistent_context(
                str(profile_dir), headless=headless, viewport=VIEWPORT, **LAUNCH_HARDENING)
    except BaseException:
        guard.release()
        raise
    _release_on_close(ctx, guard)
    return ctx


def _sweep_check_copies(profile_dir: Path, log) -> None:
    """The parallel difficulty check's profile copies hold the sign-in
    cookies; one that outlived its run (a console closed with X, a browser
    slow to let go) is deleted here, by a holder of the real profile's
    sentinel, so a live check's copies are never touched. A copy of another
    profile, or a slot itself, sweeps nothing. Never raises."""
    try:
        import assess_pool
        if not assess_pool.is_real_profile(profile_dir):
            return
        for folder in assess_pool.sweep_slots(tries=1):
            log.warning(assess_pool.LEFT_BEHIND.format(folder=folder))
    except Exception as e:      # noqa: BLE001  (a sweep never stops a browser opening)
        log.debug("the profile copies were not swept (%s)", type(e).__name__)


def _release_on_close(ctx, guard) -> None:
    """Give the profile's sentinel back when `ctx` closes (the browser
    window closed, or `close()`); the process ending gives it back too."""
    try:
        ctx._inployed_profile_guard = guard
    except Exception:       # noqa: BLE001  (a context double without attributes)
        pass
    on = getattr(ctx, "on", None)
    if on is None:
        return
    try:
        on("close", lambda *_: guard.release())
    except Exception:       # noqa: BLE001  (a context double without events)
        pass


def default_profile_dir() -> Path:
    appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return appdata / "linkedin_watcher" / "browser_profile"


def load_settings() -> dict[str, Any]:
    """The auto-apply keys from the dashboard's config (`settings.load`), with
    the defaults for anything unset. A config file that exists and cannot be
    read, or a settings module that fails, keeps the other defaults and turns
    the submit switch off (`guard_submit`): a user who switched submitting
    off and then broke the file never gets an application sent."""
    try:
        import settings
        problem = settings.submit_problem()
        stored = settings.load()
    except Exception as e:      # noqa: BLE001  (a bad config file is not a reason to stop)
        log.warning("settings unreadable (%s); using defaults in park mode", type(e).__name__)
        stored, problem = {}, f"the settings could not be loaded ({type(e).__name__})"
    if problem:
        log.warning("config unreadable: park mode (%s)", problem)
    return guard_submit({k: stored.get(k, d) for k, d in DEFAULT_SETTINGS.items()}, problem)


# --- hooks the runner implements -----------------------------------------------------

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


def hold_until_closed(ctx, *, sleep: Callable[[float], None] = time.sleep,
                      log: logging.Logger | None = None) -> None:
    """Keep the process alive until the user closes the window: every page
    closed, or the context's close event (the idea the old Playwright
    driver's parked-tab hold used)."""
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
        if _wait_for_close(ctx, apply_limits.HOLD_POLL_S, sleep):
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
    `apply_queue.build_context()`'s dict (the inbox URL), computed on demand;
    `drain_report` off keeps a drain from printing and writing its table
    (`_report`), for the matrix harness, which runs thousands of one-job
    drains and reports them itself. The answer store is read once per drain
    (`load_answers`) into `answers`; `answers_header` names the built-in
    answers that are not confirmed.
    """

    def __init__(self, *, jev: Any, queue_path: Path | None = None,
                 profile_dir: Path | None = None, settings: dict | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 accounts: Any = None, inbox: Any = None, answergen: Any = None,
                 log: logging.Logger | None = None, context: Any = None,
                 run_context: dict | None = None, drain_report: bool = True):
        self.log = log if log is not None else logging.getLogger("apply_run")
        self.drain_report = drain_report
        self.sleep = sleep
        self.jev = jev
        self.queue_path = Path(queue_path) if queue_path else None
        self.profile_dir = Path(profile_dir) if profile_dir else default_profile_dir()
        self.settings = guard_submit({**DEFAULT_SETTINGS, **(settings or {})})
        self.clock = clock
        default = NotConfigured()
        self.accounts = accounts
        self.inbox = inbox
        self.answergen = answergen if answergen is not None else default
        self._injected = context
        self._ctx = context
        self._run_context = run_context
        self.parked_pages: list = []
        self.answers: list[dict] | None = None
        self.answers_header = ""
        # set once a job's run begins (`_run_job`): `main`'s `one` gives a
        # claimed job back only when its browser never opened
        self.job_started = False

    @property
    def jev(self) -> jev.Guarded:
        """The judge, always behind `jev.Guarded` (retries and the
        breaker); a judge set here is wrapped, and its own attributes read
        through."""
        return self._jev

    @jev.setter
    def jev(self, judge: Any) -> None:
        self._jev = judge if isinstance(judge, jev.Guarded) else jev.Guarded(
            judge, sleep=lambda s: self.sleep(s), logger=self.log)

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

    def load_answers(self) -> list[dict]:
        """The answer store, read once per drain (FL-4): every job's sheet
        refresh and fact catalog use this copy. A damaged store raises
        `apply_answers.AnswerStoreError` before any job is claimed. The
        built-in answers that are not set or not confirmed are named once in
        `answers_header` (their questions, never a value): printed when the
        drain reports (else logged) and written at the top of the drain
        report."""
        from resume_tailor import apply_answers
        answers = apply_answers.load()
        missing = [str(e.get("question") or e.get("id"))
                   for e in apply_answers.with_missing_builtins(list(answers))
                   if isinstance(e, dict) and e.get("id") in apply_answers.BUILTINS
                   and not apply_answers.fact_value(e)]
        self.answers = answers
        self.answers_header = (
            "Answers not confirmed: " + ", ".join(f'"{q}"' for q in missing)
            + ". Questions that need them will stop.") if missing else ""
        if self.answers_header:
            if self.drain_report:
                _say(self.answers_header)
            else:
                self.log.info(self.answers_header)
        return answers

    @property
    def park_mode_why(self) -> str:
        """Why the run parks at every submit although nobody asked it to
        (`guard_submit`): "" when the submit switch read as a boolean."""
        return str(self.settings.get(PARK_MODE_KEY) or "")

    def announce_park_mode(self) -> None:
        """Say `park_mode_why` once, before anything is claimed: logged as a
        warning, and printed when the drain reports."""
        if not self.park_mode_why:
            return
        self.log.warning(self.park_mode_why)
        if self.drain_report:
            _say(self.park_mode_why)

    def report_header(self) -> str:
        """The lines above the drain report's table: the park-mode reason,
        then the answers not confirmed."""
        return "\n\n".join(t for t in (self.park_mode_why, self.answers_header) if t)

    # -- the drain ----------------------------------------------------------------------

    def drain(self, cap: int | None = None) -> list[Outcome]:
        """Claim FIFO until the queue is empty or `cap` jobs ran; one
        `Outcome` per job. A closed window or a crashed browser stops the
        drain: the job it ended is `needs_human` (`CLOSED_REASON`) and
        nothing more is claimed, so the rest stay queued with their attempt
        counts untouched. A judge that stays down (`jev.Guarded`'s
        breaker) stops it too: the job it was on goes back to `queued`,
        behind the others, with its attempt not counted, unless something
        may have been sent. The answer store is read first (`load_answers`):
        a damaged store raises `apply_answers.AnswerStoreError` and nothing
        is claimed."""
        limit = int(cap if cap is not None else self.settings["auto_apply_batch_cap"])
        self.announce_park_mode()
        # the store is read before anything is claimed; a damaged one raises
        # here and the queue stays as it was
        self.load_answers()
        # a new drain tries the judge again and counts its answers from 0
        self.jev.down, self.jev.refused, self.jev.answers = "", False, 0
        self.jev.request_fault = False

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
                try:
                    entry = apply_queue.claim("apply_run", path=self.queue_path)
                except apply_queue.QueueLockTimeout as e:
                    # a queue held past the lock's wait, or one that could
                    # not be read (`QueueUnreadable`): nothing was written
                    self.log.warning("the queue could not be claimed from (%s); the drain "
                                     "stops and the queued jobs stay queued",
                                     type(e).__name__)
                    break
                if entry is None:
                    break
                outcome = self._run_job(ctx, entry)
                outcomes.append(outcome)
                if outcome.browser_closed:
                    self.log.warning("the browser window closed during job %s; the drain "
                                     "stops and the queued jobs stay queued", outcome.job_id)
                    break
                if outcome.judge_down:
                    self.log.warning("%s (%s) during job %s; the drain stops and the queued "
                                     "jobs stay queued", JUDGE_DOWN_REASON, self.jev.down,
                                     outcome.job_id)
                    break
            self.log.info(summary_line(outcomes))
            self._report(outcomes)
            return outcomes
        return self._with_browser(_work)

    def _report(self, outcomes: list[Outcome]) -> None:
        """The drain's table (per job: end, pages, reason, trace) on stdout
        and in `apply_drain-<stamp>.md` beside the job folders
        (`write_drain_report`), so a live run reports its own rate. A report
        that cannot be written is logged; the drain's outcomes stand."""
        if not outcomes or not self.drain_report:
            return
        try:
            _say(drain_table(outcomes))
            path = write_drain_report(outcomes, self.queue_path, header=self.report_header())
        except Exception as e:      # noqa: BLE001  (a report is never the drain's end)
            self.log.warning("the drain report was not written (%s)", type(e).__name__)
            return
        _say(f"drain report: {path}")
        self.log.info("drain report: %s", path)

    def run_job(self, entry: dict) -> Outcome:
        """One claimed entry through the state machine (opens the browser
        when no drain is running)."""
        if self._ctx is not None:
            return self._run_job(self._ctx, entry)
        return self._with_browser(lambda ctx: self._run_job(ctx, entry))

    def _run_job(self, ctx, entry: dict) -> Outcome:
        self.job_started = True
        try:
            job = _JobRun(self, ctx, entry)
        except Exception as e:      # noqa: BLE001  (one entry never ends the drain)
            return self._not_started(ctx, entry, e)
        return job.run()

    def _not_started(self, ctx, entry: Mapping, e: Exception) -> Outcome:
        """A job whose run could not even be set up: the entry
        leaves `in_progress` as `failed`, with the error's type and no
        value, and the drain goes on. The log gets the traceback's frames,
        never the message."""
        job_id = str(entry.get("job_posting_id", "")) if isinstance(entry, Mapping) else ""
        reason = f"{type(e).__name__} while the job was set up"
        self.log.error("job %s: %s; traceback (the message left out):\n  %s", job_id, reason,
                       "\n  ".join(error_frames(e)))
        self._queue_write(job_id, "finish", lambda: apply_queue.finish(
            job_id, "failed", notes=reason, path=self.queue_path))
        return Outcome(job_id=job_id, status="failed", reason=reason, record_path="",
                       pages=0, jev_usage={}, browser_closed=_context_gone(ctx))

    def _queue_write(self, job_id: str, what: str, write: Callable[[], Any],
                     left: str = "the entry stays in_progress") -> bool:
        """One queue write (`write`, an `apply_queue` call), once more after
        `FINISH_RETRY_S` when it raises (a lock held by the dashboard), then
        an error naming the job and `left`; the drain goes on. The log keeps the error's message: a queue file's error ("disk
        full", a denied lock) carries no page text. True when the write went
        through."""
        for attempt in (1, 2):
            try:
                write()
                return True
            except Exception as e:      # noqa: BLE001  (the queue write must not end the drain)
                if attempt == 1:
                    self.log.warning("job %s: queue %s failed (%s: %s); retrying in %s s",
                                     job_id, what, type(e).__name__, e, apply_limits.FINISH_RETRY_S)
                    self.sleep(apply_limits.FINISH_RETRY_S)
                else:
                    self.log.error("job %s: queue %s failed twice (%s: %s); %s", job_id, what,
                                   type(e).__name__, e, left)
        return False


# --- one job ---------------------------------------------------------------------------------

class _JobRun(_SubmitSteps):
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
            unsure = conf < apply_judge.PAGE_STATE_MIN_CONF
            applied = apply_judge.already_applied(answers, facts)
            if applied and not self.submit_clicked and not self._code_sent:
                # A job the site says was applied to
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
            # a page that says a link was emailed, with no received words, is
            # the link step below (`_LINK_REMAPS`), whatever else it was read as
            if state == "confirmation" and not facts.link_sent:
                step, detail, then = apply_route.confirmation_step(
                    digest, answers, conf, submit_clicked=self.submit_clicked,
                    code_sent=self._code_sent,
                    before=str((self._before_submit or {}).get("text") or "")
                    if self.submit_clicked else None)
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
            if state != "confirmation" and not self.submit_clicked and not self._code_sent \
                    and not self._on_linkedin():
                sites = sso_only(digest)
                if sites:
                    # The only way on is a sign-in with another site's
                    # account, which the run never uses: a dead end
                    self._decide("sso_only", f"read as {state} ({conf:.2f}); its only way on "
                                             f"signs in with {', '.join(sites)}")
                    raise _Parked("needs_human", f"{SSO_REASON} ({', '.join(sites)}); the run "
                                                 "never signs in with another site", SSO_NOTE)
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
            if state in _LINK_REMAPS and facts.link_sent:
                # A page with no box that says a verification link was
                # emailed is the account check, whatever else it was read as;
                # its way on is the link in the email
                self._decide("remap", f"read as {state} ({conf:.2f}); the page says "
                                      f"{facts.link_sent!r} and has no box to fill: an account "
                                      "check by an emailed link", to="code_gate")
                state = "code_gate"
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


def _say(text: str) -> None:
    """`text` on stdout, in whatever the console can show (a cp1252 console
    and a page's curly quote in a reason)."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc), flush=True)


def _line(text: str, *, out=None) -> None:
    """`text` as one line on `out` (stdout by default), in whatever that
    stream can show (`_say`'s rule for a piped cp1252 console)."""
    out = sys.stdout if out is None else out
    try:
        print(text, file=out, flush=True)
    except UnicodeEncodeError:
        enc = getattr(out, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc), file=out, flush=True)


def _positive_int(text: str) -> int:
    """`--cap`'s value: a whole number of 1 or more (a drain of 0 jobs, or
    fewer, would run and do nothing)."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"{value} is under 1")
    return value


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE.parent / ".env")
    except Exception:       # noqa: BLE001  (python-dotenv absent: the environment is what it is)
        pass


def _jev_gate(mode: str) -> str:
    """Why a run on the `mode` judge cannot start on Jev (switched off,
    no key, no SDK), in the sentence the Auto-apply panel's Start button shows
    for the live judge; "" when it can. `drain`, `one` and `probe --judge` ask
    it after `_load_env`, so a key saved in `.env` counts. The fake and replay
    judges need neither a key nor the SDK, so only the master switch stops
    them here; `drain` and `one` refuse them as fixture-only before this, and
    a mode `jev.get` does not build with them (`jev_switch.mode_refusal`).
    `probe --judge` names that mode before this too (`jev_switch.unknown_mode`)."""
    return jev_switch.apply_blocked(mode=mode)


def _settings_from_args(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_settings()
    if getattr(args, "no_submit", False):
        cfg["auto_apply_submit"] = False
    if getattr(args, "headless", False):
        cfg["auto_apply_headless"] = True
    # The one judge-mode reader: the --jev flag, else the
    # setting, else typesafe, the order the panel's Start gate reads it in
    cfg["auto_apply_jev_mode"] = jev_switch.apply_mode(getattr(args, "jev", None), config=cfg)
    if getattr(args, "cap", None) is not None:
        cfg["auto_apply_batch_cap"] = int(args.cap)
    return cfg


def doctor(profile_dir: Path | None = None, out=None) -> int:
    """One line per auto-apply setup row, the profile dir, the judge mode and
    the Jev switch, then each line `setup_check.auto_apply_warnings` gives
    (the drain's refusal of a test judge or an unknown one first, as the
    drain checks it first); 0 when it gives none (a missing profile is
    created by the first run)."""
    import setup_check
    out = out or sys.stdout
    profile = Path(profile_dir) if profile_dir else default_profile_dir()
    try:
        import settings
        stored = settings.load()
        mode = jev_switch.apply_mode(config=stored)
        has_key = bool(settings.secret_status().get("TYPESAFE_API_KEY")) or bool(
            os.environ.get(jev.KEY_ENV, "").strip())
        jev_on = jev_switch.master_on(config=stored)
    except Exception:       # noqa: BLE001
        mode, has_key = jev_switch.apply_mode(), bool(os.environ.get(jev.KEY_ENV, "").strip())
        jev_on = jev_switch.master_on()
    sdk = setup_check.module_found("typesafe_sdk")
    playwright_found = setup_check.module_found("playwright")
    chrome = setup_check.chrome_installed()
    browser = playwright_found and (chrome or setup_check.chromium_installed())
    rows = [("TypeSafe API key", has_key or mode != "typesafe"),
            ("typesafe_sdk", sdk or mode != "typesafe"),
            ("playwright", playwright_found),
            ("browser: Google Chrome" if chrome else "chromium (Google Chrome not found)",
             browser)]
    for name, ok in rows:
        _line(f"{'ok     ' if ok else 'MISSING'}  {name}", out=out)
    _line(f"{'ok     ' if profile.is_dir() else 'absent '}  browser profile: {profile}"
          + ("" if profile.is_dir() else " (created by the first run or `login`)"), out=out)
    _line(f"judge mode: {mode}", out=out)
    _line(f"Jev switch: {'on' if jev_on else 'off (Settings > Jev)'}", out=out)
    warnings = setup_check.auto_apply_warnings(has_key, mode, sdk, playwright_found, browser,
                                               jev_enabled=jev_on)
    for w in warnings:
        _line(f"  {w}", out=out)
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
    view, waited = linkedin_view(page, wait_s=apply_limits.LINKEDIN_READY_S if kind == "job" else 0)
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
    _line(f"page {n}: {page.url}", out=out)
    _line(f"  title: {_one_line(digest.title, 120)}", out=out)
    _line(f"  fields ({len(digest.fields)}):", out=out)
    for f in digest.fields:
        extra = f" ({len(f.options)} options)" if f.options else ""
        frame = f" [frame {f.locator[0]}]" if f.locator[0] else ""
        _line(f"  field [{f.n}] {f.type}{' required' if f.required else ''} "
              f"{_one_line(f.label, 80)!r}{extra}{frame}", out=out)
    _line(f"  buttons ({len(digest.buttons)}):", out=out)
    for b in digest.buttons:
        frame = f" [frame {b.locator[0]}]" if b.locator[0] else ""
        _line(f"  button [{b.n}] {_one_line(b.text, 60)!r} ({b.kind_hint or 'control'}){frame}",
              out=out)
    _line(f"  text: {_one_line(digest.text, 400)}", out=out)
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
        answers.update(apply_judge.merge_answers([
            {k: v for k, v in judge.judge(s2, q2).items() if k in q2}
            for s2, q2 in apply_judge.page_requests(digest, catalog, {}, fields=with_fields)
            if q2]))
        _line(f"  judge: page_state {read} {conf:.2f} "
              f"({apply_trace.page_state_reads(answers, top=5)}; the judge read "
              f"{combined.judged} {combined.judged_conf:.2f})", out=out)
        if combined.why:
            _line(f"  judge: {combined.why}", out=out)
        for b in digest.buttons:
            role, rconf = apply_judge._choice_of(answers, f"button_{b.n}_role")
            _line(f"  judge: button [{b.n}] {role} {rconf:.2f}", out=out)
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
    _line(f"  linkedin handler: {linkedin_line}", out=out)
    n_fl = fieldless_apply_choice(digest)
    _line("  fieldless posting: " + (f"would click [{n_fl}] {_button_text(digest, n_fl)!r}"
                                     if n_fl is not None else
                                     f"no ({len(digest.fields)} form field(s))" if digest.fields
                                     else "no button says apply"), out=out)
    _line(f"  account screen: {'yes' if _credential_form(digest) else 'no'}", out=out)
    if judge is not None:
        _line(f"  the loop would: {step}", out=out)
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
    _line(f"probe: {url}", out=out)
    try:
        # the run's own first load (`open_page`): domcontentloaded, one retry
        # of a dropped load, then the settle
        for row in open_page(page, url, timeout_ms=apply_limits.PROBE_GOTO_MS, settle_s=settle_s):
            _line(f"  load: {row['why']}", out=out)
    except Exception as e:      # noqa: BLE001  (a dead or slow page is the answer)
        _line(f"probe: the page did not load ({type(e).__name__})", out=out)
        return 1
    digest, plan, linkedin = _probe_page(page, 1, judge, out, park_mode=park_mode)
    if not follow_apply:
        return 0
    loc, label, why = _probe_entry(page, digest, plan, linkedin)
    if loc is None:
        _line(f"follow-apply: nothing clicked ({why})", out=out)
        return 0
    popup, signal, _ = apply_page.click_entry(page, loc)
    target, _info = await_destination(popup or page)
    _line(f"follow-apply: clicked {label}; "
          f"{'a new tab' if popup or target is not page else 'the same tab'} at {target.url}",
          out=out)
    _probe_page(target, 2, judge, out, park_mode=park_mode)
    return 0


def probe(url: str, *, follow_apply: bool = False, judge: Any = None, headed: bool = False,
          profile_dir: Path | None = None, context: Any = None, out=None,
          settle_s: float = apply_limits.PROBE_SETTLE_S, park_mode: bool = False) -> int:
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
                browser = pw.chromium.launch(channel=BROWSER_CHANNEL, headless=not headed,
                                             chromium_sandbox=True)
            except Exception:       # noqa: BLE001  (Chrome absent: the bundled Chromium)
                browser = pw.chromium.launch(headless=not headed, chromium_sandbox=True)
            ctx = browser.new_context(viewport=VIEWPORT, accept_downloads=False)
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
    configured (Jev switched off or unusable, no judge, the job id is not
    queued, the Apply Answers file is damaged, or another browser holds the
    auto-apply profile: nothing is claimed then)."""
    ap = argparse.ArgumentParser(prog="apply_run",
                                 description="Jev-judged auto-apply: drain the queue.")
    sub = ap.add_subparsers(dest="verb", required=True)

    def _verbose(p):
        p.add_argument("--verbose", action="store_true", help="DEBUG logging")

    def _run_flags(p):
        _verbose(p)
        p.add_argument("--cap", type=_positive_int, default=None, help="jobs per drain (1 or more)")
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
            return login(profile)       # `launch_profile` refuses a held profile
        if args.verb == "probe":
            judge = None
            if args.judge:
                _load_env()
                mode = jev_switch.apply_mode(args.jev, config=load_settings())
                # A judge on every page is a Jev use. A mode `jev.get` does not
                # build is named first, in the drain's sentence; the test
                # judges run here, since this is a probe.
                blocked = jev_switch.unknown_mode(mode) or _jev_gate(mode)
                if blocked:
                    print(blocked, file=sys.stderr)
                    return 2
                try:
                    judge = jev.get(mode)
                except (jev.JevUnavailable, ValueError) as e:
                    print(f"apply_run: {e}", file=sys.stderr)
                    return 2
            return probe(args.url, follow_apply=args.follow_apply, judge=judge,
                         headed=args.headed, profile_dir=profile, park_mode=args.no_submit)
        cfg = _settings_from_args(args)
        # A test judge and a mode `jev.get` does not build are refused first, in the sentence the Auto-apply panel's
        # Start button shows for each (`jev_switch.start_blocked`)
        refused = jev_switch.mode_refusal(cfg["auto_apply_jev_mode"])
        if refused:
            print(refused, file=sys.stderr)
            return 2
        _load_env()
        # Auto-apply runs on Jev alone. Jev switched off, no key or no SDK
        # stops the run here, after `.env` is read and before a judge, a claim or
        # a browser, in the sentence the Auto-apply panel's Start button shows.
        blocked = _jev_gate(cfg["auto_apply_jev_mode"])
        if blocked:
            print(blocked, file=sys.stderr)
            return 2
        # A difficulty check or a sign-in holds the profile
        # (Chrome's lock or the sentinel): refused before a judge or a claim,
        # in the sentence the panel's Start button shows
        if profile_lock.busy(profile or default_profile_dir()):
            print(profile_lock.RUN_BUSY, file=sys.stderr)
            return 2
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
        from resume_tailor import apply_answers
        try:
            if args.verb == "one":
                runner.announce_park_mode()
                runner.load_answers()       # before the claim
                entry = apply_queue.claim("apply_run", path=queue, job_id=args.job_id)
                if entry is None:
                    print(f"apply_run: job {args.job_id} is not queued", file=sys.stderr)
                    return 2
                try:
                    outcomes = [runner.run_job(entry)]
                except Exception:
                    # the browser never opened (a check took the profile after
                    # the busy read, Playwright or Chromium missing): the job
                    # goes back to the queue with its attempt not counted. A
                    # job whose run began keeps its claim, since its page may
                    # have sent something.
                    if not getattr(runner, "job_started", False):
                        apply_queue.unclaim(args.job_id, give_back=True, outage=False,
                                            path=queue)
                    raise
            else:
                outcomes = runner.drain(cfg["auto_apply_batch_cap"])
        except apply_answers.AnswerStoreError as e:
            print(f"The Apply Answers file is damaged ({e.path}): {e.reason}. Open the "
                  f"dashboard's Apply Answers tab to restore the backup.", file=sys.stderr)
            return 2
        _say(summary_line(outcomes))
        return 0
    except profile_lock.ProfileBusy as e:
        print(e, file=sys.stderr)       # another browser opened the profile first
        return 2
    except Exception as e:      # noqa: BLE001  (one line, documented exit 1)
        # the type and the step only: a
        # Playwright message carries the page's words and the values typed;
        # the frames go to the log
        print(f"apply_run: error: {type(e).__name__} at {error_step(e)} (the traceback is in "
              f"the log)", file=sys.stderr)
        logging.getLogger("apply_run").error("apply_run: %s at %s; traceback (the message left "
                                             "out):\n  %s", type(e).__name__, error_step(e),
                                             "\n  ".join(error_frames(e)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
