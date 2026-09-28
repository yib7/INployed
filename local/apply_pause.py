"""Park and resume (cycle 19, SP7: PR-1 to PR-8).

A run that meets a question it can ask the person pauses there. A required
field with no saved answer, an option tie, a required sensitive field (the
person types it in the browser) and a way on still disabled after the fill
each pause the job. Every other park stays immediate.

The file protocol, in `pause_dir()` (`%LOCALAPPDATA%\\linkedin_watcher\\
apply_pause`, read at call time):

- `<job id>.json`, the request the run writes (`write_request`): the job,
  its company and title, the page's URL, the reason and, per question, its
  key, field id, label, help, placeholder, type, widget, live options,
  required and sensitive.
- `<job id>.answer.json`, the answer the dashboard's "Waiting for you" card
  writes (`write_answer`), or anyone by hand: the mode ("fill", "browser" or
  "park"), a value per question key and a "save for future runs" flag per key.

The run waits for the answer (`wait_for_answer`) up to
`auto_apply_pause_minutes`, polling through the page so a closed window is
seen at once. The wait stays off the job clock, as the CAPTCHA wait does. No
answer in time, or "Park it", parks the job with the reason it had before
SP7.

`Pauser` holds one job's pauses. A "fill" answer on the same page puts each
value in its own field (source `USER_SOURCE`, the exact value, a choice only
as one of the live options); a field the person filled in the browser stays
as it is (`KEPT`). A page that changed meanwhile, and every "browser"
answer, reads and plans the page again (`Replan`), and the answers apply on
that plan, on the page the run paused on only: a page that moved on during
the wait drops them. A sensitive field is never typed by code: its value in an answer
file is ignored.

A value whose "save" flag is set goes to the answer store as a confirmed
custom answer (`save_answer`, PR-6), refused as Add answer refuses it: a
question a built-in answers (`builtin_answering`) or one already saved
(`apply_answers.find_collision`).

`NEVER_WAIT` is the suite's hermetic default (the conftest sets
`INPLOYED_PAUSE_NEVER_WAIT`): with it on, no run pauses. The pause tests and
the harness's pause flows turn it off.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import apply_facts  # noqa: E402
import apply_judge  # noqa: E402
from jsonutil import atomic_write_json, read_json_dict  # noqa: E402
from resume_tailor import apply_answers  # noqa: E402

log = logging.getLogger("apply_pause")

# The suite's hermetic default: the conftest sets the variable before any
# module loads, so no run in the suite waits unless a test turns it off.
NEVER_WAIT_ENV = "INPLOYED_PAUSE_NEVER_WAIT"
NEVER_WAIT = os.environ.get(NEVER_WAIT_ENV, "").strip() == "1"
SECONDS_PER_MINUTE = 60         # the harness shortens a minute for its timeout flow
POLL_S = 0.5                    # how often the wait looks for the answer file
MAX_PAUSES = 5                  # pauses one job may make
MINUTES_MAX = 60
STALE_MARGIN_S = 120            # past a request's wait, its run is gone (`pending_requests`)
FOLDER = "apply_pause"
MODES = ("fill", "browser", "park")
KEPT = "kept"                   # a plan action: the field holds the person's own value
USER_SOURCE = "user"            # a plan field's fact key when its value is the person's
OUTLINE = "3px solid rgb(217, 119, 6)"

# the reasons a pause can answer (`apply_judge.plan`, `apply_run`)
_REQUIRED_HEAD = "required field without an answer"
_SENSITIVE_RE = re.compile(r"^asks for .*which auto-apply never fills")

# widgets a request names for a question
W_CHOICE, W_YES_NO, W_NUMBER, W_TEXT, W_BROWSER = "choice", "yes_no", "number", "text", "browser"
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")

# Windows process query (`pid_alive`)
_QUERY_LIMITED, _ACCESS_DENIED, _STILL_ACTIVE = 0x1000, 5, 259


class Replan(Exception):
    """The page is read and planned again: the person's answers apply to the
    new plan (`Pauser.apply_pending`). The state loop catches it."""


class PageClosed(Exception):
    """The job's window or tab closed during the wait."""


# -- the file protocol ---------------------------------------------------------------------

def pause_dir() -> Path:
    appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return appdata / "linkedin_watcher" / FOLDER


def _safe_id(job_id: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(job_id or ""))[:80] or "job"


def request_path(job_id: Any) -> Path:
    return pause_dir() / f"{_safe_id(job_id)}.json"


def answer_path(job_id: Any) -> Path:
    return pause_dir() / f"{_safe_id(job_id)}.answer.json"


def write_request(job: Mapping[str, Any], page_url: str, reason: str,
                  questions: Iterable[Mapping[str, Any]], *, headless: bool = False,
                  pause_id: str = "", minutes: float = MINUTES_MAX) -> Path:
    """The request file for `job` (a queue entry, or a dict with
    `job_posting_id`, `company` and `title`). A stale answer from an earlier
    pause is removed first. The request names the run's process and the
    minutes it waits, so a request whose run is gone is told apart
    (`pending_requests`)."""
    job_id = str(job.get("job_posting_id") or job.get("job") or "")
    folder = pause_dir()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        answer_path(job_id).unlink()
    except OSError:
        pass
    path = request_path(job_id)
    atomic_write_json(path, {
        "job": job_id,
        "company": str(job.get("company") or job.get("company_name") or ""),
        "title": str(job.get("title") or job.get("job_title") or ""),
        "page_url": str(page_url or ""),
        "reason": str(reason or ""),
        "headless": bool(headless),
        "pause_id": str(pause_id or ""),
        "asked_at": datetime.now().isoformat(timespec="seconds"),     # for display
        "asked_epoch": time.time(),     # the age `_stale` reads (review N3)
        "pid": os.getpid(),
        "minutes": minutes,
        "questions": [dict(q) for q in questions],
    })
    return path


def read_request(path: Path) -> dict | None:
    """A request file's contents, or None when it is missing or unreadable."""
    data = read_json_dict(Path(path))
    if not data or not isinstance(data.get("questions"), list):
        return None
    return data


def pid_alive(pid: Any) -> bool:
    """Is process `pid` still running? Never signals it: on Windows
    `os.kill` ends the process, so the process is opened for a query and
    its exit code read."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ACCESS_DENIED    # it runs as another user
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _stale(data: Mapping[str, Any]) -> bool:
    """A request whose run is gone: its process no longer runs, or it is
    older than the minutes it waits plus `STALE_MARGIN_S`. The age is
    counted in epoch seconds (`asked_epoch`), so a DST change never moves
    it; the local `asked_at` is for display only (review N3). A request
    with no epoch is aged by its process alone."""
    pid = data.get("pid")
    if pid not in (None, "") and not pid_alive(pid):
        return True
    try:
        asked = float(data["asked_epoch"])
        minutes = float(data.get("minutes") or MINUTES_MAX)
    except (KeyError, TypeError, ValueError):
        return False
    return time.time() - asked > minutes * 60 + STALE_MARGIN_S


def pending_requests() -> list[dict]:
    """Every request in `pause_dir()` still waiting for its answer, the
    oldest first. A request whose run is gone (`_stale`: a drain killed
    during its wait) is removed."""
    folder = pause_dir()
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.json")):
        if path.name.endswith(".answer.json"):
            continue
        data = read_request(path)
        if data is None or answer_path(data.get("job", "")).exists():
            continue
        if _stale(data):
            clear(data.get("job", ""))
            continue
        out.append(data)
    return sorted(out, key=_asked_order)


def _asked_order(data: Mapping[str, Any]) -> tuple[float, str]:
    """Oldest first by epoch seconds (review N3), then by the local time."""
    try:
        epoch = float(data.get("asked_epoch") or 0.0)
    except (TypeError, ValueError):
        epoch = 0.0
    return epoch, str(data.get("asked_at") or "")


def write_answer(job_id: Any, mode: str, values: Mapping[str, Any] | None = None,
                 save: Mapping[str, Any] | None = None, *, pause_id: str = "") -> Path:
    """The answer file the run waits for. `mode`: "fill" (put `values` in,
    question key -> value), "browser" (the person filled the page in the
    browser) or "park". `save`: question key -> True to keep that value as
    a custom answer for future runs."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    folder = pause_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = answer_path(job_id)
    atomic_write_json(path, {
        "job": str(job_id),
        "mode": mode,
        "pause_id": str(pause_id or ""),
        "values": {str(k): str(v) for k, v in (values or {}).items()
                   if v is not None and str(v).strip()},
        "save": {str(k): bool(v) for k, v in (save or {}).items() if v},
    })
    return path


def read_answer(job_id: Any, pause_id: str = "") -> dict | None:
    """The answer file for `job_id`, or None when there is none, it does
    not parse, its mode is unknown, or it answers another pause."""
    data = read_json_dict(answer_path(job_id))
    if not data or data.get("mode") not in MODES:
        return None
    theirs = str(data.get("pause_id") or "")
    if pause_id and theirs and theirs != pause_id:
        return None
    values = data.get("values") if isinstance(data.get("values"), dict) else {}
    save = data.get("save") if isinstance(data.get("save"), dict) else {}
    return {"mode": data["mode"],
            "values": {str(k): str(v) for k, v in values.items() if str(v).strip()},
            "save": {str(k): bool(v) for k, v in save.items()}}


def clear(job_id: Any) -> None:
    for path in (request_path(job_id), answer_path(job_id)):
        try:
            path.unlink()
        except OSError:
            pass


def _page_closed(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:       # noqa: BLE001  (a page double without the call)
        return False


def wait_for_answer(page, job_id: Any, minutes: float, *, pause_id: str = "",
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], Any] | None = None,
                    poll_s: float | None = None) -> dict | None:
    """The answer to `job_id`'s pause (`read_answer`), or None once `minutes`
    pass with none. Each poll waits through `page.wait_for_timeout`, so a
    window or tab closed meanwhile raises `PageClosed`; a page double
    without that call waits through `sleep`."""
    poll = POLL_S if poll_s is None else poll_s
    limit = max(0.0, float(minutes)) * SECONDS_PER_MINUTE
    start = clock()
    while True:
        if page is not None and _page_closed(page):
            raise PageClosed()
        got = read_answer(job_id, pause_id)
        if got is not None:
            return got
        if clock() - start >= limit:
            return None
        waiter = getattr(page, "wait_for_timeout", None) if page is not None else None
        if waiter is not None:
            try:
                waiter(int(poll * 1000))
            except Exception as e:      # noqa: BLE001  (the target closed under the wait)
                raise PageClosed() from e
        elif sleep is not None:
            sleep(poll)
        else:
            time.sleep(poll)


# -- questions -----------------------------------------------------------------------------

def _tick(f) -> bool:
    """A single tick box (a consent, "I have read ..."): answered Yes or No."""
    return (str(getattr(f, "type", "")) == "checkbox" and f.widget != "checkbox_group") \
        or f.widget == "aria_check"


def is_sensitive(f) -> bool:
    return apply_judge.is_sensitive_field(f.label, getattr(f, "id_or_name", ""))


def widget_for(f) -> str:
    """The card's widget for field `f`: the live options as a choice, a tick
    box as yes / no, a number box, a text box, or "browser" for a sensitive
    field, a file, a password or a list whose options were never read."""
    if is_sensitive(f) or f.type == "file" or getattr(f, "secret", False) \
            or f.type == "password":
        return W_BROWSER
    if _tick(f):
        return W_YES_NO
    if f.options:
        return W_CHOICE
    if f.type in ("select", "radio", "listbox") or f.widget in ("choice", "popup",
                                                               "hidden_select",
                                                               "checkbox_group"):
        return W_BROWSER        # a list with no option read: nothing to pick from
    if f.type == "number":
        return W_NUMBER
    return W_TEXT


def question_for(f, *, required: bool | None = None) -> dict:
    """The request's entry for digest field `f`."""
    widget = widget_for(f)
    return {"key": str(f.n), "field_id": str(getattr(f, "id_or_name", "") or ""),
            "label": str(f.label or ""), "help": str(getattr(f, "help", "") or ""),
            "placeholder": str(getattr(f, "placeholder", "") or ""),
            "type": apply_facts.answer_type(f.type, f.options) if widget != W_YES_NO
            else "yes_no",
            "field_type": str(f.type or ""), "widget": widget,
            "options": [str(o) for o in f.options] if widget == W_CHOICE else [],
            "required": bool(f.required if required is None else required),
            "sensitive": is_sensitive(f)}


def answerable(reason: str) -> bool:
    """Is `reason` a park a pause can answer?"""
    text = str(reason or "")
    return text.startswith(_REQUIRED_HEAD) or bool(_SENSITIVE_RE.match(text))


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def exact_option(value: str, options: Iterable[str]) -> str | None:
    """The live option `value` names, whitespace runs aside, else None."""
    want = _norm(value)
    return next((str(o) for o in options if _norm(o) == want), None) if want else None


def apply_value(pf, f, value: str) -> bool:
    """Plan field `pf` (for digest field `f`) takes the person's `value`:
    text as a fill, a choice as one of the live options exactly, a tick box
    ticked for Yes. Returns False (the plan unchanged) for a value that does
    not fit, a No for a tick box, or a field code never types."""
    widget = widget_for(f)
    value = str(value or "").strip()
    if not value or widget == W_BROWSER:
        return False
    if widget == W_CHOICE:
        opt = exact_option(value, f.options)
        if opt is None:
            return False
        pf.action, pf.option, pf.value = "select", opt, opt
    elif widget == W_YES_NO:
        if apply_answers.yes_no(value) != "Yes":
            return False
        pf.action, pf.option, pf.value = "select", "checked", "yes"
    elif widget == W_NUMBER:
        if not _NUMBER_RE.match(value):
            return False
        pf.action, pf.option, pf.value = "fill", None, value
    else:
        pf.action, pf.option, pf.value = "fill", None, value
    pf.fact_key = USER_SOURCE
    pf.confidence = 1.0
    return True


def valid_value(question: Mapping[str, Any], value: str) -> bool:
    """Does `value` fit the request's `question` as its widget takes it: a
    choice one of the live options, a yes / no a Yes or a No, a number a
    number? Text fits as typed."""
    widget = question.get("widget")
    if widget == W_CHOICE:
        return exact_option(value, question.get("options") or []) is not None
    if widget == W_YES_NO:
        return apply_answers.yes_no(value) in ("Yes", "No")
    if widget == W_NUMBER:
        return bool(_NUMBER_RE.match(str(value or "").strip()))
    return True


def holds_value(f, got: str) -> bool:
    """Does the read-back `got` of field `f` hold an answer (the person's,
    typed in the browser)? A list must show one of its options, a tick box
    its tick, a box a value other than its placeholder."""
    got = _norm(got)
    if not got:
        return False
    if _tick(f):
        return got == "checked"
    if f.options:
        return exact_option(got, f.options) is not None
    return got != _norm(getattr(f, "placeholder", ""))


def recompute_park(plan, fields: Mapping[int, Any]) -> None:
    """The plan's park reason after the person's answers: a required
    sensitive field still blank, then a required field still blank, else
    none."""
    sensitive = required = ""
    for pf in plan.fields:
        if pf.action != "skip" or not pf.required:
            continue
        f = fields.get(pf.n)
        if f is not None and is_sensitive(f):
            sensitive = sensitive or apply_judge.sensitive_reason(pf.label)
        else:
            required = required or f"{_REQUIRED_HEAD}: {pf.label}"
    plan.park_reason = sensitive or required


# -- the save (PR-6) -----------------------------------------------------------------------

def builtin_answering(question: str) -> str:
    """The question text of the built-in answer the run fills `question` from,
    or "": the built-in's own words (`apply_answers.find_collision`), or a
    question `apply_facts.question_fit` reads as that built-in's own. A
    narrower question (another country, a city, a visa type, years of one
    skill) is "", so a custom answer can hold it. So is a heading with no verb
    over a yes / no built-in ("Work authorization", `apply_facts.noun_phrase`):
    it heads a status list as often as a Yes / No, and the run fills a status
    list there from a custom answer."""
    hit = apply_answers.find_collision(question, [])
    if hit:
        return hit
    for key in apply_facts.OWN_QUESTIONS:
        builtin = apply_answers.BUILTINS.get(key)
        if builtin is None or apply_facts.question_fit(key, question) != "own":
            continue
        if builtin.type == "yes_no" and apply_facts.noun_phrase(question):
            continue
        return builtin.question
    return ""


def save_refusal(question: str, answers: list[dict]) -> str:
    """Why the card's "Save for future runs" cannot keep `question`, or "":
    the refusals of Add answer."""
    owner = builtin_answering(question)
    if owner:
        return f"the built-in answer '{owner}' already answers this question"
    dup = apply_answers.find_collision(question, answers)
    if dup:
        return f"your custom answer '{dup}' already has this question"
    return ""


def save_answer(question: Mapping[str, Any], value: str, company: str, *,
                today: date | None = None, path: Path | None = None) -> str:
    """Keep the person's `value` for a request `question` as a confirmed
    custom answer through the store's own save (atomic, with a `.bak`).
    Returns "" once saved, else why it was not."""
    text = apply_facts.saved_question(question.get("label", ""), question.get("help", ""))
    if not text:
        return "the question has no words"
    try:
        store = apply_answers.load_store(path)
    except apply_answers.AnswerStoreError as e:
        return f"the answer store could not be read ({type(e).__name__})"
    answers = list(store["answers"])
    why = save_refusal(text, answers)
    if why:
        return why
    etype = str(question.get("type") or "text")
    if etype not in apply_answers.CUSTOM_TYPES:
        etype = "text"          # a choice is saved as text (`apply_facts.answer_type`)
    answer = str(value).strip()
    if etype == "yes_no":
        answer = apply_answers.yes_no(answer) or answer
    day = (today or date.today()).isoformat()
    note = f"Saved from {company or 'an application'} on {day}"[:apply_answers.NOTE_MAX]
    taken = {str(e.get("id", "")).strip() for e in answers if isinstance(e, dict)}
    answers.append({"id": apply_answers.new_id(text, taken), "question": text, "type": etype,
                    "answer": answer, "note": note, "confirmed": True, "status": "active"})
    try:
        # the store's review list goes back as read: a version 1 file
        # migrated in memory keeps its migration review (final review A I-3)
        apply_answers.save(answers, path, review=store["review"])
    except (ValueError, apply_answers.AnswerStoreError, OSError) as e:
        return str(e) if isinstance(e, ValueError) else type(e).__name__
    return ""


# -- the notice (PR-3) ---------------------------------------------------------------------

# the outline goes on the element's style; the element object keeps the
# outline it had and the one set (the browser's own spelling of it), so the
# outline comes off only while it is still ours
_OUTLINE_JS = """(el, [on, style]) => {
  if (on) {
    if (el.__inployedPause === undefined) el.__inployedBefore = el.style.outline;
    el.style.outline = style;
    el.__inployedPause = el.style.outline;
    return;
  }
  if (el.__inployedPause !== undefined && el.style.outline === el.__inployedPause)
    el.style.outline = el.__inployedBefore || '';
  delete el.__inployedPause;
}"""


def outline(page, fields: Iterable[Any], on: bool = True) -> None:
    """Outline the asked fields on `page` (a style change only), or take the
    outline off again."""
    import apply_form
    for f in fields:
        loc = getattr(f, "click_locator", None) or f.locator
        try:
            apply_form.resolve(page, loc).first.evaluate(_OUTLINE_JS, [bool(on), OUTLINE],
                                                         timeout=1_000)
        except Exception:       # noqa: BLE001  (a control gone, a page double)
            pass


def bring_to_front(page) -> None:
    try:
        page.bring_to_front()
    except Exception:       # noqa: BLE001  (a page double, a closed tab)
        pass


# -- one job's pauses ----------------------------------------------------------------------

def _path_of(url: str) -> str:
    parts = urlsplit(str(url or ""))
    return f"{parts.hostname or ''}{parts.path}"


def field_keys(fields: Iterable[Any], path: str) -> dict[int, tuple]:
    """n -> the field's key across a replan: the page's path, its id (else
    name), its label, and its place among the page's fields with the same
    id and label. Two fields labelled alike keep their own answers (review
    I2): by id, else by their order on the page."""
    seen: dict[tuple[str, str], int] = {}
    out: dict[int, tuple] = {}
    for f in fields:
        base = (str(getattr(f, "id_or_name", "") or ""), _norm(f.label))
        place = seen.get(base, 0)
        seen[base] = place + 1
        out[f.n] = (path, *base, place)
    return out


class Pauser:
    """The pauses of one job run (`apply_run._JobRun`): `jr` gives the page,
    the settings, the clock and the run's own steps; `parked` is the run's
    park exception, raised with the park's reason when the window closes
    during a wait and the run gives no park of its own (`_closed`)."""

    def __init__(self, jr, parked: type[Exception]):
        self.jr = jr
        self.parked = parked
        self.count = 0
        # kept for the next plan only (`apply_pending` clears them), each
        # field by `field_keys`
        self.pending: dict[tuple, str] = {}     # key -> the card's value
        self.asked: set[tuple] = set()          # asked fields, read back on the replan
        self.typed: dict[tuple, str] = {}       # key -> what a box the run filled read before
        self.saved: list[str] = []                      # questions saved this job
        self.refused: list[tuple[str, str]] = []        # (question, why) not saved

    # -- settings and state

    def minutes(self) -> int:
        try:
            raw = int(self.jr.r.settings.get("auto_apply_pause_minutes", 0) or 0)
        except (TypeError, ValueError):
            raw = 0
        return max(0, min(MINUTES_MAX, raw))

    def can_pause(self) -> bool:
        jr = self.jr
        if NEVER_WAIT or jr.page is None or self.minutes() <= 0 or self.count >= MAX_PAUSES:
            return False
        sent = getattr(jr, "_maybe_sent", None)
        return not (callable(sent) and sent())

    def _headless(self) -> bool:
        return bool(self.jr.r.settings.get("auto_apply_headless"))

    def _url(self) -> str:
        return str(getattr(self.jr.page, "url", "") or "")

    def _print(self) -> tuple | None:
        """The page as the pause saw it: its URL and its fields' labels and
        types; None when it cannot be read."""
        return self._read()[0]

    def _read(self) -> tuple[tuple | None, tuple]:
        """The page's print (`_print`) and its visible buttons as (text,
        locator) pairs, from one read; (None, ()) when it cannot be read."""
        try:
            digest = self.jr._extract()
        except Exception:       # noqa: BLE001  (a page mid-navigation)
            return None, ()
        return ((self._url(), tuple((_norm(f.label), f.type) for f in digest.fields)),
                tuple((b.text, tuple(b.locator)) for b in digest.buttons))

    # -- the hooks

    def apply_pending(self, digest, plan) -> None:
        """Before a page's fill: the person's answers kept for this page (a
        page that changed during a pause, a disabled way on) go in their
        fields, and the fields asked on it that the person filled in the
        browser stay as they are (`KEPT`), and so does a box the run filled
        that the person changed in the browser (review I3). Each field is
        found by its own key (`field_keys`), and what was kept applies to
        this plan only (review M7)."""
        if not self.pending and not self.asked and not self.typed:
            return
        pending, asked, typed = self.pending, self.asked, self.typed
        self.pending, self.asked, self.typed = {}, set(), {}
        keys = field_keys(digest.fields, _path_of(self._url()))
        fields = {f.n: f for f in digest.fields}
        changed = False
        answered: set[str] = set()
        import apply_fill
        for pf in plan.fields:
            f = fields.get(pf.n)
            key = keys.get(pf.n)
            if f is None or key is None or pf.action == apply_judge.PASSWORD_ACTION:
                continue
            value = pending.get(key)
            if value is not None and not is_sensitive(f) and apply_value(pf, f, value):
                answered.add(pf.label)
                changed = True
                self.jr._decide("pause_answer", f"the answer you gave for {pf.label!r} goes in "
                                                "its field on the page read again",
                                fields=[pf.label])
            elif key in asked and pf.action not in ("fill", "select", "upload") \
                    and holds_value(f, apply_fill.read_back(self.jr.page, pf)):
                pf.action, pf.fact_key = KEPT, USER_SOURCE
                answered.add(pf.label)
                changed = True
            elif key in typed and pf.action in ("fill", "select"):
                now = apply_fill.read_back(self.jr.page, pf)
                if _norm(now) != typed[key] and holds_value(f, now):
                    pf.action, pf.fact_key = KEPT, USER_SOURCE
                    answered.add(pf.label)
                    changed = True
                    self.jr._decide("pause_kept", f"you changed {pf.label!r} in the browser "
                                                  "during the pause: your value stays",
                                    fields=[pf.label])
        if changed:
            plan.missing = [(q, c) for q, c in plan.missing if q not in answered]
            recompute_park(plan, fields)

    def _typed_now(self, digest, plan) -> dict[tuple, str]:
        """Key -> what each labelled box the run filled on this page reads
        now, before a pause after the fill: a box that reads otherwise on the
        replan is the person's (review I3)."""
        import apply_fill
        keys = field_keys(digest.fields, _path_of(self._url()))
        out: dict[tuple, str] = {}
        for pf in plan.fields:
            if pf.action not in ("fill", "select") or not pf.label or pf.n not in keys:
                continue
            try:
                out[keys[pf.n]] = _norm(apply_fill.read_back(self.jr.page, pf))
            except Exception:       # noqa: BLE001  (a control gone, a page double)
                continue
        return out

    def at_plan(self, digest, plan) -> None:
        """PR-1: a page whose plan parks on a required field with no answer,
        or a required sensitive field, pauses before the fill. Returns with
        the plan as it was when no answer came (the caller parks as before)."""
        if not answerable(plan.park_reason) or not self.can_pause():
            return
        fields = {f.n: f for f in digest.fields}
        asked = [fields[pf.n] for pf in plan.fields
                 if pf.required and pf.action == "skip" and pf.n in fields]
        if not asked:
            return
        answer, before = self._wait(plan.park_reason, asked)
        if answer is None:
            return
        self._resume_same_page(digest, plan, asked, answer, before)

    def at_tie(self, digest, plan, tied: dict) -> list:
        """PR-1: a required field left with no option chosen (a tie, a list
        never read) pauses after the fill. The person's pick goes in on the
        spot; the `apply_fill.Filled` of each pick comes back, and each
        answered field leaves `tied`."""
        required = [pf for pf in plan.fields if pf.n in tied and pf.required]
        if not required or not self.can_pause():
            return []
        fields = {f.n: f for f in digest.fields}
        asked = [fields[pf.n] for pf in required if pf.n in fields]
        if not asked:
            return []
        first = required[0]
        typed = self._typed_now(digest, plan)
        answer, before = self._wait(f"{_REQUIRED_HEAD}: {first.label} ({tied[first.n]})", asked)
        if answer is None:
            return []
        picked = self._resume_same_page(digest, plan, asked, answer, before, typed)
        import apply_fill
        errors: list[dict] = []
        filled = apply_fill.apply(self.jr.page, apply_judge.FillPlan(fields=picked),
                                  deadline=self.jr.deadline, clock=self.jr.r.clock,
                                  errors=errors) if picked else []
        failed = {e.get("n") for e in errors}
        took = {pf.n for pf in picked}
        for pf in plan.fields:
            if pf.n in tied and (pf.action == KEPT or (pf.n in took and pf.n not in failed)):
                del tied[pf.n]
        return [f for f in filled if f.n not in failed]

    def at_disabled(self, digest, plan, reason: str) -> None:
        """PR-1: a way on still disabled after the fill pauses: the person
        fixes the page in the browser (or answers its blank fields in the
        card), then the page is read and planned again (`Replan`). Every
        labelled box the run filled is read before the wait, so a value the
        person changes stays theirs on the replan. Returns when no answer
        came (the caller parks as before)."""
        if not self.can_pause():
            return
        fields = {f.n: f for f in digest.fields}
        asked = [fields[pf.n] for pf in plan.fields
                 if pf.action == "skip" and pf.label and pf.n in fields]
        typed = self._typed_now(digest, plan)
        answer, before = self._wait(f"{reason}; fix it in the browser, then continue", asked)
        if answer is None:
            return
        self._keep_for_replan(digest, asked, answer, before, typed)
        raise Replan("the way on was disabled")

    # -- the wait and the resume

    def _wait(self, reason: str, asked: list) -> tuple[dict | None, tuple | None]:
        """Write the request, give the notice, wait. Returns (the answer,
        the page's print at the start), the answer None on a timeout or
        "Park it". A window or tab closed during the wait, or found closed
        once it ends, raises the run's park (`_closed`): the person may have
        clicked through and sent it, from any page.

        A headless run whose questions only the browser takes parks at once
        (review M4). After the wait the run reads the page against its print,
        buttons and text from before (`_pause_moved`): a page that moved on
        (the person may have sent it) raises the run's park, whatever the
        answer (reviews I1, N2). Each value is checked against its question
        (`valid_value`) before anything is saved or filled (review M5), and
        every resume reads the answer store again (review M2)."""
        jr = self.jr
        minutes = self.minutes()
        questions = [question_for(f) for f in asked]
        if self._headless() and all(q["widget"] == W_BROWSER for q in questions):
            jr._decide("pause_skipped", "the browser is headless and nothing here can be "
                                        "answered in the dashboard: the job parks as before",
                       fields=[q["label"] for q in questions])
            return None, None
        self.count += 1
        pid = uuid.uuid4().hex[:12]
        before, buttons = self._read()
        text = self._text()
        write_request(jr.entry, self._url(), reason, questions, headless=self._headless(),
                      pause_id=pid, minutes=minutes)
        labels = [q["label"] for q in questions]
        jr._decide("pause", f"waiting for you (up to {minutes} min): {reason}", fields=labels)
        jr._trace("pause", reason=reason, fields=labels, minutes=minutes)
        words = (f"job {jr.job_id}: waiting for you: {reason}. Answer in the dashboard's "
                 f"Auto-apply tab" + ("" if self._headless() else " or fill it in the browser")
                 + f" (up to {minutes} min).")
        jr.log.warning("%s", words)
        if getattr(jr.r, "drain_report", False):
            try:
                print("\a" + words, flush=True)
            except UnicodeEncodeError:
                print("\a" + words.encode("ascii", "replace").decode("ascii"), flush=True)
        bring_to_front(jr.page)
        outline(jr.page, asked, True)
        start = jr.r.clock()
        try:
            answer = wait_for_answer(jr.page, jr.job_id, minutes, pause_id=pid,
                                     clock=jr.r.clock, sleep=jr.r.sleep)
        except PageClosed:
            clear(jr.job_id)
            raise self._closed(buttons, reason) from None
        finally:
            jr.deadline += max(0.0, jr.r.clock() - start)
        clear(jr.job_id)
        if _page_closed(jr.page):
            # a close after the wait's last poll (final fix review Minor 2)
            raise self._closed(buttons, reason)
        outline(jr.page, asked, False)
        moved = getattr(jr, "_pause_moved", None)
        park = moved(before, text, reason, buttons=buttons) if callable(moved) else None
        if park is not None:
            raise park
        if answer is None:
            jr._decide("pause_timeout", f"no answer in {minutes} min: the job parks as before")
            return None, before
        if answer["mode"] == "park":
            jr._decide("pause_park", "you chose Park it: the job parks as before")
            return None, before
        answer = self._checked(questions, answer)
        jr._decide("pause_resume", f"you answered ({answer['mode']}): the run goes on",
                   answered=sorted(q["label"] for q in questions
                                   if q["key"] in answer["values"]))
        self._save(questions, answer)
        reload = getattr(jr, "_pause_reload", None)
        if callable(reload):
            reload()
        return answer, before

    def _closed(self, buttons: tuple, reason: str) -> Exception:
        """The park for a window or tab closed during the wait, before or
        after its last poll. The person had the browser and may have clicked
        through and sent it before the close (final review A I-2, final fix
        review Important 2): the run's own park (`_pause_closed`, the
        check-whether end) when it gives one, else the plain park with
        `reason`."""
        hook = getattr(self.jr, "_pause_closed", None)
        park = hook(buttons, reason) if callable(hook) else None
        return park if isinstance(park, Exception) else self.parked("needs_human", reason)

    def _text(self) -> str:
        read = getattr(self.jr, "_page_text", None)
        try:
            return str(read() or "") if callable(read) else ""
        except Exception:       # noqa: BLE001  (a page mid-navigation)
            return ""

    def _checked(self, questions: list[dict], answer: dict) -> dict:
        """The answer less each value its question does not take
        (`valid_value`): a choice that names no live option is never saved
        or filled (review M5)."""
        by_key = {q["key"]: q for q in questions}
        values = {}
        for key, value in answer["values"].items():
            q = by_key.get(key)
            if q is not None and not valid_value(q, value):
                self.jr._decide("pause_value_refused", f"the answer for {q['label']!r} does not "
                                                       "fit the field: it is left out",
                                fields=[q["label"]])
                continue
            values[key] = value
        return {**answer, "values": values}

    def _save(self, questions: list[dict], answer: dict) -> None:
        """PR-6: each value flagged "save" kept as a custom answer (the run
        reads the store again after every resume, `_wait`)."""
        if answer["mode"] != "fill":
            return
        company = str(self.jr.entry.get("company") or "")
        for q in questions:
            value = answer["values"].get(q["key"])
            if not value or not answer["save"].get(q["key"]) or q["sensitive"] \
                    or q["widget"] == W_BROWSER:
                continue
            why = save_answer(q, value, company)
            if why:
                self.refused.append((q["label"], why))
                self.jr._decide("pause_save_refused", f"{q['label']!r} was not saved: {why}")
            else:
                self.saved.append(q["label"])
                self.jr._decide("pause_saved", f"{q['label']!r} saved for future runs")

    def _moved_on(self, before: tuple | None) -> bool:
        """Did the page move on from the one the run paused on (`before`,
        its print)? Its whole address changed (a hash-routed app's
        `#/step2` too), any of its labelled fields is gone, or it cannot be
        read (then or now). On a single-page app whose address never
        changes, a next step that lacks any of the paused step's labelled
        fields reads as moved, even when it repeats the asked field (final
        fix review Important 1). A next step that re-shows every one of them
        (the same labels and types, more added) does not: its box with the
        asked field's id and label takes the answer, which is the person's
        answer to that same question (an accepted residual). A field the
        person's browser edit removed costs one more pause for the same
        question at most."""
        after = self._print()
        if before is None or after is None:
            return True
        was = {row for row in before[1] if row[0]}
        return str(after[0]) != str(before[0]) or not was <= set(after[1])

    def _keep_for_replan(self, digest, asked: list, answer: dict, before: tuple | None,
                         typed: Mapping[tuple, str] | None = None) -> None:
        """What the next plan takes (`apply_pending`): the card's values and
        the asked fields by their own keys (`field_keys`) on the page the run
        paused on (`before`, its print), and what each box the run filled
        read before the wait. The card's values go only into the fields they
        answer on that page (final review A I-1): a page that moved on during
        the wait (`_moved_on`, a wizard's Next the person clicked) drops them,
        and the asked fields are still read back there."""
        url = before[0] if before is not None else self._url()
        keys = field_keys(digest.fields, _path_of(url))
        drop = answer["mode"] == "fill" and self._moved_on(before)
        if drop and any(answer["values"].get(str(f.n)) for f in asked):
            self.jr._decide("pause_answer_dropped", "the page moved on during the wait: the "
                                                    "answers you gave for the page it paused on "
                                                    "go nowhere else")
        for f in asked:
            key = keys.get(f.n)
            if key is None:
                continue
            self.asked.add(key)
            value = answer["values"].get(str(f.n))
            if answer["mode"] == "fill" and value and widget_for(f) != W_BROWSER and not drop:
                self.pending[key] = value
        self.typed.update(typed or {})

    def _resume_same_page(self, digest, plan, asked: list, answer: dict,
                          before: tuple | None, typed: Mapping[tuple, str] | None = None
                          ) -> list:
        """A "fill" answer on the page as it was: each value into its plan
        field; a field the card left blank that the person filled in the
        browser is `KEPT`. A "browser" answer, or a page that changed during
        the wait, raises `Replan` with the answers kept for the new plan.
        Returns the plan fields that took a value."""
        if answer["mode"] == "browser":
            self._keep_for_replan(digest, asked, answer, before, typed)
            raise Replan("filled in the browser")
        after = self._print()
        if before is None or after != before:
            self._keep_for_replan(digest, asked, answer, before, typed)
            self.jr._decide("pause_page_changed", "the page changed during the wait: it is "
                                                  "read and planned again before any fill")
            raise Replan("the page changed")
        import apply_fill
        fields = {f.n: f for f in digest.fields}
        by_n = {pf.n: pf for pf in plan.fields}
        took: list = []
        answered: set[str] = set()
        for f in asked:
            pf = by_n.get(f.n)
            if pf is None:
                continue
            value = answer["values"].get(str(f.n))
            if value and not is_sensitive(f) and apply_value(pf, f, value):
                took.append(pf)
                answered.add(pf.label)
            elif holds_value(f, apply_fill.read_back(self.jr.page, pf)):
                pf.action, pf.fact_key = KEPT, USER_SOURCE
                answered.add(pf.label)
        plan.missing = [(q, c) for q, c in plan.missing if q not in answered]
        recompute_park(plan, fields)
        return took


def user_drafts(plan, shaped: Mapping[int, Any]) -> dict[int, str]:
    """n -> the person's text for each fill the pause put in and `shaped`
    does not cover: checked in code (the read-back holds it), never by the
    judge against the sheet."""
    return {pf.n: str(pf.value) for pf in plan.fields
            if pf.fact_key == USER_SOURCE and pf.action == "fill" and pf.n not in shaped}


def kept_locators(plan) -> list:
    return [pf.locator for pf in plan.fields if pf.action == KEPT]


__all__ = ["FOLDER", "KEPT", "MAX_PAUSES", "MODES", "NEVER_WAIT", "POLL_S", "PageClosed",
           "Pauser", "Replan", "SECONDS_PER_MINUTE", "STALE_MARGIN_S", "USER_SOURCE",
           "answer_path", "answerable", "apply_value", "builtin_answering", "clear",
           "exact_option", "field_keys", "holds_value", "kept_locators", "outline", "pause_dir",
           "pending_requests", "pid_alive", "question_for", "read_answer", "read_request",
           "recompute_park", "request_path", "save_answer", "save_refusal", "user_drafts",
           "valid_value", "wait_for_answer", "widget_for", "write_answer", "write_request"]

