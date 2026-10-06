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
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import apply_answergen  # noqa: E402
import apply_facts  # noqa: E402
import apply_click  # noqa: E402, F401
import apply_fill  # noqa: E402, F401
import apply_form  # noqa: E402
import apply_judge  # noqa: E402
import apply_inbox  # noqa: E402, F401
import apply_linkedin  # noqa: E402
import apply_queue  # noqa: E402
import apply_trace  # noqa: E402
import ats_accounts  # noqa: E402, F401
import jev  # noqa: E402
import jev_switch  # noqa: E402
import profile_lock  # noqa: E402
from apply_judge import FillPlan  # noqa: E402

# The run's lower layers, split out of this file; an import only goes down:
# limits, outcome and send words, then sites, sendwatch, page, account flow,
# route, gate and record, then the job's steps (`apply_job_submit`,
# `apply_job_form`, `apply_job_pages`) and `_JobRun` (`apply_job`). The names
# below are re-exported for this file and for readers in tests, scripts/ and
# apply_assess.py; a test patches the module that defines a name
# (tests/test_apply_run_facade.py checks it).
import apply_limits  # noqa: E402
import apply_page  # noqa: E402
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
from apply_job import _JobRun  # noqa: E402

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
    queued, the Apply Answers file is damaged, another browser holds the
    auto-apply profile, or Playwright is not installed: nothing is claimed
    then)."""
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
    except ModuleNotFoundError as e:
        if (e.name or "").split(".")[0] != "playwright":
            return _unexpected(e)
        # README Step 7 not run yet: the browser never opened, so nothing was
        # claimed (`one` handed its job back above). Name the install.
        import setup_check
        print(setup_check.PLAYWRIGHT_MISSING, file=sys.stderr)
        return 2
    except Exception as e:      # noqa: BLE001  (one line, documented exit 1)
        return _unexpected(e)


def _unexpected(e: BaseException) -> int:
    """main's exit 1: the error's type and step on stderr, its frames in the log.
    The type and the step only: a Playwright message carries the page's words
    and the values typed."""
    print(f"apply_run: error: {type(e).__name__} at {error_step(e)} (the traceback is in "
          f"the log)", file=sys.stderr)
    logging.getLogger("apply_run").error("apply_run: %s at %s; traceback (the message left "
                                         "out):\n  %s", type(e).__name__, error_step(e),
                                         "\n  ".join(error_frames(e)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
