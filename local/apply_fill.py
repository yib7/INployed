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
proved on Greenhouse. A file control is never clicked.

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
SETTLE_STABLE_S = 2.0          # click_button: the longest wait for two equal snapshots
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
          deadline: float | None = None) -> list[Filled]:
    """Perform every `fill` / `select` / `upload` in `plan` and return the
    read-back value of each acted field. `skip` and `generate` are not acted
    on and do not appear in the result. `deadline` is a `time.monotonic()`
    instant; once it has passed the remaining fields are left alone and the
    list so far comes back (the job's wall clock belongs to the caller)."""
    out: list[Filled] = []
    for pf in plan.fields:
        if pf.action not in ("fill", "select", "upload"):
            continue
        if deadline is not None and time.monotonic() >= deadline:
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
            _say(log, f"apply_fill: {pf.action} on {pf.label!r} ({pf.locator[1]}) failed: {e}")
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


def _settle(page, timeout_s: float) -> None:
    """After a change: wait for `domcontentloaded` (a no-op on a page that did
    not navigate), then for two equal snapshots with no torn-down frame, at
    most SETTLE_STABLE_S apart in total, so the caller's next `extract` sees a
    page that has stopped moving."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=max(1, int(timeout_s * 1000)))
    except Exception:       # noqa: BLE001
        pass
    last = None
    stop = time.monotonic() + min(SETTLE_STABLE_S, max(timeout_s, 0.1))
    while time.monotonic() < stop:
        snap = _snapshot(page)
        if snap == last and ("gone",) not in snap:
            return
        last = snap
        page.wait_for_timeout(100)


def click_button(page, digest: apply_form.FormDigest, n: int, *, timeout_s: float = 20) -> bool:
    """Click button `n` of `digest` and wait, up to `timeout_s`, for a
    navigation or a DOM change. True when something changed, and only after
    `_settle` has seen the page load and hold still; False on an unknown
    button, a missing element, or a quiet page."""
    button = next((b for b in digest.buttons if b.n == n), None)
    if button is None:
        log.info("apply_fill: no button %s in the digest", n)
        return False
    loc = apply_form.resolve(page, button.locator)
    if loc.count() == 0:
        log.info("apply_fill: button %s (%s) is gone", n, button.locator[1])
        return False
    before = _snapshot(page)
    url0 = page.url
    navigated: list[str] = []

    def _on_nav(frame) -> None:
        if frame == page.main_frame:
            navigated.append(frame.url)

    page.on("framenavigated", _on_nav)
    try:
        loc.first.click(timeout=ACTION_TIMEOUT_MS)
        deadline = time.monotonic() + timeout_s
        while True:
            if navigated or page.url != url0 or _snapshot(page) != before:
                _settle(page, timeout_s)
                return True
            if time.monotonic() >= deadline:
                return False
            page.wait_for_timeout(int(POLL_S * 1000))
    except Exception as e:      # noqa: BLE001
        log.info("apply_fill: click on %r failed: %s", button.text, e)
        return False
    finally:
        page.remove_listener("framenavigated", _on_nav)


def page_text(page) -> str:
    """The visible text of every frame, main frame first, capped at
    `PAGE_TEXT_CAP` for the record writer."""
    return "\n".join(apply_form.page_texts(page))[:PAGE_TEXT_CAP]
