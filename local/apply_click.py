"""The guarded click and the settle wait, out of `apply_fill`.

`click(page, digest, n)` clicks a digest button and waits for a
navigation or a DOM change (body length and the set of visible controls,
polled every 250 ms), capped; a click a banner, a chat window or a sticky
bar took is made once more after `clear_overlay` put the cover away (a
consent banner's reject, else a close). `settle(page)` waits for a page to
load and hold still; `act_and_settle` and `wait_for_change` wait on an
action the caller makes. `watch_requests` and `background_sends` tell the
page's own requests from the ones a click set going.

It logs as `apply_fill` (one of `apply_trace.LOGGERS`). `apply_fill`
re-exports `click`, `settle`, `act_and_settle`, `ClickResult`,
`checked_live`, `wait_for_change` and `clear_overlay`; a test that shortens
the timings patches the constants here. Playwright is reached only through
the `page` argument, so this module imports without it.
"""
from __future__ import annotations

import logging
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable

import apply_form
import apply_form_js
import apply_send_words

log = logging.getLogger("apply_fill")

ACTION_TIMEOUT_MS = 5_000      # one Playwright action (fill, click, select_option)
SETTLE_QUIET_S = 2.0           # _settle returns once nothing has moved for this long
SETTLE_MAX_S = 8.0             # _settle gives up on a page that keeps moving after this
NETWORK_IDLE_MS = 3_000        # best-effort wait for the network to go quiet
POLL_S = 0.25                  # the click wait's DOM poll

_SNAPSHOT_JS = """() => {
  const visible = __VISIBLE__;
  const ids = Array.from(document.querySelectorAll('input, select, textarea, [role=combobox], button'))
    .filter(visible).map(e => e.id || e.getAttribute('name') || e.tagName).join('|');
  return [document.body ? document.body.outerHTML.length : 0, ids];
}""".replace("__VISIBLE__", apply_form_js.VISIBLE_FN_JS)

# The settle's view of a frame: the snapshot, the visible text's length, and
# whether a loading placeholder shows in the viewport (an `aria-busy=true`
# region, a skeleton or shimmer block). A placeholder below the fold, which
# only loads on scroll, does not count.
_READY_JS = """() => {
  const visible = __VISIBLE__;
  const ids = Array.from(document.querySelectorAll('input, select, textarea, [role=combobox], button'))
    .filter(visible).map(e => e.id || e.getAttribute('name') || e.tagName).join('|');
  const body = document.body;
  const vh = window.innerHeight || 0;
  let busy = 0;
  const sel = '[aria-busy=true], [class*=skeleton i], [class*=shimmer i], '
    + '[class*=placeholder-glow], [class*=placeholder-wave]';
  for (const el of document.querySelectorAll(sel)) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.bottom <= 0 || r.top >= vh) continue;
    busy = 1;
    break;
  }
  return [body ? body.outerHTML.length : 0, ids, body ? (body.innerText || '').length : 0, busy];
}""".replace("__VISIBLE__", apply_form_js.VISIBLE_FN_JS)


def _snapshot(page) -> tuple:
    out = []
    for frame in apply_form.frames(page):
        try:
            out.append(tuple(frame.evaluate(_SNAPSHOT_JS)))
        except Exception:       # noqa: BLE001  (a frame mid-navigation)
            out.append(("gone",))
    return tuple(out)


def _load(page, state: str, timeout_ms: int) -> None:
    try:
        page.wait_for_load_state(state, timeout=max(1, timeout_ms))
    except Exception:       # noqa: BLE001  (a page with no navigation, or one still busy)
        pass


def ready_snapshot(page) -> tuple[tuple, bool]:
    """(every frame's snapshot with its text length, a loading placeholder
    shows in some frame's viewport)."""
    out, busy = [], False
    for frame in apply_form.frames(page):
        try:
            outer, ids, text, placeholder = frame.evaluate(_READY_JS)
        except Exception:       # noqa: BLE001  (a frame mid-navigation)
            out.append(("gone",))
            continue
        out.append((outer, ids, text))
        busy = busy or bool(placeholder)
    return tuple(out), busy


def _settle(page, timeout_s: float, *, navigated: list | None = None) -> dict[str, Any]:
    """After a change: wait for `domcontentloaded` and a best-effort
    `networkidle`, then return once the page has been quiet for
    SETTLE_QUIET_S: no snapshot change (the body's length, its visible
    controls, its visible text's length), no torn-down frame, no navigation,
    and no loading placeholder (`aria-busy`, a skeleton) in the viewport. A
    navigation that lands inside the window (a spinner shown on click, the
    redirect a second later) re-runs the load waits and restarts the quiet
    count, so the caller's next `extract` reads the destination page. A page
    that keeps moving, or keeps a placeholder up, is released after
    SETTLE_MAX_S. Returns how long it took (`ms`), whether the cap released
    it (`capped`) and whether a placeholder showed (`busy`)."""
    start = time.monotonic()
    nav = navigated if navigated is not None else []
    seen = len(nav)
    url_seen = page.url
    _load(page, "domcontentloaded", int(timeout_s * 1000))
    _load(page, "networkidle", NETWORK_IDLE_MS)
    last = None
    busy_seen = False
    quiet_since = time.monotonic()
    hard_stop = quiet_since + SETTLE_MAX_S
    while True:
        if len(nav) > seen or page.url != url_seen:
            seen, url_seen = len(nav), page.url
            _load(page, "domcontentloaded", int(timeout_s * 1000))
            _load(page, "networkidle", NETWORK_IDLE_MS)
            last = None
            quiet_since = time.monotonic()
        snap, busy = ready_snapshot(page)
        busy_seen = busy_seen or busy
        if snap != last or ("gone",) in snap or busy:
            last = snap
            quiet_since = time.monotonic()
        now = time.monotonic()
        quiet = now - quiet_since >= SETTLE_QUIET_S
        if quiet or now >= hard_stop:
            return {"ms": int((now - start) * 1000), "capped": not quiet, "busy": busy_seen}
        page.wait_for_timeout(100)


def settle(page, timeout_s: float = 20) -> dict[str, Any]:
    """Wait for `page` to load and hold still (see `_settle`) after a
    navigation or a click the caller made itself (the runner's first `goto`,
    a posting's Apply, a popup it follows). Returns `_settle`'s timing."""
    return _settle(page, timeout_s)


@dataclass(frozen=True)
class ClickResult:
    """`clicked`: the click itself landed (the element was found and the
    click was dispatched to it; a click that dispatched and then timed out
    waiting for the navigation it started counts). `changed`: a
    navigation or a DOM change followed. A landed click on a quiet page is
    `(True, False)`; one that never landed is `(False, False)`. `refused`:
    the caller's live check (`click`'s `check`) stopped the click, and why;
    nothing was clicked. `late`: the error a dispatched click raised
    afterwards. `overlay`: what covered the control and how it was put away
    before the click was made once more. `sent`: what the click set
    going, from the click to the end of its wait ("METHOD url", no query): a
    navigation of the page or of the button's frame, or a POST, PUT or PATCH
    from either that the page was not already sending by itself.
    Truthiness is `changed`."""
    clicked: bool
    changed: bool
    refused: str = ""
    late: str = ""
    overlay: str = ""
    sent: tuple = ()

    def __bool__(self) -> bool:
        return self.changed


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


# What covers a control a click was refused on: the element at the
# control's centre, when it is neither the control nor inside it, climbed to
# its overlay (the outermost fixed or sticky box, a dialog), which is marked
# `data-apply-overlay`. Then the one control to put it away: in a cookie or
# consent banner (`apply_form.CONSENT_ROOTS_JS`) its reject, decline,
# disagree or necessary-only control (read before the words that accept,
# which "Disagree and close" and "Allow necessary only" hold), else its
# close, looked for in the banner's own root too when the climbed overlay
# holds none of its controls; anywhere else a close, dismiss, minimise, "no
# thanks" or bare "x" control of the overlay (never one that accepts,
# allows, agrees, or holds a send or last-step word of `_SEND_JS`); marked
# `data-apply-close`. A box of the application itself is no cover: nothing
# is picked and `own` says so. A fixed
# bar is the application's by where it sits (the covered control's form or
# the box around the control and its fields), a
# dialog by that or by what it holds (two or more fields, or a control
# that applies, uploads or submits). Returns {what, kind:
# consent|close|none, text, own} or null when nothing covers it.
# The loop's send and last-step words (`apply_send_words.SUBMIT_WORDS` and
# `FINAL_WORDS`) as a JS regex source for a string literal: the overlay
# picker and the click's arm (`_ARM_JS`) splice it.
_SEND_JS = apply_send_words.js_union()
_OVERLAY_JS = r"""el => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (!hit || hit === el || el.contains(hit)) return null;
  let root = hit;
  for (let n = hit; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
    const pos = getComputedStyle(n).position;
    if (pos === 'fixed' || pos === 'sticky'
        || n.matches('dialog, [role=dialog], [role=alertdialog], [aria-modal=true]')) root = n;
  }
  if (root.contains(el)) return null;
  document.querySelectorAll('[data-apply-overlay], [data-apply-close]').forEach((n) => {
    n.removeAttribute('data-apply-overlay'); n.removeAttribute('data-apply-close'); });
  root.setAttribute('data-apply-overlay', '1');
  const mine = (__CONSENT__)().filter((c) => c === root || c.contains(root) || root.contains(c));
  const consent = mine.length > 0;
  const REJECT = /^(reject|decline|refuse|deny|disagree)\b|\b(necessary|essential) only\b|\bonly (strictly )?(necessary|essential)\b|\b(necessary|essential) cookies only\b|\bwithout (agreeing|accepting|consent(ing)?)\b/i;
  const CLOSE = /^(close|dismiss|hide|minimi[sz]e|no,? thanks|not now|maybe later|skip|x)\b|^[\u00d7\u2715\u2716](\s|$)/i;
  const CLOSE_ARIA = /\b(close|dismiss|hide|minimi[sz]e)\b/i;
  const NEVER = /accept|allow|agree|submit|apply|send|sign ?up|subscribe|start chat|chat now/i;
  const SEND = new RegExp('__SEND__', 'i');
  const APP = /\b(apply|application|autofill|resume|cv|upload|submit)\b/i;
  const CTRLS = 'button, [role=button], input[type=button], input[type=submit], a:not([href]), a[href="#"]';
  const shown = (n) => { const b = n.getBoundingClientRect(); const st = getComputedStyle(n);
    return b.width > 0 && b.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const words = (c) => [norm(c.innerText) || norm(c.value),
    norm(c.getAttribute('aria-label')) || norm(c.getAttribute('title'))];
  const name = norm((root.id ? '#' + root.id + ' ' : '') + (root.getAttribute('aria-label') || '')
    + ' ' + (root.innerText || '').slice(0, 60)).slice(0, 80);
  const ctrls = Array.from(root.querySelectorAll(CTRLS)).filter((c) => shown(c) && !c.disabled);
  if (!consent) {
    // the application's own box, never put away. Any
    // fixed or sticky box is when it is part of the application: in the
    // form of the control it covers, holding a control that form owns (a
    // footer's `form=` submit), or inside the box that holds the control
    // and the application's fields. A dialog is also when it holds fields
    // or an apply, upload or submit control (Workday's "Start Your
    // Application"). A chat's pre-chat form, a talent-network or a
    // job-alert slide-in beside the application is put away, whatever
    // fields it holds
    const FIELDS = 'input:not([type=hidden]):not([type=button]):not([type=submit])'
      + ':not([type=checkbox]):not([type=radio]), select, textarea';
    const form = el.form || el.closest('form');
    let box = null;
    for (let n = el.parentElement; !box && n && n !== document.body
         && n !== document.documentElement; n = n.parentElement) {
      if (Array.from(n.querySelectorAll(FIELDS)).some((f) => shown(f) && !root.contains(f))) box = n;
    }
    const part = (!!form && (form.contains(root) || Array.from(root.querySelectorAll(
      FIELDS + ', ' + CTRLS)).some((c) => c.form === form))) || (!!box && box.contains(root));
    const dialog = root.matches('dialog, [role=dialog], [role=alertdialog], [aria-modal=true]');
    const fields = Array.from(root.querySelectorAll(FIELDS)).filter(shown);
    if (part || (dialog && (fields.length >= 2 || ctrls.some((c) => APP.test(words(c).join(' '))))))
      return {what: name, kind: 'none', text: '', own: true};
  }
  let pick = null, kind = 'none';
  const choose = (list) => {
    for (const c of list) {
      const [text, aria] = words(c);
      const label = text || aria;
      if (!label) continue;
      // a banner's reject before the words that accept: "Disagree and
      // close", "Continue without agreeing", "Allow necessary only"
      if (consent && REJECT.test(label)) { pick = c; kind = 'consent'; return; }
      if (NEVER.test(label) || NEVER.test(aria) || SEND.test(label) || SEND.test(aria)) continue;
      if (!pick && (CLOSE.test(label) || CLOSE_ARIA.test(aria))) { pick = c; kind = consent ? 'consent' : 'close'; }
    }
  };
  choose(ctrls);
  if (!pick && consent) {
    // the climbed overlay holds none of the banner's controls (JazzHR): the
    // banner's own root does
    for (const c of mine) {
      choose(Array.from(c.querySelectorAll(CTRLS)).filter((x) => shown(x) && !x.disabled
        && !ctrls.includes(x)));
      if (pick) break;
    }
  }
  if (pick) pick.setAttribute('data-apply-close', '1');
  return {what: name, kind: kind, text: pick ? (norm(pick.innerText) || norm(pick.value)
    || norm(pick.getAttribute('aria-label'))).slice(0, 60) : '', own: false};
}""".replace("__CONSENT__", apply_form.CONSENT_ROOTS_JS).replace("__SEND__", _SEND_JS)
_INTERCEPTED = ("intercepts pointer events", "is not visible", "outside of the viewport")


def clear_overlay(frame, target) -> dict:
    """put away what covers `target` (an element handle or a
    locator in `frame`): `_OVERLAY_JS` finds it and the control to click
    (a consent banner's reject, else a close); that control is clicked, and
    `target` is scrolled to the viewport's centre either way. Returns what
    was found ({} when nothing covers it)."""
    found = target.evaluate(_OVERLAY_JS)
    if not found:
        return {}
    if found.get("kind") != "none":
        try:
            frame.locator("[data-apply-close='1']").first.click(timeout=ACTION_TIMEOUT_MS)
            frame.wait_for_timeout(300)
        except Exception as e:      # noqa: BLE001  (the overlay went away on its own)
            found["error"] = type(e).__name__
    return dict(found)


# A capture listener on the element's window notes that a click reached the
# element (or a child of it), so a click whose navigation wait timed out is
# known to have been dispatched. With `want` (the text the live check read),
# a click that finds the element's text changed into a send or a last step
# (`_SEND_JS` words the checked text did not have) is cancelled there, at
# its dispatch, before any handler of the page sees it (the check
# and the click are one step). The listener is removed once the run's click
# is over (`_DISARM_JS`): a later click of the person's is never touched.
_ARM_JS = """(el, want) => {
  const w = el.ownerDocument.defaultView;
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const textOf = (x) => norm(x.innerText) || norm(x.value) || norm(x.getAttribute('aria-label'))
    || norm(x.getAttribute('title'));
  const SEND = new RegExp('""" + _SEND_JS + """', 'i');
  if (w.__applyClickMark) w.removeEventListener('click', w.__applyClickMark, true);
  w.__applyClickSeen = false;
  w.__applyClickBlocked = '';
  w.__applyClickMark = (e) => {
    // a click inside a shadow root reaches the window retargeted to its
    // host: the event's composed path still holds the element
    const path = e.composedPath ? e.composedPath() : [];
    if (!(path.includes(el) || e.target === el || el.contains(e.target))) return;
    const now = textOf(el);
    if (want !== null && now !== want && SEND.test(now) && !SEND.test(want)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      w.__applyClickBlocked = now.slice(0, 80);
      return;
    }
    w.__applyClickSeen = true;
  };
  w.addEventListener('click', w.__applyClickMark, true);
  return true;
}"""
_SEEN_JS = "() => window.__applyClickSeen === true"
_BLOCKED_JS = "() => window.__applyClickBlocked || ''"
_UNSEEN_JS = "() => { window.__applyClickSeen = false; window.__applyClickBlocked = ''; }"
_DISARM_JS = ("() => { if (window.__applyClickMark) { window.removeEventListener('click', "
              "window.__applyClickMark, true); window.__applyClickMark = null; } }")
_DISPATCH_METHODS = ("POST", "PUT", "PATCH")
# The page's own requests, logged from the moment the run starts watching it
# (`watch_requests`): the POST, PUT or PATCH pairs a page sends by itself (its
# telemetry, an autosave, a keep-alive) are no evidence a click set anything
# going
_REQUEST_LOG: "weakref.WeakKeyDictionary[Any, list]" = weakref.WeakKeyDictionary()
_REQUEST_LOG_CAP = 400
BACKGROUND_S = 120.0            # how far back the page's own requests are looked for


def watch_requests(page) -> None:
    """Start logging `page`'s POST, PUT and PATCH requests (once per page):
    (when, method, URL without its query). `click` reads the log to leave
    out what the page was already sending before the click."""
    try:
        if page in _REQUEST_LOG:
            return
        log_: list = []
        _REQUEST_LOG[page] = log_
    except TypeError:       # a page double that takes no weak reference
        return

    def _on(request) -> None:
        try:
            method = str(request.method).upper()
            if method in _DISPATCH_METHODS:
                log_.append((time.monotonic(), method, str(request.url).split("?")[0]))
                del log_[:-_REQUEST_LOG_CAP]
        except Exception:       # noqa: BLE001
            pass
    try:
        page.on("request", _on)
    except Exception:       # noqa: BLE001  (a page double)
        pass


def background_sends(page, *, before: float | None = None) -> set[tuple[str, str]]:
    """The (method, URL) pairs `page` sent by itself in the `BACKGROUND_S`
    before `before` (now by default)."""
    end = time.monotonic() if before is None else before
    try:
        rows = list(_REQUEST_LOG.get(page) or [])
    except TypeError:
        return set()
    return {(m, u) for t, m, u in rows if end - BACKGROUND_S <= t <= end}


class _ClickStopped(Exception):
    """The armed listener stopped a click whose text turned into a send."""


def _dispatched(frame, url0: str, page, requests: list, error: BaseException) -> bool:
    """Did a click that raised reach its element first? Its mark in the
    element's window, a request the page made since, a new URL, or
    Playwright's own words about the navigation it was waiting for."""
    try:
        if frame is not None and frame.evaluate(_SEEN_JS):
            return True
    except Exception:       # noqa: BLE001  (the document is mid-navigation, or gone)
        pass
    if requests:
        return True
    try:
        if str(page.url) != url0:
            return True
    except Exception:       # noqa: BLE001
        pass
    text = str(error).lower()
    return "navigation" in text or "navigating" in text


UNREAD_LIVE = "its live text could not be read, so it was never checked"


def checked_live(check: Callable[[str, dict], str], expected: str, live: dict) -> str:
    """`check(expected, live)` on a live read (`apply_form.live_text`): "" to
    go on, else the reason to refuse. A live read that came back empty (the
    node detached, the frame gone, the evaluate failed) is refused: a check
    that never ran never lets a click through."""
    if not live:
        return UNREAD_LIVE
    return check(expected, live)


def click(page, digest: apply_form.FormDigest, n: int, *, timeout_s: float = 20,
          check: Callable[[str, dict], str] | None = None, guard: bool = True) -> ClickResult:
    """Click button `n` of `digest` and wait, up to `timeout_s`, for a
    navigation or a DOM change; the `ClickResult` says whether the click
    landed and whether anything changed (settled through `_settle` when it
    did).

    `check(expected_text, live)` reads the live element just before the
    click (`apply_form.live_text`: its text, aria-label, type) and returns
    "" to go on or the reason to refuse; a live text that cannot be read is
    refused too (`checked_live`), here and on every read again below. A
    control whose live text differs from the digest's is found again by that text in its frame
    (`apply_form.find_by_text`) and checked again; when no single control
    reads it, the click is refused. The checked element itself is clicked
    (an element handle), and with `guard` a change of its text into a send
    between the check and the click cancels the click at its dispatch
    (`_ARM_JS`): refused as well. A click that raised after it was
    dispatched (a form post whose navigation outlived the action timeout)
    has landed: `late` carries the error."""
    button = next((b for b in digest.buttons if b.n == n), None)
    if button is None:
        log.info("apply_fill: no button %s in the digest", n)
        return ClickResult(clicked=False, changed=False)
    try:
        loc = apply_form.resolve(page, button.locator)
        if loc.count() == 0:
            log.info("apply_fill: button %s (%s) is gone", n, button.locator[1])
            return ClickResult(clicked=False, changed=False)
    except Exception as e:      # noqa: BLE001  (a torn-down frame)
        log.info("apply_fill: button %s (%s) unreachable: %s", n, button.locator[1],
                 type(e).__name__)
        return ClickResult(clicked=False, changed=False)
    want = None
    if check is not None:
        live = apply_form.live_text(loc)
        why = checked_live(check, button.text, live)
        if live:
            want = _norm(live.get("text"))
            if not why and _norm(live.get("text")) != _norm(button.text):
                found = apply_form.find_by_text(page, button.locator[0], button.text)
                if len(found) == 1:
                    loc = apply_form.resolve(page, (button.locator[0], found[0]))
                    live = apply_form.live_text(loc)
                    why = checked_live(check, button.text, live)
                    want = _norm(live.get("text")) if live else None
                    log.info("apply_fill: button %s (%r) moved; found again at %s", n,
                             button.text, found[0])
                else:
                    why = (f"the control there now reads {_norm(live.get('text'))[:60]!r}, and "
                           f"{len(found)} controls read {_norm(button.text)[:60]!r}")
        if why:
            log.info("apply_fill: click on %r refused: %s", button.text, why)
            return ClickResult(clicked=False, changed=False, refused=why)
    landed: list[bool] = []
    late: list[str] = []
    try:
        frame = apply_form.frames(page)[int(button.locator[0])]
    except Exception:       # noqa: BLE001  (a page double, a frame gone: the click finds out)
        frame = None
    requests: list[str] = []
    blocked: list[str] = []
    overlays: list[dict] = []
    # every navigation, POST, PUT or PATCH from the click to the end of its
    # wait ("METHOD url"): what the click set going
    sent: list[str] = []

    def _on_request(request) -> None:
        # a navigation or a send proves the dispatch; a GET for an image does not
        try:
            method = str(request.method).upper()
            if method in _DISPATCH_METHODS or request.is_navigation_request():
                requests.append(method)
        except Exception:       # noqa: BLE001
            pass

    def _on_sent(request) -> None:
        # what the click set going: a navigation of the
        # page or of the button's frame, or a POST, PUT or PATCH from either
        # that the page was not already sending by itself (`background`); a
        # beacon (`ping`) is never one
        try:
            method = str(request.method).upper()
            url = str(request.url).split("?")[0]
            mine = request.frame in (main, frame) if main is not None else True
            if request.is_navigation_request():
                if mine:
                    sent.append(f"{method} {url}")
            elif method in _DISPATCH_METHODS and mine and request.resource_type != "ping" \
                    and (method, url) not in background:
                sent.append(f"{method} {url}")
        except Exception:       # noqa: BLE001
            pass

    handles: list = []

    def _arm():
        """Pin the element to click (an element handle, disposed after the
        click) and arm its window's one-shot listener."""
        target = loc.first
        try:
            if frame is not None:
                frame.evaluate(_UNSEEN_JS)      # an earlier click's mark never counts
            handle = loc.first.element_handle(timeout=ACTION_TIMEOUT_MS)
            if handle is not None:
                handles.append(handle)
                target = handle
            target.evaluate(_ARM_JS, want if guard else None)
        except Exception:       # noqa: BLE001  (the click finds out)
            pass
        return target

    def _refind() -> None:
        """The node was re-rendered between the check and the click: find it
        again once, by its locator, else by its text in its frame, and check
        it again; a control that now reads otherwise is refused."""
        nonlocal loc
        live2 = apply_form.live_text(loc)
        if not live2 or (want is not None and _norm(live2.get("text")) != want):
            found = apply_form.find_by_text(page, button.locator[0], button.text)
            if len(found) != 1:
                blocked.append(f"it was re-rendered and {len(found)} controls read "
                               f"{_norm(button.text)[:60]!r}")
                raise _ClickStopped(blocked[-1])
            loc = apply_form.resolve(page, (button.locator[0], found[0]))
            live2 = apply_form.live_text(loc)
        why = checked_live(check, button.text, live2) if check is not None else ""
        if why:
            blocked.append(why)
            raise _ClickStopped(why)

    def _do_click() -> None:
        url0 = str(page.url)
        page.on("request", _on_request)
        try:
            target = _arm()
            for attempt in (1, 2):
                try:
                    target.click(timeout=ACTION_TIMEOUT_MS)
                    break
                except Exception as e:      # noqa: BLE001  (a node re-rendered under the click)
                    text = str(e).lower()
                    if attempt == 1 and ("not attached" in text or "detached" in text) \
                            and not _dispatched(frame, url0, page, requests, e):
                        log.info("apply_fill: button %s (%r) was re-rendered; found again", n,
                                 button.text)
                        _refind()
                        target = _arm()
                        continue
                    if attempt == 1 and frame is not None \
                            and any(w in text for w in _INTERCEPTED) \
                            and not _dispatched(frame, url0, page, requests, e):
                        # a banner, a chat window or a sticky bar took
                        # the click; the click never reached the button, so
                        # it is made once more once the cover is put away
                        found = clear_overlay(frame, target)
                        if found:
                            overlays.append(found)
                            log.info("apply_fill: %r was covered by %r; %s %r", button.text,
                                     found.get("what"), found.get("kind"), found.get("text"))
                            continue
                    raise
            if frame is not None:
                try:
                    stopped = frame.evaluate(_BLOCKED_JS)
                except Exception:       # noqa: BLE001  (the page moved on: nothing was stopped)
                    stopped = ""
                if stopped:
                    blocked.append(f"its text turned into {str(stopped)!r} at the click; the "
                                   "click was stopped")
                    raise _ClickStopped(str(stopped))
        except _ClickStopped:
            raise
        except Exception as e:      # noqa: BLE001  (Playwright's TimeoutError, among others)
            if _dispatched(frame, url0, page, requests, e):
                late.append(type(e).__name__)
                landed.append(True)
                log.info("apply_fill: the click on %r was dispatched, then %s", button.text,
                         type(e).__name__)
                return
            raise
        finally:
            try:
                page.remove_listener("request", _on_request)
            except Exception:       # noqa: BLE001  (a page double)
                pass
            # the guard is one-shot: the person's own click on the same node
            # later (a footer that reads "Submit" on the review step) goes through
            if frame is not None:
                try:
                    frame.evaluate(_DISARM_JS)
                except Exception:       # noqa: BLE001  (the page moved on: its listener went too)
                    pass
            for handle in handles:
                try:
                    handle.dispose()
                except Exception:       # noqa: BLE001  (already gone)
                    pass
            handles.clear()
        landed.append(True)

    def _cover() -> str:
        if not overlays:
            return ""
        o = overlays[0]
        return (f"{o.get('what') or 'an overlay'}: {o.get('kind')}"
                + (f" {o.get('text')!r}" if o.get("text") else ""))[:160]
    watch_requests(page)
    background = background_sends(page)
    try:
        main = page.main_frame
    except Exception:       # noqa: BLE001  (a page double)
        main = None
    try:
        page.on("request", _on_sent)
    except Exception:       # noqa: BLE001  (a page double)
        pass
    try:
        changed = _await_change(page, _do_click, timeout_s)
    except Exception as e:      # noqa: BLE001
        if blocked:
            log.info("apply_fill: click on %r refused: %s", button.text, blocked[0])
            return ClickResult(clicked=False, changed=False, refused=blocked[0], overlay=_cover())
        log.info("apply_fill: click on %r failed: %s", button.text, type(e).__name__)
        return ClickResult(clicked=bool(landed), changed=False, late=late[0] if late else "",
                           overlay=_cover(), sent=tuple(sent))
    finally:
        try:
            page.remove_listener("request", _on_sent)
        except Exception:       # noqa: BLE001  (a page double)
            pass
    return ClickResult(clicked=True, changed=changed, late=late[0] if late else "",
                       overlay=_cover(), sent=tuple(sent))


def wait_for_change(page, *, timeout_s: float = 20) -> bool:
    """`click`'s wait without the click: up to `timeout_s` for a
    navigation or a DOM change from now, then `_settle`. True when something
    changed. For a click the caller already made whose effect may still be
    on its way (a submit that answered quietly)."""
    try:
        return _await_change(page, lambda: None, timeout_s)
    except Exception as e:      # noqa: BLE001
        log.info("apply_fill: wait_for_change failed: %s", type(e).__name__)
        return False


def act_and_settle(page, act: Callable[[], Any], *, timeout_s: float = 20) -> dict[str, Any]:
    """`act()` (a click the caller makes itself, the consent banner's), then
    `_await_change`'s wait: up to `timeout_s` for a navigation or a DOM
    change, then the settle, so a navigation the act started has landed
    before the caller reads the page. Returns the settle's timing plus
    `changed`."""
    info: dict[str, Any] = {}
    changed = _await_change(page, act, timeout_s, info_out=info)
    return {"ms": 0, "capped": False, "busy": False, **info, "changed": changed}


def _await_change(page, act: Callable[[], Any], timeout_s: float, *,
                  info_out: dict | None = None) -> bool:
    """Snapshot, `act()`, then poll for a navigation or a DOM change until
    `timeout_s`; settle and return True on a change, False on a quiet page.
    `info_out` receives the settle's timing."""
    before = _snapshot(page)
    url0 = page.url
    navigated: list[str] = []

    def _on_nav(frame) -> None:
        if frame == page.main_frame:
            navigated.append(frame.url)

    page.on("framenavigated", _on_nav)
    try:
        # the listener stays on through _settle so a late navigation restarts the quiet count
        act()
        deadline = time.monotonic() + timeout_s
        while True:
            if navigated or page.url != url0 or _snapshot(page) != before:
                info = _settle(page, timeout_s, navigated=navigated)
                if info_out is not None:
                    info_out.update(info)
                return True
            if time.monotonic() >= deadline:
                return False
            page.wait_for_timeout(int(POLL_S * 1000))
    finally:
        page.remove_listener("framenavigated", _on_nav)
