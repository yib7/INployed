"""The auto-apply difficulty check: a 1-10 score per
queued job, so the user knows which jobs to leave to the drain.

The score is code: a base by application system plus fixed steps for what
the first application page asks, rounded half up and clamped to 1-10. Easy
Apply, a closed or dead posting and a payment page are 10 at once. Jev reads
the page; the counting and the arithmetic stay here, with the constants below.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import apply_queue  # noqa: E402
import jev_switch  # noqa: E402
import profile_lock  # noqa: E402

# --- the score ------------------------------------------------------------------------

# The base by application system (the `apply_queue.infer_ats` names, plus
# "bamboohr"); a system missing here scores UNKNOWN_BASE.
SYSTEM_BASE = {
    "greenhouse": 2, "lever": 2, "ashby": 2,
    "workable": 3, "smartrecruiters": 3, "jobvite": 3, "bamboohr": 3,
    "workday": 6, "icims": 6,
    "taleo": 7, "successfactors": 7, "oracle": 7,
}
UNKNOWN_BASE = 4
PER_UNANSWERED = 1.5        # each required question the answers cannot fill
UNANSWERED_CAP = 5
PER_ESSAY = 0.5             # each required essay the run would draft
ESSAY_CAP = 2
SENSITIVE = 3               # a required sensitive field (always the user's to type)
CAPTCHA = 2                 # a CAPTCHA or bot check
ACCOUNT_WALL = 1            # an account wall with no saved account
HISTORY_EASIER = -1         # past runs on the system reached the end twice, no park
HISTORY_HARDER = 1          # past runs on the system parked twice
HISTORY_MIN = 2
SCORE_MIN, SCORE_MAX = 1, 10
STOP_SCORE = 10

BANDS = ((1, 3, "Queue it"), (4, 6, "May need an answer or two"), (7, 10, "Do it yourself"))

# The pages the score stops at, at STOP_SCORE, each with its one reason.
STOP_REASONS = {
    "easy_apply": "Easy Apply: the run leaves Easy Apply jobs to you",
    "closed": "The posting is closed",
    "dead": "The application is a dead end: an error page, or no Apply entry to follow",
    "payment": "The application asks for a payment",
}

SYSTEM_NAMES = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
    "workable": "Workable", "smartrecruiters": "SmartRecruiters", "jobvite": "Jobvite",
    "bamboohr": "BambooHR", "workday": "Workday", "icims": "iCIMS", "taleo": "Taleo",
    "successfactors": "SuccessFactors", "oracle": "Oracle",
}


def band_for(value: int) -> str:
    """The band a score falls in: 1-3 "Queue it", 4-6 "May need an answer or
    two", 7-10 "Do it yourself"."""
    for low, high, label in BANDS:
        if low <= value <= high:
            return label
    raise ValueError(f"a difficulty score runs {SCORE_MIN} to {SCORE_MAX}; got {value!r}")


def _half_up(total: float) -> int:
    return int(math.floor(total + 0.5))


def _step(value: float) -> str:
    return f"{value:+g}"


def _capped(count: int, per: float, cap: float) -> tuple[float, str]:
    raw = count * per
    if raw > cap:
        return float(cap), f"{_step(cap)}, the most this step adds"
    return raw, _step(raw)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def score(*, system: str = "", unanswered: int = 0, essays: int = 0,
          sensitive: bool = False, captcha: bool = False, account_wall: bool = False,
          past_submits: int = 0, past_parks: int = 0, stop: str = "") -> dict:
    """{"score", "band", "reasons"} for what the check read.

    `system` is the application system (a SYSTEM_BASE key; anything else is
    unknown). `unanswered` counts the required questions the user's confirmed
    answers cannot fill, `essays` the required essays the run would draft.
    `past_submits` and `past_parks` count the user's past runs on that system
    that reached the end and that parked (`past_runs`): two parks add
    HISTORY_HARDER, and two runs that reached the end with no park add
    HISTORY_EASIER. `stop` (a STOP_REASONS key) scores STOP_SCORE at once with
    its one reason; an unknown stop raises ValueError."""
    if stop:
        if stop not in STOP_REASONS:
            raise ValueError(f"unknown stop {stop!r}; expected one of {', '.join(STOP_REASONS)}")
        return {"score": STOP_SCORE, "band": band_for(STOP_SCORE),
                "reasons": [STOP_REASONS[stop]]}
    key = str(system or "").strip().lower()
    name = SYSTEM_NAMES.get(key)
    base = SYSTEM_BASE.get(key, UNKNOWN_BASE)
    total = float(base)
    reasons = [f"Application system: {name} (base {base})" if name else
               f"Application system: not one the check knows (base {base})"]
    if unanswered > 0:
        add, shown = _capped(unanswered, PER_UNANSWERED, UNANSWERED_CAP)
        total += add
        reasons.append(f"{_plural(unanswered, 'required question', 'required questions')} "
                       f"your answers cannot fill ({shown})")
    if essays > 0:
        add, shown = _capped(essays, PER_ESSAY, ESSAY_CAP)
        total += add
        reasons.append(f"{_plural(essays, 'required essay', 'required essays')} "
                       f"the run would draft ({shown})")
    if sensitive:
        total += SENSITIVE
        reasons.append(f"A required sensitive field, always yours to type ({_step(SENSITIVE)})")
    if captcha:
        total += CAPTCHA
        reasons.append(f"A CAPTCHA or bot check ({_step(CAPTCHA)})")
    if account_wall:
        total += ACCOUNT_WALL
        reasons.append(f"An account wall with no saved account ({_step(ACCOUNT_WALL)})")
    where = name or "this system"
    if past_parks >= HISTORY_MIN:
        total += HISTORY_HARDER
        reasons.append(f"Past runs on {where} parked at least twice ({_step(HISTORY_HARDER)})")
    elif past_submits >= HISTORY_MIN and past_parks == 0:
        total += HISTORY_EASIER
        reasons.append(f"Past runs on {where} reached the end at least twice with no park "
                       f"({_step(HISTORY_EASIER)})")
    value = min(SCORE_MAX, max(SCORE_MIN, _half_up(total)))
    return {"score": value, "band": band_for(value), "reasons": reasons}


# --- the gate and the profile -------------------------------------------------------------

ENTRY_HOPS_MAX = 4          # Apply entry clicks per job: LinkedIn's, two job boards', the posting's
PAGES_MAX = ENTRY_HOPS_MAX + 2
STALE_DAYS = 7              # a result older than this shows its age
PROFILE_BUSY = profile_lock.BUSY_LEAD + " Check difficulty once that window closes."
NO_BROWSER = ("The browser did not start ({why}): Google Chrome and the bundled Chromium both "
              "failed to open the auto-apply profile, so the difficulty check stops here.")
NO_PLAYWRIGHT = ("Playwright is not installed, so the difficulty check cannot open a browser. "
                 "Run: pip install playwright==1.61.0, then python -m playwright install "
                 "chromium.")
QUEUE_LOCKED = ("the queue file stayed locked (the dashboard or a drain was writing it), so "
                "the result was not saved")
ACCOUNT_NOTE = "An account step comes first: its questions show once you sign in"
CHECK_NOTE = "A bot check comes first: its questions show once it clears"
MAILTO_NOTE = "The Apply opens an email to {address}"
SSO_NOTE = "Its only way on signs in with {sites}; the run signs in with no other site"


def account_worded(text: str) -> bool:
    """Does a posting's control read as a way into an account (the walk
    never clicks a sign-in or a sign-up): a sign-in or log-in
    (`apply_send_words.SIGN_IN_WORDS`), a sign-up or a new account
    (`apply_run._CREATE_ACCOUNT`), or a Next or Continue with no Apply word
    (`apply_judge.entry_worded`). "Sign in to apply" is one; "Continue
    to apply" is not."""
    import apply_judge
    import apply_run
    import apply_send_words
    text = " ".join(str(text or "").split())
    if apply_send_words.SIGN_IN_WORDS.search(text) or apply_run._CREATE_ACCOUNT.search(text):
        return True
    return bool(apply_run._NEXT_WORDS.search(text)) and not apply_judge.entry_worded(text)


def mode_gate(mode: str) -> str:
    """The judge mode's own refusal, asked before the Jev gate: a test
    judge's or a mode `jev.get` does not build (`jev_switch.mode_refusal`),
    as the drain asks it. `mode` is a mode `jev_switch.apply_mode`
    resolved."""
    return jev_switch.mode_refusal(mode)


def refusal(*, config: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None,
            mode: str | None = None, saved_key: bool = False) -> str:
    """Why `apply_assess.py` refuses to run, in the sentence it prints and the
    panel's Check difficulty shows; "" when it runs. A test judge first
    or an unknown one (`mode_gate`, as the drain), then the Jev gate for the
    difficulty area (`jev_switch.difficulty_blocked`). `mode` is the judge mode
    (None reads the Auto-apply judge setting); `saved_key` counts a key saved in
    Settings, since the check's console loads `.env` itself."""
    resolved = jev_switch.apply_mode(mode, config=config)
    return mode_gate(resolved) or jev_switch.difficulty_blocked(
        config=config, env=env, mode=resolved, saved_key=saved_key)


def default_profile_dir() -> Path:
    """The drain's persistent browser profile (`profile_lock.default_profile_dir`),
    read here without importing the runner, so the dashboard stays light."""
    return profile_lock.default_profile_dir()


def profile_busy(profile_dir: Path | None = None) -> bool:
    """Does a browser hold the auto-apply profile (a drain, a sign-in, or
    another check)? Chrome's own lock, or the sentinel every browser
    `apply_run.launch_profile` opens takes (`profile_lock.busy`); a missing
    profile is free."""
    return profile_lock.busy(profile_dir)


# --- what the queue table shows ----------------------------------------------------------

_FAMILIES = ((1, 3, "success"), (4, 6, "warning"), (7, 10, "danger"))


def band_family(value: Any) -> str:
    """The theme family a score's band is painted in: success 1-3, warning
    4-6, danger 7-10, neutral for anything else."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "neutral"
    return next((fam for low, high, fam in _FAMILIES if low <= n <= high), "neutral")


def age_days(checked_at: str, now: datetime | None = None) -> int | None:
    """Whole days since `checked_at` (an ISO time), or None when unreadable."""
    try:
        then = datetime.fromisoformat(str(checked_at))
    except ValueError:
        return None
    now = now or datetime.now()
    if then.tzinfo is not None and now.tzinfo is None:
        then = then.replace(tzinfo=None)
    return max(0, (now - then).days)


def age_text(checked_at: str, now: datetime | None = None) -> str:
    """"N days old" (whole days) for a result older than STALE_DAYS to the
    second, else ""."""
    try:
        then = datetime.fromisoformat(str(checked_at))
    except ValueError:
        return ""
    now = now or datetime.now()
    if then.tzinfo is not None and now.tzinfo is None:
        then = then.replace(tzinfo=None)
    if now - then <= timedelta(days=STALE_DAYS):
        return ""
    return f"{(now - then).days} days old"


def failed_text(difficulty: Mapping[str, Any], now: datetime | None = None) -> str:
    """The line for a check that read nothing (`apply_queue.FAILED_KEYS`):
    "Last check failed <when>: <why>.", with "Showing the earlier result."
    when a score from before stays; "" when the last check read its page."""
    when = str(difficulty.get("last_failed_at") or "")
    if not when:
        return ""
    days = age_days(when, now)
    ago = ("today" if days == 0 else "yesterday" if days == 1
           else f"{days} days ago" if days is not None else "")
    why = str(difficulty.get("last_failed_why") or "no reason noted").rstrip(".")
    line = f"Last check failed {ago}: {why}." if ago else f"Last check failed: {why}."
    return line + (" Showing the earlier result." if difficulty.get("score") is not None else "")


def system_for(url: str) -> str:
    """The application system a URL's host belongs to: a SYSTEM_BASE key, or
    "" for a host the check does not know (LinkedIn included)."""
    raw = str(url or "").strip()
    host = (urlsplit(raw).hostname or "") if "://" in raw else raw.split("/")[0]
    host = host.lower()
    if not host:
        return ""
    if host == "bamboohr.com" or host.endswith(".bamboohr.com"):
        return "bamboohr"
    system = apply_queue.infer_ats(f"https://{host}")["system"]
    return system if system in SYSTEM_BASE else ""


def past_runs(entries: list[Mapping[str, Any]], system: str, job_id: str = "") -> tuple[int, int]:
    """(runs that reached the end, runs that parked) among the queue's other
    jobs on `system` that the drain ran at least once: submitted and
    ready_to_submit reach the end, needs_human parked. (0, 0) for an unknown
    system.

    The queue is the one record of the drain's outcomes: the drain keeps no
    run log of system and outcome beyond it (its per-job trace folders and
    `apply_drain-*.md` reports are for reading), so a job the user removed,
    or "Clear finished" dropped, no longer counts. Each entry counts once,
    by its last outcome."""
    if not system:
        return 0, 0
    ends = parks = 0
    for e in entries:
        if str(e.get("job_posting_id")) == str(job_id) or apply_queue.attempts(e) < 1:
            continue
        ats = e.get("ats") or {}
        own = system_for(str(ats.get("domain") or "")) or str(ats.get("system") or "")
        if own != system:
            continue
        status = str(e.get("status") or "")
        # ready_to_submit reaches the end: the run filled every page up to
        # the submit gate and stops there for park mode, a CAPTCHA checkbox
        # or a submit that did not register, none of them a page it could
        # not do (a park of its own is needs_human)
        if status in ("submitted", "ready_to_submit"):
            ends += 1
        elif status == "needs_human":
            parks += 1
    return ends, parks


# --- the walk to the first application page ---------------------------------------------

@dataclass
class Walk:
    """What the check found: the first application page (its digest), or the
    stop it scored at once, or why nothing could be read (`unread`)."""
    url: str = ""
    system: str = ""
    state: str = ""
    stop: str = ""
    captcha: bool = False
    account_wall: bool = False
    digest: Any = None
    unread: str = ""
    notes: list[str] = field(default_factory=list)
    clicks: int = 0
    checked_at: str = ""


@dataclass
class Step:
    """The walk's step on one page (`_Walker._decision`): "entry" (click
    `button`), "form" (the first application page), "account" (an account
    step or a code gate first), "check" (a bot check first), "stop" (scored
    at once as `stop`) or "unread" (nothing to score; `note` says why). A
    step from the page's kind carries it (`state`), with the digest and
    facts it was read from."""
    kind: str
    state: str = ""
    stop: str = ""
    note: str = ""
    button: Any = None
    digest: Any = None
    facts: Any = None


class _Walker:
    """One job's walk: the posting in the profile, its Apply entry followed
    the way the drain follows it (LinkedIn's offsite Apply, a job board's
    company link, the posting's Apply), then the first application page read.
    The only clicks are Apply entries; nothing is typed, ticked, picked or
    uploaded, and no sign-in, sign-up, next or submit is clicked."""

    def __init__(self, context, entry: Mapping[str, Any], judge, log: logging.Logger):
        self.ctx = context
        self.entry = entry
        self.judge = judge
        self.log = log
        self.page = None
        self.walk = Walk()
        self.boards: list[str] = []
        self.linkedin_clicks = 0
        self.hosts: set[str] = set()
        self.last_sig = None

    # --- the walk's ends ------------------------------------------------------------------

    def _stop(self, kind: str, note: str = "") -> bool:
        self.walk.stop = kind
        if note:
            self.walk.notes.append(note)
        return True

    def _unread(self, why: str) -> bool:
        self.walk.unread = why
        return True

    def run(self) -> Walk:
        import apply_run
        job_id = str(self.entry.get("job_posting_id") or "")
        if apply_run._easy_apply(self.entry):
            self.walk.system = "linkedin"
            self.walk.checked_at = _now()
            self._stop("easy_apply")
            return self.walk
        url = str(self.entry.get("apply_url") or "")
        if not url:
            self._unread("the queue entry has no apply URL")
            return self.walk
        before = list(self.ctx.pages)
        self.page = self.ctx.new_page()
        try:
            try:
                apply_run.open_page(self.page, url)
            except Exception as e:      # noqa: BLE001  (a dead or slow page is the answer)
                if apply_run._closed_error(e):
                    raise
                self._unread(f"the posting did not load ({type(e).__name__})")
                return self.walk
            for _ in range(PAGES_MAX):
                if self._step():
                    break
            else:
                self._unread(f"no application page within {PAGES_MAX} pages")
        finally:
            self.walk.url = self.walk.url or str(getattr(self.page, "url", "") or "")
            for p in list(self.ctx.pages):
                if not any(p is b for b in before):
                    try:
                        p.close()
                    except Exception:   # noqa: BLE001  (already closed)
                        pass
            self.log.info("job %s: difficulty walk %s", job_id,
                          self.walk.stop or self.walk.state or self.walk.unread)
        return self.walk

    # --- one page -------------------------------------------------------------------------

    def _step(self) -> bool:
        import apply_linkedin
        import apply_run
        page = self.page
        url = str(page.url or "")
        if apply_run._error_page(url):
            return self._unread("a page did not load (Chrome's error page)")
        kind = apply_linkedin.url_kind(url)
        if kind == "signed_out":
            return self._unread(f"{apply_linkedin.SIGNED_OUT_REASON}; "
                                f"{apply_run.LINKEDIN_LOGIN_NOTE}")
        if kind == "redirector":
            self._arrive()
            if apply_run._on_linkedin_redirector(str(self.page.url)):
                return self._unread("the LinkedIn redirect did not move on")
            return False
        if not apply_linkedin.is_linkedin(url):
            self.hosts.add(apply_run._host(url))
        digest = self._digest()
        sig = apply_run.page_signature(url, digest)
        if sig == self.last_sig:
            return self._unread("the page did not advance after the Apply entry")
        self.last_sig = sig
        if kind in ("job", "other"):
            done = self._linkedin(kind)
            if done is not None:
                return done
        host = apply_run._host(url)
        if apply_run._aggregator(host):
            return self._board(host, digest)
        return self._judged(digest)

    def _digest(self):
        """The page's digest the way the drain reads it: an empty read or a
        loading placeholder waited on, and the controls of a bot-check frame,
        another site's frame or a LinkedIn widget left out."""
        import apply_click
        import apply_fill
        import apply_run
        import apply_limits
        page = self.page

        def busy() -> bool:
            try:
                return bool(apply_click.ready_snapshot(page)[1])
            except Exception:       # noqa: BLE001  (a page mid-navigation)
                return False

        def read():
            before = busy()
            digest = self._drop_foreign(self._extract())
            after = not before and not digest.fields and busy()
            return digest, (before or after) and not digest.fields

        digest, loading = read()
        empty = apply_run._empty_read(digest)
        if not empty and not loading:
            return digest
        start = time.monotonic()
        last, stable_since = json.dumps(digest.to_dict(), sort_keys=True), start
        if empty:
            apply_fill.settle(page, apply_limits.CLICK_TIMEOUT_S)
        while True:
            digest, loading_now = read()
            now = time.monotonic()
            loading = loading and loading_now and now - start < apply_limits.LOADING_WAIT_S
            if not apply_run._empty_read(digest) and not loading:
                break
            seen = json.dumps(digest.to_dict(), sort_keys=True)
            if seen != last:
                last, stable_since = seen, now
            if now - start >= apply_limits.EMPTY_READ_MAX_S or (
                    not loading and now - stable_since >= apply_limits.EMPTY_READ_STABLE_S):
                break
            page.wait_for_timeout(int(apply_limits.EMPTY_READ_POLL_S * 1000))
        return digest

    def _extract(self):
        import apply_click
        import apply_form
        import apply_run
        page = self.page
        apply_click.watch_requests(page)
        return apply_form.extract(page, content_site=lambda url: apply_run.content_frame_site(
            url, str(page.url), self.hosts))

    def _frame_url(self, frames: list, idx: int) -> str:
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

    def _drop_foreign(self, digest):
        import apply_linkedin
        import apply_run
        frames = list(self.page.frames)
        page_url = str(self.page.url)
        on_linkedin = apply_linkedin.is_linkedin(page_url)
        dropped: set[int] = set()
        for idx in sorted({int(item.locator[0]) for item in (*digest.fields, *digest.buttons)}):
            if not 0 <= idx < len(frames):
                dropped.add(idx)
                continue
            url = self._frame_url(frames, idx)
            host = "" if url.startswith("about:") else apply_run._host(url)
            if apply_run._is_captcha_url(url) or (
                    idx > 0 and host and not apply_run.content_frame_site(url, page_url,
                                                                          self.hosts)) \
                    or (idx > 0 and not on_linkedin and apply_linkedin.is_linkedin(host)):
                dropped.add(idx)
        if not dropped:
            return digest
        return dataclasses.replace(
            digest, fields=[f for f in digest.fields if int(f.locator[0]) not in dropped],
            buttons=[b for b in digest.buttons if int(b.locator[0]) not in dropped])

    def _challenge(self, *, checkbox: bool = False) -> bool:
        """A bot check waiting for the person (the drain's
        `_human_check_showing`): a CAPTCHA provider's frame tall enough to be
        a challenge, or with `checkbox` an unticked CAPTCHA checkbox."""
        import apply_form
        import apply_run
        import apply_limits
        try:
            frames = list(self.page.frames)
        except Exception:       # noqa: BLE001
            return False
        for frame in frames:
            if not apply_run._is_captcha_url(str(getattr(frame, "url", "") or "")):
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

    # --- LinkedIn and job boards, read by code ------------------------------------------------

    def _linkedin(self, kind: str) -> bool | None:
        import apply_linkedin
        import apply_run
        import apply_limits
        view, _waited = apply_run.linkedin_view(
            self.page, wait_s=apply_limits.LINKEDIN_READY_S if kind == "job" else 0,
            job_title=str(self.entry.get("title") or ""),
            company=str(self.entry.get("company") or ""))
        d = apply_linkedin.decide(view)
        if kind == "other" and d.kind not in ("form_dialog", "signed_out"):
            return None
        self.walk.system = self.walk.system or "linkedin"
        if d.kind in ("form_dialog", "easy_apply"):
            return self._stop("easy_apply")
        if d.kind == "closed":
            return self._stop("closed")
        if d.kind == "applied":
            return self._unread(apply_linkedin.APPLIED_REASON)
        if d.kind == "signed_out":
            return self._unread(f"{apply_linkedin.SIGNED_OUT_REASON} ({d.why}); "
                                f"{apply_run.LINKEDIN_LOGIN_NOTE}")
        if d.kind != "offsite" or d.control is None:
            return self._stop("dead", f"{apply_linkedin.NO_APPLY_REASON} ({d.why})")
        self.linkedin_clicks += 1
        if self.linkedin_clicks > 1:
            return self._unread(f"the offsite Apply ({d.control.label}) did not open the "
                                f"company's site")
        return self._click(self.page.main_frame.locator(d.control.css), d.control.label)

    def _board(self, host: str, digest) -> bool:
        import apply_form
        import apply_run
        if host in self.boards or len(self.boards) >= apply_run.AGGREGATOR_BOARDS_MAX:
            chain = " -> ".join([*self.boards, host])
            return self._stop("dead", f"A chain of job boards ({chain}) and no company site")
        self.boards.append(host)
        control = apply_run.company_site_control(
            digest, board=host, targets=apply_run.link_targets(self.page, digest))
        if control is None:
            return self._stop("dead", f"A job board's posting ({host}) with no link to the "
                                      f"company's site")
        done = self._click(apply_form.resolve(self.page, control.locator), control.text)
        if not done and apply_run._host(str(self.page.url)) == host:
            return self._stop("dead", f"The job board's link to the company's site stayed on "
                                      f"{host}")
        return done

    # --- every other page, read by Jev ------------------------------------------------------

    def _read(self, digest):
        import apply_judge
        url = str(self.page.url or "")
        facts = apply_judge.page_facts(digest, url, captcha_frame=self._challenge())
        state_q, questions = apply_judge.read_questions(digest, url)
        raw = dict(self.judge.judge(state_q, questions))
        read = apply_judge.read_page(raw, facts)
        answers = {k: v for k, v in raw.items() if k in questions}
        if "page_state" in answers:
            answers["page_state_judged"] = answers["page_state"]
        answers["page_state"] = apply_judge.read_answer(read)
        return answers, facts

    def _judged(self, digest) -> bool:
        """The page's step (`_decision`), taken: the Apply entry clicked, the
        form read, an account step or a bot check noted, or the walk's end."""
        import apply_form
        step = self._decision(digest)
        if step.kind == "unread":
            return self._unread(step.note)
        if step.state:
            self.walk.state = step.state
            self.walk.url = str(self.page.url or "")
            self.walk.system = self._system() or (
                "linkedin" if self._on_linkedin() else "")
        if step.kind == "stop":
            return self._stop(step.stop, step.note)
        if step.kind == "entry":
            b = step.button
            return self._click(apply_form.resolve(self.page, b.locator), b.text)
        if step.kind == "form":
            return self._form(step.digest, step.facts)
        if step.kind == "account":
            import ats_accounts
            self.walk.account_wall = ats_accounts.lookup(str(self.page.url or "")) is None
            self.walk.notes.append(ACCOUNT_NOTE)
            return True
        self.walk.captcha = True            # "check"
        self.walk.notes.append(CHECK_NOTE)
        return True

    def _on_linkedin(self) -> bool:
        import apply_linkedin
        return apply_linkedin.is_linkedin(str(self.page.url or ""))

    def _decision(self, digest) -> Step:
        """What the drain's loop does with this page (`apply_run._JobRun._loop`,
        in the order `apply_run.loop_step` describes it), as the walk's step:
        the page read (a read under the floor read once more after a settle),
        a job the site says was applied to, a sign-in read of form boxes
        taken as the form, a confirmation read before any submit (one under
        the floor goes to the unsure rule, as the drain's does), a form step
        on LinkedIn, a sign-in with another site as the only way on, the
        unsure and `other` rules, the emailed-link remap, then the page's
        kind. A posting's Apply entry is chosen over the buttons' mapped
        roles (`_posting_plan`), as the drain chooses it, with one exception:
        an entry that reads as a sign-in or a sign-up
        (`account_worded`) is never clicked. A judged one gives way to the
        text choice, and a text choice that reads so is an account step."""
        import apply_fill
        import apply_judge
        import apply_linkedin
        import apply_run
        import apply_limits
        answers, facts = self._read(digest)
        state, conf = apply_judge.read_page_state(answers)
        if conf < apply_judge.PAGE_STATE_MIN_CONF:
            apply_fill.settle(self.page, apply_limits.CLICK_TIMEOUT_S)
            digest = self._digest()
            answers, facts = self._read(digest)
            state, conf = apply_judge.read_page_state(answers)
        url = str(self.page.url or "")
        on_linkedin = apply_linkedin.is_linkedin(url)
        easy = Step("stop", stop="easy_apply")
        if apply_judge.already_applied(answers, facts):
            return Step("unread", note="the site says this job was applied to before")
        if apply_run.remaps_to_form(state, digest, url):
            state = "application_form"
        unsure = conf < apply_judge.PAGE_STATE_MIN_CONF
        if state == "confirmation" and not facts.link_sent:
            step, detail, then = apply_run.confirmation_step(digest, answers, conf,
                                                             submit_clicked=False)
            if step == "go_on":
                state, conf = detail, then
                unsure = conf < apply_judge.PAGE_STATE_MIN_CONF
            elif not (step == "park" and unsure):
                return Step("unread", note="the page reads as a confirmation before any "
                                           "submit; check whether this job was applied to "
                                           "before")
        if on_linkedin and state in apply_run._LINKEDIN_FORM_STATES:
            return easy
        if state != "confirmation" and not on_linkedin:
            sites = apply_run.sso_only(digest)
            if sites:
                return Step("stop", stop="dead", note=SSO_NOTE.format(sites=", ".join(sites)))
        if unsure:
            settled, _how = apply_run.unsure_step(state, digest, facts)
            if settled is None:
                return Step("unread", note=f"unsure what this page is ({state}, {conf:.2f})")
            state = settled
        elif state == "other":
            state = apply_run.other_step(facts, digest) or state
        if on_linkedin and state in apply_run._LINKEDIN_FORM_STATES:
            return easy
        if state in apply_run._LINK_REMAPS and facts.link_sent:
            state = "code_gate"
        found = dict(state=state, digest=digest, facts=facts)
        if state == "job_posting":
            plan = self._posting_plan(digest, answers)
            if on_linkedin:
                if digest.fields and apply_run.posting_entry_choice(digest, plan)[0] is None:
                    return Step("stop", stop="easy_apply", **found)
                return Step("stop", stop="dead", note=apply_linkedin.NO_APPLY_REASON, **found)
            apart, unclassified, _scan = apply_run.posting_context(self.page, digest, plan)
            n, how = apply_run.posting_entry_choice(digest, plan, apart=apart,
                                                    unclassified=unclassified)
            if how == "judged_apply_entry" and self._account_entry(digest, n):
                text_only = dataclasses.replace(plan, buttons={
                    role: held for role, held in plan.buttons.items() if role != "apply_entry"})
                n, how = apply_run.posting_entry_choice(digest, text_only, apart=apart,
                                                        unclassified=unclassified)
            if n is not None and self._account_entry(digest, n):
                return Step("account", **found)
            if n is not None:
                return Step("entry", button=next(b for b in digest.buttons if b.n == n),
                            **found)
            if digest.fields:
                return Step("form", **found)
            return Step("stop", stop="dead", note="No Apply button on the posting", **found)
        if state in ("application_form", "review_page"):
            return Step("form", **found)
        if state in ("login_wall", "signup_form", "code_gate"):
            return Step("account", **found)
        if state == "captcha_or_bot_check":
            return Step("check", **found)
        if state == "payment_request":
            return Step("stop", stop="payment", **found)
        if apply_judge.closed_posting(answers, facts, state):
            return Step("stop", stop="closed", **found)
        if state == "error_or_dead":
            return Step("stop", stop="dead", **found)
        return Step("unread", note=f"the page reads as none of the kinds the check knows "
                                   f"({state}, {conf:.2f})")

    def _account_entry(self, digest, n: int) -> bool:
        """Is button `n` a way into an account (`account_worded`) by the text
        it was read with or by its live text now (`apply_form.live_text`)."""
        import apply_form
        button = next(b for b in digest.buttons if b.n == n)
        if account_worded(button.text):
            return True
        live = apply_form.live_text(apply_form.resolve(self.page, button.locator))
        return account_worded(str(live.get("text") or ""))

    def _posting_plan(self, digest, answers):
        """The posting's plan with its buttons' roles mapped, as the drain
        maps a posting (`apply_run._JobRun._map`: the buttons alone on a
        posting with no field, the fields too on one with fields), over an
        empty fact catalog: the walk fills nothing, so only the roles count."""
        import apply_facts
        import apply_judge
        catalog = apply_facts.FactCatalog([])
        merged = dict(answers)
        requests = [(st, q) for st, q in apply_judge.page_requests(
            digest, catalog, dict(self.entry), fields=bool(digest.fields)) if q]
        if requests:
            merged.update(apply_judge.merge_answers([
                {k: v for k, v in self.judge.judge(st, q).items() if k in q}
                for st, q in requests]))
        return apply_judge.plan(digest, catalog, merged)

    def _system(self) -> str:
        """The application system: the page's host, else a frame's (an
        embedded Greenhouse or iCIMS form)."""
        found = system_for(str(self.page.url or ""))
        if found:
            return found
        try:
            frames = list(self.page.frames)
        except Exception:       # noqa: BLE001
            return ""
        for frame in frames[1:]:
            found = system_for(str(getattr(frame, "url", "") or ""))
            if found:
                return found
        return ""

    def _form(self, digest, facts) -> bool:
        self.walk.digest = digest
        self.walk.captcha = self._challenge(checkbox=True) or bool(facts.captcha)
        self.walk.checked_at = _now()
        return True

    # --- the one click ----------------------------------------------------------------------

    def _click(self, loc, text: str) -> bool:
        """Click an Apply entry (`apply_run.click_entry`) and follow it: a new
        tab is taken, the same tab waits out LinkedIn's redirect and a
        tracker's hop, and a tab that opens late is taken when the click left
        the page as it was. True when the walk ends here."""
        import apply_form
        import apply_run
        import apply_limits
        live = apply_form.live_text(loc)
        why = apply_run.live_refusal("apply_entry", text, live) if live else ""
        if why:
            return self._unread(f"the Apply entry {why}")
        address = apply_run.mailto_address(loc)
        if address:
            return self._stop("dead", MAILTO_NOTE.format(address=address))
        if self.walk.clicks >= ENTRY_HOPS_MAX:
            return self._unread(f"no application page after {ENTRY_HOPS_MAX} Apply entries")
        source = str(self.page.url)
        popup, signal, _waited = apply_run.click_entry(self.page, loc)
        self.walk.clicks += 1
        if signal.startswith("failed"):
            return self._unread(f"the Apply entry did not take the click ({signal[8:]})")
        if popup is not None:
            try:
                popup.wait_for_load_state("domcontentloaded",
                                          timeout=apply_limits.CLICK_TIMEOUT_S * 1000)
            except Exception:       # noqa: BLE001
                pass
            self.page = popup
            self._arrive()
            return False
        watch = apply_run.LateWatch(self.page, signal, source)
        watch.start()
        try:
            self._arrive()
        finally:
            watch.stop()
        late = [p for p in watch.popups if p is not self.page and not apply_run._page_closed(p)]
        if late and signal in ("dom", "none"):
            self.page = late[0]
            self._arrive()
        return False

    def _arrive(self) -> None:
        """Settle the page an entry led to. LinkedIn's redirect is waited out;
        its safety reminder's Continue link is opened as an address (a load,
        no click); a tracker's hop is waited out (`apply_run._past_trackers`)."""
        import apply_fill
        import apply_linkedin
        import apply_run
        import apply_limits
        page = self.page
        apply_fill.settle(page, apply_limits.CLICK_TIMEOUT_S)
        if apply_run._on_linkedin_redirector(str(page.url)):
            cont = apply_linkedin.continue_control(page)
            href = urljoin(str(page.url), cont.href) if cont is not None and cont.href else ""
            if href.lower().startswith(("http://", "https://")) \
                    and not apply_linkedin.is_linkedin(href):
                try:
                    apply_run.open_page(page, href)
                except Exception as e:      # noqa: BLE001  (the next read says what shows)
                    if apply_run._closed_error(e):
                        raise
            else:
                try:
                    page.wait_for_url(lambda u: not apply_run._on_linkedin_redirector(u),
                                      timeout=apply_limits.REDIRECT_TIMEOUT_S * 1000)
                    apply_fill.settle(page, apply_limits.CLICK_TIMEOUT_S)
                except Exception as e:      # noqa: BLE001  (the next read says it stayed)
                    if apply_run._closed_error(e):
                        raise
        apply_run._past_trackers(page, {}, self.log,
                                 str(self.entry.get("job_posting_id") or ""))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- the screening and the result --------------------------------------------------------

def catalog_for(entry: Mapping[str, Any], answers: list[dict]):
    """The job's fact catalog, as the drain builds it: the job folder's sheet
    and the confirmed answers, with the entry's PDFs; None when the job has no
    apply.md. The check writes nothing to the folder."""
    import apply_facts
    arts = entry.get("artifacts") or {}
    apply_md = str(arts.get("apply_md") or "")
    folder = Path(apply_md).parent if apply_md else (
        Path(str(arts["folder"])) if arts.get("folder") else None)
    if folder is None or not (folder / "apply.md").exists():
        return None
    catalog = apply_facts.build(folder, answers=answers)
    for art_key, fact_key in (("resume_pdf", "resume_file"),
                              ("cover_letter_pdf", "cover_letter_file")):
        path = str(arts.get(art_key) or "")
        fact = catalog.facts.get(fact_key)
        if not path or fact is None:
            continue
        catalog.facts[fact_key] = apply_facts.Fact(
            key=fact_key, value=path if Path(path).is_file() else "",
            description=fact.description, kind=fact.kind)
    return catalog


@dataclass
class Tally:
    unanswered: int = 0
    essays: int = 0
    sensitive: bool = False
    password: bool = False
    questions: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def tally(digest, plan, *, generate: bool) -> Tally:
    """Count what the screening plan leaves to the user, over the page's
    required fields: a question the answers cannot fill (listed with its
    label, help, options and type; an upload with no file in the job folder
    is one, of type "file"), an essay the drafter would write (with
    `generate` on; else it is a question), a sensitive field and a password
    box (an account made inside the form)."""
    import apply_facts
    import apply_judge
    by_n = {f.n: f for f in digest.fields}
    out = Tally()
    for pf in plan.fields:
        f = by_n.get(pf.n)
        if f is None or not pf.required:
            continue
        if apply_judge.AUTOFILL_PARSER in (f.help or ""):
            continue
        if pf.action == apply_judge.PASSWORD_ACTION:
            out.password = True
            continue
        if pf.action != "skip":
            continue
        if apply_judge.is_sensitive_field(f.label, f.id_or_name):
            out.sensitive = True
            continue
        if pf.fact_key == "needs_generation" and generate:
            out.essays += 1
            continue
        out.unanswered += 1
        kind = "file" if f.type == "file" else apply_facts.answer_type(f.type, f.options)
        out.questions.append({"label": f.label, "help": f.help or "",
                              "options": list(f.options or []), "required": True,
                              "type": kind})
    return out


def page_record(walk: Walk, job_id: str) -> dict:
    """What the walk read on a job's page, as `assess_page` scores it."""
    return {"job_id": str(job_id), "url": walk.url, "system": walk.system,
            "state": walk.state, "captcha": walk.captcha, "account_wall": walk.account_wall,
            "stop": walk.stop, "notes": list(walk.notes),
            "checked_at": walk.checked_at or _now(),
            "digest": walk.digest.to_dict() if walk.digest is not None else None}


def assess_page(page: Mapping[str, Any], entry: Mapping[str, Any], *, judge, answers: list[dict],
                settings: Mapping[str, Any], entries: list[Mapping[str, Any]]) -> dict:
    """The difficulty for a read page: the stop's 10, or
    the score over the screening of its form (`apply_screening.screen_page`
    with the catalog of the confirmed answers) and the past runs on its
    system. Returns the queue entry's `difficulty` without `jev_usd`."""
    import apply_screening
    import ats_accounts
    from apply_form import FormDigest
    system = str(page.get("system") or "")
    base = {"checked_at": str(page.get("checked_at") or _now()), "system": system}
    if page.get("stop"):
        result = score(stop=str(page["stop"]))
        return {**base, **result, "reasons": result["reasons"] + list(page.get("notes") or []),
                "questions": []}
    found = Tally()
    raw = page.get("digest")
    if raw:
        digest = FormDigest.from_dict(raw)
        catalog = catalog_for(entry, answers)
        if catalog is not None:
            plan = apply_screening.screen_page(
                digest, catalog, judge,
                {"company_name": str(entry.get("company") or ""),
                 "job_title": str(entry.get("title") or "")})
            found = tally(digest, plan, generate=bool(settings.get("auto_apply_generate", True)))
        else:
            found.notes.append("The job folder has no apply.md, so its answers were not read")
    wall = bool(page.get("account_wall"))
    if found.password and not wall:
        wall = ats_accounts.lookup(str(page.get("url") or "")) is None
    ends, parks = past_runs(list(entries), system, str(entry.get("job_posting_id") or ""))
    result = score(system=system, unanswered=found.unanswered, essays=found.essays,
                   sensitive=found.sensitive, captcha=bool(page.get("captcha")),
                   account_wall=wall, past_submits=ends, past_parks=parks)
    return {**base, **result,
            "reasons": result["reasons"] + list(page.get("notes") or []) + found.notes,
            "questions": found.questions}


def check_job(entry: Mapping[str, Any], *, context, judge, answers: list[dict],
              settings: Mapping[str, Any], entries: list[Mapping[str, Any]],
              log: logging.Logger | None = None) -> tuple[dict | None, str]:
    """(the difficulty, "") for one queued job, read in `context` (the
    persistent profile's browser context), or (None, why) when no page could
    be read."""
    log = log or logging.getLogger("apply_assess")
    job_id = str(entry.get("job_posting_id") or "")
    walk = _Walker(context, entry, judge, log).run()
    if walk.unread:
        return None, walk.unread
    return assess_page(page_record(walk, job_id), entry, judge=judge, answers=answers,
                       settings=settings, entries=entries), ""


# --- the console -----------------------------------------------------------------------

class _Counting:
    """The judge with a count of the requests it answered."""

    def __init__(self, inner):
        self.inner = inner
        self.requests = 0

    def judge(self, state, questions):
        answers = self.inner.judge(state, questions)
        self.requests += 1
        return answers


@dataclass(frozen=True)
class _Spent:
    """A request count for `_note` when no `_Counting` judge is left to read."""
    requests: int


def _say(text: str) -> None:
    """`text` on stdout, in whatever the console can show. `apply_run._say`
    does the same; importing `apply_run` for it would add its import time to
    the dashboard's, which imports this module."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc), flush=True)


# --- the worker's result line ----------------------------------------------------------

RESULT_PREFIX = "@@assess-result "
OUTCOMES = ("scored", "unread", "outage", "closed", "error")
# An error result may carry one mark for the coordinator: `refusal` (the
# check refused to run at all: a gate, no browser; the pool stops) or
# `failed` (the worker crashed, or its slot stayed busy, and noted nothing on
# the queue; the pool notes it and goes on). Neither key is there otherwise.
MARKS = ("refusal", "failed")
WORKER_USAGE = "apply_assess: --worker takes exactly one job id and --profile, and no --all"
SLOT_BUSY = "its profile copy was still open in the browser of the job before"
SLOT_BUSY_WAIT_S = 5.0      # how long a worker waits for its slot's last browser to close
SLOT_BUSY_POLL_S = 0.25


def _note(report: dict | None, outcome: str, why: str, *, job_id: str = "", counted=None,
          usd: float = 0.0, score: int | None = None, band: str = "",
          refusal: bool = False, failed: bool = False) -> None:
    """Record the last job's outcome for a worker (`run`'s `report`); a run
    with no report records nothing. `refusal` and `failed` set the MARKS."""
    if report is None:
        return
    report.clear()
    report.update(job_id=job_id, outcome=outcome, score=score, band=band, why=why,
                  requests=int(getattr(counted, "requests", 0) or 0), usd=round(float(usd), 6))
    if refusal:
        report["refusal"] = True
    if failed:
        report["failed"] = True


def worker_result(report: Mapping[str, Any], *, job_id: str = "") -> dict:
    """The worker's result: the keys of `RESULT_PREFIX`'s JSON, whole, and a
    mark (MARKS) when the report has one. A run that recorded nothing is an
    error, so the coordinator never has to guess."""
    got = dict(report)
    if got.get("outcome") not in OUTCOMES:
        got = {"outcome": "error", "why": "the check ended with no result"}
    result = {"job_id": str(got.get("job_id") or job_id), "outcome": got["outcome"],
              "score": got.get("score"), "band": str(got.get("band") or ""),
              "why": str(got.get("why") or ""), "requests": int(got.get("requests") or 0),
              "usd": float(got.get("usd") or 0.0)}
    for mark in MARKS:
        if got.get(mark):
            result[mark] = True
    return result


def result_line(result: Mapping[str, Any]) -> str:
    """The worker's last stdout line for `result` (`worker_result`)."""
    return RESULT_PREFIX + json.dumps(dict(result), separators=(",", ":"))


def parse_result_line(text: str) -> dict | None:
    """The result a worker's output ends with: the last `RESULT_PREFIX` line,
    when it holds a JSON object with a known outcome; None otherwise."""
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            got = json.loads(line[len(RESULT_PREFIX):])
        except ValueError:
            return None
        if isinstance(got, dict) and got.get("outcome") in OUTCOMES:
            return got
        return None
    return None


def select_jobs(entries: list[Mapping[str, Any]], job_ids: list[str], *,
                all_queued: bool) -> tuple[list[Mapping[str, Any]], list[str]]:
    """(the entries to check, the ids that are not in the queue). `all_queued`
    takes every queued job; ids take any job the drain is not running."""
    if all_queued:
        return [e for e in entries if e.get("status") == "queued"], []
    by_id = {str(e.get("job_posting_id")): e for e in entries}
    chosen, unknown = [], []
    for jid in job_ids:
        e = by_id.get(str(jid))
        if e is None:
            unknown.append(str(jid))
        elif e.get("status") != "in_progress":
            chosen.append(e)
    return chosen, unknown


def run(job_ids: list[str], *, all_queued: bool = False, judge,
        settings: Mapping[str, Any], context=None, queue_path: Path | None = None,
        log: logging.Logger | None = None, report: dict | None = None) -> int:
    """Check each job and store its difficulty on its queue entry, printing
    one line per job with the running Jev total. `context` is the browser
    context (the persistent profile's; tests inject one). Exit 0, 1 when the
    judge went down or the window closed, 2 when nothing could be checked.
    `report`, when given, is filled with the last job's outcome for a worker
    (`worker_result`); the printed lines do not change."""
    import apply_run
    import jev
    from resume_tailor import apply_answers
    log = log or logging.getLogger("apply_assess")
    try:
        answers = apply_answers.load()
    except apply_answers.AnswerStoreError as e:
        text = (f"The Apply Answers file is damaged ({e.path}): {e.reason}. Open the "
                f"dashboard's Apply Answers tab to restore the backup.")
        _note(report, "error", text, refusal=True)
        _say(text)
        return 2
    entries = list(apply_queue.load(queue_path).get("jobs") or [])
    chosen, unknown = select_jobs(entries, job_ids, all_queued=all_queued)
    for jid in unknown:
        _say(f"apply_assess: job {jid} is not in the queue")
        _note(report, "error", f"job {jid} is not in the queue", job_id=str(jid))
    if not chosen:
        _say("apply_assess: no job to check" + ("" if unknown or not all_queued
                                                  else " (nothing is queued)"))
        if not unknown:
            _note(report, "error", "no job to check")
        return 0 if all_queued and not unknown else 2
    counted = _Counting(judge)
    # the lifetime counter: a reset of usage() elsewhere never hides this spend
    usd0 = jev.total_usage()["usd"]
    code = 0
    for i, entry in enumerate(chosen, 1):
        jid = str(entry.get("job_posting_id") or "")
        name = f"{entry.get('company') or '?'} / {entry.get('title') or '?'}"
        usd_before = jev.total_usage()["usd"]
        unread = False
        try:
            result, why = check_job(entry, context=context, judge=counted, answers=answers,
                                    settings=settings, entries=entries, log=log)
        except jev.JudgeOutage as e:
            _note(report, "outage", f"Jev is down ({e})", job_id=jid, counted=counted,
                  usd=jev.total_usage()["usd"] - usd0)
            _say(f"[{i}/{len(chosen)}] {name}: Jev is down ({e}); the check stops here")
            return 1
        except Exception as e:      # noqa: BLE001  (one line; the frames go to the log)
            if apply_run._closed_error(e):
                _note(report, "closed", "the browser window was closed", job_id=jid,
                      counted=counted, usd=jev.total_usage()["usd"] - usd0)
                _say(f"[{i}/{len(chosen)}] {name}: the browser window was closed; the check "
                     f"stops here")
                return 1
            result, why = None, f"{type(e).__name__} at {apply_run.error_step(e)}"
            log.error("job %s: %s; traceback (the message left out):\n  %s", jid, why,
                      "\n  ".join(apply_run.error_frames(e)))
        else:
            unread = result is None
        spent = jev.total_usage()["usd"] - usd_before
        total = jev.total_usage()["usd"] - usd0
        running = f"Jev so far: {counted.requests} request(s), ${total:.4f}"
        if result is None:
            _note(report, "unread" if unread else "error", why, job_id=jid, counted=counted,
                  usd=total)
            _say(f"[{i}/{len(chosen)}] {name}: not checked ({why}). {running}")
            try:
                # the earlier result stays; the panel shows the failure beside it
                apply_queue.note_difficulty_failure(jid, why, path=queue_path)
            except apply_queue.UnknownJobError:
                pass
            except apply_queue.QueueLockTimeout:
                _say(f"[{i}/{len(chosen)}] {name}: {QUEUE_LOCKED}.")
            continue
        result["jev_usd"] = round(spent, 6)
        try:
            apply_queue.set_difficulty(jid, result, path=queue_path)
        except apply_queue.UnknownJobError:
            _note(report, "error", "left the queue while it was checked", job_id=jid,
                  counted=counted, usd=total)
            _say(f"[{i}/{len(chosen)}] {name}: left the queue while it was checked. {running}")
            continue
        except apply_queue.QueueLockTimeout:
            # nothing is noted on the queue: `failed` has the pool note it
            _note(report, "error", QUEUE_LOCKED, job_id=jid, counted=counted, usd=total,
                  failed=True)
            _say(f"[{i}/{len(chosen)}] {name}: {result['score']}/10, but {QUEUE_LOCKED}. "
                 f"{running}")
            code = 1
            continue
        _note(report, "scored", "", job_id=jid, counted=counted, usd=total,
              score=result["score"], band=result["band"])
        _say(f"[{i}/{len(chosen)}] {name}: {result['score']}/10, {result['band']}. {running}")
    return code


def _refuse(report: dict | None, text: str, code: int, *, job_id: str = "") -> int:
    """Print `text` on stderr, record it as the worker's error marked as a
    refusal (the pool stops), return `code`."""
    print(text, file=sys.stderr)
    _note(report, "error", text, job_id=job_id, refusal=True)
    return code


def _slot_busy(report: dict, *, job_id: str = "") -> int:
    """A worker's slot still held by the browser of the job before it: this
    job's own failure (marked `failed`, the pool notes it and goes on), exit 1."""
    print(f"apply_assess: job {job_id}: {SLOT_BUSY}", file=sys.stderr)
    _note(report, "error", SLOT_BUSY, job_id=job_id, failed=True)
    return 1


def _slot_frees(profile: Path) -> bool:
    """Wait up to SLOT_BUSY_WAIT_S for a slot's browser to let go of it."""
    deadline = time.monotonic() + SLOT_BUSY_WAIT_S
    while profile_busy(profile):
        if time.monotonic() >= deadline:
            return False
        time.sleep(SLOT_BUSY_POLL_S)
    return True


def main(argv: list[str] | None = None, *, context=None) -> int:
    """`python local/apply_assess.py [--all | <id> ...]`. Exit 0,
    1 on an error the check could not go past (no browser starting among
    them), 2 when it refuses (Jev off, a test judge or an unknown one, the
    profile in use, nothing to check).

    The browser is the auto-apply profile as the drain opens it
    (`apply_run.launch_profile`): Google Chrome, or the bundled Chromium when
    Chrome will not start. The sentinel `launch_profile` takes covers either
    one and stays held while the check runs, so a drain started meanwhile
    refuses.

    Two or more jobs with more than one check at once (`--parallel N`, else
    the `auto_apply_check_parallel` setting, 1 to 10) run in parallel
    (`assess_pool.run_pool`): each job in its own browser window on a copy
    of the profile, the real profile held for the whole run. One job, or one
    check at once, is the path above.

    `--worker --profile <dir> <id>` is one job of a parallel check
    (`assess_pool`): the same path on that profile, and whatever happens its
    last stdout line is `RESULT_PREFIX` and the outcome as JSON."""
    import argparse
    ap = argparse.ArgumentParser(prog="apply_assess",
                                 description="Score how hard each queued job is to auto-apply.")
    ap.add_argument("job_ids", nargs="*", help="queue job ids")
    ap.add_argument("--all", action="store_true", dest="all_queued", help="every queued job")
    ap.add_argument("--headless", action="store_true", help="no browser window")
    ap.add_argument("--queue", default=None, help="queue file")
    ap.add_argument("--profile", default=None, help="browser profile dir")
    ap.add_argument("--worker", action="store_true",
                    help="check one job on --profile and end with a result line "
                         "(the parallel check's child)")
    ap.add_argument("--parallel", type=int, default=None,
                    help="checks at once, 1 to 10, each in its own browser window "
                         "(the auto_apply_check_parallel setting unless given)")
    ap.add_argument("--verbose", action="store_true", help="DEBUG logging")
    args = ap.parse_args(argv)
    if not args.worker:
        return _main(args, ap, context, None)
    import jev
    report: dict = {}
    job_id = str(args.job_ids[0]) if args.job_ids else ""
    start = jev.total_usage()
    try:
        if args.all_queued or len(args.job_ids) != 1 or not args.profile:
            code = _refuse(report, WORKER_USAGE, 2, job_id=job_id)
        else:
            code = _main(args, ap, context, report)
    except Exception as e:      # noqa: BLE001  (the coordinator reads the line; the log keeps the frames)
        code = 1
        logging.getLogger("apply_assess").exception("job %s: the worker failed", job_id)
        # what Jev was paid before the crash still counts toward the pool's
        # "Jev so far": the larger of the job's last note and the lifetime
        # counter's delta since the worker began
        now = jev.total_usage()
        requests = max(int(report.get("requests") or 0), now["requests"] - start["requests"])
        usd = max(float(report.get("usd") or 0.0), now["usd"] - start["usd"])
        _note(report, "error", f"the worker failed ({type(e).__name__})", job_id=job_id,
              counted=_Spent(requests), usd=usd, failed=True)
    _say(result_line(worker_result(report, job_id=job_id)))
    return code


def _pool(args, settings: Mapping[str, Any], profile: Path, queue: Path | None,
          log: logging.Logger) -> int | None:
    """The parallel check (`assess_pool.run_pool`) for two or more jobs with
    more than one check at once (`--parallel`, else the
    `auto_apply_check_parallel` setting), its exit code; None for one job or
    one check at once, which is today's path."""
    import assess_pool
    flag = args.parallel if args.parallel is not None else settings.get(
        "auto_apply_check_parallel")
    parallel = assess_pool.parallel_setting(flag)
    if parallel < 2:
        return None
    entries = list(apply_queue.load(queue).get("jobs") or [])
    chosen, unknown = select_jobs(entries, list(args.job_ids), all_queued=args.all_queued)
    if len(chosen) < 2:
        return None
    return assess_pool.run_pool(chosen, unknown=unknown, parallel=parallel, profile=profile,
                                queue_path=queue, headless=bool(args.headless),
                                verbose=bool(args.verbose), log=log)


def _main(args, ap, context, report: dict | None) -> int:
    """`main` after the arguments; `report` is the worker's, None otherwise."""
    import apply_run
    import jev
    job_id = str(args.job_ids[0]) if args.job_ids else ""
    if not args.all_queued and not args.job_ids:
        ap.print_usage(sys.stderr)
        print("apply_assess: name job ids or pass --all", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log = logging.getLogger("apply_assess")
    settings = apply_run.load_settings()
    mode = jev_switch.apply_mode()
    early = mode_gate(mode)
    if early:
        return _refuse(report, early, 2, job_id=job_id)
    apply_run._load_env()
    refused = refusal(mode=mode)
    if refused:
        return _refuse(report, refused, 2, job_id=job_id)
    profile = Path(args.profile) if args.profile else default_profile_dir()
    if context is None and profile_busy(profile):
        if report is None:
            return _refuse(report, PROFILE_BUSY, 2, job_id=job_id)
        if not _slot_frees(profile):        # the slot's last browser still closing
            return _slot_busy(report, job_id=job_id)
    try:
        judge = jev.Guarded(jev.get(mode), logger=log)
    except (jev.JevUnavailable, ValueError) as e:
        return _refuse(report, f"apply_assess: {e}", 2, job_id=job_id)
    queue = Path(args.queue) if args.queue else None
    common = dict(all_queued=args.all_queued, judge=judge,
                  settings=settings, queue_path=queue, log=log)
    if report is not None:
        common["report"] = report
    if context is not None:
        return run(list(args.job_ids), context=context, **common)
    if report is None:
        pooled = _pool(args, settings, profile, queue, log)
        if pooled is not None:
            return pooled
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        # a refusal: the pool stops at the first worker that says so
        return _refuse(report, NO_PLAYWRIGHT, 1, job_id=job_id)
    with sync_playwright() as pw:
        profile.mkdir(parents=True, exist_ok=True)
        try:
            ctx = apply_run.launch_profile(
                pw, profile, headless=bool(settings.get("auto_apply_headless")) or args.headless,
                log=log)
        except profile_lock.ProfileBusy:
            if report is not None:
                return _slot_busy(report, job_id=job_id)
            return _refuse(report, PROFILE_BUSY, 2, job_id=job_id)  # opened after the read
        except Exception as e:      # noqa: BLE001  (Chrome and the bundled build both failed)
            return _refuse(report, NO_BROWSER.format(why=type(e).__name__), 1, job_id=job_id)
        try:
            return run(list(args.job_ids), context=ctx, **common)
        finally:
            try:
                ctx.close()
            except Exception:       # noqa: BLE001  (closed by the user)
                pass


if __name__ == "__main__":
    raise SystemExit(main())
