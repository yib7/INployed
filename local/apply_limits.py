"""The auto-apply run's tunables: the waits, caps and polls that `apply_run`
and the modules split out of it read.

Every reader reads them qualified (`apply_limits.CLICK_TIMEOUT_S`), so a test
that shortens a wait patches this module and reaches every reader. Pure
constants: this module imports nothing.
"""
from __future__ import annotations


JOB_WALL_CLOCK_S = 15 * 60         # per job, on the injectable clock (a solved CAPTCHA's wait is added back)
GENERATE_MAX = 3                   # generated answers per job (spec 3.7)
POPUP_TIMEOUT_MS = 5_000           # for the Apply entry to open a new tab
POPUP_GRACE_S = 0.5                # after a same-tab DOM change, for a popup that follows it
ENTRY_POLL_MS = 100                # the entry click's watch for a popup, a navigation or a change
GOTO_TIMEOUT_MS = 45_000           # the first load, to `domcontentloaded`
GOTO_RETRY_S = 2.0                 # before the one retry of a first load that failed on the network
GOTO_ERROR_PAGE_S = 10.0           # at most, for Chromium's error page to be up before that retry
EMPTY_TEXT_MIN = 200               # a fieldless read with less visible text is read again
EMPTY_READ_MAX_S = 10.0            # an empty read is re-read after a settle for up to this long
EMPTY_READ_STABLE_S = 3.0          # or until the page has held the same empty read this long
EMPTY_READ_POLL_S = 0.5
LOADING_WAIT_S = 3.0               # a read with a loading placeholder up waits this long, once
                                   # per page (an ad or widget region may never clear)
LINKEDIN_READY_S = 12.0            # for a LinkedIn job page's top card to render
LINKEDIN_POLL_MS = 250
LINKEDIN_EASY_RECHECK_S = 1.5      # an Easy Apply read is read again after this, and a settle
LINKEDIN_CLICKS_MAX = 3            # offsite Apply clicks the handler makes per job
CONSENT_MAX = 3                    # consent banners dismissed per job
CONSENT_WAIT_S = 5                 # for a consent click's effect (the banner gone, a reload)
CLICK_TIMEOUT_S = 20               # apply_fill.click's wait for a change
FILL_ROUNDS_MAX = 3                # re-reads after a page's fill: revealed fields, the page's
                                   # own changes
DISABLED_WAIT_S = 2.0              # a way on still disabled after the fill: waited on this long
REPAIR_ROUNDS = 2                  # repairs of the fields a form refused, per step
BUSY_WAIT_S = 60                   # a loading indicator after a click: waited on this long
STEP_SETTLE_S = 20                 # a quiet click that set a request going: waited on this long
                                   # more, never clicked again
BUSY_POLL_S = 0.25
SUBMIT_SETTLE_S = 10               # after a quiet submit click: wait this long for the page
POST_SUBMIT_WAIT_S = 45            # after the submit click, the page is read again while a
                                   # request it sent is in flight or the page still moves
POST_SUBMIT_POLL_S = 1.0
POST_SUBMIT_QUIET_S = 2.0          # a page this still, with no request in flight, is read as is
POST_SUBMIT_READS = 5              # judge requests the post-submit read makes at most
HOLD_POLL_S = 1.0                  # while holding the window open
FINISH_RETRY_S = 1.0               # before the one retry of a failed queue finish
TAKEOVER_WAIT_S = 3.0              # for a tab the flow may go on in to move, once the site
TAKEOVER_POLL_S = 0.25             # closed the job's

REDIRECT_TIMEOUT_S = 20            # for that hop's script to send the tab on
HUMAN_CHECK_WAIT_S = 5 * 60        # for the user to solve a CAPTCHA in the visible window
HUMAN_CHECK_POLL_S = 2.0
HUMAN_CHECK_AUTO_S = 16            # headless: for a whole-page check to clear itself
HUMAN_CHECK_MIN_PX = 150           # a bot-check frame this tall is a challenge, not a badge

# The judge down under the same job a second time parks it: a failure
# its own request causes would otherwise stop every drain at the queue's head.
# An outage counts only after the judge answered in the drain: one
# down for every job is no job's doing, and the park's reason names the count.
# It counts only for an error a request can cause
# (`jev.Guarded.request_fault`): never a busy or overloaded service
OUTAGES_MAX = 2
OUTAGES_PARKED = (f"the judge went down under this job {OUTAGES_MAX} times; parked so the "
                  f"queue moves on")
OUTAGES_NOTE = "Re-queue once the judge answers again"
EVIDENCE_CAP = 300                # characters of evidence a park reason carries
PROBE_SETTLE_S = 10                # the probe's wait for a page to hold still
PROBE_GOTO_MS = 30_000
