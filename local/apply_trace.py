"""The per-job trace of an auto-apply run: what each page looked like, what
the judge read, what the run did and why, so a live park or failure can be
explained from the job folder alone.

    <job folder>/apply_record.md
    <job folder>/apply_trace/attempt-<n>/
        page-<k>.json    the digest, every answer with its confidence, the
                         plan, the page's events (fills, clicks and their
                         results, decisions, parks), dropped frames, timings
        page-<k>.jpg     the page as it was judged, full length, password and
                         code boxes masked
        end.jpg          the page the run ended on, masked the same way
        run.json         the job's status and reason, the URL chain, the
                         events before the first page, the timings
        job.log          the job's log lines
        apply_record.md  this attempt's record

One `attempt-<n>` folder per attempt (`n` follows the queue entry's attempt
count), so an earlier attempt's trace survives a re-queue. `page-<k>.json` is
written as soon as the page is judged and again with every event, so a crash
still leaves every page before it.

What the trace never holds: a field's value, the master password or an
emailed code. The digest carries labels, types and options; a fill event
says which boxes hold something. A page's URL is kept as its scheme, host
and path (`bare_url`): a form sent with method=get puts its answers in the
query. The screenshots show the page as a person
would see it, with every password and code box masked, and every box the
run typed the password or a code into (`extra_mask`).

`Trace.off()` is the disabled trace: every call is a no-op, so a `_JobRun`
built by a test without `run()` needs no folder. A trace that cannot write
(a locked folder, a full disk) disables itself and logs once; it never ends
a job.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from jsonutil import replace_with_retry

log = logging.getLogger("apply_trace")


def bare_url(url: object) -> str:
    """`scheme://host/path`: the query and the fragment left off (a form
    sent with method=get carries its answers there, a code or a password
    among them)."""
    raw = str(url or "")
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if not parts.scheme:
        return raw.split("?", 1)[0].split("#", 1)[0]
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>|]*[?#][^\s\"'<>|]*", re.I)
_TRAILING = re.compile(r"[.,;:!?)\]]+$")


def _bare_in_text(m: re.Match) -> str:
    url = m.group(0)
    tail = _TRAILING.search(url)
    end = tail.group(0) if tail else ""
    return bare_url(url[:len(url) - len(end)]) + end


def scrub_urls(text: object) -> str:
    """`text` with every web address in it cut to `bare_url` (a stop, a
    comma or a bracket after it stays)."""
    text = str(text or "")
    if "://" not in text:
        return text
    return _URL_IN_TEXT.sub(_bare_in_text, text)


def _scrubbed(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_urls(value)
    if isinstance(value, Mapping):
        return {k: _scrubbed(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrubbed(v) for v in value]
    return value


class _UrlScrub(logging.Filter):
    """Every line the job's loggers write, with its web addresses cut to
    `bare_url`: a page reached by a method=get form carries the answers in
    its address, and a log line names the page."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:       # noqa: BLE001  (a malformed call is logging's to report)
            return True
        if "://" in message:
            scrubbed = scrub_urls(message)
            if scrubbed != message:
                record.msg, record.args = scrubbed, None
        return True

TRACE_DIR = "apply_trace"
LOG_NAME = "job.log"
RUN_NAME = "run.json"
JPEG_QUALITY = 60
# a screenshot waits for the page's `load`, which a stalled image can hold off
# for good: past this it is skipped (a `screenshot_failed` event)
SCREENSHOT_TIMEOUT_MS = 3_000
TOP_PROBABILITIES = 3              # per answer, beside the page state's full distribution
# The boxes a screenshot masks: every password input, the one-time-code
# autocomplete, and any box whose name or id says code, OTP or passcode.
MASK_CSS = ("input[type=password], input[autocomplete=one-time-code], "
            "input[autocomplete=current-password], input[autocomplete=new-password], "
            "input[name*=code i], input[id*=code i], input[name*=otp i], input[id*=otp i], "
            "input[name*=passcode i], input[id*=passcode i]")
# The loggers whose lines land in `job.log` (the runner's own logger joins them).
LOGGERS = ("apply_run", "apply_fill", "apply_form", "apply_judge", "apply_inbox",
           "apply_answergen", "apply_verify", "ats_accounts", "jev", "apply_trace")
_ATTEMPT_RE = re.compile(r"^attempt-(\d+)$")
# on every one of them, whether a trace is open or not (a logger's own filter
# runs before any of its handlers: the job log, the dashboard's, a test's)
URL_SCRUB = _UrlScrub()
for _name in LOGGERS:
    logging.getLogger(_name).addFilter(URL_SCRUB)


def _as_text(value: Any) -> Any:
    """JSON for what `json` cannot write itself: a path or any other object as
    its text, a set as a sorted list's text (cut to 200 characters). A
    dataclass, or any object with a `value` attribute (a `PlannedField`, a
    `Filled`), is written as its type's name alone: its text would carry the
    value it holds, and the trace never holds a field's value."""
    if dataclasses.is_dataclass(value) or hasattr(value, "value"):
        return f"<{type(value).__name__}>"
    if isinstance(value, (set, frozenset)):
        value = [v if v is None or isinstance(v, (str, int, float, bool)) else _as_text(v)
                 for v in sorted(value, key=str)]
    return str(value)[:200]


def write_json(path: Path, data: Any) -> None:
    """`data` as JSON at `path`, atomically (a same-folder temp file, then a
    replace). A value `json` cannot write goes in as text, so one odd value
    never turns the trace off."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, default=_as_text), encoding="utf-8")
        replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def attempt_dirs(folder: Path) -> list[tuple[int, Path]]:
    """(n, path) for every `attempt-<n>` folder under the job folder's trace,
    in attempt order."""
    root = Path(folder) / TRACE_DIR
    out = []
    try:
        for child in root.iterdir():
            m = _ATTEMPT_RE.match(child.name)
            if m and child.is_dir():
                out.append((int(m.group(1)), child))
    except OSError:
        return []
    return sorted(out)


def _answer_json(qid: str, a: Any) -> dict[str, Any]:
    kind = str(getattr(a, "kind", ""))
    row: dict[str, Any] = {"kind": kind}
    if kind == "noul":
        row["noul"] = getattr(a, "noul", None)
        return row
    if getattr(a, "choice", None) is not None:
        row["choice"] = a.choice
    if getattr(a, "score", None) is not None:
        row["score"] = a.score
    row["confidence"] = getattr(a, "confidence", None)
    probs = dict(getattr(a, "probabilities", {}) or {})
    ranked = sorted(probs.items(), key=lambda kv: -float(kv[1] or 0.0))
    if qid == "page_state" or qid.startswith("button_"):
        row["probabilities"] = dict(ranked)
    else:
        row["probabilities"] = dict(ranked[:TOP_PROBABILITIES])
    return row


def answers_json(answers: Mapping[str, Any]) -> dict[str, Any]:
    """Every answer with its kind, choice, confidence and probabilities (the
    page state's and the button roles' in full, the rest cut to the top
    three). Answers carry no field value."""
    return {qid: _answer_json(qid, a) for qid, a in sorted(answers.items())}


def plan_json(plan: Any) -> dict[str, Any]:
    """The fill plan without the values it would type: per field the fact
    key, the action, the option picked, the confidence."""
    fields = [{"n": pf.n, "label": pf.label, "required": pf.required,
               "fact_key": pf.fact_key, "action": pf.action, "option": pf.option,
               "confidence": pf.confidence, "quick": pf.quick}
              for pf in getattr(plan, "fields", [])]
    return {"fields": fields,
            "buttons": {role: list(v) for role, v in getattr(plan, "buttons", {}).items()},
            "flags": dict(getattr(plan, "flags", {}) or {}),
            "park_reason": getattr(plan, "park_reason", ""),
            "missing": [list(m) for m in getattr(plan, "missing", [])]}


def page_state_reads(answers: Mapping[str, Any], top: int = 3) -> str:
    """The page state's most probable reads, "other 0.30, job_posting 0.25",
    for a park reason's evidence; "" when the judge gave no distribution."""
    a = answers.get("page_state") if answers else None
    probs = dict(getattr(a, "probabilities", {}) or {}) if a is not None else {}
    ranked = sorted(((k, float(v or 0.0)) for k, v in probs.items()), key=lambda kv: -kv[1])
    return ", ".join(f"{k} {v:.2f}" for k, v in ranked[:top] if v > 0)


def mask_locators(page, extra: Iterable[Any] = ()) -> list:
    """The locators a screenshot masks, one per frame of `page`."""
    out = []
    try:
        frames = list(page.frames)
    except Exception:       # noqa: BLE001  (the page is gone)
        frames = []
    for frame in frames:
        try:
            out.append(frame.locator(MASK_CSS))
        except Exception:   # noqa: BLE001  (a detached frame)
            continue
    out.extend(extra)
    return out


class Trace:
    """One attempt's trace (see the module docstring)."""

    def __init__(self, folder: Path | None = None, *, attempt: int = 0, job_id: str = "",
                 loggers: Iterable[logging.Logger] = (), clock=time.monotonic):
        self.folder = Path(folder) if folder else None
        self.attempt_hint = int(attempt or 0)
        self.job_id = str(job_id)
        self.clock = clock
        self.t0 = clock()
        self.dir: Path | None = None
        self.attempt = 0
        self.pages: list[dict[str, Any]] = []
        self.setup: list[dict[str, Any]] = []
        self.url_chain: list[str] = []
        self._loggers = list(loggers)
        self._handler: logging.Handler | None = None
        self._attached: list[logging.Logger] = []
        self._failed = False

    @classmethod
    def off(cls) -> Trace:
        return cls(None)

    @property
    def enabled(self) -> bool:
        return self.dir is not None and not self._failed

    @property
    def rel_dir(self) -> str:
        """The attempt folder relative to the job folder, posix style."""
        return f"{TRACE_DIR}/{self.dir.name}" if self.dir is not None else ""

    def _t(self) -> float:
        return round(self.clock() - self.t0, 3)

    def _fail(self, what: str, e: Exception) -> None:
        if not self._failed:
            log.warning("trace for job %s disabled: %s failed (%s)", self.job_id, what,
                        type(e).__name__)
        self._failed = True

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        """Make the attempt folder and attach the job log. Without a folder
        the trace stays off."""
        if self.folder is None:
            return
        try:
            taken = {n for n, _ in attempt_dirs(self.folder)}
            n = self.attempt_hint
            if n <= 0 or n in taken:
                n = max(taken, default=0) + 1
            path = self.folder / TRACE_DIR / f"attempt-{n}"
            path.mkdir(parents=True, exist_ok=False)
        except Exception as e:      # noqa: BLE001  (a trace never ends a job)
            self._fail("making the trace folder", e)
            return
        self.dir, self.attempt = path, n
        self._attach_log()
        self._write_run({})

    def _attach_log(self) -> None:
        try:
            handler = logging.FileHandler(self.dir / LOG_NAME, encoding="utf-8")
        except Exception as e:      # noqa: BLE001
            self._fail("opening the job log", e)
            return
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        self._handler = handler
        names = {lg.name for lg in self._loggers}
        loggers = list(self._loggers) + [logging.getLogger(n) for n in LOGGERS if n not in names]
        for lg in loggers:
            lg.addFilter(URL_SCRUB)         # the runner's own logger too (a no-op when on)
            if handler not in lg.handlers:
                lg.addHandler(handler)
                self._attached.append(lg)

    def close(self) -> None:
        """Detach and close the job log (idempotent)."""
        handler, self._handler = self._handler, None
        for lg in self._attached:
            try:
                lg.removeHandler(handler)
            except Exception:       # noqa: BLE001
                pass
        self._attached = []
        if handler is not None:
            try:
                handler.close()
            except Exception:       # noqa: BLE001
                pass

    # -- pages and events ------------------------------------------------------------

    def page(self, n: int, url: str, digest: Any, answers: Mapping[str, Any],
             state: str, confidence: float, *, dropped: Mapping[int, str] | None = None,
             timings: Mapping[str, float] | None = None) -> None:
        """Start page `n`'s entry and write `page-<n>.json` at once."""
        if not self.enabled:
            return
        entry: dict[str, Any] = {
            "n": n, "url": bare_url(url), "t": self._t(),
            "state": state, "confidence": confidence,
            "page_state": _answer_json("page_state", answers["page_state"])
            if answers and "page_state" in answers else None,
            "digest": digest.to_dict() if digest is not None else None,
            "answers": answers_json(answers or {}),
            "dropped_frames": {str(k): v for k, v in (dropped or {}).items()},
            "timings": dict(timings or {}),
            "events": []}
        self.pages.append(entry)
        self._write_page(entry)

    def add_answers(self, answers: Mapping[str, Any]) -> None:
        """More answers for the current page: its mapping, asked after the
        page read (SP4), joins the read's answers in `page-<n>.json`."""
        if not self.enabled or not self.pages:
            return
        self.pages[-1]["answers"].update(answers_json(answers))
        self._write_page(self.pages[-1])

    def event(self, kind: str, **data: Any) -> None:
        """One step of the run on the current page (or before the first one):
        a plan, a fill, a click and its result, a decision and why, a park."""
        if self.dir is None or self._failed:
            return
        # an event names pages by their address: cut to `bare_url`
        row = {"t": self._t(), "kind": kind, **_scrubbed(data)}
        if self.pages:
            self.pages[-1]["events"].append(row)
            self._write_page(self.pages[-1])
        else:
            self.setup.append(row)
            self._write_run({})

    def nav(self, url: str) -> None:
        """A main-frame navigation of a page the job drives (redirects and
        popups included), in order, as `bare_url` keeps it."""
        url = bare_url(url)
        if url and (not self.url_chain or self.url_chain[-1] != url):
            self.url_chain.append(url)

    def screenshot(self, page, name: str, *, extra_mask: Iterable[Any] = ()) -> str:
        """`<name>.jpg`: the whole page, password and code boxes masked.
        Returns the file name, "" when the page could not be captured."""
        if not self.enabled or page is None:
            return ""
        path = self.dir / f"{name}.jpg"
        try:
            page.screenshot(path=str(path), full_page=True, type="jpeg", quality=JPEG_QUALITY,
                            mask=mask_locators(page, extra_mask), timeout=SCREENSHOT_TIMEOUT_MS)
        except Exception as e:      # noqa: BLE001  (a closed page, a page too tall to draw)
            self.event("screenshot_failed", name=name, error=type(e).__name__)
            return ""
        return path.name

    def finish(self, status: str, reason: str, page=None, *,
               extra_mask: Iterable[Any] = ()) -> None:
        """The end: the last page's screenshot and `run.json`."""
        if not self.enabled:
            return
        shot = self.screenshot(page, "end", extra_mask=extra_mask) if page is not None else ""
        self._write_run({"status": status, "reason": scrub_urls(reason), "end_screenshot": shot,
                         "seconds": self._t()})

    # -- files -----------------------------------------------------------------------

    def _write_page(self, entry: dict[str, Any]) -> None:
        try:
            write_json(self.dir / f"page-{entry['n']}.json", entry)
        except Exception as e:      # noqa: BLE001
            self._fail("writing a page", e)

    def _write_run(self, extra: Mapping[str, Any]) -> None:
        if self.dir is None or self._failed:
            return
        path = self.dir / RUN_NAME
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            current = {}
        current.update({"job_id": self.job_id, "attempt": self.attempt,
                        "pages": len(self.pages), "url_chain": list(self.url_chain),
                        "setup_events": list(self.setup)})
        current.update(extra)
        try:
            write_json(path, current)
        except Exception as e:      # noqa: BLE001
            self._fail("writing run.json", e)
