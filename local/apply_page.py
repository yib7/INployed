"""Opening and reading a page: the page signature, the empty, loading and
error page reads, `open_page`, the run's own step and frames of an error
(`error_step`, `error_frames`), popups, an email Apply's address,
`click_entry` and the tracker hops after it.

Split out of `apply_run`, which re-exports these names. It logs as
`apply_run` (one of `apply_trace.LOGGERS`).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import apply_click
import apply_fill
import apply_form
import apply_judge
import apply_limits
import apply_linkedin
from apply_judge import FillPlan, VerifyResult
from apply_outcome import _cap, _closed_error
from apply_sites import _host, _on_linkedin_redirector, _tracker, TRACKER_HOPS_MAX

log = logging.getLogger("apply_run")


# Text that changes while a page stands still: a relative time
# ("posted 3 minutes ago"), a clock, a count; left out of `page_signature`.
_VOLATILE_TEXT = re.compile(
    r"\b\d+\s*(?:s|sec|second|min|minute|h|hr|hour|d|day|week|month|year)s?\s+ago\b"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|\d+", re.I)
SIGNATURE_TEXT_CHARS = 400         # of the steadied text `page_signature` keeps
# A wizard's step marker ("Step 2 of 5", "1 / 3"): a number that moves only
# when the page does, kept in the signature
_STEP_MARKER = re.compile(r"\b(?:step\s+)?\d+\s*(?:of|/)\s*\d+\b", re.I)


def page_signature(url: str, digest: apply_form.FormDigest) -> tuple:
    """The page as it stands, for "did not advance": its host and
    path, its title, its fields (label, type, required), its button texts,
    its step markers ("2 of 3", `_STEP_MARKER`), and the head of its text
    with times, clocks and numbers taken out. Never the judge's read (a read
    that flips on the same page is the same page) and never a ticker, a
    timestamp or a counter."""
    parts = urlsplit(str(url or ""))
    raw = f"{digest.title or ''}\n{digest.text or ''}"
    steps = tuple(" ".join(m.group(0).lower().split()) for m in _STEP_MARKER.finditer(raw))
    text = " ".join(_VOLATILE_TEXT.sub(" ", digest.text or "").split())
    return (parts.hostname or "", parts.path, " ".join((digest.title or "").split()),
            tuple((" ".join((f.label or "").split()), f.type, bool(f.required))
                  for f in digest.fields),
            tuple(" ".join((b.text or "").split()) for b in digest.buttons),
            steps, text[:SIGNATURE_TEXT_CHARS])


def _fields_sig(digest: apply_form.FormDigest) -> tuple:
    """The page's form as the run saw it: each field's label and type."""
    return tuple((" ".join((f.label or "").split()), f.type) for f in digest.fields)


def _record_verification(rec: dict, verification: list[VerifyResult]) -> None:
    """The page record's verification, merged by field: a
    row for each field the page's fills verified, the latest read of each;
    a sub-fill (a revealed field, a repair) or a re-verification (a value
    the page changed, a retyped box) replaces its own fields' rows and
    keeps the rest."""
    rows = {int(r["n"]): r for r in rec.get("verification") or []}
    for v in verification:
        rows[v.n] = {"n": v.n, "label": v.label, "ok": v.ok, "p_correct": v.p_correct,
                     "p_placeholder": v.p_placeholder}
    rec["verification"] = list(rows.values())


def _spare_judged(plan: FillPlan, judged: set[int]) -> None:
    """The fields of a repair's plan that only the judge's reading of a
    message named (`judged`) are asked as required but kept
    optional in the plan: one without an answer stays blank, never the
    plan's park reason and never a missing question for the person (a
    confident wrong reading must not park on another field or ask for an
    answer the form never asked for). `_repair_named` decides on them."""
    if not judged:
        return
    labels = set()
    for pf in plan.fields:
        if pf.n in judged:
            pf.required = False
            if pf.action == "skip":
                labels.add(pf.label)
    plan.missing = [(q, c) for q, c in plan.missing if q not in labels]
    head = "required field without an answer: "
    if plan.park_reason.startswith(head) and plan.park_reason[len(head):] in labels:
        hard = next((pf for pf in plan.fields if pf.action == "skip" and pf.required), None)
        plan.park_reason = f"{head}{hard.label}" if hard is not None else ""


# A button outside any form (a wizard's footer): the fields of the lowest box
# above it that holds any, by their id, name or type; a box of another form
# (a talent-community sign-up) holds none of them
_BUTTON_HOME_JS = r"""el => {
  const FIELD = 'input:not([type=hidden]):not([type=submit]):not([type=button])'
    + ':not([type=image]):not([type=reset]), select, textarea';
  for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
    const got = Array.from(p.querySelectorAll(FIELD));
    if (got.length) return got.map((f) => f.id || f.getAttribute('name') || f.type).slice(0, 60);
  }
  return [];
}"""


def _ident_attrs(ident: str) -> str:
    """An identity (`apply_form.IDENT_FN_JS`) without its label part: the
    control's own tag, type, id, name, aria-label, test attributes and
    placeholder."""
    return str(ident).rsplit("|", 1)[0]


_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                         "\u00ab": '"', "\u00bb": '"', "`": "'"})


def _plain(text: str) -> str:
    """Words for a literal label match: case folded, curly quotes made
    straight, spaces folded, a required mark at either end dropped."""
    t = " ".join(str(text or "").translate(_QUOTES).lower().split())
    return t.strip(" *\u2731\uff0a:")


def field_named_in(message: str, fields) -> int | None:
    """The one field whose whole label `message` holds, literally, after
    `_plain` (a label inside a longer word never counts: "Name" in
    "Username"); None when no label or more than one does. A label that also
    appears inside another field's label ("Email" in "Email confirmation")
    is ambiguous and never names a field."""
    labels = {f.n: _plain(f.label) for f in fields if _plain(f.label)}
    text = _plain(message)
    found = []
    for n, label in labels.items():
        if any(m != n and label in other for m, other in labels.items()):
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(label)}(?![a-z0-9])", text):
            found.append(n)
    return found[0] if len(found) == 1 else None


def _message_key(text: str) -> str:
    """A form's message, spaces folded (the key of `_JobRun._spared`)."""
    return " ".join(str(text or "").split())


def _label_key(label: str) -> str:
    """A field's question words, spaces and case folded (the key the
    messages' tried fields keep)."""
    return " ".join((label or "").split()).lower()


def _empty_read(digest: apply_form.FormDigest) -> bool:
    """A read taken before the page rendered (0 characters and 0
    controls at `load` on five ATSs): no button at all (a form whose footer
    renders late), or no form field and under `EMPTY_TEXT_MIN` characters of
    visible text. A page with form fields and a button has rendered, however
    short it is: a sign-in box and its button make a whole page."""
    if not digest.buttons:
        return True
    if digest.fields:
        return False
    return len((digest.text or "").strip()) < apply_limits.EMPTY_TEXT_MIN


def _dropped_load(e: BaseException) -> bool:
    """A load the network dropped: Playwright's `net::ERR_...`, or Chromium
    swapping the page for its own error page mid-load."""
    text = str(e)
    return "net::ERR_" in text or "chrome-error://" in text


MALFORMED_REASON = "malformed queue entry"
# the entry's values the run reads as text: a path, an address, a host
_ENTRY_TEXT = (("artifacts", ("apply_md", "folder", "resume_pdf", "cover_letter_pdf")),
               ("ats", ("domain", "system")))


def entry_problem(entry: Any) -> str:
    """What makes a queue entry one the run cannot work, in words
    that carry no value of it (what the value must be, and the type it has),
    or "" for a sound one: `artifacts` or `ats` that is no mapping, a path,
    address or host that is no text (or holds a NUL, which no path takes),
    an `apply_url` that is no text, `attempts` that is no number."""
    if not isinstance(entry, Mapping):
        return f"the entry must be a mapping (got {type(entry).__name__})"
    for key, names in _ENTRY_TEXT:
        value = entry.get(key)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            return f"{key} must be a mapping (got {type(value).__name__})"
        for name in names:
            v = value.get(name)
            if v is None:
                continue
            if not isinstance(v, str):
                return f"{key}.{name} must be text (got {type(v).__name__})"
            if "\x00" in v:
                return f"{key}.{name} holds a NUL character"
    url = entry.get("apply_url")
    if url is not None and not isinstance(url, str):
        return f"apply_url must be text (got {type(url).__name__})"
    attempts = entry.get("attempts")
    if attempts is not None:
        try:
            int(attempts)
        except (TypeError, ValueError):
            return "attempts must be a number"
    return ""


# The run's own code: apply_run.py and the modules split out of it. An error's
# step is the innermost function of any of them, as when they were one file.
_RUN_MODULES = ("apply_run", "apply_limits", "apply_outcome", "apply_sites", "apply_sendwatch",
                "apply_page", "apply_account_flow", "apply_route", "apply_gate", "apply_record",
                "apply_job", "apply_job_pages", "apply_job_form", "apply_job_submit")
_HERE = frozenset(os.path.normcase(os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{m}.py"))
                  for m in _RUN_MODULES)
ERROR_FRAMES = 12                  # the innermost frames of an error the trace keeps


def error_step(e: BaseException) -> str:
    """The run's own step an error came out of: the innermost
    function of the run's own modules (`_RUN_MODULES`) on its traceback,
    without the leading underscore ("run" when none is)."""
    step = ""
    tb = e.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        try:
            if os.path.normcase(os.path.abspath(code.co_filename)) in _HERE:
                step = code.co_name
        except (TypeError, ValueError):
            pass
        tb = tb.tb_next
    return step.lstrip("_") or "run"


def error_frames(e: BaseException) -> list[str]:
    """The innermost `ERROR_FRAMES` frames of an error's traceback (file,
    line, function and the source line): the code's words, never the
    error's message or a value."""
    rows = traceback.extract_tb(e.__traceback__)[-ERROR_FRAMES:]
    return [f"{Path(f.filename).name}:{f.lineno} {f.name}: {(f.line or '').strip()[:160]}"
            for f in rows]


def _error_page(url: str) -> bool:
    """Chrome's own error page (`chrome-error://chromewebdata/`), shown after
    a load the network dropped: never a site of the flow."""
    return str(url or "").startswith("chrome-error://")


def _settled_ms(info: Any) -> int:
    """The milliseconds `apply_fill.settle` reported (0 from a stand-in)."""
    return int(info.get("ms", 0)) if isinstance(info, Mapping) else 0


def _settle_capped(info: Any) -> bool:
    return bool(info.get("capped")) if isinstance(info, Mapping) else False


def settled_words(info: Any, then: str = "") -> str:
    """A settle in the trace's words: "settled N ms", and when the cap
    released it (a loading placeholder left up, a page that keeps moving),
    "settled N ms, capped", so a live run shows a page that makes every
    settle wait its whole cap."""
    words = f"settled {_settled_ms(info)} ms"
    if _settle_capped(info):
        words += (", capped (a loading placeholder stayed up or the page kept moving; "
                  "read as it was)")
    return f"{words} {then}".strip()


def _has_content(page) -> bool:
    try:
        return bool(page.evaluate(
            "() => !!document.body && (document.body.innerText || '').trim().length > 0"))
    except Exception:       # noqa: BLE001
        return False


def open_page(page, url: str, *, timeout_ms: int | None = None,
              settle_s: float | None = None) -> list[dict[str, Any]]:
    """The first load of a job's page, and the probe's: to
    `domcontentloaded` (a page whose `load` never fires, a stalled image or
    a script, is read all the same), one retry after `GOTO_RETRY_S` of a
    load the network dropped (`_dropped_load`), a timeout with a page on the
    screen read as it is; then the page settles. Returns the decisions
    taken ({what, why, ...}); a load that failed raises."""
    rows: list[dict[str, Any]] = []
    timeout = apply_limits.GOTO_TIMEOUT_MS if timeout_ms is None else int(timeout_ms)
    for attempt in (1, 2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            break
        except Exception as e:      # noqa: BLE001  (Playwright's Error and TimeoutError)
            if _closed_error(e):
                raise
            if type(e).__name__ == "TimeoutError" and _has_content(page):
                rows.append({"what": "goto_timeout", "why": "the first load timed out with a "
                                                            "page on the screen; reading it "
                                                            "as it is"})
                break
            if attempt == 2 or not _dropped_load(e):
                raise
            rows.append({"what": "goto_retry", "why": f"the first load failed on the network "
                                                      f"({type(e).__name__}); one retry",
                         "error": _cap(str(e).splitlines()[0] if str(e) else "", 120)})
            # Chromium swaps in its own error page after a dropped load; its
            # navigation would cut the retry short, so the retry waits for it
            # to be up (a fixed pause lost that race on a busy machine), then
            # pauses
            _error_page_up(page, apply_limits.GOTO_ERROR_PAGE_S)
            page.wait_for_timeout(int(apply_limits.GOTO_RETRY_S * 1000))
    info = apply_fill.settle(page, apply_limits.CLICK_TIMEOUT_S if settle_s is None else settle_s)
    rows.append({"what": "settled", "why": settled_words(info, "after the first load"),
                 **(info if isinstance(info, Mapping) else {})})
    return rows


def _error_page_up(page, cap_s: float) -> bool:
    """Wait, up to `cap_s` in all, for Chromium's error page (chrome-error://)
    to be the page and loaded: the condition `open_page`'s retry needs. False
    at the cap (a browser that shows no error page) or when the error page
    does not load in the time left: the retry goes
    on either way."""
    step_ms = 50
    cap_ms = max(step_ms, int(cap_s * 1000))
    waited = 0
    while waited < cap_ms:
        try:
            up = _error_page(str(page.url))
        except Exception:       # noqa: BLE001  (a page mid-navigation)
            up = False
        if up:
            try:
                page.wait_for_load_state("load", timeout=max(1, cap_ms - waited))
            except Exception:   # noqa: BLE001  (Playwright's TimeoutError: no load in time)
                return False
            return True
        page.wait_for_timeout(step_ms)
        waited += step_ms
    return False


def _page_closed(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:       # noqa: BLE001  (a page double)
        return False


def _page_print(page) -> tuple[str, str]:
    """(a tab's address and visible text hashed, the text): whether it moved
    on after the run left it, and what it showed. The hash leaves
    out what changes while a page stands still (`_VOLATILE_TEXT`: a relative
    time, a clock, a count) and keeps its step markers (`_STEP_MARKER`), as
    `page_signature` does. ("", "") for a tab that cannot
    be read."""
    try:
        text = apply_fill.page_text(page)
        url = str(page.url)
    except Exception:       # noqa: BLE001  (a closed tab, a page double)
        return "", ""
    steps = " ".join(" ".join(m.group(0).lower().split()) for m in _STEP_MARKER.finditer(text))
    steady = " ".join(_VOLATILE_TEXT.sub(" ", text).split())
    seen = f"{url}\n{steps}\n{steady}"
    return hashlib.sha256(seen.encode("utf-8", "replace")).hexdigest(), text


def _snapshot_or_none(page) -> Any:
    try:
        return apply_click._snapshot(page)
    except Exception:       # noqa: BLE001  (a page double, a page mid-navigation)
        return None


def _is_password(row: dict) -> bool:
    """A recorded row that came from a password-shaped control
    (`apply_form.is_password_field`, the one definition the planner and the
    accounts hook read too): its value is written as `<hidden>`."""
    return apply_form.is_password_field(str(row.get("type", "")),
                                        str(row.get("id_or_name", "")),
                                        str(row.get("label", "")),
                                        str(row.get("autocomplete", "")))


# The box the emailed code goes in (`apply_judge.code_field`: a verification,
# security, one-time or OTP code box first, never a postal or promo code).
_code_field = apply_judge.code_field


def _button_text(digest: apply_form.FormDigest, n: int) -> str:
    return next((b.text for b in digest.buttons if b.n == n), "")


def _chrome(digest: apply_form.FormDigest, n: int) -> bool:
    """Is button `n` in the site's header or top bar (`Button.chrome`)?"""
    return any(b.n == n and b.chrome for b in digest.buttons)


@contextmanager
def _popups(page):
    """The tabs `page` opens while the block runs, in order."""
    opened: list = []

    def _add(p) -> None:
        opened.append(p)
    try:
        page.on("popup", _add)
        listening = True
    except Exception:       # noqa: BLE001  (a page double)
        listening = False
    try:
        yield opened
    finally:
        if listening:
            try:
                page.remove_listener("popup", _add)
            except Exception:   # noqa: BLE001  (the page is gone)
                pass


_MAILTO_JS = ("el => { const a = el.closest('a[href]'); "
              "return a ? (a.getAttribute('href') || '') : ''; }")


def mailto_address(loc) -> str:
    """The address an Apply control mails to (`mailto:` on it or its link),
    without the query; "" when it is no email link."""
    try:
        href = str(loc.first.evaluate(_MAILTO_JS, timeout=apply_click.ACTION_TIMEOUT_MS) or "")
    except Exception:       # noqa: BLE001  (a page double, a detached element)
        return ""
    if not href.lower().startswith("mailto:"):
        return ""
    return _cap(href[len("mailto:"):].split("?", 1)[0], 120)


def click_entry(page, loc, *, timeout_ms: int | None = None,
                on_popup: Callable[[Any], None] | None = None) -> tuple[Any, str, int]:
    """Click an Apply entry and wait for what it does, whichever comes
    first: a new tab (the popup), a same-tab navigation, or a same-tab DOM
    change (none of 24 real entry clicks opened a popup, and a
    fixed popup wait cost 5 s on each). A link that opens a new tab
    (`target=_blank`) waits the whole window for its popup; a DOM change
    gets `POPUP_GRACE_S` more for a popup that follows it. Returns (the popup
    or None, "popup" | "navigation" | "dom" | "none" | "failed: <error>", the
    ms waited). A click that raised sent nothing on its way: "failed". A
    popup later still is the caller's (`LateWatch`). `on_popup` gets the new
    tab at its popup event, while it still shows the address it opened at
    (LinkedIn's `/safety/go/` hop moves on within a second)."""
    window_s = (apply_limits.POPUP_TIMEOUT_MS if timeout_ms is None else int(timeout_ms)) / 1000
    popups: list = []

    def _on_popup(p) -> None:
        popups.append(p)
        if on_popup is not None:
            try:
                on_popup(p)
            except Exception:   # noqa: BLE001  (a watch that fails never stops the click)
                pass

    try:
        page.on("popup", _on_popup)
    except Exception:       # noqa: BLE001  (a page double)
        pass
    start = time.monotonic()
    try:
        try:
            new_tab = bool(loc.first.evaluate(
                "el => { const a = el.closest('a[href]'); return !!a && a.target === '_blank'; }",
                timeout=apply_click.ACTION_TIMEOUT_MS))
        except Exception:   # noqa: BLE001  (the element is read again by the click)
            new_tab = False
        before = _snapshot_or_none(page)
        url0 = str(page.url)
        try:
            loc.first.click(timeout=apply_click.ACTION_TIMEOUT_MS)
        except Exception as e:  # noqa: BLE001  (an overlay took the click, the element went)
            if _closed_error(e):
                raise
            return None, f"failed: {type(e).__name__}", int((time.monotonic() - start) * 1000)
        changed_at = None
        while True:
            now = time.monotonic()
            if popups:
                signal = "popup"
                break
            if str(page.url) != url0:
                signal = "navigation"
                break
            if changed_at is None and _snapshot_or_none(page) != before:
                changed_at = now
            if changed_at is not None and not new_tab and now - changed_at >= apply_limits.POPUP_GRACE_S:
                signal = "dom"
                break
            if now - start >= window_s:
                signal = "dom" if changed_at is not None else "none"
                break
            page.wait_for_timeout(apply_limits.ENTRY_POLL_MS)
        return (popups[0] if popups else None), signal, int((time.monotonic() - start) * 1000)
    finally:
        try:
            page.remove_listener("popup", _on_popup)
        except Exception:   # noqa: BLE001
            pass


def await_destination(page, log: logging.Logger | None = None,
                      job_id: str = "") -> tuple[Any, dict[str, Any]]:
    """Settle `page` after an Apply click. On LinkedIn's `/safety/go/` hop,
    whose script sends the tab to the company's site a few seconds after it
    boots, wait for the tab to leave it and settle again; the job-search
    safety interstitial, which waits for a click instead, gets its visible
    "Continue" clicked (a tab that opens is the destination). A hop that
    never moves on stays on LinkedIn and admits nothing. Returns (the page
    the destination is on, {"settled_ms", "continue"})."""
    logger = log or logging.getLogger("apply_run")
    first = apply_fill.settle(page, apply_limits.CLICK_TIMEOUT_S)
    info: dict[str, Any] = {"settled_ms": _settled_ms(first), "capped": _settle_capped(first),
                            "continue": ""}
    if not _on_linkedin_redirector(page.url):
        return page, _past_trackers(page, info, logger, job_id)
    cont = apply_linkedin.continue_control(page)
    if cont is not None:
        info["continue"] = cont.label
        logger.info("job %s: the LinkedIn interstitial waits for %r; clicking it", job_id,
                    cont.label)
        popup, signal, _ = click_entry(page, page.main_frame.locator(cont.css))
        info["continue_signal"] = signal
        if popup is not None:
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=apply_limits.CLICK_TIMEOUT_S * 1000)
            except Exception:   # noqa: BLE001
                pass
            return popup, info
    if _on_linkedin_redirector(page.url):
        try:
            page.wait_for_url(lambda u: not _on_linkedin_redirector(u),
                              timeout=apply_limits.REDIRECT_TIMEOUT_S * 1000)
        except Exception as e:      # noqa: BLE001  (the loop reads whatever the tab shows)
            logger.info("job %s: the LinkedIn redirect did not move on (%s)", job_id,
                        type(e).__name__)
            if _dropped_load(e):
                # the hop's load of the company's site was dropped: the tab is
                # left once Chrome's error page is up, for its retry
                _error_page_up(page, apply_limits.GOTO_ERROR_PAGE_S)
            return page, info
    again = apply_fill.settle(page, apply_limits.CLICK_TIMEOUT_S)
    info["settled_ms"] += _settled_ms(again)
    info["capped"] = info["capped"] or _settle_capped(again)
    return page, _past_trackers(page, info, logger, job_id)


def _past_trackers(page, info: dict[str, Any], logger: logging.Logger,
                   job_id: str) -> dict[str, Any]:
    """Wait out an ad tracker's or a link shortener's hop (Appcast,
    Joveo, `grnh.se`, `bit.ly` send the tab on by script, a few seconds
    later): up to `TRACKER_HOPS_MAX` hops of `REDIRECT_TIMEOUT_S` each, a
    settle after each. The hops land in `info["trackers"]`; a hop that never
    moves on leaves the page on it."""
    hops: list[str] = []
    while _tracker(page.url) and len(hops) < TRACKER_HOPS_MAX:
        hops.append(_host(page.url))
        try:
            page.wait_for_url(lambda u: not _tracker(u), timeout=apply_limits.REDIRECT_TIMEOUT_S * 1000)
        except Exception as e:      # noqa: BLE001  (the loop parks on a hop that stays)
            logger.info("job %s: the tracker hop %s did not move on (%s)", job_id, hops[-1],
                        type(e).__name__)
            if _dropped_load(e):
                _error_page_up(page, apply_limits.GOTO_ERROR_PAGE_S)     # for its retry
            break
        again = apply_fill.settle(page, apply_limits.CLICK_TIMEOUT_S)
        info["settled_ms"] = info.get("settled_ms", 0) + _settled_ms(again)
    if hops:
        info["trackers"] = hops
    return info
