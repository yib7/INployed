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

SP6: uploads go first and the page settles before the rest (a resume
parser's writes land before the planned values); a value the page reshapes
goes in its shape (a masked phone key by key, a phone's national digits, a
text date in the format its box names, a number box's number); an upload is
read from the widget's chip when the input was reset, and never sent twice;
`repair(page, pf, hint)` types a value the form refused again in the shape
its message asks; `apply` reports how it acted on each field (`outcomes`).

Cycle 18: a yes or a no picks only an option in its own alias set (FM-1);
a list the plan read no options for takes no option code cannot match
(`OptionsUnread`, FM-2); a number box takes a plain number only (FM-5);
`clear(page, pf)` takes an answer out again (FM-4).

`click_button(page, digest, n)` clicks a digest button and waits for a
navigation or a DOM change (body length and the set of visible controls,
polled every 250 ms), capped; a click a banner, a chat window or a sticky
bar took is made once more after `clear_overlay` put the cover away (a
consent banner's reject, else a close). `open_listbox_options(page, field)`
reads a React-select style menu on demand. `page_text(page)` is the capped
visible text for the record writer. Playwright is reached only through the
`page` argument, so this module imports without it.
"""
from __future__ import annotations

import logging
import re
import time
import weakref
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


def number_value(value: str) -> str | None:
    """A number box's value (FILL-06): `value` when it is a plain number
    ("3", "1.5"), else None (cycle 18, FM-5: "$120,000", "5+", "3-5" and
    "120k" are no number, and the box is left blank)."""
    text = str(value or "")
    return text.strip() if apply_judge.PLAIN_NUMBER.match(text) else None


# `_ci_match`'s answer when the options that hold a value tie and differ in
# meaning (final review B R2 M2): no option is chosen
OPTION_TIE = -2


class OptionTie(LookupError):
    """The options that hold the value tie and differ in meaning
    (`OPTION_TIE`): none is chosen, a typeahead's box is cleared, and the
    runner parks a required box and leaves an optional one blank."""


class OptionsUnread(LookupError):
    """A list whose options were never read ahead (the plan holds none, so
    no judge picked among them) shows none that code matches to the value
    (cycle 18, FM-2): none is chosen, the words typed to bring the options
    are taken out, and the runner parks a required box and leaves an
    optional one blank."""


# the words that turn an option against a value that lacks them: a negation
# ("Not Hispanic or Latino" for "Latino") or a qualifier ("Yes, but I will
# require sponsorship" for "Yes"; final review B R2 M2)
_QUALIFIER = re.compile(r"(?<!\w)(?:not|no|non|never|without|none|neither|nor|declin(?:e|ed|es|ing)"
                        r"|but|except|unless|requir(?:e|ed|es|ing)|need(?:s|ed)?)(?!\w)"
                        r"|n['’]t(?!\w)", re.I)


def _qualifiers(text: str) -> set[str]:
    """The negations and qualifiers `text` holds, each by its stem."""
    out: set[str] = set()
    for m in _QUALIFIER.finditer(text):
        word = m.group(0).lower()
        out.add("not" if word.endswith("t") and word[-2:-1] in ("'", "’")
                else "decline" if word.startswith("declin")
                else "require" if word.startswith("requir")
                else "need" if word.startswith("need") else word)
    return out


def _ci_match(want: str, candidates: list[str]) -> int:
    """Index of the candidate equal to `want` case-insensitively (whitespace
    folded), else the one a name of `want` matches (`apply_judge.match_option`:
    USA for United States, CA for California, a decline for a decline;
    FILL-07), else the closest one that holds it (`_holds`: "no" is never
    inside "None"), else -1.

    A negation or a qualifier the value lacks (`_qualifiers`) only breaks a
    tie between options that each hold the value (final review B R2 M2, fix
    round 3); it never puts out a lone one ("Not Hispanic or Latino" for
    "Latino" beside "White"). Among two or more, the ones that turn the
    value are out while one that leaves it unturned is left ("Hispanic or
    Latino" over "Not Hispanic or Latino" for "Latino"). When every one of
    them turns it, and they are not all the same words, they tie (`OPTION_TIE`): the value names nothing
    that tells their turns apart. A stored decline takes the one option
    that declines in its own words ("Prefer not to say"). The closest names
    each of the value's comma parts as one of its own ("Chicago, IL" for
    "chicago", never "Chicago Heights, IL"; `_names_each`); among such equal
    fits ("Anytown, California" and "Anytown, New York" for "anytown") the
    site's own order stands (final review B-M2): the value names nothing
    that sets them apart, and the site's first match is what typing it gives
    a person. Two or more that only hold the value ("Software Engineering"
    and "Hardware Engineering" for "Engineering") differ in what the value
    leaves out: `OPTION_TIE`.

    A yes or a no (`apply_judge.yes_no`) matches only an option in its own
    alias set (cycle 18, FM-1): "Yes" is none of "Yes - on a work visa" and
    "Yes, without sponsorship", and a qualified option is the judge's pick."""
    w = " ".join((want or "").split()).lower()
    folded = [" ".join(str(c).split()).lower() for c in candidates]
    if w in folded:
        return folded.index(w)
    if apply_judge.yes_no(want):
        texts = [str(c) for c in candidates]
        found = apply_judge.match_option(want, texts)
        return texts.index(found) if found is not None else -1
    mine = _qualifiers(w)
    parts = [p.strip() for p in w.split(",") if p.strip()]
    held = [i for i, c in enumerate(folded) if parts and _holds(parts, c)]
    # the options that hold the value and leave it unturned
    plain = [i for i in held if not _qualifiers(folded[i]) - mine]
    found = apply_judge.match_option(want, [str(c) for c in candidates])
    if found is not None:
        k = [str(c) for c in candidates].index(found)
        # the one option that starts with the value, a name it goes by, or a
        # decline, stands unless it turns the value while another option
        # holds it too; a stored decline keeps the one option that declines
        if not _qualifiers(folded[k]) - mine or apply_judge.declines(want) \
                or not any(folded[i] != folded[k] for i in held):
            return k
    if not parts:
        return -1
    if not held:
        return -1
    if not plain:
        # every option that holds the value turns it: a lone one stands, and
        # two that differ tie
        return OPTION_TIE if len({folded[i] for i in held}) > 1 else held[0]
    held = plain
    closest = [i for i in held if _names_each(parts, folded[i])]
    if closest:
        return closest[0]
    if len({folded[i] for i in held}) > 1:
        return OPTION_TIE
    return held[0]


def _ci_first(tries: list[tuple[str, list[str]]]) -> int:
    """`_ci_match` over (value, candidates) in turn (an option's labels,
    then its values; a value, then its first comma part): the first index
    found; else `OPTION_TIE` when a try tied; else -1."""
    tied = False
    for want, candidates in tries:
        i = _ci_match(want, candidates)
        if i >= 0:
            return i
        tied = tied or i == OPTION_TIE
    return OPTION_TIE if tied else -1


def _no_option(want: str, i: int, labels: list) -> LookupError:
    """The error for a pick `_ci_first` did not make (`i`): a tie
    (`OptionTie`) or no option at all."""
    if i == OPTION_TIE:
        return OptionTie(f"the options that hold {want!r} tie: {labels}")
    return LookupError(f"no option {want!r} among {labels}")


def _pieces(option: str) -> set[str]:
    return {apply_judge._norm_option(p) for p in option.split(",")}


def _holds(parts: list[str], option: str) -> bool:
    """Every comma part of a value is in `option` as whole words, or is a
    name one of the option's comma parts goes by ("Anytown, CA" in
    "Anytown, California, United States"; `apply_judge._alias_set`)."""
    pieces = _pieces(option)
    for part in parts:
        if re.search(r"(?<!\w)" + re.escape(part) + r"(?!\w)", option):
            continue
        if not apply_judge._alias_set(part) & pieces:
            return False
    return True


def _names_each(parts: list[str], option: str) -> bool:
    """Is every comma part of a value one of `option`'s own comma parts, or
    a name one of them goes by ("chicago" in "Chicago, IL", never in
    "Chicago Heights, IL")?"""
    pieces = _pieces(option)
    return all(apply_judge._norm_option(part) in pieces
               or apply_judge._alias_set(part) & pieces for part in parts)


# --- the actions ------------------------------------------------------------------

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def _keys_for(loc, text: object) -> str:
    """`text` as keys for the box `loc` names: a line break is a space in
    anything but a TEXTAREA, where a key-by-key Enter would submit the form
    (implicit submission; final review B-I1)."""
    text = str(text or "")
    try:
        tag = str(loc.first.evaluate("el => el.tagName", timeout=ACTION_TIMEOUT_MS) or "")
    except Exception:       # noqa: BLE001  (a box gone: the typing finds out)
        tag = ""
    return text if tag.upper() == "TEXTAREA" else _LINE_BREAK.sub(" ", text)


def _typed(loc, text: str) -> None:
    """Clear the box and type `text` key by key (a masked box takes keys,
    never a pasted value); a line break is a space outside a TEXTAREA
    (`_keys_for`)."""
    keys = _keys_for(loc, text)
    loc.first.fill("", timeout=ACTION_TIMEOUT_MS)
    loc.first.press_sequentially(keys, delay=15, timeout=ACTION_TIMEOUT_MS)


def _fill(loc, kind: dict[str, str], value: str) -> str:
    """Type `value` into a text-like box in the shape the box asks for: a
    native date control ISO; a number box a plain number (FILL-06; the
    phone's digits in a box named for a phone, cycle 18 FM-5), and nothing
    else (LookupError); a
    text box whose hints name a date format that format (FILL-05, "Date
    (MM/DD/YYYY)"); a phone box its national digits when a country-code
    control sits on its row or its pattern or length asks for bare digits,
    typed key by key into a masked box, and typed again key by key when a
    mask left the box holding other digits (FILL-04). Returns how
    (FILL-15)."""
    if kind["tag"] == "INPUT" and kind["type"] == "date":
        loc.first.fill(_date_value(value), timeout=ACTION_TIMEOUT_MS)
        return "date"
    if kind["tag"] == "INPUT" and kind["type"] == "number":
        number = number_value(value)
        if number is None and len(phone_digits(value)) >= 7:
            # the phone's digits, only in a box whose label or name say phone
            try:
                hints = dict(loc.first.evaluate(_HINTS_JS, timeout=ACTION_TIMEOUT_MS) or {})
            except Exception:       # noqa: BLE001  (no hints: no phone box)
                hints = {}
            if apply_judge.phone_named(*(hints.get(k) for k in ("label", "aria", "name"))):
                number = phone_digits(value)
        if number is None:
            raise LookupError("the number box takes a plain number")
        loc.first.fill(number, timeout=ACTION_TIMEOUT_MS)
        return "number"
    if kind["tag"] != "INPUT" or kind["type"] in ("checkbox", "radio", "file", "password"):
        loc.first.fill(value, timeout=ACTION_TIMEOUT_MS)
        return "fill"
    try:
        hints = dict(loc.first.evaluate(_HINTS_JS, timeout=ACTION_TIMEOUT_MS) or {})
    except Exception:       # noqa: BLE001  (the fill finds out)
        hints = {}
    d = parse_date(value)
    fmt = date_format(hints) if d is not None else ""
    if fmt:
        loc.first.fill(format_date(d, fmt), timeout=ACTION_TIMEOUT_MS)
        return f"date as {fmt}"
    digits = phone_digits(value)
    if not _phoneish(hints) or len(digits) < 7:
        loc.first.fill(value, timeout=ACTION_TIMEOUT_MS)
        return "fill"
    bare = _DIGITS_ONLY.search(str(hints.get("pattern") or ""))
    national = bool(hints.get("cc")) or bool(bare) or hints.get("maxlength") == len(digits)
    text = digits if national else value
    if _masked(hints):
        _typed(loc, digits)
        return "phone digits key by key"
    loc.first.fill(text, timeout=ACTION_TIMEOUT_MS)
    how = "phone national digits" if national else "fill"
    try:
        now = str(loc.first.input_value(timeout=ACTION_TIMEOUT_MS) or "")
    except Exception:       # noqa: BLE001  (the read-back finds out)
        return how
    if phone_digits(now) != digits:
        # a mask that takes keys only, or mangled the pasted value
        _typed(loc, digits)
        return "phone digits key by key"
    return how


def _select_native(loc, want: str) -> None:
    options = loc.first.evaluate(_SELECT_OPTIONS_JS, timeout=ACTION_TIMEOUT_MS)
    labels = [o[0] for o in options]
    i = _ci_first([(want, labels), (want, [o[1] for o in options])])
    if i < 0:
        raise _no_option(want, i, labels)
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
        j = _ci_match(want, values)
        i = j if j >= 0 or i == -1 else i       # a tie among the labels stands
    if i < 0:
        raise _no_option(want, i, labels)
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
    if i == OPTION_TIE:
        raise _no_option(want, i, options)
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
# never "Does not apply". SP6 reads them as a name (`send_phrase`, review
# round 9 and SP6 review I1):
# - a "submit" or "send" that leads a short name names a send, whatever
#   follows it ("Submit for review", "Submit resume", "Send to recruiter");
# - a last-step verb ("finish", "complete", "confirm", "done", "finalize"),
#   leading or not, and a send verb anywhere else, name one when nothing
#   follows, or what follows names the application or the send itself
#   ("Done", "Complete application", "Confirm and submit", "Submit ▾",
#   "More submit options", "Choose how to submit your application"), never
#   another thing ("Finish month", "Confirm your citizenship status",
#   "Expected finish date", "Willing to submit references") (SP6 review
#   R2-I3);
# - a leading "Apply" names one alone or with "with", "now", "for"... ("Apply
#   a location" is a placeholder).
# Words that ask ("... a background check? Select One Required", Workday's
# aria-label) are no name; a shown value or a title under an outside
# question label is an answer ("I confirm" under "Do you agree to the
# terms?", "Send by post" under "Delivery method" or "Document delivery",
# "Complete" under "Resume status"), and under a label that names the
# application, a document, a step or an action ("Your application", "Resume
# *", "Step 3", "Share your profile") it is read.
_POPUP_VERB = re.compile(r"\b(submit|send|finish|complete|confirm|finali[sz]e|done)\b", re.I)
_POPUP_LEAD_MAX = 6             # words: a name a leading submit or send makes a send
_POPUP_LEAD = re.compile(r"^(submit|send)$", re.I)
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
# a label that asks: a "?", "all that apply", Workday's "Select One" tail, or
# an interrogative first word
_ASKS = re.compile(
    r"\?|\ball that apply\b|\bselect one\b|^\s*(how|what|which|when|where|why|who|whom|whose|do|does"
    r"|did|are|is|was|were|will|would|can|could|have|has|had|should|may|might|shall)\b", re.I)
# a label that names the application, one of its documents, a step, or an
# action (a card's heading, a section's name): no question of a value's
_NAMES_THE_SEND = re.compile(
    r"\b(applications?|applying|submissions?|resumes?|résumés?|cv|cover\s+letters?"
    r"|documents?|attachments?|profiles?|candidacy|step\s*\d+)\b"
    r"|^\s*(share|send|submit|apply|upload|attach|save|review|continue|complete|finish|confirm"
    r"|finali[sz]e|proceed|next|done)\b", re.I)
# a label that names a document or the application and then the value asked
# of it ("Document delivery", "Resume status", "Application source"): a
# question (SP6 review R2-I3)
_VALUE_TAIL = re.compile(
    r"\b(status|delivery|method|type|format|date|preferences?|source|language|option|choice"
    r"|level|stage|mode|frequency|channel)\s*$", re.I)
_LEADS_ACTION = re.compile(
    r"^\s*(share|send|submit|apply|upload|attach|save|review|continue|complete|finish|confirm"
    r"|finali[sz]e|proceed|next|done)\b", re.I)
_POPUP_WORDS_JS = """el => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const root = el.getRootNode();
  const byId = (id) => (root && root.getElementById ? root.getElementById(id) : null)
    || document.getElementById(id);
  // the words from outside the control that name or ask about it: its
  // labels, and the aria-labelledby targets outside it
  const labels = (el.labels ? Array.from(el.labels) : []).filter((l) => !l.contains(el));
  const by = el.getAttribute('aria-labelledby');
  const named = by ? by.split(/\\s+/).map(byId).filter((n) => n && n !== el && !el.contains(n))
    : [];
  // the question box's words: the words before the control in a box (three
  // levels up) that holds no other control
  const CTRL = 'input:not([type=hidden]), select, textarea, button, [role=combobox], '
    + '[aria-haspopup], [role=radio], [role=checkbox]';
  let box = '';
  let p = el.parentElement;
  for (let i = 0; p && i < 3 && !box; i++, p = p.parentElement) {
    if (p.matches('body, html, form, fieldset, main, dialog, [role=dialog]')) break;
    if (Array.from(p.querySelectorAll(CTRL)).some((c) => c !== el && !el.contains(c))) break;
    const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
    let t = '';
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (el.contains(n)) break;
      t += ' ' + n.data;
    }
    t = norm(t).replace(/^[*\\u2731\\s]+|[*\\u2731\\s]+$/g, '');
    if (/[a-z]{3}/i.test(t)) box = t;
  }
  return {shown: norm(el.innerText) || norm(el.value), aria: norm(el.getAttribute('aria-label')),
          title: norm(el.getAttribute('title')),
          label: norm(labels.map((n) => n.innerText).join(' ')),
          named: norm(named.map((n) => n.innerText).join(' ')), box: box};
}"""


def _question_shaped(text: str) -> bool:
    """Words that ask: a "?" in them, or Workday's tail ("... Select One
    Required")."""
    return "?" in text or bool(_POPUP_QUESTION_TAIL.search(text))


def question_label(text: str) -> bool:
    """Is `text` (a label, a labelling element's or a question box's words)
    a question a value answers: words that ask (`_ASKS`), words that name no
    application, document, step or action (`_NAMES_THE_SEND`: "Degree
    status", "Delivery method" ask for a value; "Your application", "Resume
    *", "Step 3", "Share your profile" name the thing a send sends), or
    words that name one and then the value asked of it ("Document
    delivery", "Resume status": `_VALUE_TAIL`, SP6 review R2-I3)?"""
    t = " ".join(str(text or "").split()).strip(" *✱＊")
    if not t:
        return False
    return bool(_ASKS.search(t)) or not _NAMES_THE_SEND.search(t) or (
        bool(_VALUE_TAIL.search(t)) and not _LEADS_ACTION.search(t))


def send_phrase(text: str) -> bool:
    """Do `text`'s words name a send or a last step (see `_POPUP_VERB`): a
    "submit" or "send" that leads a name of at most `_POPUP_LEAD_MAX` words,
    whatever follows it; any other send or last-step verb, leading or not,
    with nothing after it but fillers or symbols, or followed by the
    application, the send's own words or another send verb (SP6 review
    R2-I3: "Finish month" and "Confirm your citizenship status" ask); a
    leading "Apply" alone or with "with", "now", "for"..."""
    words = _POPUP_WORD.findall(str(text or ""))
    if words and _POPUP_LEAD.fullmatch(words[0]) and len(words) <= _POPUP_LEAD_MAX:
        return True
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
    named, box}) must not be opened, or "": its aria-label, its title, the
    element outside it that names it (`named`, aria-labelledby), its
    `<label for>` (SP6 review R2-M4) or its shown text names a send or a
    last step (`send_phrase`). Words that ask (`_question_shaped`) are never
    read as a name. The shown text and the title are never read under an
    outside question label (`question_label` of its label, its labelling
    element or its question box's words) or a question in its own
    aria-label: they are the answer. Under any other outside label they are
    read (SP6 review I1)."""
    aria = " ".join(str(words.get("aria") or "").split())
    answered = (bool(aria) and _question_shaped(aria)) or any(
        question_label(words.get(key) or "") for key in ("label", "named", "box"))
    for key in ("aria", "named", "label", "title", "shown"):
        text = " ".join(str(words.get(key) or "").split())
        if not text:
            continue
        if key in ("shown", "title") and answered:
            continue
        if key != "shown" and _question_shaped(text):
            continue
        if send_phrase(text):
            what = {"shown": "text", "named": "label"}.get(key, key)
            return f"its {what} reads {text[:60]!r}, a send"
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
    keys = _keys_for(target, str(want or "").split(",")[0].strip())
    target.fill("", timeout=ACTION_TIMEOUT_MS)
    target.press_sequentially(keys, delay=10, timeout=ACTION_TIMEOUT_MS)
    options = _options_locator(frame, loc)
    try:
        options.first.wait_for(state="visible", timeout=LISTBOX_WAIT_MS)
    except Exception:       # noqa: BLE001  (Playwright's TimeoutError)
        return None
    return options


NOTHING_SHOWN = "the listbox showed no options"


def _pick_listbox(page, frame, loc, want: str, *, popup: bool = False, face=None) -> None:
    options = _open_menu(frame, loc, popup=popup, face=face)
    if options is None and not popup:
        options = _type_to_filter(page, frame, loc, want)
    if options is None:
        raise LookupError(NOTHING_SHOWN)
    texts = [t.strip() for t in options.all_inner_texts()]
    i = _ci_match(want, texts)
    if i < 0:
        _close_menu(page, loc, options)
        raise _no_option(want, i, texts)
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
# marked for the click. An earlier typeahead's marks are cleared first, in
# the document and every open shadow root: the click's locator reaches into
# shadow roots and takes the first mark it meets (final review B-M1, B R2 nit).
_TYPEAHEAD_OPTIONS_JS = """el => {
  const clear = (root) => {
    root.querySelectorAll('[data-apply-option]')
      .forEach((n) => n.removeAttribute('data-apply-option'));
    root.querySelectorAll('*').forEach((n) => { if (n.shadowRoot) clear(n.shadowRoot); });
  };
  clear(el.ownerDocument || document);
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
    click the one that fits; with no match the typed value stays. Matches
    that tie and differ in meaning (`OPTION_TIE`) leave the box empty and
    raise `OptionTie` (final review B R2 M2)."""
    keys = _keys_for(loc, value)
    loc.first.fill("", timeout=ACTION_TIMEOUT_MS)
    loc.first.press_sequentially(keys, delay=10, timeout=ACTION_TIMEOUT_MS)
    deadline = time.monotonic() + LISTBOX_WAIT_MS / 1000
    texts: list[str] = []
    while time.monotonic() < deadline:
        texts = list(loc.first.evaluate(_TYPEAHEAD_OPTIONS_JS, timeout=ACTION_TIMEOUT_MS) or [])
        if texts:
            break
        page.wait_for_timeout(100)
    if not texts:
        return
    first = (value or "").split(",")[0].strip()
    i = _ci_first([(value, texts), (first, texts)])
    if i == OPTION_TIE:
        loc.first.fill("", timeout=ACTION_TIMEOUT_MS)
        raise _no_option(value, i, texts)
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
    i = _ci_first([(want, labels), (want, [o[1] for o in options])])
    if i < 0:
        raise _no_option(want, i, labels)
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


def _pick_unread(pf: PlannedField, loc, pick: Callable[[], None]) -> None:
    """`pick` a list's option; on a list whose options the plan never read
    (`pf.options` empty) and that shows options none of which code matches
    to the value, the miss is `OptionsUnread` (cycle 18, FM-2), and the
    words typed to bring its options are taken out of its box. A tie, a
    refused popup and a list that shows nothing keep their own errors (the
    last one is filled once more and checked, as before)."""
    try:
        pick()
    except LookupError as e:
        if pf.options or isinstance(e, (OptionTie, PopupRefused)) or str(e) == NOTHING_SHOWN:
            raise
        try:
            tag = loc.first.evaluate("el => el.tagName", timeout=ACTION_TIMEOUT_MS) or ""
            box = loc.first if tag == "INPUT" else loc.first.locator("input").first
            if box.count() and str(box.input_value(timeout=ACTION_TIMEOUT_MS) or ""):
                box.fill("", timeout=ACTION_TIMEOUT_MS)
        except Exception:       # noqa: BLE001  (the read-back reports what stays)
            pass
        raise OptionsUnread(f"the options of {pf.label!r} were never read and none "
                            f"is the answer") from e


def _act(page, pf: PlannedField, loc, kind: dict[str, str]) -> str:
    want = pf.option if pf.option is not None else pf.value
    tag, typ, role = kind["tag"], kind["type"], kind["role"]
    if pf.action == "upload":
        if upload_shown(page, loc, pf):
            # this run put the file in this box on this page, the chip showed
            # it, and it still does: a second upload would attach it twice
            # (FILL-01)
            log.info("apply_fill: %r already holds %s from this run; not uploaded again",
                     pf.label, _file_name(pf.value))
            return "upload already shown"
        # the box's words before this run's upload: a file of the same name
        # the page showed already (a stored resume) never counts as this one
        _before_upload(page)[_box_key(pf)] = _upload_state(loc).get("text", "")
        _upload(page, pf.locator, pf.value)
        return "upload"
    if pf.action not in ("fill", "select"):
        return ""
    frame = apply_form.frames(page)[int(pf.locator[0])]
    widget = pf.widget or ""
    if widget in ("choice", "checkbox_group"):
        _choose(page, pf, want)
        return "option click"
    if widget == "popup":
        _pick_unread(pf, loc, lambda: _pick_listbox(page, frame, loc, want, popup=True))
        return "menu pick"
    if widget == "combo":
        _pick_unread(pf, loc, lambda: _pick_listbox(
            page, frame, loc, pf.value if pf.action == "fill" else want))
        return "list pick"
    if widget == "typeahead":
        _type_ahead(page, frame, loc, pf.value if pf.action == "fill" else want)
        return "typeahead"
    if widget == "hidden_select":
        _select_hidden(loc, want)
        return "hidden select"
    if widget == "aria_check":
        _aria_check(loc, want)
        return "custom tick"
    if widget == "editable":
        _fill_editable(page, loc, pf.value if pf.action == "fill" else want)
        return "rich text"
    if widget.startswith("date:"):
        _fill_date_parts(page, pf, pf.value)
        return "date parts"
    if tag == "SELECT":
        _select_native(loc, want)
        return "select"
    if tag == "INPUT" and typ == "radio":
        _check_radio(page, loc, want, pf)
        return "radio"
    if tag == "INPUT" and typ == "checkbox":
        _check_box(page, loc, want, pf)
        return "tick"
    if role in ("combobox", "listbox"):
        face = _clicked(page, pf.click_locator[0], pf.click_locator[1]) \
            if pf.click_locator else None
        _pick_unread(pf, loc, lambda: _pick_listbox(page, frame, loc, want, face=face))
        return "list pick"
    if tag == "INPUT" and typ == "file":
        raise LookupError("a file input takes an upload action")
    return _fill(loc, kind, pf.value if pf.action == "fill" else want)


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
_FILE_STATE_JS = r"""el => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const file = el.files && el.files.length ? el.files[0].name : '';
  let box = el.parentElement, text = '';
  for (let i = 0; box && i < 4; i++, box = box.parentElement) {
    if (box.matches('form, body, html, main, [role=main]')) break;
    if (Array.from(box.querySelectorAll('input[type=file]')).some((f) => f !== el)) break;
    text = norm(box.innerText);
  }
  return {file: file, text: text};
}"""
_UPLOAD_DONE = re.compile(r"\b(successfully\s+uploaded|upload(ed)?\s+(complete|successful(ly)?"
                          r"|succeeded)|file\s+(uploaded|attached))\b", re.I)
# words a widget shows when it did not take the file
_UPLOAD_FAILED = re.compile(
    r"\b(could\s*n[o']t|cannot|can't|failed|failure|errors?|invalid|too\s+large|not\s+supported"
    r"|unsupported|try\s+again|rejected)\b", re.I)
# Per page (a job's tab): the box words before this run's upload into a box,
# and the (box, file name) pairs this run uploaded and saw verified
_BEFORE_UPLOAD: "weakref.WeakKeyDictionary[Any, dict]" = weakref.WeakKeyDictionary()
_VERIFIED_UPLOADS: "weakref.WeakKeyDictionary[Any, set]" = weakref.WeakKeyDictionary()


def _before_upload(page) -> dict:
    try:
        return _BEFORE_UPLOAD.setdefault(page, {})
    except TypeError:       # a page double that takes no weak reference
        return {}


def _verified_uploads(page) -> set:
    try:
        return _VERIFIED_UPLOADS.setdefault(page, set())
    except TypeError:
        return set()


def _box_key(pf: PlannedField) -> tuple[int, str]:
    return int(pf.locator[0]), str(pf.locator[1])


def _upload_state(loc) -> dict:
    """The file box as it reads now: its own file's name and the words of
    the box around it (four levels up, never the form or the page, holding
    no other file box)."""
    try:
        return dict(loc.first.evaluate(_FILE_STATE_JS, timeout=ACTION_TIMEOUT_MS) or {})
    except Exception:       # noqa: BLE001  (gone)
        return {}


def upload_read(state: dict, name: str, before: str | None) -> str:
    """An upload's read-back (FILL-01, SP6 review I5): the box's own file,
    else the file's name when the box's words show it more often than
    before this run's upload (a chip the upload added), a success note that
    was not there before, or words that changed and still show it with no
    failure among them (a widget that replaced a kept chip of the same name,
    SP6 review R2-M1); "" when none. With no upload by this run (`before`
    None) the name shown counts only as the box's own words."""
    if state.get("file"):
        return str(state["file"])
    now, was, want = (str(state.get("text") or "").lower(), str(before or "").lower(),
                      str(name or "").lower())
    if not want:
        return ""
    if now.count(want) > was.count(want):
        return name
    if _UPLOAD_DONE.search(now) and not _UPLOAD_DONE.search(was):
        return name
    if before is not None and now != was and want in now \
            and not (_UPLOAD_FAILED.search(now) and not _UPLOAD_FAILED.search(was)):
        return name
    return ""


def upload_shown(page, loc, pf: PlannedField) -> bool:
    """May the upload of `pf` be skipped (FILL-01)? Only when this run put
    that file in that box on this page and saw it verified
    (`_verified_uploads`), and the box still shows it. A file of the same
    name the page showed before this run's upload (a resume kept from an
    earlier application) never counts (SP6 review I5)."""
    name = _file_name(pf.value)
    if not name or (_box_key(pf) + (name,)) not in _verified_uploads(page):
        return False
    before = _before_upload(page).get(_box_key(pf))
    return upload_read(_upload_state(loc), name, before) == name


def _file_name(path: str) -> str:
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1]


def _read_back(loc, kind: dict[str, str] | None, page=None, pf: PlannedField | None = None) -> str:
    try:
        if loc.count() == 0:
            return ""
        if pf is not None and pf.action == "upload" and kind and kind["type"] == "file":
            name = _file_name(pf.value)
            before = _before_upload(page).get(_box_key(pf)) if page is not None else None
            got = upload_read(_upload_state(loc), name, before)
            if got == name and page is not None and before is not None:
                _verified_uploads(page).add(_box_key(pf) + (name,))
            return got
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
          errors: list | None = None, outcomes: list | None = None) -> list[Filled]:
    """Perform every `fill` / `select` / `upload` in `plan` and return the
    read-back value of each acted field. `skip` and `generate` are not acted
    on and do not appear in the result. `deadline` is an instant on `clock`
    (`time.monotonic` by default; the runner passes its own); once it has
    passed the remaining fields are left alone and the list so far comes back
    (the job's wall clock belongs to the caller). A failed action lands in
    `errors` as `{n, label, action, error}` with the error's type name only
    (a Playwright message can quote the value). Before each act the control
    is checked to be the one planned for (`_same_control`, FILL-02). The
    uploads go first and the page settles after them (FILL-03: a resume
    parser writes its guesses then, and the planned values go in after
    them); the result keeps the plan's order. `outcomes` (FILL-15) takes
    one row per acted field: {n, label, action, how, error} (how it was
    acted on, the error's type name when it failed; never the value)."""
    done: dict[int, Filled] = {}
    acted = [pf for pf in plan.fields if pf.action in ("fill", "select", "upload")]
    order = [pf for pf in acted if pf.action == "upload"] + \
        [pf for pf in acted if pf.action != "upload"]
    uploads = len(acted) - sum(1 for pf in acted if pf.action != "upload")
    for i, pf in enumerate(order):
        if i == uploads and 0 < uploads < len(order):
            try:
                settle(page, SETTLE_MAX_S)
            except Exception:       # noqa: BLE001  (a page double; the fill goes on)
                pass
        if deadline is not None and clock() >= deadline:
            _say(log, f"apply_fill: deadline passed before {pf.label!r}; {len(done)} filled")
            break
        loc = None
        kind = None
        how, failed = "", ""
        try:
            loc = apply_form.resolve(page, pf.locator)
            if loc.count() == 0:
                raise LookupError(f"no element at {pf.locator}")
            if pf.action != "upload":
                loc = _same_control(page, pf, loc)
            kind = _kind(loc)
            how = _act(page, pf, loc, kind)
        except Exception as e:      # noqa: BLE001  (the read-back reports the outcome)
            failed = type(e).__name__
            # the type alone: a Playwright message quotes the call, value included
            _say(log, f"apply_fill: {pf.action} on {pf.label!r} ({pf.locator[1]}) failed: "
                      f"{type(e).__name__}")
            if errors is not None:
                errors.append({"n": pf.n, "label": pf.label, "action": pf.action,
                               "error": type(e).__name__})
        value = _read_back(loc, kind, page, pf) if loc is not None else ""
        done[pf.n] = Filled(n=pf.n, label=pf.label, value=value)
        if outcomes is not None:
            outcomes.append({"n": pf.n, "label": pf.label, "action": pf.action, "how": how,
                             "error": failed})
    return [done[pf.n] for pf in acted if pf.n in done]


# a validation message that asks for bare digits
_DIGITS_HINT = re.compile(r"\b(digits?|numbers?\s+only|numeric|numerals?|0\s*-\s*9)\b", re.I)


def repair_value(pf: PlannedField, hint: str, hints: dict | None = None) -> str:
    """The value a refused box takes on its repair (ADV-02): the planned
    value in the shape the form's message asks: a date in the format the
    message (or the box) names, a phone's national digits when the message
    asks for digits, else the value as planned. Only a phone (its fact, or
    a box the hints call a phone) has its digits joined (cycle 18, FM-5):
    "3-5" never becomes 35."""
    value = str(pf.value or "")
    d = parse_date(value)
    fmt = (date_format({"label": hint}) or date_format(hints or {})) if d is not None else ""
    if d is not None and fmt:
        return format_date(d, fmt)
    if _DIGITS_HINT.search(hint or "") and (_phoneish(hints or {}) or pf.fact_key == "phone"):
        return phone_digits(value) or value
    return value


def repair(page, pf: PlannedField, hint: str = "") -> Filled:
    """Put a value the form refused in again (ADV-02): a text box is cleared
    and typed key by key with `repair_value` (a script that checks keys, a
    mask, a format the message names); any other control is acted on its
    own way once more. Returns the read-back."""
    loc = None
    kind = None
    try:
        loc = apply_form.resolve(page, pf.locator)
        if loc.count() == 0:
            raise LookupError(f"no element at {pf.locator}")
        loc = _same_control(page, pf, loc)
        kind = _kind(loc)
        textish = (kind["tag"] == "TEXTAREA" or (kind["tag"] == "INPUT" and kind["type"] not in (
            "checkbox", "radio", "file", "date", "hidden"))) and not pf.widget \
            and kind["role"] not in ("combobox", "listbox")
        if textish and pf.action == "fill":
            try:
                hints = dict(loc.first.evaluate(_HINTS_JS, timeout=ACTION_TIMEOUT_MS) or {})
            except Exception:       # noqa: BLE001
                hints = {}
            value = repair_value(pf, hint, hints)
            if kind["type"] == "number":
                value = number_value(value)
                if value is None:
                    raise LookupError("the number box takes a plain number")
            _typed(loc, value)
        else:
            _act(page, pf, loc, kind)
    except Exception as e:      # noqa: BLE001  (the read-back reports the outcome)
        log.info("apply_fill: repair of %r failed: %s", pf.label, type(e).__name__)
    value = _read_back(loc, kind, page, pf) if loc is not None else ""
    return Filled(n=pf.n, label=pf.label, value=value)


def read_back(page, pf: PlannedField) -> str:
    """What the control `pf` names holds now, read the way `apply` reads it
    after its act ("" when it is gone)."""
    try:
        loc = apply_form.resolve(page, pf.locator)
        if loc.count() == 0:
            return ""
        return _read_back(loc, _kind(loc), page, pf)
    except Exception:       # noqa: BLE001  (a frame or a control gone)
        return ""


# a select's empty choice: the first option with no value or a placeholder's
# words ("Select...", "-- choose --"); -1 when it has none
_EMPTY_OPTION_JS = """el => Array.from(el.options).findIndex(
  (o) => o.value === '' || __PLACEHOLDER__.test((o.text || '').trim()))""".replace(
    "__PLACEHOLDER__", apply_form.PLACEHOLDER_TEXT_JS)
_EMPTY_CHOSEN_JS = "(el, i) => el.selectedIndex === i"
_UNSET_JS = "el => el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ? el.value === '' : true"


def _clear_select(loc, *, hidden: bool) -> bool:
    i = int(loc.first.evaluate(_EMPTY_OPTION_JS, timeout=ACTION_TIMEOUT_MS))
    if i < 0:
        return False
    try:
        loc.first.select_option(index=i, force=hidden, timeout=ACTION_TIMEOUT_MS)
    except Exception:       # noqa: BLE001  (a select the page keeps out of reach)
        value = loc.first.evaluate("(el, i) => el.options[i].value", i,
                                   timeout=ACTION_TIMEOUT_MS)
        loc.first.evaluate(_SET_SELECT_JS, value, timeout=ACTION_TIMEOUT_MS)
    return bool(loc.first.evaluate(_EMPTY_CHOSEN_JS, i, timeout=ACTION_TIMEOUT_MS))


def clear(page, pf: PlannedField) -> bool:
    """Take the answer out of `pf`'s control (cycle 18, FM-4: an optional
    answer that failed its check). A text box, a rich-text box, a
    typeahead and a dropdown's text box are emptied; a select returns to
    its empty or placeholder option; a tick box, a custom tick and a
    question's tick boxes are unticked; date parts are emptied; an upload
    lets its file go. A radio group, a popup menu and a list keep their
    choice. True when the control ends empty (or is gone), False when it
    still holds an answer."""
    try:
        loc = apply_form.resolve(page, pf.locator)
        if loc.count() == 0:
            return True
        kind = _kind(loc)
        widget = pf.widget or ""
        tag, typ, role = kind["tag"], kind["type"], kind["role"]
        if widget in ("choice", "popup") or (not widget and (
                (tag == "INPUT" and typ == "radio") or role in ("combobox", "listbox"))):
            return not _read_back(loc, kind, page, pf)
        if widget == "checkbox_group":
            for css in pf.option_locators:
                target = _clicked(page, pf.locator[0], css) if css else None
                if target is not None and _ticked(target):
                    target.click(timeout=ACTION_TIMEOUT_MS)
        elif widget == "aria_check":
            if _ticked(loc.first):
                loc.first.click(timeout=ACTION_TIMEOUT_MS)
        elif widget == "editable":
            loc.first.click(timeout=ACTION_TIMEOUT_MS)
            loc.first.evaluate(_SELECT_ALL_JS, timeout=ACTION_TIMEOUT_MS)
            page.keyboard.press("Delete")
        elif widget == "combo":
            box = loc.first if tag == "INPUT" else loc.first.locator("input").first
            box.fill("", timeout=ACTION_TIMEOUT_MS)
        elif widget.startswith("date:"):
            for css in pf.option_locators:
                target = _clicked(page, pf.locator[0], css)
                if target.evaluate(_UNSET_JS, timeout=ACTION_TIMEOUT_MS):
                    continue
                target.fill("", timeout=ACTION_TIMEOUT_MS)
        elif widget == "hidden_select" or tag == "SELECT":
            return _clear_select(loc, hidden=widget == "hidden_select")
        elif tag == "INPUT" and typ == "checkbox":
            if pf.click_locator:
                if _ticked(loc.first):
                    _clicked(page, pf.click_locator[0],
                             pf.click_locator[1]).click(timeout=ACTION_TIMEOUT_MS)
            else:
                loc.first.uncheck(timeout=ACTION_TIMEOUT_MS)
        elif tag == "INPUT" and typ == "file":
            loc.first.set_input_files([], timeout=ACTION_TIMEOUT_MS)
            return not loc.first.evaluate("el => el.files ? el.files.length : 0",
                                          timeout=ACTION_TIMEOUT_MS)
        else:
            loc.first.fill("", timeout=ACTION_TIMEOUT_MS)
        return not _read_back(loc, kind, page, pf)
    except Exception:       # noqa: BLE001  (a control that takes no clearing keeps its answer)
        return False


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
    afterwards. `overlay`: what covered the control and how it was put away
    before the click was made once more (ADV-04). `sent`: what the click set
    going, from the click to the end of its wait ("METHOD url", no query): a
    navigation of the page or of the button's frame, or a POST, PUT or PATCH
    from either that the page was not already sending by itself (SP6 review
    I3, R2-I2). Truthiness is `changed`, the shape `click_button`
    returns."""
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


# What covers a control a click was refused on (ADV-04): the element at the
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
# is picked and `own` says so (SP6 review M4; final review B-M6). A fixed
# bar is the application's by where it sits (the covered control's form or
# the box around the control and its fields, final review B R2 M6), a
# dialog by that or by what it holds (two or more fields, or a control
# that applies, uploads or submits). Returns {what, kind:
# consent|close|none, text, own} or null when nothing covers it.
# The loop's send and last-step words (`apply_run.SUBMIT_WORDS` and
# `FINAL_WORDS`) as a JS regex source for a string literal: the overlay
# picker and the click's arm (`_ARM_JS`) splice it (final review B-M4).
_SEND_JS = r"\\b(submit|apply|send|finish|complete|confirm|finali[sz]e|done)\\b"
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
    // the application's own box, never put away (final review B-M6). Any
    // fixed or sticky box is when it is part of the application: in the
    // form of the control it covers, holding a control that form owns (a
    // footer's `form=` submit), or inside the box that holds the control
    // and the application's fields. A dialog is also when it holds fields
    // or an apply, upload or submit control (Workday's "Start Your
    // Application"). A chat's pre-chat form, a talent-network or a
    // job-alert slide-in beside the application is put away, whatever
    // fields it holds (final review B R2 M6)
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
    """ADV-04: put away what covers `target` (an element handle or a
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
# its dispatch, before any handler of the page sees it (INV-04: the check
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
# The page's own requests, logged from the moment the run starts watching it
# (`watch_requests`): the POST, PUT or PATCH pairs a page sends by itself (its
# telemetry, an autosave, a keep-alive) are no evidence a click set anything
# going (SP6 review R2-I2)
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
    overlays: list[dict] = []
    # every navigation, POST, PUT or PATCH from the click to the end of its
    # wait ("METHOD url"): what the click set going (SP6 review I3)
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
        # what the click set going (SP6 review R2-I2): a navigation of the
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
                    if attempt == 1 and frame is not None \
                            and any(w in text for w in _INTERCEPTED) \
                            and not _dispatched(frame, url0, page, requests, e):
                        # ADV-04: a banner, a chat window or a sticky bar took
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
        changed = _await_change(page, _act, timeout_s)
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
