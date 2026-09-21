"""The auto-apply drain loop: claim a queued job, drive a persistent Chromium
through the page state machine, submit behind the confidence gate or park,
write `apply_record.md`, finish the queue entry.

    python local/apply_run.py drain [--cap N] [--no-submit] [--headless]
                                    [--jev fake|replay|typesafe] [--profile DIR]
    python local/apply_run.py one <job_id> [same flags]
    python local/apply_run.py login      sign in to LinkedIn and the inbox once
    python local/apply_run.py doctor     key, SDK, Playwright, Chromium, profile

Per job (`Runner.run_job`): the fact catalog from the job folder's apply.md
(`apply_facts.build`), then one page at a time up to `apply_judge.MAX_PAGES`
and an eight-minute wall clock: `apply_form.extract` -> one Jev request
(`apply_judge.page_questions`) -> `read_page_state` -> the state table from
the design (section 3.5):

    job_posting            click the Apply entry, follow a popup
    application_form       plan, fill, verify, then advance, or the submit gate
    review_page            the submit gate
    login_wall / signup    the accounts hook (SP5); the default parks
    code_gate              the inbox hook (SP5); the default parks
    confirmation           finish submitted
    captcha / payment / error / other / low confidence
                           park needs_human

The submit gate is `can_submit(plan, verification, settings)`; it returns the
first failing reason. A submit click is recorded before the page is judged
again, so a crash after it finishes the entry `submitted` with an
"unconfirmed" note. Every terminal moment writes the record (fields, uploads,
verification, buttons, flags, missing questions, Jev totals; a password
field's value is never written) and calls `apply_queue.finish`.

Playwright is imported inside the functions that open a browser, so the
module imports without it; tests inject a `context`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import apply_facts  # noqa: E402
import apply_fill  # noqa: E402
import apply_form  # noqa: E402
import apply_judge  # noqa: E402
import apply_queue  # noqa: E402
import jev  # noqa: E402
from apply_judge import FillPlan, PlannedField, VerifyResult  # noqa: E402

log = logging.getLogger("apply_run")

JOB_WALL_CLOCK_S = 8 * 60          # per job, on the injectable clock
GENERATE_MAX = 3                   # generated answers per job (spec 3.7)
POPUP_TIMEOUT_MS = 5_000           # for the Apply entry to open a new tab
CLICK_TIMEOUT_S = 20               # click_button's wait for a change
HOLD_POLL_S = 1.0                  # while holding the window open
LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_HOSTS = ("linkedin.com", "www.linkedin.com")
UNKNOWN_ATS = ("", "other", "linkedin")   # the popup after Apply names the real ATS
VIEWPORT = {"width": 1400, "height": 1000}
RECORD_NAME = "apply_record.md"
HIDDEN = "<hidden>"

LOGIN_NOTE = "log in manually, then Re-queue"
CODE_NOTE = "enter the emailed code manually, then Re-queue"
REVIEW_NOTE = "review and submit"

DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_apply_submit": True,
    "auto_apply_headless": False,
    "auto_apply_jev_mode": "typesafe",
    "auto_apply_batch_cap": 10,
    "auto_apply_generate": True,
}
_PARK_STATES = {
    "captcha_or_bot_check": "captcha or bot check on the page",
    "payment_request": "payment requested",
    "error_or_dead": "error or dead page",
    "other": "unrecognised page",
}


def default_profile_dir() -> Path:
    appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return appdata / "linkedin_watcher" / "browser_profile"


def load_settings() -> dict[str, Any]:
    """The auto-apply keys from the dashboard's config (`settings.load`), with
    the defaults for anything unset or unreadable."""
    try:
        import settings
        stored = settings.load()
    except Exception as e:      # noqa: BLE001  (a bad config file is not a reason to stop)
        log.warning("settings unreadable (%s); using defaults", e)
        stored = {}
    return {k: stored.get(k, d) for k, d in DEFAULT_SETTINGS.items()}


# --- hooks SP5 / SP6 implement -----------------------------------------------------

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


# --- outcomes and the record ----------------------------------------------------------

@dataclass
class Outcome:
    job_id: str
    status: str
    reason: str
    record_path: str
    pages: int
    jev_usage: dict[str, Any] = field(default_factory=dict)


class _Parked(Exception):
    """Raised inside the state loop to end the job in a terminal status."""

    def __init__(self, status: str, reason: str, tab_note: str = ""):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.tab_note = tab_note


def _host(url_or_netloc: str) -> str:
    """The lowercased hostname of a URL or a netloc, without a port."""
    raw = str(url_or_netloc or "").strip()
    if "://" in raw:
        return (urlsplit(raw).hostname or "").lower()
    return raw.split("/")[0].rsplit("@", 1)[-1].split(":")[0].lower()


def _is_password(row: dict) -> bool:
    if str(row.get("type", "")) != "other":
        return False
    blob = f"{row.get('id_or_name', '')} {row.get('label', '')}".lower()
    return "pass" in blob


def _usage_delta(before: dict, after: dict) -> dict[str, Any]:
    return {"requests": after["requests"] - before["requests"],
            "input_tokens": after["input_tokens"] - before["input_tokens"],
            "usd": after["usd"] - before["usd"]}


def write_record(folder: Path, entry: dict, outcome_status: str, reason: str,
                 pages: list[dict], jev_usage: dict, page_text: str, *,
                 missing: list[dict] | None = None) -> Path:
    """`apply_record.md` in the job folder. A value from a password field
    (type `other`, id or label containing "pass") is written as `<hidden>`."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    lines = [f"# Apply record: {entry.get('title', '')} at {entry.get('company', '')}", "",
             f"- Job id: {entry.get('job_posting_id', '')}",
             f"- Company: {entry.get('company', '')}",
             f"- Title: {entry.get('title', '')}",
             f"- Status: {outcome_status}",
             f"- Reason: {reason}",
             f"- Written: {datetime.now().isoformat(timespec='seconds')}",
             f"- Apply URL: {entry.get('apply_url', '')}", ""]
    for i, p in enumerate(pages, 1):
        lines.append(f"## Page {i}: {p.get('url', '')}")
        lines.append(f"- State: {p.get('state', '')} ({float(p.get('confidence', 0.0)):.2f})")
        filled = [r for r in p.get("filled", []) if not r.get("upload")]
        uploads = [r for r in p.get("filled", []) if r.get("upload")]
        if filled:
            lines.append("- Filled:")
            for r in filled:
                value = HIDDEN if _is_password(r) else str(r.get("value", ""))
                lines.append(f"  - {r.get('label', '')}: {value}")
        if uploads:
            lines.append("- Uploads:")
            for r in uploads:
                lines.append(f"  - {r.get('label', '')}: {r.get('value', '')}")
        if p.get("verification"):
            lines.append("- Verification:")
            for v in p["verification"]:
                mark = "ok" if v.get("ok") else "FAILED"
                lines.append(f"  - {v.get('label', '')}: {mark} (p_correct "
                             f"{float(v.get('p_correct', 0.0)):.2f}, p_placeholder "
                             f"{float(v.get('p_placeholder', 0.0)):.2f})")
        if p.get("clicked"):
            lines.append("- Clicked: " + "; ".join(str(c) for c in p["clicked"]))
        if p.get("flags"):
            lines.append("- Flags: " + ", ".join(f"{k} {float(v):.2f}"
                                                   for k, v in p["flags"].items()))
        lines.append("")
    lines.append("## Missing questions")
    rows = missing if missing is not None else entry.get("missing_answers") or []
    if rows:
        for m in rows:
            ctx = str(m.get("context", "") or "")
            lines.append(f"- {m.get('question', '')}" + (f" ({ctx})" if ctx else ""))
    else:
        lines.append("- none")
    lines += ["", "## Jev",
              f"- Requests: {int(jev_usage.get('requests', 0))}",
              f"- Input tokens: {int(jev_usage.get('input_tokens', 0))}",
              f"- Cost: ${float(jev_usage.get('usd', 0.0)):.4f}",
              f"- Model: {jev.MODEL}", ""]
    if page_text:
        lines += ["## Final page text", "", "```", page_text.strip(), "```", ""]
    path = folder / RECORD_NAME
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --- the submit gate ----------------------------------------------------------------------

def can_submit(plan: FillPlan, verification: list[VerifyResult],
               settings: dict) -> tuple[bool, str]:
    """(True, "") when the application may be sent, else (False, the first
    failing reason): the setting, the plan's park reason, a required field
    skipped, a required field unverified, the prohibited and captcha flags,
    the submit button's confidence."""
    if not settings.get("auto_apply_submit", True):
        return False, "auto_apply_submit is off"
    if plan.park_reason:
        return False, plan.park_reason
    for pf in plan.fields:
        if pf.required and pf.action == "skip":
            return False, f"required field skipped: {pf.label}"
    by_n = {v.n: v for v in verification}
    for pf in plan.fields:
        if pf.required and pf.action in ("fill", "select", "upload"):
            v = by_n.get(pf.n)
            if v is None:
                return False, f"required field not verified: {pf.label}"
            if not v.ok:
                return False, (f"required field failed verification: {pf.label} "
                               f"(p_correct {v.p_correct:.2f}, p_placeholder "
                               f"{v.p_placeholder:.2f})")
    prohibited = float(plan.flags.get("asks_for_prohibited", 0.0))
    if prohibited > apply_judge.PROHIBITED_MAX:
        return False, f"page asks for prohibited data (p={prohibited:.2f})"
    captcha = float(plan.flags.get("has_captcha", 0.0))
    if captcha > apply_judge.CAPTCHA_MAX:
        return False, f"captcha or bot check on the page (p={captcha:.2f})"
    submit = plan.buttons.get("submit")
    if submit is None:
        return False, "no submit button"
    if submit[1] < apply_judge.BUTTON_SUBMIT_MIN_CONF:
        return False, (f"submit button confidence {submit[1]:.2f} below "
                       f"{apply_judge.BUTTON_SUBMIT_MIN_CONF:.2f}")
    return True, ""


def hold_until_closed(ctx, *, sleep: Callable[[float], None] = time.sleep,
                      log: logging.Logger | None = None) -> None:
    """Keep the process alive until the user closes the window: every page
    closed, or the context's close event (the idea `apply_playwright._hold`
    uses for its parked tab)."""
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
        sleep(HOLD_POLL_S)


# --- the runner -----------------------------------------------------------------------------

class Runner:
    """Drains the queue through one browser context.

    `jev` is any `jev.Jev`; `settings` carries the `auto_apply_*` keys
    (`DEFAULT_SETTINGS` fills gaps); `clock` and `sleep` are injectable for
    the wall clock and the hold; `accounts`, `inbox`, `answergen` are the
    hook objects (`NotConfigured` by default); `context` is an already open
    Playwright browser context (tests), else the persistent profile at
    `profile_dir` is launched per drain and, when a page is parked and the
    window is visible, held open until the user closes it; `run_context` is
    `apply_queue.build_context()`'s dict (the inbox URL), computed on demand.
    """

    def __init__(self, *, jev: Any, queue_path: Path | None = None,
                 profile_dir: Path | None = None, settings: dict | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 accounts: Any = None, inbox: Any = None, answergen: Any = None,
                 log: logging.Logger | None = None, context: Any = None,
                 run_context: dict | None = None):
        self.jev = jev
        self.queue_path = Path(queue_path) if queue_path else None
        self.profile_dir = Path(profile_dir) if profile_dir else default_profile_dir()
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        self.clock = clock
        self.sleep = sleep
        default = NotConfigured()
        self.accounts = accounts if accounts is not None else default
        self.inbox = inbox if inbox is not None else default
        self.answergen = answergen if answergen is not None else default
        self.log = log if log is not None else logging.getLogger("apply_run")
        self._injected = context
        self._ctx = context
        self._run_context = run_context
        self.parked_pages: list = []

    # -- browser lifecycle --------------------------------------------------------------

    def _launch(self):
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            ctx = pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=bool(self.settings["auto_apply_headless"]),
                viewport=VIEWPORT)
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

    # -- the drain ----------------------------------------------------------------------

    def drain(self, cap: int | None = None) -> list[Outcome]:
        """Claim FIFO until the queue is empty or `cap` jobs ran; one
        `Outcome` per job."""
        limit = int(cap if cap is not None else self.settings["auto_apply_batch_cap"])

        def _work(ctx) -> list[Outcome]:
            outcomes: list[Outcome] = []
            while len(outcomes) < limit:
                entry = apply_queue.claim("apply_run", path=self.queue_path)
                if entry is None:
                    break
                outcomes.append(self._run_job(ctx, entry))
            self.log.info(summary_line(outcomes))
            return outcomes
        return self._with_browser(_work)

    def run_job(self, entry: dict) -> Outcome:
        """One claimed entry through the state machine (opens the browser
        when no drain is running)."""
        if self._ctx is not None:
            return self._run_job(self._ctx, entry)
        return self._with_browser(lambda ctx: self._run_job(ctx, entry))

    def _run_job(self, ctx, entry: dict) -> Outcome:
        job = _JobRun(self, ctx, entry)
        return job.run()


# --- one job ---------------------------------------------------------------------------------

class _JobRun:
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
        self.gen_budget = GENERATE_MAX
        self.catalog: apply_facts.FactCatalog | None = None
        self.allowed: set[str] = set()
        self.last_sig: tuple | None = None
        self.usage_before = jev.usage()
        self.start = runner.clock()
        self.deadline = self.start + JOB_WALL_CLOCK_S
        self.folder = self._folder()

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
        inbox_host = _host(str(self.r.run_context().get("inbox_url") or ""))
        if inbox_host:
            self.allowed.add(inbox_host)

    def _check_host(self, url: str) -> None:
        host = _host(url)
        if host and host not in self.allowed:
            raise _Parked("needs_human", f"left the allowed sites: {host}")

    # -- the run ----------------------------------------------------------------------------

    def _prepare(self) -> str:
        """The catalog, the PDFs from the entry's artifacts, the allowlist;
        returns the apply URL. Raises `_Parked("failed", ...)` when the sheet
        or the URL is missing."""
        if self.folder is None or not (self.folder / "apply.md").exists():
            raise _Parked("failed", "no apply.md")
        self.catalog = apply_facts.build(self.folder)
        self._apply_artifacts()
        self._build_allowlist()
        url = str(self.entry.get("apply_url") or "")
        if not url:
            raise _Parked("failed", "no apply_url")
        return url

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

    def run(self) -> Outcome:
        self.log.info("job %s: start (%s)", self.job_id, self.entry.get("apply_url", ""))
        try:
            url = self._prepare()
            self._check_host(url)
            self.page = self.ctx.new_page()
            self.page.goto(url)
            self._loop()
            raise _Parked("needs_human", f"page budget exhausted ({apply_judge.MAX_PAGES} pages)")
        except _Parked as p:
            return self._finish(p.status, p.reason, p.tab_note)
        except Exception as e:      # noqa: BLE001  (the entry must leave in_progress)
            self.log.exception("job %s: unexpected error", self.job_id)
            if self.submit_clicked:
                return self._finish("submitted",
                                    f"submitted (unconfirmed): {type(e).__name__}: {e}")
            return self._finish("failed", f"{type(e).__name__}: {e}")

    def _loop(self) -> None:
        for page_no in range(apply_judge.MAX_PAGES):
            if self.r.clock() >= self.deadline:
                raise _Parked("needs_human", "time budget exhausted")
            self._check_host(self.page.url)
            digest = apply_form.extract(self.page)
            answers = self._judge_page(digest)
            state, conf = apply_judge.read_page_state(answers)
            sig = (state, self.page.url, json.dumps(digest.to_dict(), sort_keys=True))
            rec = self._new_page_record(state, conf)
            self.log.info("job %s page %d: %s (%.2f) at %s", self.job_id, page_no + 1,
                          state, conf, self.page.url)
            if sig == self.last_sig:
                raise _Parked("needs_human", "page did not advance")
            self.last_sig = sig
            if conf < apply_judge.PAGE_STATE_MIN_CONF:
                raise _Parked("needs_human", f"unsure what this page is ({state}, {conf:.2f})")
            plan = apply_judge.plan(digest, self.catalog, answers,
                                    generation_enabled=bool(self.r.settings["auto_apply_generate"]))
            rec["flags"] = dict(plan.flags)
            if state == "job_posting":
                self._job_posting(digest, plan)
            elif state == "application_form":
                self._application_form(digest, answers, plan, rec)
            elif state == "review_page":
                self._submit_gate(digest, plan, [], rec)
            elif state == "login_wall":
                if not self.r.accounts.login(self.page, digest, digest.url_host):
                    raise _Parked("needs_human", "login wall", LOGIN_NOTE)
            elif state == "signup_form":
                if not self.r.accounts.signup(self.page, digest, digest.url_host):
                    raise _Parked("needs_human", "account signup needed", LOGIN_NOTE)
            elif state == "code_gate":
                self._code_gate(digest, plan, rec)
            elif state == "confirmation":
                raise _Parked("submitted", "confirmation page")
            else:
                reason = _PARK_STATES.get(state, state)
                if state == "captcha_or_bot_check":
                    reason += f" (has_captcha p={plan.flags.get('has_captcha', 0.0):.2f})"
                elif state == "payment_request":
                    reason += f" (asks_for_prohibited p={plan.flags.get('asks_for_prohibited', 0.0):.2f})"
                raise _Parked("needs_human", reason)

    # -- per state ------------------------------------------------------------------------

    def _judge_page(self, digest: apply_form.FormDigest) -> dict:
        state, questions = apply_judge.page_questions(digest, self.catalog, self.entry)
        return dict(self.r.jev.judge(state, questions))

    def _new_page_record(self, state: str, conf: float) -> dict:
        rec = {"url": self.page.url, "state": state, "confidence": conf,
               "filled": [], "verification": [], "clicked": [], "flags": {}}
        self.pages.append(rec)
        return rec

    def _job_posting(self, digest: apply_form.FormDigest, plan: FillPlan) -> None:
        n = None
        if "apply_entry" in plan.buttons:
            n = plan.buttons["apply_entry"][0]
        else:
            for b in digest.buttons:
                if "apply" in b.text.lower():
                    n = b.n
                    break
        if n is None:
            raise _Parked("needs_human", "no Apply button on the posting")
        button = next(b for b in digest.buttons if b.n == n)
        loc = apply_form.resolve(self.page, button.locator)
        self.pages[-1]["clicked"].append(f"{button.text} (apply_entry)")
        popup = None
        try:
            with self.page.expect_popup(timeout=POPUP_TIMEOUT_MS) as info:
                loc.first.click(timeout=apply_fill.ACTION_TIMEOUT_MS)
            popup = info.value
        except Exception as e:      # noqa: BLE001  (no popup within the timeout)
            self.log.debug("job %s: no popup after Apply (%s)", self.job_id, e)
        if popup is None:
            apply_fill.settle(self.page, CLICK_TIMEOUT_S)
            return
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=CLICK_TIMEOUT_S * 1000)
        except Exception:       # noqa: BLE001
            pass
        self.log.info("job %s: Apply opened %s", self.job_id, popup.url)
        self._follow_popup(popup)

    def _follow_popup(self, popup) -> None:
        host = _host(popup.url)
        ats = self.entry.get("ats") or {}
        ats_host = _host(str(ats.get("domain") or ""))
        if host and host not in self.allowed:
            if str(ats.get("system") or "") in UNKNOWN_ATS or ats_host in ("",) + LINKEDIN_HOSTS:
                inferred = apply_queue.infer_ats(popup.url)
                apply_queue.update(self.job_id, path=self.r.queue_path,
                                   ats={"domain": host, "system": inferred["system"]})
                self.entry["ats"] = {**ats, "domain": host, "system": inferred["system"]}
                self.allowed.add(host)
        self.page = popup
        self.last_sig = None
        apply_fill.settle(self.page, CLICK_TIMEOUT_S)

    def _application_form(self, digest: apply_form.FormDigest, answers: dict,
                          plan: FillPlan, rec: dict) -> None:
        s2, q2 = apply_judge.option_questions(digest, plan)
        if q2:
            answers.update(self.r.jev.judge(s2, q2))
            plan = apply_judge.plan(digest, self.catalog, answers,
                                    generation_enabled=bool(self.r.settings["auto_apply_generate"]))
            rec["flags"] = dict(plan.flags)
        self._resolve_generation(digest, plan)
        for question, context in plan.missing:
            self._add_missing(question, context)
        if plan.park_reason:
            raise _Parked("needs_human", plan.park_reason)
        filled = apply_fill.apply(self.page, plan, deadline=self.deadline)
        verification = self._verify(filled)
        verification = self._retry_failed(plan, filled, verification)
        self._record_fill(rec, digest, plan, filled, verification)
        still = [v.label for v in verification if not v.ok
                 and any(pf.n == v.n and pf.required for pf in plan.fields)]
        if still:
            raise _Parked("needs_human", "could not verify: " + ", ".join(still))
        advance = plan.buttons.get("advance")
        submit = plan.buttons.get("submit")
        if submit is None and advance is not None \
                and advance[1] >= apply_judge.BUTTON_ADVANCE_MIN_CONF:
            self._click(digest, advance[0], "advance", rec)
            return
        if submit is not None:
            self._submit_gate(digest, plan, verification, rec)
            return
        raise _Parked("needs_human", "no way forward on this page")

    def _resolve_generation(self, digest: apply_form.FormDigest, plan: FillPlan) -> None:
        by_n = {f.n: f for f in digest.fields}
        for pf in plan.fields:
            if pf.action != "generate":
                continue
            f = by_n.get(pf.n)
            text = None
            if self.gen_budget > 0 and f is not None:
                text = self.r.answergen.answer(f, self.catalog, self.r.jev,
                                               budget=self.gen_budget)
            if text:
                self.gen_budget -= 1
                pf.action, pf.value = "fill", str(text)
                continue
            pf.action = "skip"
            hint = (f.help or f.placeholder or f.type) if f is not None else ""
            plan.missing.append((pf.label, hint))
            if pf.required and not plan.park_reason:
                plan.park_reason = f"required field without an answer: {pf.label}"

    def _add_missing(self, question: str, context: str) -> None:
        self.missing.append({"question": question, "context": context, "suggestion": ""})
        apply_queue.add_missing(self.job_id, question, context=context, path=self.r.queue_path)

    def _verify(self, filled: list[apply_fill.Filled]) -> list[VerifyResult]:
        if not filled:
            return []
        rows = [f.to_dict() for f in filled]
        state, questions = apply_judge.verify_questions(rows, self.catalog.sheet_excerpt())
        return apply_judge.read_verification(rows, self.r.jev.judge(state, questions))

    def _retry_failed(self, plan: FillPlan, filled: list[apply_fill.Filled],
                      verification: list[VerifyResult]) -> list[VerifyResult]:
        """A required field that failed verification is filled once more with
        the same value (a read-back mismatch is usually widget timing)."""
        required = {pf.n for pf in plan.fields if pf.required}
        failed_ns = {v.n for v in verification if not v.ok and v.n in required}
        if not failed_ns:
            return verification
        self.log.info("job %s: retrying %d field(s) that failed verification",
                      self.job_id, len(failed_ns))
        retry = FillPlan(fields=[pf for pf in plan.fields if pf.n in failed_ns])
        refilled = apply_fill.apply(self.page, retry, deadline=self.deadline)
        again = {v.n: v for v in self._verify(refilled)}
        by_n = {f.n: f for f in refilled}
        for i, f in enumerate(filled):
            if f.n in by_n:
                filled[i] = by_n[f.n]
        return [again.get(v.n, v) for v in verification]

    def _record_fill(self, rec: dict, digest: apply_form.FormDigest, plan: FillPlan,
                     filled: list[apply_fill.Filled], verification: list[VerifyResult]) -> None:
        fields = {f.n: f for f in digest.fields}
        actions = {pf.n: pf.action for pf in plan.fields}
        for f in filled:
            df = fields.get(f.n)
            rec["filled"].append({
                "n": f.n, "label": f.label, "value": f.value,
                "type": df.type if df else "", "id_or_name": df.id_or_name if df else "",
                "upload": actions.get(f.n) == "upload"})
        rec["verification"] = [{"n": v.n, "label": v.label, "ok": v.ok,
                                "p_correct": v.p_correct, "p_placeholder": v.p_placeholder}
                               for v in verification]

    def _click(self, digest: apply_form.FormDigest, n: int, role: str, rec: dict) -> bool:
        button = next((b for b in digest.buttons if b.n == n), None)
        text = button.text if button else f"button {n}"
        rec["clicked"].append(f"{text} ({role})")
        timeout = max(1.0, min(CLICK_TIMEOUT_S, self.deadline - self.r.clock()))
        changed = apply_fill.click_button(self.page, digest, n, timeout_s=timeout)
        if not changed:
            self.log.info("job %s: %s click changed nothing; retrying once", self.job_id, role)
            changed = apply_fill.click_button(self.page, digest, n, timeout_s=timeout)
        if not changed and role == "advance":
            raise _Parked("needs_human", f"the {role} button ({text}) did nothing")
        return changed

    # -- the submit path ----------------------------------------------------------------------

    def _submit_gate(self, digest: apply_form.FormDigest, plan: FillPlan,
                     verification: list[VerifyResult], rec: dict) -> None:
        ok, why = can_submit(plan, verification, self.r.settings)
        if not ok:
            forced = {**self.r.settings, "auto_apply_submit": True}
            ready, why_on = can_submit(plan, verification, forced)
            if ready:
                raise _Parked("ready_to_submit", why, REVIEW_NOTE)
            raise _Parked("needs_human", why_on)
        submit_n = plan.buttons["submit"][0]
        self.log.info("job %s: SUBMIT CLICKED", self.job_id)
        self.submit_clicked = True
        rec["clicked"].append("SUBMIT CLICKED")
        self._click(digest, submit_n, "submit", rec)
        self._after_submit()

    def _after_submit(self) -> None:
        digest = apply_form.extract(self.page)
        answers = self._judge_page(digest)
        state, conf = apply_judge.read_page_state(answers)
        rec = self._new_page_record(state, conf)
        self.log.info("job %s after submit: %s (%.2f)", self.job_id, state, conf)
        if state == "confirmation" and conf >= apply_judge.PAGE_STATE_MIN_CONF:
            raise _Parked("submitted", "confirmation page")
        if state == "code_gate" and conf >= apply_judge.PAGE_STATE_MIN_CONF:
            plan = apply_judge.plan(digest, self.catalog, answers)
            rec["flags"] = dict(plan.flags)
            self._code_gate(digest, plan, rec)
            digest = apply_form.extract(self.page)
            state, conf = apply_judge.read_page_state(self._judge_page(digest))
            self._new_page_record(state, conf)
            if state == "confirmation" and conf >= apply_judge.PAGE_STATE_MIN_CONF:
                raise _Parked("submitted", "confirmation page after the emailed code")
        raise _Parked("submitted", f"submitted (unconfirmed): the page after submit reads "
                                   f"as {state} ({conf:.2f})")

    def _code_gate(self, digest: apply_form.FormDigest, plan: FillPlan, rec: dict) -> None:
        site = digest.url_host or _host(self.page.url)
        code = self.r.inbox.fetch_code(self.page, site, str(self.r.run_context().get("inbox_url") or ""))
        if not code:
            raise _Parked("needs_human", "emailed code needed", CODE_NOTE)
        target = next((f for f in digest.fields
                       if "code" in f"{f.label} {f.id_or_name}".lower()), None)
        if target is None:
            raise _Parked("needs_human", "code gate without a code box", CODE_NOTE)
        pf = PlannedField(n=target.n, locator=target.locator, label=target.label,
                          required=True, fact_key=None, value=str(code), option=None,
                          confidence=1.0, action="fill")
        filled = apply_fill.apply(self.page, FillPlan(fields=[pf]), deadline=self.deadline)
        rec["filled"].append({"n": target.n, "label": target.label,
                              "value": filled[0].value if filled else "",
                              "type": target.type, "id_or_name": target.id_or_name,
                              "upload": False})
        button = plan.buttons.get("advance") or plan.buttons.get("submit")
        if button is None:
            raise _Parked("needs_human", "code entered; no button to continue", CODE_NOTE)
        self._click(digest, button[0], "advance", rec)

    # -- the end --------------------------------------------------------------------------------

    def _finish(self, status: str, reason: str, tab_note: str = "") -> Outcome:
        usage = _usage_delta(self.usage_before, jev.usage())
        text = ""
        if self.page is not None:
            try:
                text = apply_fill.page_text(self.page)
            except Exception:       # noqa: BLE001  (the page is gone)
                text = ""
        record = ""
        if self.folder is not None:
            try:
                record = str(write_record(self.folder, self.entry, status, reason, self.pages,
                                          usage, text, missing=self.missing))
            except Exception as e:      # noqa: BLE001  (a record failure must not lose the finish)
                self.log.warning("job %s: record not written: %s", self.job_id, e)
        if not tab_note and self.page is not None and status != "submitted":
            try:
                tab_note = f"{self.page.url} | {self.page.title()}"
            except Exception:       # noqa: BLE001
                tab_note = ""
        apply_queue.finish(self.job_id, status, tab_note=tab_note, record=record,
                           notes=reason, path=self.r.queue_path)
        if self.page is not None:
            if status == "submitted":
                try:
                    self.page.close()
                except Exception:       # noqa: BLE001
                    pass
            else:
                self.r.parked_pages.append(self.page)
        self.log.info("job %s: %s (%s)", self.job_id, status, reason)
        return Outcome(job_id=self.job_id, status=status, reason=reason, record_path=record,
                       pages=len(self.pages), jev_usage=usage)


# --- the summary and the CLI ---------------------------------------------------------------

def summary_line(outcomes: list[Outcome]) -> str:
    counts = {s: 0 for s in ("submitted", "ready_to_submit", "needs_human", "failed")}
    requests = tokens = 0
    usd = 0.0
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
        requests += int(o.jev_usage.get("requests", 0))
        tokens += int(o.jev_usage.get("input_tokens", 0))
        usd += float(o.jev_usage.get("usd", 0.0))
    return (f"drained {len(outcomes)}: submitted {counts['submitted']}, "
            f"ready_to_submit {counts['ready_to_submit']}, needs_human {counts['needs_human']}, "
            f"failed {counts['failed']}; Jev {requests} requests, {tokens} tokens, ${usd:.4f}")


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE.parent / ".env")
    except Exception:       # noqa: BLE001  (python-dotenv absent: the environment is what it is)
        pass


def _settings_from_args(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_settings()
    if getattr(args, "no_submit", False):
        cfg["auto_apply_submit"] = False
    if getattr(args, "headless", False):
        cfg["auto_apply_headless"] = True
    if getattr(args, "jev", None):
        cfg["auto_apply_jev_mode"] = args.jev
    if getattr(args, "cap", None) is not None:
        cfg["auto_apply_batch_cap"] = int(args.cap)
    return cfg


def doctor(profile_dir: Path | None = None, out=None) -> int:
    """One line per auto-apply setup row plus the profile dir; 0 when every
    row passes (a missing profile is created by the first run)."""
    import setup_check
    out = out or sys.stdout
    profile = Path(profile_dir) if profile_dir else default_profile_dir()
    try:
        import settings
        stored = settings.load()
        mode = str(stored.get("auto_apply_jev_mode") or "typesafe").strip().lower()
        has_key = bool(settings.secret_status().get("TYPESAFE_API_KEY")) or bool(
            os.environ.get(jev.KEY_ENV, "").strip())
    except Exception:       # noqa: BLE001
        mode, has_key = "typesafe", bool(os.environ.get(jev.KEY_ENV, "").strip())
    sdk = setup_check.module_found("typesafe_sdk")
    playwright_found = setup_check.module_found("playwright")
    chromium = playwright_found and setup_check.chromium_installed()
    rows = [("TypeSafe API key", has_key or mode != "typesafe"),
            ("typesafe_sdk", sdk or mode != "typesafe"),
            ("playwright", playwright_found),
            ("chromium", chromium)]
    for name, ok in rows:
        print(f"{'ok     ' if ok else 'MISSING'}  {name}", file=out)
    print(f"{'ok     ' if profile.is_dir() else 'absent '}  browser profile: {profile}"
          + ("" if profile.is_dir() else " (created by the first run or `login`)"), file=out)
    print(f"judge mode: {mode}", file=out)
    warnings = setup_check.auto_apply_warnings(has_key, mode, sdk, playwright_found, chromium)
    for w in warnings:
        print(f"  {w}", file=out)
    return 0 if not warnings else 2


def login(profile_dir: Path | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """Open the persistent profile at LinkedIn's login and the inbox in two
    tabs, headed, and wait for the user to close the window."""
    from playwright.sync_api import sync_playwright
    profile = Path(profile_dir) if profile_dir else default_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    inbox_url = str(apply_queue.build_context().get("inbox_url") or apply_queue.DEFAULT_INBOX_URL)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), headless=False, viewport=VIEWPORT)
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


def main(argv: list[str] | None = None) -> int:
    """Exit codes: 0 drained (or nothing queued), 1 unexpected error, 2 not
    configured (no judge, or the job id is not queued)."""
    ap = argparse.ArgumentParser(prog="apply_run",
                                 description="Jev-judged auto-apply: drain the queue.")
    sub = ap.add_subparsers(dest="verb", required=True)

    def _verbose(p):
        p.add_argument("--verbose", action="store_true", help="DEBUG logging")

    def _run_flags(p):
        _verbose(p)
        p.add_argument("--cap", type=int, default=None, help="jobs per drain")
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
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    profile = Path(args.profile) if getattr(args, "profile", None) else None
    try:
        if args.verb == "doctor":
            return doctor(profile)
        if args.verb == "login":
            return login(profile)
        _load_env()
        cfg = _settings_from_args(args)
        try:
            judge = jev.get(cfg["auto_apply_jev_mode"])
        except (jev.JevUnavailable, ValueError) as e:
            print(f"apply_run: {e}", file=sys.stderr)
            return 2
        queue = Path(args.queue) if args.queue else None
        runner = Runner(jev=judge, queue_path=queue, profile_dir=profile, settings=cfg)
        if args.verb == "one":
            entry = apply_queue.claim("apply_run", path=queue, job_id=args.job_id)
            if entry is None:
                print(f"apply_run: job {args.job_id} is not queued", file=sys.stderr)
                return 2
            outcomes = [runner.run_job(entry)]
        else:
            outcomes = runner.drain(cfg["auto_apply_batch_cap"])
        print(summary_line(outcomes))
        return 0
    except Exception as e:      # noqa: BLE001  (one line, documented exit 1)
        print(f"apply_run: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
