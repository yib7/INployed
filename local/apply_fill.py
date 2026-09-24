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
proved on Greenhouse. A file control gets no click. A field the extractor
read as a widget (`PlannedField.widget`, SP5) is acted on its own way: a
custom radio group, Yes / No buttons and a question's tick boxes by clicking
the option's own element; a dropdown drawn as a button by opening it and
clicking the option; a typeahead or an async combobox by typing the value and
clicking its match; a hidden select in place; a hidden tick box or radio
through its label; a rich-text box by typing; date parts part by part. The
control is checked to be the one planned for before the act (FILL-02).

`click_button(page, digest, n)` clicks a digest button and waits for a
navigation or a DOM change (body length and the set of visible controls,
polled every 250 ms), capped. `open_listbox_options(page, field)` reads a
React-select style menu on demand. `page_text(page)` is the capped visible
text for the record writer. Playwright is reached only through the `page`
argument, so this module imports without it.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable

import apply_form
import apply_judge
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
    // react-select clears its input after a pick and shows the choice in a
    // sibling (EXT-10): the single value in the widget's control box
    let box = el;
    for (let i = 0; box && i < 4; i++, box = box.parentElement) {
      const sv = box.querySelector('[class*=single-value], [class*=singleValue], '
        + '[class*=multi-value__label], [class*=multiValue] [class*=label]');
      if (sv) return Array.from(box.querySelectorAll('[class*=single-value], [class*=singleValue], '
        + '[class*=multi-value__label]')).map((n) => norm(n.textContent)).filter(Boolean).join(', ');
      if (box !== el && /__control\\b|(^|\\s)control(\\s|$)/i.test(box.getAttribute('class') || '')) break;
    }
    return tag === 'INPUT' ? '' : norm(el.innerText);
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


def parse_date(value: str):
    """The date `value` names in ISO, the common US and long shapes, or
    DD.MM.YYYY; None when it names none."""
    v = (value or "").strip()
    for shape in (*_DATE_SHAPES, "%d.%m.%Y"):
        try:
            return datetime.strptime(v, shape).date()
        except ValueError:
            continue
    return None


def _date_value(value: str) -> str:
    """ISO `YYYY-MM-DD` for a native date control, from ISO or the common US
    and long shapes; anything unparsed goes through as given."""
    d = parse_date(value)
    return d.isoformat() if d is not None else (value or "").strip()


# A text box's own hints (FILL-04, FILL-05): its type, placeholder, pattern,
# length cap, label and aria-label, input mode, autocomplete and mask
# attribute, and whether a country-code control sits on its row (a select,
# a dropdown or a box named for the country code or showing "+1").
_HINTS_JS = r"""el => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const labels = el.labels ? Array.from(el.labels).map((l) => norm(l.innerText)).join(' ') : '';
  const CC = /country\s*(code|dial)|dial(ing)?\s*code|calling\s*code|phone\s*(country\s*)?code|^\s*\+\d{1,3}\b/i;
  let cc = false;
  let p = el.parentElement;
  for (let i = 0; p && i < 3 && !cc; i++, p = p.parentElement) {
    if (p.matches('form, body, fieldset')) break;
    for (const c of p.querySelectorAll('select, [role=combobox], [aria-haspopup], input')) {
      if (c === el || c.type === 'hidden') continue;
      const shown = c.tagName === 'SELECT' ? (c.selectedOptions[0] ? c.selectedOptions[0].text : '')
        : (c.tagName === 'INPUT' ? c.value : c.innerText);
      const t = [c.getAttribute('aria-label'), c.id, c.getAttribute('name'),
                 c.labels ? Array.from(c.labels).map((l) => l.innerText).join(' ') : '',
                 shown].map(norm).join(' | ');
      if (t.split(' | ').some((part) => CC.test(part))) { cc = true; break; }
    }
  }
  return {type: (el.getAttribute('type') || 'text').toLowerCase(), placeholder: norm(el.placeholder),
          pattern: el.getAttribute('pattern') || '', maxlength: el.maxLength > 0 ? el.maxLength : 0,
          label: labels, aria: norm(el.getAttribute('aria-label')),
          name: (el.getAttribute('name') || '') + ' ' + (el.id || ''),
          inputmode: (el.getAttribute('inputmode') || '').toLowerCase(),
          autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
          mask: el.getAttribute('data-mask') || el.getAttribute('data-inputmask')
            || el.getAttribute('data-format') || el.getAttribute('data-date-format') || '',
          cc: cc};
}"""
_DATE_FORMATS = (
    # (a hint's words, the strftime shape): the first that the hints name
    (re.compile(r"\byyyy\s*-\s*mm\s*-\s*dd\b", re.I), "%Y-%m-%d"),
    (re.compile(r"\byyyy\s*/\s*mm\s*/\s*dd\b", re.I), "%Y/%m/%d"),
    (re.compile(r"\bdd\s*/\s*mm\s*/\s*yyyy\b", re.I), "%d/%m/%Y"),
    (re.compile(r"\bdd\s*\.\s*mm\s*\.\s*yyyy\b", re.I), "%d.%m.%Y"),
    (re.compile(r"\bdd\s*-\s*mm\s*-\s*yyyy\b", re.I), "%d-%m-%Y"),
    (re.compile(r"\bmm\s*/\s*dd\s*/\s*yyyy\b", re.I), "%m/%d/%Y"),
    (re.compile(r"\bmm\s*-\s*dd\s*-\s*yyyy\b", re.I), "%m-%d-%Y"),
    (re.compile(r"\bmm\s*/\s*dd\s*/\s*yy\b", re.I), "%m/%d/%y"),
    (re.compile(r"\bm\s*/\s*d\s*/\s*yyyy\b", re.I), "M/D/YYYY"),
    (re.compile(r"\bmm\s*/\s*yyyy\b", re.I), "%m/%Y"),
    # a pattern attribute: two digits, two digits, four digits
    (re.compile(r"^\^?(?:\\d|\[0-9\])\{2\}/(?:\\d|\[0-9\])\{2\}/(?:\\d|\[0-9\])\{4\}\$?$"),
     "%m/%d/%Y"),
    (re.compile(r"^\^?(?:\\d|\[0-9\])\{4\}-(?:\\d|\[0-9\])\{2\}-(?:\\d|\[0-9\])\{2\}\$?$"),
     "%Y-%m-%d"),
)
_PHONE_WORDS = re.compile(r"\b(phone|mobile|cell|telephone)\b", re.I)
_MASK_SHAPE = re.compile(r"[_#X9x]{2,}|\(\s*[_#X9x]{3}\s*\)")
# a pattern attribute that takes bare digits only ("\d{10}", "[0-9]+")
_DIGITS_ONLY = re.compile(r"^\^?(?:\\d|\[0-9\])(?:\{\d+(?:,\d*)?\}|\+|\*)\$?$")


def date_format(hints: dict) -> str:
    """The strftime shape the box's hints name (`_DATE_FORMATS`), or ""."""
    for key in ("pattern", "mask", "placeholder", "label", "aria"):
        text = str(hints.get(key) or "")
        for shape, fmt in _DATE_FORMATS:
            if text and shape.search(text):
                return fmt
    return ""


def format_date(d, fmt: str) -> str:
    if fmt == "M/D/YYYY":
        return f"{d.month}/{d.day}/{d.year}"
    return d.strftime(fmt)


def phone_digits(value: str) -> str:
    """A phone number's digits, the leading US country code off an
    eleven-digit number (the national number)."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def _phoneish(hints: dict) -> bool:
    return (hints.get("type") == "tel" or str(hints.get("autocomplete") or "").startswith("tel")
            or bool(_PHONE_WORDS.search(" ".join(str(hints.get(k) or "") for k in (
                "label", "aria", "name", "placeholder")))))


def _masked(hints: dict) -> bool:
    return bool(hints.get("mask")) or bool(_MASK_SHAPE.search(str(hints.get("placeholder") or "")))


def number_value(value: str) -> str:
    """A number box's value (FILL-06): the first number in `value`, its
    thousands separators and currency off ("$120,000" 120000, "5+" 5)."""
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value or ""))
    return m.group(0).replace(",", "") if m else str(value or "").strip()


def _ci_match(want: str, candidates: list[str]) -> int:
    """Index of the candidate equal to `want` case-insensitively (whitespace
    folded), else the one a name of `want` matches (`apply_judge.match_option`:
    USA for United States, CA for California, a decline for a decline;
    FILL-07), else the one containing it, else -1."""
    w = " ".join((want or "").split()).lower()
    folded = [" ".join(str(c).split()).lower() for c in candidates]
    if w in folded:
        return folded.index(w)
    found = apply_judge.match_option(want, [str(c) for c in candidates])
    if found is not None:
        return [str(c) for c in candidates].index(found)
    for i, c in enumerate(folded):
        if w and w in c:
            return i
    return -1


# --- the actions ------------------------------------------------------------------

def _typed(loc, text: str) -> None:
    """Clear the box and type `text` key by key (a masked box takes keys,
    never a pasted value)."""
    loc.first.fill("", timeout=ACTION_TIMEOUT_MS)
    loc.first.press_sequentially(text, delay=15, timeout=ACTION_TIMEOUT_MS)


def _fill(loc, kind: dict[str, str], value: str) -> None:
    """Type `value` into a text-like box in the shape the box asks for: a
    native date control ISO; a number box the value's number (FILL-06); a
    text box whose hints name a date format that format (FILL-05, "Date
    (MM/DD/YYYY)"); a phone box its national digits when a country-code
    control sits on its row or its pattern or length asks for bare digits,
    typed key by key into a masked box, and typed again key by key when a
    mask left the box holding other digits (FILL-04)."""
    if kind["tag"] == "INPUT" and kind["type"] == "date":
        loc.first.fill(_date_value(value), timeout=ACTION_TIMEOUT_MS)
        return
    if kind["tag"] == "INPUT" and kind["type"] == "number":
        loc.first.fill(number_value(value), timeout=ACTION_TIMEOUT_MS)
        return
    if kind["tag"] != "INPUT" or kind["type"] in ("checkbox", "radio", "file", "password"):
        loc.first.fill(value, timeout=ACTION_TIMEOUT_MS)
        return
    try:
        hints = dict(loc.first.evaluate(_HINTS_JS, timeout=ACTION_TIMEOUT_MS) or {})
    except Exception:       # noqa: BLE001  (the fill finds out)
        hints = {}
    d = parse_date(value)
    fmt = date_format(hints) if d is not None else ""
    if fmt:
        loc.first.fill(format_date(d, fmt), timeout=ACTION_TIMEOUT_MS)
        return
    digits = phone_digits(value)
    if not _phoneish(hints) or len(digits) < 7:
        loc.first.fill(value, timeout=ACTION_TIMEOUT_MS)
        return
    bare = _DIGITS_ONLY.search(str(hints.get("pattern") or ""))
    national = bool(hints.get("cc")) or bool(bare) or hints.get("maxlength") == len(digits)
    text = digits if national else value
    if _masked(hints):
        _typed(loc, digits)
        return
    loc.first.fill(text, timeout=ACTION_TIMEOUT_MS)
    try:
        now = str(loc.first.input_value(timeout=ACTION_TIMEOUT_MS) or "")
    except Exception:       # noqa: BLE001  (the read-back finds out)
        return
    if phone_digits(now) != digits:
        # a mask that takes keys only, or mangled the pasted value
        _typed(loc, digits)


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


def _clicked(page, frame_index: int, css: str):
    """A locator for `css` in frame `frame_index` (a widget's option or proxy)."""
    return apply_form.resolve(page, (int(frame_index), str(css))).first


# Is the box behind an option or a proxy ticked: the element itself (a
# native box), its label's control, a box inside it, or its aria state.
_TICKED_JS = """el => {
  const box = el.matches('input') ? el : (el.control || el.querySelector('input[type=checkbox], input[type=radio]'));
  if (box) return !!box.checked;
  return el.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-pressed') === 'true'
    || el.getAttribute('aria-selected') === 'true';
}"""


def _ticked(loc) -> bool:
    try:
        return bool(loc.evaluate(_TICKED_JS, timeout=ACTION_TIMEOUT_MS))
    except Exception:       # noqa: BLE001  (gone)
        return False


def _check_radio(page, loc, want: str, pf: PlannedField | None = None) -> None:
    labels = loc.evaluate_all(_RADIO_LABELS_JS)
    i = _ci_match(want, labels)
    if i < 0:
        values = loc.evaluate_all("els => els.map(e => e.value)")
        i = _ci_match(want, values)
    if i < 0:
        raise LookupError(f"no radio {want!r} among {labels}")
    if pf is not None and i < len(pf.option_locators) and pf.option_locators[i]:
        # a hidden native radio behind its label (study G6): the label takes the click
        target = _clicked(page, pf.locator[0], pf.option_locators[i])
        if not _ticked(target):
            target.click(timeout=ACTION_TIMEOUT_MS)
        return
    loc.nth(i).check(timeout=ACTION_TIMEOUT_MS)


def _check_box(page, loc, want: str, pf: PlannedField | None = None) -> None:
    w = (want or "").strip().lower()
    if w not in CHECKED_WORDS and w not in UNCHECKED_WORDS:
        raise LookupError(f"checkbox option {want!r} is neither checked nor unchecked")
    tick = w in CHECKED_WORDS
    if pf is not None and pf.click_locator:
        # a hidden or see-through box behind its label (study G6): the label
        # takes the click, the box's own state is read
        if _ticked(loc.first) != tick:
            _clicked(page, pf.click_locator[0], pf.click_locator[1]).click(timeout=ACTION_TIMEOUT_MS)
        return
    if tick:
        loc.first.check(timeout=ACTION_TIMEOUT_MS)
    else:
        loc.first.uncheck(timeout=ACTION_TIMEOUT_MS)


# a form's submit control: a <button> with no type or type=submit inside a
# form, an input[type=submit]; `_choose` never clicks one (review M14)
_SUBMITS_JS = """el => {
  const form = el.form || el.closest('form');
  if (!form) return false;
  if (el.tagName === 'INPUT') return (el.getAttribute('type') || '').toLowerCase() === 'submit';
  if (el.tagName !== 'BUTTON') return false;
  const t = (el.getAttribute('type') || '').toLowerCase();
  return !t || t === 'submit';
}"""
_PARTS = re.compile(r"\s*(?:[,;/]|\band\b)\s*", re.I)


def _chosen(pf: PlannedField, want: str) -> list[int]:
    """The options to choose: the planned one; for a question's tick boxes
    also every option the value names ("Python, SQL": each is ticked, the
    study's G8), in the options' order. A value that is one option's whole
    name is that option alone ("Research and Development" never ticks
    Research and Development apart, review R2 Minor 3)."""
    options = list(pf.options)
    i = _ci_match(want, options)
    if i < 0 and pf.widget == "checkbox_group" and len(options) == 1 \
            and str(want or "").strip().lower() in CHECKED_WORDS:
        i = 0
    picked = [i] if i >= 0 else []
    if pf.widget == "checkbox_group" \
            and apply_judge.match_option(str(pf.value or ""), options) is None:
        for part in _PARTS.split(str(pf.value or "")):
            found = apply_judge.match_option(part, options) if part.strip() else None
            if found is not None and options.index(found) not in picked:
                picked.append(options.index(found))
    return sorted(picked)


def _choose(page, pf: PlannedField, want: str) -> None:
    """A custom radio group or Yes / No buttons (widget "choice"), a question's
    tick boxes (widget "checkbox_group", each chosen option ticked): the
    option's own element takes the click, unless it is already chosen, and
    never when it is a form's submit control."""
    picked = _chosen(pf, want)
    if not picked or any(i >= len(pf.option_locators) for i in picked):
        raise LookupError(f"no option {want!r} among {list(pf.options)}")
    for i in picked:
        target = _clicked(page, pf.locator[0], pf.option_locators[i])
        if target.evaluate(_SUBMITS_JS, timeout=ACTION_TIMEOUT_MS):
            raise LookupError(f"the option {pf.options[i]!r} is a form's submit control")
        if not _ticked(target):
            target.click(timeout=ACTION_TIMEOUT_MS)


def _aria_check(loc, want: str) -> None:
    w = (want or "").strip().lower()
    if w not in CHECKED_WORDS and w not in UNCHECKED_WORDS:
        raise LookupError(f"checkbox option {want!r} is neither checked nor unchecked")
    if _ticked(loc.first) != (w in CHECKED_WORDS):
        loc.first.click(timeout=ACTION_TIMEOUT_MS)


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


# the entries a dropdown drawn as a button shows (study G7): a listbox's
# options or a menu's radio items, visible
_MENU_OPTIONS = ("[role=option], [role=menuitemradio], [role=menuitem], "
                 "[role=menuitemcheckbox]")


class PopupRefused(LookupError):
    """A popup whose own words send was not opened (`popup_refusal`)."""


# What a popup's own words must not say for the run to open it (review round
# 8): a send or a last step, as the run's other clicks read them
# (`apply_run._send_worded`), a leading "Apply" too ("Apply with LinkedIn");
# never "Does not apply". SP6 (review round 9's false parks) reads them as a
# name: a send or last-step verb counts when nothing follows it, or what
# follows names the application or the send itself ("Submit ▾", "More
# submit options", "Choose how to submit your application", "Confirm and
# submit"), never another thing ("Submit a source", "Expected finish date",
# "Finish month", "Apply a location", "Send by post"). A question the words
# ask ("... a background check? Select One Required", Workday's aria-label,
# "Please confirm you are at least 18 ... Select One Required") is no name,
# and a shown value under a question is an answer ("I confirm", "Done").
_POPUP_VERB = re.compile(r"\b(submit|send|finish|complete|confirm|finali[sz]e|done)\b", re.I)
_POPUP_APPLY = re.compile(r"^\s*apply\b", re.I)
# what a send's verb may be followed by and still name the send: the
# application or its parts, the send's own words, a time, another send verb
_POPUP_SEND_OBJECT = re.compile(
    r"^(applications?|forms?|answers?|responses?|options?|request|submission|now|here"
    r"|everything|all|it|this|submit|send|finish|complete|confirm|finali[sz]e|done|apply)$",
    re.I)
_POPUP_APPLY_OBJECT = re.compile(r"^(with|using|via|through|now|here|for|to|online|today)$",
                                 re.I)
_POPUP_FILLER = frozenset(("your", "the", "my", "this", "our", "a", "an", "and", "or", "&"))
_POPUP_QUESTION_TAIL = re.compile(r"\b(select one|required)\s*$", re.I)
_POPUP_WORD = re.compile(r"[a-z]+", re.I)
_POPUP_WORDS_JS = """el => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const root = el.getRootNode();
  const byId = (id) => (root && root.getElementById ? root.getElementById(id) : null)
    || document.getElementById(id);
  // a question from outside the control: a label of it, or an
  // aria-labelledby target outside it
  const labels = (el.labels ? Array.from(el.labels) : []).filter((l) => !l.contains(el));
  const by = el.getAttribute('aria-labelledby');
  const named = by ? by.split(/\\s+/).map(byId).filter((n) => n && n !== el && !el.contains(n))
    : [];
  const label = norm(labels.concat(named).map((n) => n.innerText).join(' '));
  const labelled = !!label;
  // a value picker in a question's box: the words before it in a box (three
  // levels up) that holds no other control
  let boxed = false;
  if ((el.getAttribute('aria-haspopup') || '').toLowerCase() === 'listbox') {
    const CTRL = 'input:not([type=hidden]), select, textarea, button, [role=combobox], '
      + '[aria-haspopup], [role=radio], [role=checkbox]';
    let p = el.parentElement;
    for (let i = 0; p && i < 3 && !boxed; i++, p = p.parentElement) {
      if (p.matches('body, html, form, fieldset, main, dialog, [role=dialog]')) break;
      if (Array.from(p.querySelectorAll(CTRL)).some((c) => c !== el && !el.contains(c))) break;
      const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
      let t = '';
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (el.contains(n)) break;
        t += ' ' + n.data;
      }
      boxed = /[a-z]{3}/i.test(norm(t).replace(/^[*\\u2731\\s]+|[*\\u2731\\s]+$/g, ''));
    }
  }
  return {shown: norm(el.innerText) || norm(el.value), aria: norm(el.getAttribute('aria-label')),
          title: norm(el.getAttribute('title')), label: label, labelled: labelled, boxed: boxed};
}"""


def _question_shaped(text: str) -> bool:
    """Words that ask: a "?" in them, or Workday's tail ("... Select One
    Required")."""
    return "?" in text or bool(_POPUP_QUESTION_TAIL.search(text))


def send_phrase(text: str) -> bool:
    """Do `text`'s words name a send or a last step (see `_POPUP_VERB`): a
    send verb with nothing after it but fillers or symbols, or followed by
    the application, the send's own words or another send verb; a leading
    "Apply" alone or with "with", "now", "for"..."""
    words = _POPUP_WORD.findall(str(text or ""))
    for i, w in enumerate(words):
        verb = bool(_POPUP_VERB.fullmatch(w))
        apply_lead = i == 0 and w.lower() == "apply" and bool(_POPUP_APPLY.search(text))
        if not (verb or apply_lead):
            continue
        rest = [x for x in words[i + 1:] if x.lower() not in _POPUP_FILLER]
        if not rest:
            return True
        if (_POPUP_APPLY_OBJECT if apply_lead else _POPUP_SEND_OBJECT).fullmatch(rest[0]):
            return True
    return False


def popup_refusal(words: dict) -> str:
    """Why a popup whose own words read `words` ({shown, aria, title, label,
    labelled, boxed}) must not be opened, or "": its aria-label, its title,
    the label or labelling element that names it, or its shown text names a
    send or a last step (`send_phrase`). Words that ask (`_question_shaped`)
    are never read as a name; a shown text is never read under a question:
    a label or a labelling element outside the control (`labelled`), a
    question in its own aria-label, or a value picker's question box
    (`boxed`): it is the answer (SP6, review round 9)."""
    aria = " ".join(str(words.get("aria") or "").split())
    answered = bool(words.get("labelled")) or bool(words.get("boxed")) \
        or (bool(aria) and _question_shaped(aria))
    for key in ("aria", "title", "label", "shown"):
        text = " ".join(str(words.get(key) or "").split())
        if not text:
            continue
        if key == "shown" and answered:
            continue
        if key != "shown" and _question_shaped(text):
            continue
        if send_phrase(text):
            return f"its {'text' if key == 'shown' else key} reads {text[:60]!r}, a send"
    return ""


def _open_menu(frame, loc, *, popup: bool = False, face=None):
    """Click the combobox (or `face`, the box a person clicks for it:
    react-select's dummy input) unless its menu is already open; return the
    visible options locator, or None when nothing rendered within
    LISTBOX_WAIT_MS. The control and the face are read on their element
    handles just before the click, and the handle read is the one clicked:
    one whose own words send is never opened, whatever the extractor made of
    it (`PopupRefused`, review round 8)."""
    expanded = loc.first.get_attribute("aria-expanded", timeout=ACTION_TIMEOUT_MS)
    if expanded != "true":
        handles = [loc.first.element_handle(timeout=ACTION_TIMEOUT_MS)]
        if face is not None:
            handles.append(face.element_handle(timeout=ACTION_TIMEOUT_MS))
        try:
            for handle in handles:
                why = popup_refusal(dict(handle.evaluate(_POPUP_WORDS_JS)))
                if why:
                    raise PopupRefused(why)
            handles[-1].click(timeout=ACTION_TIMEOUT_MS)
        finally:
            for handle in handles:
                try:
                    handle.dispose()
                except Exception:   # noqa: BLE001
                    pass
    options = frame.locator(_MENU_OPTIONS).filter(visible=True) if popup \
        else _options_locator(frame, loc)
    try:
        options.first.wait_for(state="visible", timeout=LISTBOX_WAIT_MS)
    except Exception:       # noqa: BLE001  (Playwright's TimeoutError)
        return None
    return options


def _menu_showing(options) -> bool:
    try:
        return options is not None and options.count() > 0 and options.first.is_visible()
    except Exception:       # noqa: BLE001  (gone)
        return False


def _close_menu(page, loc, options=None) -> None:
    """Escape only while a menu of options shows (FILL-08): its options
    (`options`, the locator the open returned) are visible. A box that says
    `aria-expanded="true"` with nothing shown (a typeahead waiting for keys)
    gets no Escape: with no menu to take it, an Escape closes the dialog
    the form lives in."""
    if _menu_showing(options):
        page.keyboard.press("Escape")


_TYPEABLE_JS = """el => {
  const box = el.matches('input, textarea') ? el : el.querySelector('input');
  return !!box && !box.readOnly && !box.disabled;
}"""


def _type_to_filter(page, frame, loc, want: str):
    """An async combobox that shows nothing until typed in (EXT-06:
    Greenhouse's location and school, Ashby's location): the value is typed,
    and the options it brings are waited for."""
    try:
        if not loc.first.evaluate(_TYPEABLE_JS, timeout=ACTION_TIMEOUT_MS):
            return None
    except Exception:       # noqa: BLE001
        return None
    target = loc.first if (loc.first.evaluate("el => el.tagName") or "") == "INPUT" \
        else loc.first.locator("input").first
    target.fill("", timeout=ACTION_TIMEOUT_MS)
    target.press_sequentially(str(want or "").split(",")[0].strip(), delay=10,
                              timeout=ACTION_TIMEOUT_MS)
    options = _options_locator(frame, loc)
    try:
        options.first.wait_for(state="visible", timeout=LISTBOX_WAIT_MS)
    except Exception:       # noqa: BLE001  (Playwright's TimeoutError)
        return None
    return options


def _pick_listbox(page, frame, loc, want: str, *, popup: bool = False, face=None) -> None:
    options = _open_menu(frame, loc, popup=popup, face=face)
    if options is None and not popup:
        options = _type_to_filter(page, frame, loc, want)
    if options is None:
        raise LookupError("the listbox showed no options")
    texts = [t.strip() for t in options.all_inner_texts()]
    i = _ci_match(want, texts)
    if i < 0:
        _close_menu(page, loc, options)
        raise LookupError(f"no option {want!r} among {texts}")
    try:
        options.nth(i).click(timeout=ACTION_TIMEOUT_MS)
    except Exception:       # noqa: BLE001  (the menu closed under the click: open it once more)
        again = _open_menu(frame, loc, popup=popup, face=face)
        if again is None:
            raise
        texts = [t.strip() for t in again.all_inner_texts()]
        i = _ci_match(want, texts)
        if i < 0:
            raise
        again.nth(i).click(timeout=ACTION_TIMEOUT_MS)


# The matches a typeahead offers under its box (study G7: Lever's location has
# no ARIA): the visible entries of the nearest results list around it, each
# marked for the click.
_TYPEAHEAD_OPTIONS_JS = """el => {
  const visible = (n) => { const st = getComputedStyle(n); const r = n.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && (r.width > 0 || r.height > 0); };
  let box = el.parentElement;
  for (let i = 0; box && i < 3; i++, box = box.parentElement) {
    const lists = box.querySelectorAll('[role=listbox], [class*=dropdown-results], '
      + '[class*=autocomplete-results], [class*=suggestions], [class*=results]');
    for (const list of lists) {
      if (!visible(list)) continue;
      let opts = Array.from(list.querySelectorAll('[role=option]'));
      if (!opts.length) opts = Array.from(list.children);
      opts = opts.filter((o) => visible(o) && (o.innerText || '').trim());
      if (!opts.length) continue;
      opts.forEach((o, k) => o.setAttribute('data-apply-option', String(k)));
      return opts.map((o) => (o.innerText || '').replace(/\\s+/g, ' ').trim());
    }
  }
  return [];
}"""


def _type_ahead(page, frame, loc, value: str) -> None:
    """Type the value into a typeahead (study G7), wait for its matches and
    click the one that fits; with no match the typed value stays."""
    loc.first.fill("", timeout=ACTION_TIMEOUT_MS)
    loc.first.press_sequentially(value, delay=10, timeout=ACTION_TIMEOUT_MS)
    deadline = time.monotonic() + LISTBOX_WAIT_MS / 1000
    texts: list[str] = []
    while time.monotonic() < deadline:
        texts = list(loc.first.evaluate(_TYPEAHEAD_OPTIONS_JS, timeout=ACTION_TIMEOUT_MS) or [])
        if texts:
            break
        page.wait_for_timeout(100)
    if not texts:
        return
    i = _ci_match(value, texts)
    if i < 0:
        first = (value or "").split(",")[0].strip()
        i = _ci_match(first, texts)
    if i < 0:
        return
    frame.locator(f'[data-apply-option="{i}"]').first.click(timeout=ACTION_TIMEOUT_MS)


_SET_SELECT_JS = """(el, value) => {
  el.value = value;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return el.value === value;
}"""


def _select_hidden(loc, want: str) -> None:
    """A hidden <select> behind a styled trigger (study G6): picked in place,
    forced past the actionability check, else set and announced by script."""
    options = loc.first.evaluate(_SELECT_OPTIONS_JS, timeout=ACTION_TIMEOUT_MS)
    labels = [o[0] for o in options]
    i = _ci_match(want, labels)
    if i < 0:
        i = _ci_match(want, [o[1] for o in options])
    if i < 0:
        raise LookupError(f"no option {want!r} among {labels}")
    try:
        loc.first.select_option(value=options[i][1], force=True, timeout=ACTION_TIMEOUT_MS)
    except Exception:       # noqa: BLE001  (a select the page keeps out of reach)
        loc.first.evaluate(_SET_SELECT_JS, options[i][1], timeout=ACTION_TIMEOUT_MS)


_SELECT_ALL_JS = """el => {
  el.focus();
  const r = document.createRange();
  r.selectNodeContents(el);
  const s = window.getSelection();
  s.removeAllRanges();
  s.addRange(r);
}"""


def _fill_editable(page, loc, value: str) -> None:
    """A rich-text box (EXT-13): focused, its content selected, the text
    inserted as typing would."""
    loc.first.click(timeout=ACTION_TIMEOUT_MS)
    loc.first.evaluate(_SELECT_ALL_JS, timeout=ACTION_TIMEOUT_MS)
    page.keyboard.insert_text(value)


def _date_parts(value: str) -> dict[str, str]:
    iso = _date_value(value)
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError as e:
        raise LookupError(f"not a date: {value!r}") from e
    return {"M": f"{d.month:02d}", "D": f"{d.day:02d}", "Y": f"{d.year:04d}"}


def _fill_date_parts(page, pf: PlannedField, value: str) -> None:
    """Month / Day / Year boxes (EXT-12): each part typed into its own box, in
    the order the widget names ("date:MDY")."""
    parts = _date_parts(value)
    order = pf.widget.split(":", 1)[1] if ":" in pf.widget else ""
    for kind, css in zip(order, pf.option_locators):
        target = _clicked(page, pf.locator[0], css)
        if (target.evaluate("el => el.tagName", timeout=ACTION_TIMEOUT_MS) or "") == "INPUT":
            target.fill(parts[kind], timeout=ACTION_TIMEOUT_MS)
        else:
            target.click(timeout=ACTION_TIMEOUT_MS)
            page.keyboard.insert_text(parts[kind])


def _upload(page, locator: tuple[int, str], path: str) -> None:
    frame = apply_form.frames(page)[int(locator[0])]
    frame.set_input_files(str(locator[1]), path, timeout=ACTION_TIMEOUT_MS)


def _act(page, pf: PlannedField, loc, kind: dict[str, str]) -> None:
    want = pf.option if pf.option is not None else pf.value
    tag, typ, role = kind["tag"], kind["type"], kind["role"]
    if pf.action == "upload":
        if upload_shown(loc, pf.value):
            # the box or its widget shows the file already: a second upload
            # would attach it twice (FILL-01)
            log.info("apply_fill: %r already holds %s; not uploaded again", pf.label,
                     _file_name(pf.value))
            return
        _upload(page, pf.locator, pf.value)
        return
    if pf.action not in ("fill", "select"):
        return
    frame = apply_form.frames(page)[int(pf.locator[0])]
    widget = pf.widget or ""
    if widget in ("choice", "checkbox_group"):
        _choose(page, pf, want)
    elif widget == "popup":
        _pick_listbox(page, frame, loc, want, popup=True)
    elif widget == "combo":
        _pick_listbox(page, frame, loc, pf.value if pf.action == "fill" else want)
    elif widget == "typeahead":
        _type_ahead(page, frame, loc, pf.value if pf.action == "fill" else want)
    elif widget == "hidden_select":
        _select_hidden(loc, want)
    elif widget == "aria_check":
        _aria_check(loc, want)
    elif widget == "editable":
        _fill_editable(page, loc, pf.value if pf.action == "fill" else want)
    elif widget.startswith("date:"):
        _fill_date_parts(page, pf, pf.value)
    elif tag == "SELECT":
        _select_native(loc, want)
    elif tag == "INPUT" and typ == "radio":
        _check_radio(page, loc, want, pf)
    elif tag == "INPUT" and typ == "checkbox":
        _check_box(page, loc, want, pf)
    elif role in ("combobox", "listbox"):
        face = _clicked(page, pf.click_locator[0], pf.click_locator[1]) \
            if pf.click_locator else None
        _pick_listbox(page, frame, loc, want, face=face)
    elif tag == "INPUT" and typ == "file":
        raise LookupError("a file input takes an upload action")
    else:
        _fill(loc, kind, pf.value if pf.action == "fill" else want)


_POPUP_READ_JS = """el => {
  const t = (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim();
  return __PLACEHOLDER__.test(t) ? '' : t;
}""".replace("__PLACEHOLDER__", apply_form.PLACEHOLDER_TEXT_JS)
_EDITABLE_READ_JS = "el => (el.innerText || '').replace(/\\s+/g, ' ').trim()"
# a text box inside a dropdown drawn as a box: its value, else the value the
# box shows beside it (cleared after a pick, as react-select's is)
_COMBO_READ_JS = """el => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  if (el.value) return norm(el.value);
  const box = el.closest('[aria-haspopup]');
  if (!box) return '';
  const sv = box.querySelector('[class*=single-value], [class*=singleValue]');
  const t = norm(sv ? sv.textContent : box.innerText);
  return __PLACEHOLDER__.test(t) ? '' : t;
}""".replace("__PLACEHOLDER__", apply_form.PLACEHOLDER_TEXT_JS)


def _read_widget(page, pf: PlannedField, loc) -> str:
    """What a widget holds now, in the words its options use."""
    widget = pf.widget or ""
    if widget in ("choice", "checkbox_group"):
        chosen = [opt for opt, css in zip(pf.options, pf.option_locators)
                  if css and _ticked(_clicked(page, pf.locator[0], css))]
        return ", ".join(chosen) if widget == "checkbox_group" else (chosen[0] if chosen else "")
    if widget == "popup":
        return str(loc.first.evaluate(_POPUP_READ_JS, timeout=ACTION_TIMEOUT_MS) or "")
    if widget == "combo":
        return str(loc.first.evaluate(_COMBO_READ_JS, timeout=ACTION_TIMEOUT_MS) or "")
    if widget == "aria_check":
        return "checked" if _ticked(loc.first) else ""
    if widget == "editable":
        return str(loc.first.evaluate(_EDITABLE_READ_JS, timeout=ACTION_TIMEOUT_MS) or "")
    if widget.startswith("date:"):
        order = widget.split(":", 1)[1]
        got = {k: str(_clicked(page, pf.locator[0], css).evaluate(
            "el => (el.value !== undefined ? el.value : el.innerText) || ''",
            timeout=ACTION_TIMEOUT_MS)).strip() for k, css in zip(order, pf.option_locators)}
        if not all(got.values()):
            return ""
        return f"{got.get('M', '')}/{got.get('D', '')}/{got.get('Y', '')}"
    return ""


# An upload's read-back (FILL-01): the input's file, else the widget's chip
# (a box that consumes the file and resets the input shows its name) or,
# unless `named`, its success note, in the box around the input that holds
# no other file input (four levels up, never the form or the page). The
# file's name when one of them shows it, else "".
_FILE_READ_JS = r"""(el, [want, named]) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  if (el.files && el.files.length) return el.files[0].name;
  const DONE = /\b(successfully\s+uploaded|upload(ed)?\s+(complete|successful(ly)?|succeeded)|file\s+(uploaded|attached))\b/i;
  let box = el.parentElement;
  for (let i = 0; box && i < 4; i++, box = box.parentElement) {
    if (box.matches('form, body, html, main, [role=main]')) break;
    if (Array.from(box.querySelectorAll('input[type=file]')).some((f) => f !== el)) break;
    const text = norm(box.innerText);
    if (want && text.toLowerCase().includes(want.toLowerCase())) return want;
    if (want && !named && DONE.test(text)) return want;
  }
  return '';
}"""


def upload_shown(loc, path: str) -> bool:
    """Does the file box, or its widget's chip, already show the file
    `path` names (its name, never a success note alone)? Such a file is
    never uploaded again (FILL-01)."""
    name = _file_name(path)
    try:
        return bool(name) and loc.first.evaluate(_FILE_READ_JS, [name, True],
                                                  timeout=ACTION_TIMEOUT_MS) == name
    except Exception:       # noqa: BLE001  (the upload finds out)
        return False


def _file_name(path: str) -> str:
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1]


def _read_back(loc, kind: dict[str, str] | None, page=None, pf: PlannedField | None = None) -> str:
    try:
        if loc.count() == 0:
            return ""
        if pf is not None and pf.action == "upload" and kind and kind["type"] == "file":
            return str(loc.first.evaluate(_FILE_READ_JS, [_file_name(pf.value), False],
                                          timeout=ACTION_TIMEOUT_MS) or "")
        if pf is not None and pf.widget and pf.widget not in ("typeahead", "hidden_select") \
                and page is not None:
            return _read_widget(page, pf, loc)
        if kind and kind["tag"] == "INPUT" and kind["type"] == "radio":
            i = loc.evaluate_all(_CHECKED_INDEX_JS)
            if i is None or i < 0:
                return ""
            labels = loc.evaluate_all(_RADIO_LABELS_JS)
            return str(labels[i]) if i < len(labels) else ""
        return str(loc.first.evaluate(_READ_JS, timeout=ACTION_TIMEOUT_MS) or "")
    except Exception:       # noqa: BLE001
        return ""


# Who the control is now (FILL-02): `apply_form.IDENT_FN_JS`, the extractor's
# own identity, read on the live element; and every element of the frame,
# open shadow roots walked, whose identity names the same control
# (`apply_form.SAME_IDENT_JS`): how a control the page moved is found again.
_FIND_IDENT_JS = r"""(want) => {
  const identOf = __IDENT__;
  const same = __SAME__;
  const all = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      all.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  all.forEach((el) => el.removeAttribute('data-apply-found'));
  const out = all.filter((el) => el.matches('input, select, textarea, button, [role], '
                                            + '[contenteditable]') && same(identOf(el), want));
  if (out.length === 1) out[0].setAttribute('data-apply-found', '1');
  return out.length;
}""".replace("__IDENT__", apply_form.IDENT_FN_JS).replace("__SAME__", apply_form.SAME_IDENT_JS)
_IDENTITY_BLIND = ("choice", "checkbox_group")      # a group's locator names no one control


def _same_control(page, pf: PlannedField, loc):
    """The control `pf` was planned for (FILL-02): the live element at its
    locator when it is still that control (its identity the extractor read),
    else the one element of the frame with that identity, else a
    LookupError: a value is never typed into another box."""
    if not pf.ident or pf.widget in _IDENTITY_BLIND or "radio" in str(pf.locator[1]):
        return loc
    try:
        live = str(loc.first.evaluate(apply_form.IDENT_FN_JS, timeout=ACTION_TIMEOUT_MS))
    except Exception:       # noqa: BLE001  (the click finds out)
        return loc
    if apply_form.same_ident(live, pf.ident):
        return loc
    frame = apply_form.frames(page)[int(pf.locator[0])]
    found = frame.evaluate(_FIND_IDENT_JS, pf.ident)
    if found != 1:
        raise LookupError(f"the control at {pf.locator[1]} is another one now "
                          f"({found} match its identity)")
    log.info("apply_fill: %r moved; found again by its identity", pf.label)
    return frame.locator("[data-apply-found='1']")


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
    (a Playwright message can quote the value). Before each act the control
    is checked to be the one planned for (`_same_control`, FILL-02)."""
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
            if pf.action != "upload":
                loc = _same_control(page, pf, loc)
            kind = _kind(loc)
            _act(page, pf, loc, kind)
        except Exception as e:      # noqa: BLE001  (the read-back reports the outcome)
            # the type alone: a Playwright message quotes the call, value included
            _say(log, f"apply_fill: {pf.action} on {pf.label!r} ({pf.locator[1]}) failed: "
                      f"{type(e).__name__}")
            if errors is not None:
                errors.append({"n": pf.n, "label": pf.label, "action": pf.action,
                               "error": type(e).__name__})
        value = _read_back(loc, kind, page, pf) if loc is not None else ""
        out.append(Filled(n=pf.n, label=pf.label, value=value))
    return out


def open_listbox_options(page, field) -> list[str]:
    """Open a combobox / listbox field (a dropdown drawn as a button too),
    read its option texts, close it (an Escape only while it is open)."""
    loc = apply_form.resolve(page, field.locator)
    frame = apply_form.frames(page)[int(field.locator[0])]
    options = _open_menu(frame, loc, popup=getattr(field, "widget", "") == "popup")
    texts = [t.strip() for t in options.all_inner_texts()] if options is not None else []
    _close_menu(page, loc, options)
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
# known to have been dispatched. With `want` (the text the live check read),
# a click that finds the element's text changed into a send or a last step
# (`_SEND_JS` words the checked text did not have) is cancelled there, at
# its dispatch, before any handler of the page sees it (INV-04: the check
# and the click are one step). The listener is removed once the run's click
# is over (`_DISARM_JS`): a later click of the person's is never touched.
_SEND_JS = r"\\b(submit|apply|send|finish|complete|confirm|finali[sz]e|done)\\b"
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
    if (!(e.target === el || el.contains(e.target))) return;
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


def click(page, digest: apply_form.FormDigest, n: int, *, timeout_s: float = 20,
          check: Callable[[str, dict], str] | None = None, guard: bool = True) -> ClickResult:
    """Click button `n` of `digest` and wait, up to `timeout_s`, for a
    navigation or a DOM change; the `ClickResult` says whether the click
    landed and whether anything changed (settled through `_settle` when it
    did).

    `check(expected_text, live)` reads the live element just before the
    click (`apply_form.live_text`: its text, aria-label, type) and returns
    "" to go on or the reason to refuse (INV-04). A control whose live text
    differs from the digest's is found again by that text in its frame
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
        if live:
            want = _norm(live.get("text"))
            why = check(button.text, live)
            if not why and _norm(live.get("text")) != _norm(button.text):
                found = apply_form.find_by_text(page, button.locator[0], button.text)
                if len(found) == 1:
                    loc = apply_form.resolve(page, (button.locator[0], found[0]))
                    live = apply_form.live_text(loc)
                    why = check(button.text, live) if live else ""
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

    def _on_request(request) -> None:
        # a navigation or a send proves the dispatch; a GET for an image does not
        try:
            method = str(request.method).upper()
            if method in _DISPATCH_METHODS or request.is_navigation_request():
                requests.append(method)
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
        why = check(button.text, live2) if check is not None and live2 else ""
        if why:
            blocked.append(why)
            raise _ClickStopped(why)

    def _act() -> None:
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

    try:
        changed = _await_change(page, _act, timeout_s)
    except Exception as e:      # noqa: BLE001
        if blocked:
            log.info("apply_fill: click on %r refused: %s", button.text, blocked[0])
            return ClickResult(clicked=False, changed=False, refused=blocked[0])
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
