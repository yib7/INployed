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
import logging
import os
import sys
import time
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
from apply_judge import FillPlan  # noqa: E402

# The run's lower layers, split out of this file; an import only goes down:
# limits, outcome and send words, then sites, sendwatch, page, account flow,
# route, and gate and record. The names below are re-exported for this file
# and for readers in tests, scripts/ and apply_assess.py; a test patches the
# module that defines a name (tests/test_apply_run_facade.py checks it).
import apply_limits  # noqa: E402
import apply_sendwatch  # noqa: E402
import apply_page  # noqa: E402
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
from apply_job_form import _FormSteps  # noqa: E402
from apply_job_pages import _PageSteps  # noqa: E402

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
