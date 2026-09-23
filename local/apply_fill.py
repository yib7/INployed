"""Acting on a `FillPlan`: fill, select, check, upload, click, and read back.

`apply(page, plan)` walks the plan's fields and performs each `action` on the
control its `locator` names (`apply_form.resolve`), then re-reads the DOM and
returns one `Filled(n, label, value)` per acted field so the judge's
verification request sees what the page holds, whatever the fill call
reported. A failed action is logged and the loop continues; the read-back
(empty) reveals it. `skip` and `generate` are left alone (the caller resolves
`generate` before calling `apply`).

The control's kind decides the mechanics, probed from the DOM so the plan
needs no type: text-like inputs and textareas `fill` (a native date control
gets ISO `YYYY-MM-DD`, converted from the common US shapes); a `select` picks
by option label, case-insensitively; a radio group checks the radio whose
label equals the option; a checkbox checks on `"checked"`; a combobox or
listbox opens and clicks the option text; a file input takes
`set_input_files` through its frame, the same call `apply_driver._set_files`
proved on Greenhouse. A file control gets no click.

`click_button(page, digest, n)` clicks a digest button and waits for a
navigation or a DOM change (body length and the set of visible controls,
polled every 250 ms), capped. `open_listbox_options(page, field)` reads a
React-select style menu on demand. `page_text(page)` is the capped visible
text for the record writer. Playwright is reached only through the `page`
argument, so this module imports without it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable

import apply_form
from apply_judge import PAGE_TEXT_CAP, FillPlan, PlannedField

log = logging.getLogger(__name__)

ACTION_TIMEOUT_MS = 5_000      # one Playwright action (fill, click, select_option)
SETTLE_QUIET_S = 2.0           # _settle returns once nothing has moved for this long
SETTLE_MAX_S = 8.0             # _settle gives up on a page that keeps moving after this
NETWORK_IDLE_MS = 3_000        # best-effort wait for the network to go quiet
LISTBOX_WAIT_MS = 2_000        # for a combobox menu to render its options
POLL_S = 0.25                  # click_button's DOM poll
CHECKED_WORDS = ("checked", "yes", "true", "on", "1")
UNCHECKED_WORDS = ("unchecked", "no", "false", "off", "0")
_DATE_SHAPES = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y",
                "%b %d, %Y", "%d %b %Y", "%m-%d-%Y")


@dataclass
class Filled:
    n: int
    label: str
    value: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- DOM probes -----------------------------------------------------------------

_KIND_JS = """el => ({
  tag: el.tagName, type: (el.getAttribute('type') || '').toLowerCase(),
  role: el.getAttribute('role') || '',
})"""

_READ_JS = """el => {
  const tag = el.tagName, type = (el.getAttribute('type') || '').toLowerCase();
  const role = el.getAttribute('role') || '';
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  if (tag === 'SELECT') { const o = el.selectedOptions[0]; return o ? norm(o.text) : ''; }
  if (type === 'checkbox') return el.checked ? 'checked' : '';
  if (type === 'file') return el.files && el.files.length ? el.files[0].name : '';
  if (role === 'combobox' || role === 'listbox') {
    const inp = tag === 'INPUT' ? el : el.querySelector('input');
    if (inp && inp.value) return norm(inp.value);
    const dv = el.getAttribute('data-value');
    if (dv) return norm(dv);
    const sel = el.querySelector('[aria-selected=true]');
    if (sel) return norm(sel.textContent);
    return norm(el.innerText);
  }
  if (el.value !== undefined) return el.value;
  return norm(el.textContent);
}"""

_RADIO_LABELS_JS = "els => els.map(" + apply_form.RADIO_OPTION_LABEL_JS + ")"

_CHECKED_INDEX_JS = "els => els.findIndex(el => el.checked)"

_SELECT_OPTIONS_JS = "el => Array.from(el.options).map(o => [o.text.trim(), o.value])"

_SNAPSHOT_JS = """() => {
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const ids = Array.from(document.querySelectorAll('input, select, textarea, [role=combobox], button'))
    .filter(visible).map(e => e.id || e.getAttribute('name') || e.tagName).join('|');
  return [document.body ? document.body.outerHTML.length : 0, ids];
}"""

# The settle's view of a frame: the snapshot, the visible text's length, and
# whether a loading placeholder shows in the viewport (an `aria-busy=true`
# region, a skeleton or shimmer block). A placeholder below the fold, which
# only loads on scroll, does not count.
_READY_JS = """() => {
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
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
}"""


def _say(log_fn: Callable[[str], Any] | None, msg: str) -> None:
    if log_fn is not None:
        log_fn(msg)
    else:
        log.info(msg)


def _kind(loc) -> dict[str, str]:
    return loc.first.evaluate(_KIND_JS, timeout=ACTION_TIMEOUT_MS)


def _date_value(value: str) -> str:
    """ISO `YYYY-MM-DD` for a native date control, from ISO or the common US
    and long shapes; anything unparsed goes through as given."""
    v = (value or "").strip()
    for shape in _DATE_SHAPES:
        try:
            return datetime.strptime(v, shape).date().isoformat()
        except ValueError:
            continue
    return v


def _ci_match(want: str, candidates: list[str]) -> int:
    """Index of the candidate equal to `want` case-insensitively (whitespace
    folded), else the one containing it, else -1."""
    w = " ".join((want or "").split()).lower()
    folded = [" ".join(c.split()).lower() for c in candidates]
    if w in folded:
        return folded.index(w)
    for i, c in enumerate(folded):
        if w and w in c:
            return i
    return -1


# --- the actions ------------------------------------------------------------------

def _fill(loc, kind: dict[str, str], value: str) -> None:
    if kind["tag"] == "INPUT" and kind["type"] == "date":
        value = _date_value(value)
    loc.first.fill(value, timeout=ACTION_TIMEOUT_MS)


def _select_native(loc, want: str) -> None:
    options = loc.first.evaluate(_SELECT_OPTIONS_JS, timeout=ACTION_TIMEOUT_MS)
    labels = [o[0] for o in options]
    i = _ci_match(want, labels)
    if i < 0:
        values = [o[1] for o in options]
        i = _ci_match(want, values)
    if i < 0:
        raise LookupError(f"no option {want!r} among {labels}")
    loc.first.select_option(value=options[i][1], timeout=ACTION_TIMEOUT_MS)


def _check_radio(loc, want: str) -> None:
    labels = loc.evaluate_all(_RADIO_LABELS_JS)
    i = _ci_match(want, labels)
    if i < 0:
        values = loc.evaluate_all("els => els.map(e => e.value)")
        i = _ci_match(want, values)
    if i < 0:
        raise LookupError(f"no radio {want!r} among {labels}")
    loc.nth(i).check(timeout=ACTION_TIMEOUT_MS)


def _check_box(loc, want: str) -> None:
    w = (want or "").strip().lower()
    if w in CHECKED_WORDS:
        loc.first.check(timeout=ACTION_TIMEOUT_MS)
    elif w in UNCHECKED_WORDS:
        loc.first.uncheck(timeout=ACTION_TIMEOUT_MS)
    else:
        raise LookupError(f"checkbox option {want!r} is neither checked nor unchecked")


def _options_locator(frame, loc):
    """The `[role=option]` entries a combobox shows: the listbox its
    `aria-controls` / `aria-owns` names when set, else any visible listbox in
    the frame (React-select portals its menu to the body)."""
    for attr in ("aria-controls", "aria-owns"):
        ref = loc.first.get_attribute(attr, timeout=ACTION_TIMEOUT_MS)
        if ref:
            target = frame.locator(f'[id="{ref.split()[0]}"] [role=option]')
            if target.count():
                return target
    return frame.locator("[role=listbox] [role=option]").filter(visible=True)


def _open_menu(frame, loc):
    """Click the combobox unless its menu is already open; return the visible
    options locator, or None when nothing rendered within LISTBOX_WAIT_MS."""
    expanded = loc.first.get_attribute("aria-expanded", timeout=ACTION_TIMEOUT_MS)
    if expanded != "true":
        loc.first.click(timeout=ACTION_TIMEOUT_MS)
    options = _options_locator(frame, loc)
    try:
        options.first.wait_for(state="visible", timeout=LISTBOX_WAIT_MS)
    except Exception:       # noqa: BLE001  (Playwright's TimeoutError)
        return None
    return options


def _pick_listbox(page, frame, loc, want: str) -> None:
    options = _open_menu(frame, loc)
    if options is None:
        raise LookupError("the listbox showed no options")
    texts = [t.strip() for t in options.all_inner_texts()]
    i = _ci_match(want, texts)
    if i < 0:
        page.keyboard.press("Escape")
        raise LookupError(f"no option {want!r} among {texts}")
    options.nth(i).click(timeout=ACTION_TIMEOUT_MS)


def _upload(page, locator: tuple[int, str], path: str) -> None:
    frame = apply_form.frames(page)[int(locator[0])]
    frame.set_input_files(str(locator[1]), path, timeout=ACTION_TIMEOUT_MS)


def _act(page, pf: PlannedField, loc, kind: dict[str, str]) -> None:
    want = pf.option if pf.option is not None else pf.value
    tag, typ, role = kind["tag"], kind["type"], kind["role"]
    if pf.action == "upload":
        _upload(page, pf.locator, pf.value)
        return
    if pf.action not in ("fill", "select"):
        return
    if tag == "SELECT":
        _select_native(loc, want)
    elif tag == "INPUT" and typ == "radio":
        _check_radio(loc, want)
    elif tag == "INPUT" and typ == "checkbox":
        _check_box(loc, want)
    elif role in ("combobox", "listbox"):
        frame = apply_form.frames(page)[int(pf.locator[0])]
        _pick_listbox(page, frame, loc, want)
    elif tag == "INPUT" and typ == "file":
        raise LookupError("a file input takes an upload action")
    else:
        _fill(loc, kind, pf.value if pf.action == "fill" else want)


def _read_back(loc, kind: dict[str, str] | None) -> str:
    try:
        if loc.count() == 0:
            return ""
        if kind and kind["tag"] == "INPUT" and kind["type"] == "radio":
            i = loc.evaluate_all(_CHECKED_INDEX_JS)
            if i is None or i < 0:
                return ""
            labels = loc.evaluate_all(_RADIO_LABELS_JS)
            return str(labels[i]) if i < len(labels) else ""
        return str(loc.first.evaluate(_READ_JS, timeout=ACTION_TIMEOUT_MS) or "")
    except Exception:       # noqa: BLE001
        return ""


def apply(page, plan: FillPlan, *, log: Callable[[str], Any] | None = None,
          deadline: float | None = None,
          clock: Callable[[], float] = time.monotonic,
          errors: list | None = None) -> list[Filled]:
    """Perform every `fill` / `select` / `upload` in `plan` and return the
    read-back value of each acted field. `skip` and `generate` are not acted
    on and do not appear in the result. `deadline` is an instant on `clock`
    (`time.monotonic` by default; the runner passes its own); once it has
    passed the remaining fields are left alone and the list so far comes back
    (the job's wall clock belongs to the caller). A failed action lands in
    `errors` as `{n, label, action, error}` with the error's type name only
    (a Playwright message can quote the value)."""
    out: list[Filled] = []
    for pf in plan.fields:
        if pf.action not in ("fill", "select", "upload"):
            continue
        if deadline is not None and clock() >= deadline:
            _say(log, f"apply_fill: deadline passed before {pf.label!r}; {len(out)} filled")
            break
        loc = None
        kind = None
        try:
            loc = apply_form.resolve(page, pf.locator)
            if loc.count() == 0:
                raise LookupError(f"no element at {pf.locator}")
            kind = _kind(loc)
            _act(page, pf, loc, kind)
        except Exception as e:      # noqa: BLE001  (the read-back reports the outcome)
            # the type alone: a Playwright message quotes the call, value included
            _say(log, f"apply_fill: {pf.action} on {pf.label!r} ({pf.locator[1]}) failed: "
                      f"{type(e).__name__}")
            if errors is not None:
                errors.append({"n": pf.n, "label": pf.label, "action": pf.action,
                               "error": type(e).__name__})
        value = _read_back(loc, kind) if loc is not None else ""
        out.append(Filled(n=pf.n, label=pf.label, value=value))
    return out


def open_listbox_options(page, field) -> list[str]:
    """Open a combobox / listbox field, read its option texts, close it."""
    loc = apply_form.resolve(page, field.locator)
    frame = apply_form.frames(page)[int(field.locator[0])]
    options = _open_menu(frame, loc)
    texts = [t.strip() for t in options.all_inner_texts()] if options is not None else []
    page.keyboard.press("Escape")
    return [t for t in texts if t]


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


def _ready_snapshot(page) -> tuple[tuple, bool]:
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
        snap, busy = _ready_snapshot(page)
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
    waiting for the navigation it started counts, TERM-02). `changed`: a
    navigation or a DOM change followed. A landed click on a quiet page is
    `(True, False)`; one that never landed is `(False, False)`. `refused`:
    the caller's live check (`click`'s `check`) stopped the click, and why;
    nothing was clicked. `late`: the error a dispatched click raised
    afterwards. Truthiness is `changed`, the shape `click_button` returns."""
    clicked: bool
    changed: bool
    refused: str = ""
    late: str = ""

    def __bool__(self) -> bool:
        return self.changed


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


# A capture listener on the element's window notes that a click reached the
# element (or a child of it), so a click whose navigation wait timed out is
# known to have been dispatched.
_ARM_JS = """el => {
  const w = el.ownerDocument.defaultView;
  if (w.__applyClickMark) w.removeEventListener('click', w.__applyClickMark, true);
  w.__applyClickSeen = false;
  w.__applyClickMark = (e) => { if (e.target === el || el.contains(e.target)) w.__applyClickSeen = true; };
  w.addEventListener('click', w.__applyClickMark, true);
  return true;
}"""
_SEEN_JS = "() => window.__applyClickSeen === true"
_UNSEEN_JS = "() => { window.__applyClickSeen = false; }"


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


def click(page, digest: apply_form.FormDigest, n: int, *, timeout_s: float = 20,
          check: Callable[[str, dict], str] | None = None) -> ClickResult:
    """Click button `n` of `digest` and wait, up to `timeout_s`, for a
    navigation or a DOM change; the `ClickResult` says whether the click
    landed and whether anything changed (settled through `_settle` when it
    did).

    `check(expected_text, live)` reads the live element just before the
    click (`apply_form.live_text`: its text, aria-label, type) and returns
    "" to go on or the reason to refuse (INV-04). A control whose live text
    differs from the digest's is found again by that text in its frame
    (`apply_form.find_by_text`) and checked again; when no single control
    reads it, the click is refused. A click that raised after it was
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
    if check is not None:
        live = apply_form.live_text(loc)
        if live:
            why = check(button.text, live)
            if not why and _norm(live.get("text")) != _norm(button.text):
                found = apply_form.find_by_text(page, button.locator[0], button.text)
                if len(found) == 1:
                    loc = apply_form.resolve(page, (button.locator[0], found[0]))
                    live = apply_form.live_text(loc)
                    why = check(button.text, live) if live else ""
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

    def _on_request(request) -> None:
        requests.append(str(getattr(request, "method", "")))

    def _act() -> None:
        try:
            if frame is not None:
                frame.evaluate(_UNSEEN_JS)      # an earlier click's mark never counts
            loc.first.evaluate(_ARM_JS, timeout=ACTION_TIMEOUT_MS)
        except Exception:       # noqa: BLE001  (the click finds out)
            pass
        url0 = str(page.url)
        page.on("request", _on_request)
        try:
            loc.first.click(timeout=ACTION_TIMEOUT_MS)
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
        landed.append(True)

    try:
        changed = _await_change(page, _act, timeout_s)
    except Exception as e:      # noqa: BLE001
        log.info("apply_fill: click on %r failed: %s", button.text, type(e).__name__)
        return ClickResult(clicked=bool(landed), changed=False, late=late[0] if late else "")
    return ClickResult(clicked=True, changed=changed, late=late[0] if late else "")


def click_button(page, digest: apply_form.FormDigest, n: int, *, timeout_s: float = 20) -> bool:
    """`click(...).changed`: True only when the click landed and something
    changed (and the page has settled); False on an unknown button, a missing
    element, a click that raised, or a quiet page. `click` tells those apart."""
    return click(page, digest, n, timeout_s=timeout_s).changed


def wait_for_change(page, *, timeout_s: float = 20) -> bool:
    """`click_button`'s wait without the click: up to `timeout_s` for a
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


def page_text(page) -> str:
    """The visible text of every frame, main frame first, capped at
    `PAGE_TEXT_CAP` for the record writer."""
    return "\n".join(apply_form.page_texts(page))[:PAGE_TEXT_CAP]
