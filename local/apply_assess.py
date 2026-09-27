"""The auto-apply difficulty check (cycle 19, DF-1 to DF-6): a 1-10 score per
queued job, so the user knows which jobs to leave to the drain.

DF-3's score is code: a base by application system plus fixed steps for what
the first application page asks, rounded half up and clamped to 1-10. Easy
Apply, a closed or dead posting and a payment page are 10 at once. Jev reads
the page; the counting and the arithmetic stay here, with the constants below.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import apply_queue  # noqa: E402
import jev_switch  # noqa: E402

# --- DF-3: the score -------------------------------------------------------------------

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
    """DF-3: {"score", "band", "reasons"} for what the check read.

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


# --- DF-1 and DF-5: the gate, the profile, the cache ------------------------------------

ENTRY_HOPS_MAX = 4          # Apply entry clicks per job: LinkedIn's, two job boards', the posting's
PAGES_MAX = ENTRY_HOPS_MAX + 2
STALE_DAYS = 7              # a result older than this shows its age
CACHE_FOLDER = "apply_assess"
PROFILE_BUSY = ("The auto-apply browser is open: a run or a sign-in holds its profile. "
                "Check difficulty once that window closes.")
NO_SAVED_PAGE = "no saved page for this job; run Check difficulty first"
ACCOUNT_NOTE = "An account step comes first: its questions show once you sign in"
CHECK_NOTE = "A bot check comes first: its questions show once it clears"
UPLOAD_NOTE = "A required upload ({label}) has no file in the job folder"
MAILTO_NOTE = "The Apply opens an email to {address}"
SSO_NOTE = "Its only way on signs in with {sites}; the run signs in with no other site"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def mode_gate(mode: str) -> str:
    """The judge mode's own refusal, asked before the Jev gate: a test
    judge's (`jev_switch.fixture_only`), as the drain asks it. `mode` is a
    mode `jev_switch.apply_mode` resolved."""
    return jev_switch.fixture_only(mode)


def refusal(*, config: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None,
            mode: str | None = None, saved_key: bool = False) -> str:
    """Why `apply_assess.py` refuses to run, in the sentence it prints and the
    panel's Check difficulty shows; "" when it runs. A test judge first
    (`mode_gate`, as the drain), then the Jev gate for the
    difficulty area (`jev_switch.difficulty_blocked`). `mode` is the judge mode
    (None reads the Auto-apply judge setting); `saved_key` counts a key saved in
    Settings, since the check's console loads `.env` itself."""
    resolved = jev_switch.apply_mode(mode, config=config)
    return mode_gate(resolved) or jev_switch.difficulty_blocked(
        config=config, env=env, mode=resolved, saved_key=saved_key)


def _appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))


def default_profile_dir() -> Path:
    """The drain's persistent browser profile (`apply_run.default_profile_dir`),
    read here without importing the runner, so the dashboard stays light."""
    return _appdata() / "linkedin_watcher" / "browser_profile"


def profile_busy(profile_dir: Path | None = None) -> bool:
    """Does a running browser hold the auto-apply profile (a drain, a sign-in,
    or another check)? Chrome locks a profile to one browser: on Windows its
    `lockfile` stays open for writing, so a second writer is refused; elsewhere
    `SingletonLock` names the host and the process. A missing profile is
    free."""
    folder = Path(profile_dir) if profile_dir else default_profile_dir()
    lock = folder / "lockfile"
    if lock.exists():
        try:
            fd = os.open(str(lock), os.O_WRONLY)
        except PermissionError:
            return True
        except OSError:
            return False
        os.close(fd)
        return False
    if os.name == "nt":
        return False
    try:
        target = os.readlink(str(folder / "SingletonLock"))
    except OSError:
        return False
    try:
        pid = int(target.rsplit("-", 1)[-1])
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def cache_dir() -> Path:
    """Where the check keeps each job's page (DF-5)."""
    return _appdata() / "linkedin_watcher" / CACHE_FOLDER


def cache_path(job_id: str) -> Path:
    """The cached page's file for `job_id`: its id in safe characters, with a
    short hash of the id when any character was replaced."""
    raw = str(job_id)
    safe = _UNSAFE.sub("_", raw) or "_"
    if safe != raw:
        safe += "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return cache_dir() / f"{safe}.json"


def save_page(page: Mapping[str, Any]) -> Path:
    """Write one job's page record (`job_id`, `url`, `system`, `state`,
    `captcha`, `account_wall`, `stop`, `notes`, `checked_at`, `digest`)."""
    from jsonutil import atomic_write_json
    path = cache_path(str(page["job_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, dict(page))
    return path


def load_page(job_id: str) -> dict | None:
    """The cached page for `job_id`, or None when there is none (or it holds
    another job's page)."""
    from jsonutil import read_json_dict
    page = read_json_dict(cache_path(job_id))
    if not page or str(page.get("job_id")) != str(job_id):
        return None
    return page


# --- DF-4: what the queue table shows -----------------------------------------------------

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
    """"N days old" for a result older than STALE_DAYS, else ""."""
    days = age_days(checked_at, now)
    if days is None or days <= STALE_DAYS:
        return ""
    return f"{days} days old"


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
    system."""
    if not system:
        return 0, 0
    ends = parks = 0
    for e in entries:
        if str(e.get("job_posting_id")) == str(job_id) or int(e.get("attempts") or 0) < 1:
            continue
        ats = e.get("ats") or {}
        own = system_for(str(ats.get("domain") or "")) or str(ats.get("system") or "")
        if own != system:
            continue
        status = str(e.get("status") or "")
        if status in ("submitted", "ready_to_submit"):
            ends += 1
        elif status == "needs_human":
            parks += 1
    return ends, parks


# --- DF-2: the walk to the first application page ----------------------------------------

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
        import apply_fill
        import apply_run
        page = self.page

        def busy() -> bool:
            try:
                return bool(apply_fill.ready_snapshot(page)[1])
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
            apply_fill.settle(page, apply_run.CLICK_TIMEOUT_S)
        while True:
            digest, loading_now = read()
            now = time.monotonic()
            loading = loading and loading_now and now - start < apply_run.LOADING_WAIT_S
            if not apply_run._empty_read(digest) and not loading:
                break
            seen = json.dumps(digest.to_dict(), sort_keys=True)
            if seen != last:
                last, stable_since = seen, now
            if now - start >= apply_run.EMPTY_READ_MAX_S or (
                    not loading and now - stable_since >= apply_run.EMPTY_READ_STABLE_S):
                break
            page.wait_for_timeout(int(apply_run.EMPTY_READ_POLL_S * 1000))
        return digest

    def _extract(self):
        import apply_fill
        import apply_form
        import apply_run
        page = self.page
        apply_fill.watch_requests(page)
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
            if box and box["height"] >= apply_run.HUMAN_CHECK_MIN_PX and box["y"] + box["height"] > 0:
                return True
        return checkbox and bool(apply_form.unsolved_checkbox(self.page))

    # --- LinkedIn and job boards, read by code ------------------------------------------------

    def _linkedin(self, kind: str) -> bool | None:
        import apply_linkedin
        import apply_run
        view, _waited = apply_run.linkedin_view(
            self.page, wait_s=apply_run.LINKEDIN_READY_S if kind == "job" else 0,
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
        import apply_fill
        import apply_judge
        import apply_linkedin
        import apply_run
        from apply_judge import FillPlan
        answers, facts = self._read(digest)
        state, conf = apply_judge.read_page_state(answers)
        if conf < apply_judge.PAGE_STATE_MIN_CONF:
            apply_fill.settle(self.page, apply_run.CLICK_TIMEOUT_S)
            digest = self._digest()
            answers, facts = self._read(digest)
            state, conf = apply_judge.read_page_state(answers)
        url = str(self.page.url or "")
        on_linkedin = apply_linkedin.is_linkedin(url)
        if apply_judge.already_applied(answers, facts, state):
            return self._unread("the site says this job was applied to before")
        if apply_run.remaps_to_form(state, digest, url):
            state = "application_form"
        if state == "confirmation" and not facts.link_sent:
            step, detail, then = apply_run.confirmation_step(digest, answers, conf,
                                                             submit_clicked=False)
            if step != "go_on":
                return self._unread("the page reads as a confirmation before any submit; "
                                    "check whether this job was applied to before")
            state, conf = detail, then
        if on_linkedin and state in apply_run._LINKEDIN_FORM_STATES:
            return self._stop("easy_apply")
        if state != "confirmation" and not on_linkedin:
            sites = apply_run.sso_only(digest)
            if sites:
                return self._stop("dead", SSO_NOTE.format(sites=", ".join(sites)))
        if conf < apply_judge.PAGE_STATE_MIN_CONF:
            step, _how = apply_run.unsure_step(state, digest, facts)
            if step is None:
                return self._unread(f"unsure what this page is ({state}, {conf:.2f})")
            state = step
        elif state == "other":
            state = apply_run.other_step(facts, digest) or state
        if on_linkedin and state in apply_run._LINKEDIN_FORM_STATES:
            return self._stop("easy_apply")
        if state in apply_run._LINK_REMAPS and facts.link_sent:
            state = "code_gate"
        self.walk.state = state
        self.walk.url = url
        self.walk.system = self._system() or ("linkedin" if on_linkedin else "")
        if state == "job_posting":
            if on_linkedin:
                return self._stop("dead", apply_linkedin.NO_APPLY_REASON)
            plan = FillPlan()
            apart, unclassified, _scan = apply_run.posting_context(self.page, digest, plan)
            n, _how = apply_run.posting_entry_choice(digest, plan, apart=apart,
                                                     unclassified=unclassified)
            if n is None:
                if digest.fields:
                    return self._form(digest, facts)
                return self._stop("dead", "No Apply button on the posting")
            import apply_form
            button = next(b for b in digest.buttons if b.n == n)
            return self._click(apply_form.resolve(self.page, button.locator), button.text)
        if state in ("application_form", "review_page"):
            return self._form(digest, facts)
        if state in ("login_wall", "signup_form", "code_gate"):
            import ats_accounts
            self.walk.account_wall = ats_accounts.lookup(url) is None
            self.walk.notes.append(ACCOUNT_NOTE)
            return True
        if state == "captcha_or_bot_check":
            self.walk.captcha = True
            self.walk.notes.append(CHECK_NOTE)
            return True
        if state == "payment_request":
            return self._stop("payment")
        if apply_judge.closed_posting(answers, facts, state):
            return self._stop("closed")
        if state == "error_or_dead":
            return self._stop("dead")
        return self._unread(f"the page reads as none of the kinds the check knows "
                            f"({state}, {conf:.2f})")

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
                                          timeout=apply_run.CLICK_TIMEOUT_S * 1000)
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
        page = self.page
        apply_fill.settle(page, apply_run.CLICK_TIMEOUT_S)
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
                                      timeout=apply_run.REDIRECT_TIMEOUT_S * 1000)
                    apply_fill.settle(page, apply_run.CLICK_TIMEOUT_S)
                except Exception as e:      # noqa: BLE001  (the next read says it stayed)
                    if apply_run._closed_error(e):
                        raise
        apply_run._past_trackers(page, {}, self.log,
                                 str(self.entry.get("job_posting_id") or ""))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- DF-2 and DF-3: the screening and the result ------------------------------------------

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
    label, help, options and type), an essay the drafter would write (with
    `generate` on; else it is a question), a sensitive field, a password box
    (an account made inside the form) and an upload with no file (a note)."""
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
        if f.type == "file":
            out.notes.append(UPLOAD_NOTE.format(label=f.label))
            continue
        out.questions.append({"label": f.label, "help": f.help or "",
                              "options": list(f.options or []), "required": True,
                              "type": apply_facts.answer_type(f.type, f.options)})
    return out


def page_record(walk: Walk, job_id: str) -> dict:
    """The page the cache keeps for a job (DF-5)."""
    return {"job_id": str(job_id), "url": walk.url, "system": walk.system,
            "state": walk.state, "captcha": walk.captcha, "account_wall": walk.account_wall,
            "stop": walk.stop, "notes": list(walk.notes),
            "checked_at": walk.checked_at or _now(),
            "digest": walk.digest.to_dict() if walk.digest is not None else None}


def assess_page(page: Mapping[str, Any], entry: Mapping[str, Any], *, judge, answers: list[dict],
                settings: Mapping[str, Any], entries: list[Mapping[str, Any]]) -> dict:
    """The difficulty for a read page (fresh or cached): the stop's 10, or
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
    be read. The page is cached for "Check again with my answers"."""
    log = log or logging.getLogger("apply_assess")
    job_id = str(entry.get("job_posting_id") or "")
    walk = _Walker(context, entry, judge, log).run()
    if walk.unread:
        return None, walk.unread
    page = page_record(walk, job_id)
    save_page(page)
    return assess_page(page, entry, judge=judge, answers=answers, settings=settings,
                       entries=entries), ""


def recheck_job(entry: Mapping[str, Any], *, judge, answers: list[dict],
                settings: Mapping[str, Any],
                entries: list[Mapping[str, Any]]) -> tuple[dict | None, str]:
    """"Check again with my answers" (DF-5): the cached page screened again
    with the answers as they are now; no browser."""
    page = load_page(str(entry.get("job_posting_id") or ""))
    if page is None:
        return None, NO_SAVED_PAGE
    return assess_page(page, entry, judge=judge, answers=answers, settings=settings,
                       entries=entries), ""


# --- DF-1 and DF-6: the console ------------------------------------------------------------

class _Counting:
    """The judge with a count of the requests it answered."""

    def __init__(self, inner):
        self.inner = inner
        self.requests = 0

    def judge(self, state, questions):
        answers = self.inner.judge(state, questions)
        self.requests += 1
        return answers


def _say(text: str) -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc), flush=True)


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


def run(job_ids: list[str], *, all_queued: bool = False, recheck: bool = False, judge,
        settings: Mapping[str, Any], context=None, queue_path: Path | None = None,
        log: logging.Logger | None = None) -> int:
    """Check each job and store its difficulty on its queue entry, printing
    one line per job with the running Jev total. `context` is the browser
    context (the persistent profile's; tests inject one); `recheck` screens
    the cached pages again with no browser. Exit 0, 1 when the judge went
    down or the window closed, 2 when nothing could be checked."""
    import apply_run
    import jev
    from resume_tailor import apply_answers
    log = log or logging.getLogger("apply_assess")
    try:
        answers = apply_answers.load()
    except apply_answers.AnswerStoreError as e:
        _say(f"The Apply Answers file is damaged ({e.path}): {e.reason}. Open the dashboard's "
             f"Apply Answers tab to restore the backup.")
        return 2
    entries = list(apply_queue.load(queue_path).get("jobs") or [])
    chosen, unknown = select_jobs(entries, job_ids, all_queued=all_queued)
    for jid in unknown:
        _say(f"apply_assess: job {jid} is not in the queue")
    if not chosen:
        _say("apply_assess: no job to check" + ("" if unknown or not all_queued
                                                  else " (nothing is queued)"))
        return 0 if all_queued and not unknown else 2
    counted = _Counting(judge)
    usd0 = jev.usage()["usd"]
    code = 0
    for i, entry in enumerate(chosen, 1):
        jid = str(entry.get("job_posting_id") or "")
        name = f"{entry.get('company') or '?'} / {entry.get('title') or '?'}"
        usd_before = jev.usage()["usd"]
        try:
            if recheck:
                result, why = recheck_job(entry, judge=counted, answers=answers,
                                          settings=settings, entries=entries)
            else:
                result, why = check_job(entry, context=context, judge=counted, answers=answers,
                                        settings=settings, entries=entries, log=log)
        except jev.JudgeOutage as e:
            _say(f"[{i}/{len(chosen)}] {name}: Jev is down ({e}); the check stops here")
            return 1
        except Exception as e:      # noqa: BLE001  (one line; the frames go to the log)
            if apply_run._closed_error(e):
                _say(f"[{i}/{len(chosen)}] {name}: the browser window was closed; the check "
                     f"stops here")
                return 1
            result, why = None, f"{type(e).__name__} at {apply_run.error_step(e)}"
            log.error("job %s: %s; traceback (the message left out):\n  %s", jid, why,
                      "\n  ".join(apply_run.error_frames(e)))
        spent = jev.usage()["usd"] - usd_before
        total = jev.usage()["usd"] - usd0
        running = f"Jev so far: {counted.requests} request(s), ${total:.4f}"
        if result is None:
            _say(f"[{i}/{len(chosen)}] {name}: not checked ({why}). {running}")
            continue
        result["jev_usd"] = round(spent, 6)
        try:
            apply_queue.set_difficulty(jid, result, path=queue_path)
        except apply_queue.UnknownJobError:
            _say(f"[{i}/{len(chosen)}] {name}: left the queue while it was checked. {running}")
            continue
        _say(f"[{i}/{len(chosen)}] {name}: {result['score']}/10, {result['band']}. {running}")
    return code


def main(argv: list[str] | None = None, *, context=None) -> int:
    """`python local/apply_assess.py [--all | <id> ...] [--recheck]`. Exit 0,
    1 on an error the check could not go past, 2 when it refuses (Jev off,
    a test judge, the profile in use, nothing to check)."""
    import argparse
    import apply_run
    import jev
    ap = argparse.ArgumentParser(prog="apply_assess",
                                 description="Score how hard each queued job is to auto-apply.")
    ap.add_argument("job_ids", nargs="*", help="queue job ids")
    ap.add_argument("--all", action="store_true", dest="all_queued", help="every queued job")
    ap.add_argument("--recheck", action="store_true",
                    help="screen the saved pages again with your answers; no browser")
    ap.add_argument("--headless", action="store_true", help="no browser window")
    ap.add_argument("--queue", default=None, help="queue file")
    ap.add_argument("--profile", default=None, help="browser profile dir")
    ap.add_argument("--verbose", action="store_true", help="DEBUG logging")
    args = ap.parse_args(argv)
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
        print(early, file=sys.stderr)
        return 2
    apply_run._load_env()
    refused = refusal(mode=mode)
    if refused:
        print(refused, file=sys.stderr)
        return 2
    profile = Path(args.profile) if args.profile else default_profile_dir()
    if not args.recheck and context is None and profile_busy(profile):
        print(PROFILE_BUSY, file=sys.stderr)
        return 2
    try:
        judge = jev.Guarded(jev.get(mode), logger=log)
    except (jev.JevUnavailable, ValueError) as e:
        print(f"apply_assess: {e}", file=sys.stderr)
        return 2
    queue = Path(args.queue) if args.queue else None
    common = dict(all_queued=args.all_queued, recheck=args.recheck, judge=judge,
                  settings=settings, queue_path=queue, log=log)
    if args.recheck or context is not None:
        return run(list(args.job_ids), context=context, **common)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        profile.mkdir(parents=True, exist_ok=True)
        ctx = apply_run.launch_profile(
            pw, profile, headless=bool(settings.get("auto_apply_headless")) or args.headless,
            log=log)
        try:
            return run(list(args.job_ids), context=ctx, **common)
        finally:
            try:
                ctx.close()
            except Exception:       # noqa: BLE001  (closed by the user)
                pass


if __name__ == "__main__":
    raise SystemExit(main())
