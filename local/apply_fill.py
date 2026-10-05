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
`set_input_files` through its frame. A file control gets no click. A field the extractor
read as a widget (`PlannedField.widget`) is acted on its own way: a
custom radio group, Yes / No buttons and a question's tick boxes by clicking
the option's own element; a dropdown drawn as a button by opening it and
clicking the option; a typeahead or an async combobox by typing the value and
clicking its match; a hidden select in place; a hidden tick box or radio
through its label; a rich-text box by typing; date parts part by part. The
control is checked to be the one planned for before the act.

Uploads go first and the page settles before the rest (a resume
parser's writes land before the planned values); a value the page reshapes
goes in its shape (a masked phone key by key, a phone's national digits, a
text date in the format its box names, a number box's number); an upload is
read from the widget's chip when the input was reset, and never sent twice;
`repair(page, pf, hint)` types a value the form refused again in the shape
its message asks; `apply` reports how it acted on each field (`outcomes`).

A yes or a no picks only an option in its own alias set;
a list the plan read no options for takes no option code cannot match
(`OptionsUnread`), and a yes or a no typed into a list or a
typeahead that offers no option of its own is taken out again, never kept
as the answer, nor matched by an option's hidden value;
a number box takes a plain number only;
`clear(page, pf)` takes an answer out again.

The guarded click and the settle wait (`click`, `settle`, `act_and_settle`,
`clear_overlay`) live in `apply_click`, re-exported here for their readers.
`open_listbox_options(page, field)`
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
from typing import Any, Callable, Iterable

import apply_click
import apply_form
import apply_form_js
import apply_judge
# The click and the settle live in apply_click; their readers reach them here.
from apply_click import (ClickResult, _norm, act_and_settle, checked_live,  # noqa: F401
                         clear_overlay, click, settle, wait_for_change)
from apply_judge import PAGE_TEXT_CAP, FillPlan, PlannedField
from apply_send_words import PopupRefused, popup_refusal

log = logging.getLogger(__name__)

LISTBOX_WAIT_MS = 2_000        # for a combobox menu to render its options
CHECKED_WORDS = ("checked", "yes", "true", "on", "1")
UNCHECKED_WORDS = ("unchecked", "no", "false", "off", "0")
_DATE_SHAPES = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y",
                "%b %d, %Y", "%d %b %Y", "%m-%d-%Y")


class Ticked(str):
    """A question's tick boxes read back: the ticked options joined with
    ", " for the record and the trace, and each ticked option whole in
    `options`, so a check of the pick compares option by option (an option's
    own comma never splits it)."""
    options: tuple[str, ...]

    def __new__(cls, options: Iterable[str]) -> Ticked:
        opts = tuple(str(o) for o in options)
        out = super().__new__(cls, ", ".join(opts))
        out.options = opts
        return out

    def __reduce__(self):
        return (Ticked, (self.options,))


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

# what a password box reads back when it holds anything: its value never
# leaves the page (a read-back can reach a trace or a record)
PASSWORD_MASK = "********"
_READ_JS = """el => {
  const tag = el.tagName, type = (el.getAttribute('type') || '').toLowerCase();
  const role = el.getAttribute('role') || '';
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  if (tag === 'INPUT' && type === 'password') return el.value ? '__MASK__' : '';
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
    // sibling: the single value in the widget's control box
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
}""".replace("__MASK__", PASSWORD_MASK)

_RADIO_LABELS_JS = "els => els.map(" + apply_form.RADIO_OPTION_LABEL_JS + ")"

_CHECKED_INDEX_JS = "els => els.findIndex(el => el.checked)"

_SELECT_OPTIONS_JS = "el => Array.from(el.options).map(o => [o.text.trim(), o.value])"


def _say(log_fn: Callable[[str], Any] | None, msg: str) -> None:
    if log_fn is not None:
        log_fn(msg)
    else:
        log.info(msg)


def _kind(loc) -> dict[str, str]:
    return loc.first.evaluate(_KIND_JS, timeout=apply_click.ACTION_TIMEOUT_MS)


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


# A text box's own hints: its type, placeholder, pattern,
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
    """A number box's value: `value` when it is a plain number
    ("3", "1.5"), else None ("$120,000", "5+", "3-5" and
    "120k" are no number, and the box is left blank)."""
    text = str(value or "")
    return text.strip() if apply_judge.PLAIN_NUMBER.match(text) else None


# `_ci_match`'s answer when the options that hold a value tie and differ in
# meaning: no option is chosen
OPTION_TIE = -2


class OptionTie(LookupError):
    """The options that hold the value tie and differ in meaning
    (`OPTION_TIE`): none is chosen, a typeahead's box is cleared, and the
    runner parks a required box and leaves an optional one blank."""


class OptionsUnread(LookupError):
    """A list whose options were never read ahead (the plan holds none, so
    no judge picked among them) shows none that code matches to the value,
    or a yes or a no finds no option of its own alias set
    in a list or a typeahead, whatever it showed: none is
    chosen, the words typed to bring the options are taken out, and the
    runner parks a required box and leaves an optional one blank."""


class NothingShown(LookupError):
    """A list that showed no option, neither when clicked nor when typed
    in (`_pick_listbox`)."""


# the words that turn an option against a value that lacks them: a negation
# ("Not Hispanic or Latino" for "Latino") or a qualifier ("Yes, but I will
# require sponsorship" for "Yes")
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
    USA for United States, CA for California, a decline for a decline),
    else the closest one that holds it (`_holds`: "no" is never
    inside "None"), else -1.

    A negation or a qualifier the value lacks (`_qualifiers`) only breaks a
    tie between options that each hold the value; it never puts out a lone one ("Not Hispanic or Latino" for
    "Latino" beside "White"). Among two or more, the ones that turn the
    value are out while one that leaves it unturned is left ("Hispanic or
    Latino" over "Not Hispanic or Latino" for "Latino"). When every one of
    them turns it, and they are not all the same words, they tie
    (`OPTION_TIE`): the value names nothing that tells their turns apart. A stored decline takes the one option
    that declines in its own words ("Prefer not to say"). The closest names
    each of the value's comma parts as one of its own ("Chicago, IL" for
    "chicago", never "Chicago Heights, IL"; `_names_each`); among such equal
    fits ("Anytown, California" and "Anytown, New York" for "anytown") the
    site's own order stands: the value names nothing
    that sets them apart, and the site's first match is what typing it gives
    a person. Two or more that only hold the value ("Software Engineering"
    and "Hardware Engineering" for "Engineering") differ in what the value
    leaves out: `OPTION_TIE`.

    A yes or a no (`apply_judge.yes_no`) matches only an option in its own
    alias set: "Yes" is none of "Yes - on a work visa" and
    "Yes, without sponsorship", and a qualified option is the judge's pick.
    Two or more different options that each hold it and each turn it still
    tie (`OPTION_TIE`)."""
    w = " ".join((want or "").split()).lower()
    folded = [" ".join(str(c).split()).lower() for c in candidates]
    if w in folded:
        return folded.index(w)
    mine = _qualifiers(w)
    if apply_judge.yes_no(want):
        texts = [str(c) for c in candidates]
        found = apply_judge.match_option(want, texts)
        if found is not None:
            return texts.index(found)
        held = [i for i, c in enumerate(folded) if _holds([w], c)]
        turned = bool(held) and all(_qualifiers(folded[i]) - mine for i in held)
        return OPTION_TIE if turned and len({folded[i] for i in held}) > 1 else -1
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
    (implicit submission)."""
    text = str(text or "")
    try:
        tag = str(loc.first.evaluate("el => el.tagName", timeout=apply_click.ACTION_TIMEOUT_MS) or "")
    except Exception:       # noqa: BLE001  (a box gone: the typing finds out)
        tag = ""
    return text if tag.upper() == "TEXTAREA" else _LINE_BREAK.sub(" ", text)


# key-by-key typing: the pause between keys for a short text (a mask's
# script reads each key), and the time each key may take on top of the
# action's own timeout (a 600-character answer outlasts a flat 5 s)
KEY_DELAY_MS = 15
KEY_DELAY_MAX_KEYS = 200        # longer text is typed with no pause between keys
KEY_BUDGET_MS = 40


def _typed(loc, text: str) -> None:
    """Clear the box and type `text` key by key (a masked box takes keys,
    never a pasted value); a line break is a space outside a TEXTAREA
    (`_keys_for`). The typing's timeout grows with the text, so a long
    answer is typed whole."""
    keys = _keys_for(loc, text)
    delay = KEY_DELAY_MS if len(keys) <= KEY_DELAY_MAX_KEYS else 0
    loc.first.fill("", timeout=apply_click.ACTION_TIMEOUT_MS)
    loc.first.press_sequentially(keys, delay=delay,
                                 timeout=apply_click.ACTION_TIMEOUT_MS + len(keys) * (KEY_BUDGET_MS + delay))


def _fill(loc, kind: dict[str, str], value: str) -> str:
    """Type `value` into a text-like box in the shape the box asks for: a
    native date control ISO; a number box a plain number (the
    phone's digits in a box named for a phone), and nothing
    else (LookupError); a
    text box whose hints name a date format that format ("Date
    (MM/DD/YYYY)"); a phone box its national digits when a country-code
    control sits on its row or its pattern or length asks for bare digits,
    typed key by key into a masked box, and typed again key by key when a
    mask left the box holding other digits. Returns how it acted."""
    if kind["tag"] == "INPUT" and kind["type"] == "date":
        loc.first.fill(_date_value(value), timeout=apply_click.ACTION_TIMEOUT_MS)
        return "date"
    if kind["tag"] == "INPUT" and kind["type"] == "number":
        number = number_value(value)
        if number is None and len(phone_digits(value)) >= 7:
            # the phone's digits, only in a box whose label or name say phone
            try:
                hints = dict(loc.first.evaluate(_HINTS_JS, timeout=apply_click.ACTION_TIMEOUT_MS) or {})
            except Exception:       # noqa: BLE001  (no hints: no phone box)
                hints = {}
            if apply_judge.phone_named(*(hints.get(k) for k in ("label", "aria", "name"))):
                number = phone_digits(value)
        if number is None:
            raise LookupError("the number box takes a plain number")
        loc.first.fill(number, timeout=apply_click.ACTION_TIMEOUT_MS)
        return "number"
    if kind["tag"] != "INPUT" or kind["type"] in ("checkbox", "radio", "file", "password"):
        loc.first.fill(value, timeout=apply_click.ACTION_TIMEOUT_MS)
        return "fill"
    try:
        hints = dict(loc.first.evaluate(_HINTS_JS, timeout=apply_click.ACTION_TIMEOUT_MS) or {})
    except Exception:       # noqa: BLE001  (the fill finds out)
        hints = {}
    d = parse_date(value)
    fmt = date_format(hints) if d is not None else ""
    if fmt:
        loc.first.fill(format_date(d, fmt), timeout=apply_click.ACTION_TIMEOUT_MS)
        return f"date as {fmt}"
    digits = phone_digits(value)
    if not _phoneish(hints) or len(digits) < 7:
        loc.first.fill(value, timeout=apply_click.ACTION_TIMEOUT_MS)
        return "fill"
    bare = _DIGITS_ONLY.search(str(hints.get("pattern") or ""))
    national = bool(hints.get("cc")) or bool(bare) or hints.get("maxlength") == len(digits)
    text = digits if national else value
    if _masked(hints):
        _typed(loc, digits)
        return "phone digits key by key"
    loc.first.fill(text, timeout=apply_click.ACTION_TIMEOUT_MS)
    how = "phone national digits" if national else "fill"
    try:
        now = str(loc.first.input_value(timeout=apply_click.ACTION_TIMEOUT_MS) or "")
    except Exception:       # noqa: BLE001  (the read-back finds out)
        return how
    if phone_digits(now) != digits:
        # a mask that takes keys only, or mangled the pasted value
        _typed(loc, digits)
        return "phone digits key by key"
    return how


def _by_label_then_value(want: str, labels: list[str], values: list[str]) -> list:
    """The tries for an option's pick: its words, then its hidden value;
    a yes or a no by the words alone (value="yes" behind
    "Yes - on a work visa" is no Yes)."""
    tries = [(want, labels)]
    if not apply_judge.yes_no(want):
        tries.append((want, values))
    return tries


def _select_native(loc, want: str) -> None:
    options = loc.first.evaluate(_SELECT_OPTIONS_JS, timeout=apply_click.ACTION_TIMEOUT_MS)
    labels = [o[0] for o in options]
    i = _ci_first(_by_label_then_value(want, labels, [o[1] for o in options]))
    if i < 0:
        raise _no_option(want, i, labels)
    loc.first.select_option(value=options[i][1], timeout=apply_click.ACTION_TIMEOUT_MS)


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
        return bool(loc.evaluate(_TICKED_JS, timeout=apply_click.ACTION_TIMEOUT_MS))
    except Exception:       # noqa: BLE001  (gone)
        return False


def _check_radio(page, loc, want: str, pf: PlannedField | None = None) -> None:
    """Choose the radio that reads `want`. With the extractor's option
    locators (one per shown, enabled radio, in `pf.options` order) the
    option is matched among `pf.options`, so a hidden or disabled radio of
    the same name never shifts the pick onto its neighbour; else among the
    live labels of every radio `loc` names."""
    if pf is not None and pf.option_locators and len(pf.option_locators) == len(pf.options):
        _check_radio_by_option(page, loc, want, pf)
        return
    labels = loc.evaluate_all(_RADIO_LABELS_JS)
    i = _ci_match(want, labels)
    if i < 0 and not apply_judge.yes_no(want):
        # a yes or a no by the words alone: value="yes"
        # behind "Yes - on a work visa" is no Yes
        values = loc.evaluate_all("els => els.map(e => e.value)")
        j = _ci_match(want, values)
        i = j if j >= 0 or i == -1 else i       # a tie among the labels stands
    if i < 0:
        raise _no_option(want, i, labels)
    loc.nth(i).check(timeout=apply_click.ACTION_TIMEOUT_MS)


def _check_radio_by_option(page, loc, want: str, pf: PlannedField) -> None:
    """`_check_radio` through the extractor's option locators: `want` among
    `pf.options`, else (never for a yes or a no) by a live radio's value,
    taken back to its own label among `pf.options`. The option's locator (a
    hidden native radio's label, else the radio) takes the click."""
    options = list(pf.options)
    i = _ci_match(want, options)
    if i < 0 and i != OPTION_TIE and not apply_judge.yes_no(want):
        labels = loc.evaluate_all(_RADIO_LABELS_JS)
        values = loc.evaluate_all("els => els.map(e => e.value)")
        j = _ci_match(want, values)
        if 0 <= j < len(labels):
            hits = [k for k, o in enumerate(options) if _norm(o) == _norm(labels[j])]
            i = hits[0] if len(hits) == 1 else -1
    if i < 0:
        raise _no_option(want, i, options)
    css = pf.option_locators[i]
    if not css:
        raise LookupError(f"the option {options[i]!r} has no locator")
    target = _clicked(page, pf.locator[0], css)
    if not _ticked(target):
        target.click(timeout=apply_click.ACTION_TIMEOUT_MS)


def _check_box(page, loc, want: str, pf: PlannedField | None = None) -> None:
    w = (want or "").strip().lower()
    if w not in CHECKED_WORDS and w not in UNCHECKED_WORDS:
        raise LookupError(f"checkbox option {want!r} is neither checked nor unchecked")
    tick = w in CHECKED_WORDS
    if pf is not None and pf.click_locator:
        # a hidden or see-through box behind its label: the label
        # takes the click, the box's own state is read
        if _ticked(loc.first) != tick:
            _clicked(page, pf.click_locator[0], pf.click_locator[1]).click(timeout=apply_click.ACTION_TIMEOUT_MS)
        return
    if tick:
        loc.first.check(timeout=apply_click.ACTION_TIMEOUT_MS)
    else:
        loc.first.uncheck(timeout=apply_click.ACTION_TIMEOUT_MS)


# a form's submit control: a <button> with no type or type=submit inside a
# form, an input[type=submit]; `_choose` never clicks one
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
    also every option the value names ("Python, SQL": each is
    ticked), in the options' order. A value that is one option's whole
    name is that option alone ("Research and Development" never ticks
    Research and Development apart)."""
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
    tick boxes (widget "checkbox_group", each chosen option ticked and every
    other one the page ticked before unticked): the option's own element
    takes the click, unless it is already as wanted, and never when it is a
    form's submit control."""
    picked = _chosen(pf, want)
    if not picked or any(i >= len(pf.option_locators) for i in picked):
        raise LookupError(f"no option {want!r} among {list(pf.options)}")
    if pf.widget == "checkbox_group":
        for i, css in enumerate(pf.option_locators):
            if i in picked or not css:
                continue
            target = _clicked(page, pf.locator[0], css)
            if _ticked(target) and not target.evaluate(_SUBMITS_JS, timeout=apply_click.ACTION_TIMEOUT_MS):
                target.click(timeout=apply_click.ACTION_TIMEOUT_MS)
    for i in picked:
        target = _clicked(page, pf.locator[0], pf.option_locators[i])
        if target.evaluate(_SUBMITS_JS, timeout=apply_click.ACTION_TIMEOUT_MS):
            raise LookupError(f"the option {pf.options[i]!r} is a form's submit control")
        if not _ticked(target):
            target.click(timeout=apply_click.ACTION_TIMEOUT_MS)


def _aria_check(loc, want: str) -> None:
    w = (want or "").strip().lower()
    if w not in CHECKED_WORDS and w not in UNCHECKED_WORDS:
        raise LookupError(f"checkbox option {want!r} is neither checked nor unchecked")
    if _ticked(loc.first) != (w in CHECKED_WORDS):
        loc.first.click(timeout=apply_click.ACTION_TIMEOUT_MS)


def _css_quoted(text: str) -> str:
    """`text` for a CSS attribute value in double quotes: its backslashes
    and double quotes escaped."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _options_locator(frame, loc):
    """The `[role=option]` entries a combobox shows: the listbox its
    `aria-controls` / `aria-owns` names when set, else any visible listbox in
    the frame (React-select portals its menu to the body)."""
    for attr in ("aria-controls", "aria-owns"):
        ref = loc.first.get_attribute(attr, timeout=apply_click.ACTION_TIMEOUT_MS)
        if ref:
            target = frame.locator(f'[id="{_css_quoted(ref.split()[0])}"] [role=option]')
            if target.count():
                return target
    return frame.locator("[role=listbox] [role=option]").filter(visible=True)


# the entries a dropdown drawn as a button shows: a listbox's
# options or a menu's radio items, visible
_MENU_OPTIONS = ("[role=option], [role=menuitemradio], [role=menuitem], "
                 "[role=menuitemcheckbox]")
_MENU_PARTS = tuple(p.strip() for p in _MENU_OPTIONS.split(","))
# the menu entries that already show before a popup's click (a site's menu
# bar, another open list) are marked, so the popup's options are the ones its
# click brought; `clear` only takes earlier marks away
_MARK_SHOWN_JS = """([sel, clear]) => {
  document.querySelectorAll('[data-apply-shown]').forEach((n) => n.removeAttribute('data-apply-shown'));
  if (clear) return 0;
  let marked = 0;
  for (const n of document.querySelectorAll(sel)) {
    const st = getComputedStyle(n), r = n.getBoundingClientRect();
    if (st.display !== 'none' && st.visibility !== 'hidden' && (r.width > 0 || r.height > 0)) {
      n.setAttribute('data-apply-shown', '1');
      marked += 1;
    }
  }
  return marked;
}"""


def _mark_shown(frame, *, clear: bool = False) -> None:
    """Mark the menu entries that show now (`_MARK_SHOWN_JS`); `clear` only
    takes earlier marks away."""
    try:
        frame.evaluate(_MARK_SHOWN_JS, [_MENU_OPTIONS, bool(clear)])
    except Exception:       # noqa: BLE001  (a frame double: nothing marked)
        pass


def _popup_options(frame, loc):
    """The entries a popup shows: the menu its `aria-controls` / `aria-owns`
    names when set, else the visible entries its click brought (none that
    `_mark_shown` marked before it)."""
    for attr in ("aria-controls", "aria-owns"):
        ref = loc.first.get_attribute(attr, timeout=apply_click.ACTION_TIMEOUT_MS)
        if ref:
            box = f'[id="{_css_quoted(ref.split()[0])}"]'
            target = frame.locator(", ".join(f"{box} {p}, {box}{p}" for p in _MENU_PARTS))
            if target.count():
                return target.filter(visible=True)
    return frame.locator(", ".join(f"{p}:not([data-apply-shown])" for p in _MENU_PARTS)) \
        .filter(visible=True)


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


def _open_menu(frame, loc, *, popup: bool = False, face=None):
    """Click the combobox (or `face`, the box a person clicks for it:
    react-select's dummy input) unless its menu is already open; return the
    visible options locator, or None when nothing rendered within
    LISTBOX_WAIT_MS. The control and the face are read on their element
    handles just before the click, and the handle read is the one clicked:
    one whose own words send is never opened, whatever the extractor made of
    it (`PopupRefused`)."""
    expanded = loc.first.get_attribute("aria-expanded", timeout=apply_click.ACTION_TIMEOUT_MS)
    if popup:
        _mark_shown(frame, clear=expanded == "true")
    if expanded != "true":
        handles = [loc.first.element_handle(timeout=apply_click.ACTION_TIMEOUT_MS)]
        if face is not None:
            handles.append(face.element_handle(timeout=apply_click.ACTION_TIMEOUT_MS))
        try:
            for handle in handles:
                why = popup_refusal(dict(handle.evaluate(_POPUP_WORDS_JS)))
                if why:
                    raise PopupRefused(why)
            handles[-1].click(timeout=apply_click.ACTION_TIMEOUT_MS)
        finally:
            for handle in handles:
                try:
                    handle.dispose()
                except Exception:   # noqa: BLE001
                    pass
    options = _popup_options(frame, loc) if popup else _options_locator(frame, loc)
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
    """Escape only while a menu of options shows: its options
    (`options`, the locator the open returned) are visible, and the control
    does not say `aria-expanded="false"`. A box that says
    `aria-expanded="true"` with nothing shown (a typeahead waiting for keys)
    gets no Escape: with no menu to take it, an Escape closes the dialog
    the form lives in."""
    if not _menu_showing(options):
        return
    try:
        expanded = loc.first.get_attribute("aria-expanded", timeout=apply_click.ACTION_TIMEOUT_MS)
    except Exception:       # noqa: BLE001  (gone: no menu of its own to close)
        return
    if expanded != "false":
        page.keyboard.press("Escape")


_TYPEABLE_JS = """el => {
  const box = el.matches('input, textarea') ? el : el.querySelector('input');
  return !!box && !box.readOnly && !box.disabled;
}"""


def _type_to_filter(page, frame, loc, want: str):
    """An async combobox that shows nothing until typed in
    (Greenhouse's location and school, Ashby's location): the value is typed,
    and the options it brings are waited for."""
    try:
        if not loc.first.evaluate(_TYPEABLE_JS, timeout=apply_click.ACTION_TIMEOUT_MS):
            return None
    except Exception:       # noqa: BLE001
        return None
    target = loc.first if (loc.first.evaluate("el => el.tagName") or "") == "INPUT" \
        else loc.first.locator("input").first
    keys = _keys_for(target, str(want or "").split(",")[0].strip())
    target.fill("", timeout=apply_click.ACTION_TIMEOUT_MS)
    target.press_sequentially(keys, delay=10, timeout=apply_click.ACTION_TIMEOUT_MS)
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
        raise NothingShown("the listbox showed no options")
    texts = [t.strip() for t in options.all_inner_texts()]
    i = _ci_match(want, texts)
    if i < 0:
        _close_menu(page, loc, options)
        raise _no_option(want, i, texts)
    try:
        options.nth(i).click(timeout=apply_click.ACTION_TIMEOUT_MS)
    except Exception:       # noqa: BLE001  (the menu closed under the click: open it once more)
        again = _open_menu(frame, loc, popup=popup, face=face)
        if again is None:
            raise
        texts = [t.strip() for t in again.all_inner_texts()]
        i = _ci_match(want, texts)
        if i < 0:
            raise
        again.nth(i).click(timeout=apply_click.ACTION_TIMEOUT_MS)


# The matches a typeahead offers under its box (Lever's location has
# no ARIA): the visible entries of the nearest results list around it, each
# marked for the click. An earlier typeahead's marks are cleared first, in
# the document and every open shadow root: the click's locator reaches into
# shadow roots and takes the first mark it meets.
_TYPEAHEAD_OPTIONS_JS = """el => {
  const clear = (root) => {
    root.querySelectorAll('[data-apply-option]')
      .forEach((n) => n.removeAttribute('data-apply-option'));
    root.querySelectorAll('*').forEach((n) => { if (n.shadowRoot) clear(n.shadowRoot); });
  };
  clear(el.ownerDocument || document);
  const visible = __VISIBLE__;
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
}""".replace("__VISIBLE__", apply_form_js.VISIBLE_FN_JS)


def _type_ahead(page, frame, loc, value: str) -> None:
    """Type the value into a typeahead, wait for its matches and
    click the one that fits; with no match the typed value stays (a place
    the site does not list). Matches that tie and differ in meaning
    (`OPTION_TIE`) leave the box empty and raise `OptionTie`. A yes or a no that finds no option of its own alias set,
    whether the box showed matches or none, leaves the box empty and raises
    `OptionsUnread`: a typed "No" is no answer
    beside "No, I do not require sponsorship"."""
    keys = _keys_for(loc, value)
    loc.first.fill("", timeout=apply_click.ACTION_TIMEOUT_MS)
    loc.first.press_sequentially(keys, delay=10, timeout=apply_click.ACTION_TIMEOUT_MS)
    deadline = time.monotonic() + LISTBOX_WAIT_MS / 1000
    texts: list[str] = []
    while time.monotonic() < deadline:
        texts = list(loc.first.evaluate(_TYPEAHEAD_OPTIONS_JS, timeout=apply_click.ACTION_TIMEOUT_MS) or [])
        if texts:
            break
        page.wait_for_timeout(100)
    first = (value or "").split(",")[0].strip()
    i = _ci_first([(value, texts), (first, texts)]) if texts else -1
    if i == OPTION_TIE:
        loc.first.fill("", timeout=apply_click.ACTION_TIMEOUT_MS)
        raise _no_option(value, i, texts)
    if i < 0:
        if apply_judge.yes_no(value):
            loc.first.fill("", timeout=apply_click.ACTION_TIMEOUT_MS)
            raise OptionsUnread(f"no option of the typeahead is {value!r}: {texts}")
        return
    frame.locator(f'[data-apply-option="{i}"]').first.click(timeout=apply_click.ACTION_TIMEOUT_MS)


_SET_SELECT_JS = """(el, value) => {
  el.value = value;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return el.value === value;
}"""


def _select_hidden(loc, want: str) -> None:
    """A hidden <select> behind a styled trigger: picked in place,
    forced past the actionability check, else set and announced by script."""
    options = loc.first.evaluate(_SELECT_OPTIONS_JS, timeout=apply_click.ACTION_TIMEOUT_MS)
    labels = [o[0] for o in options]
    i = _ci_first(_by_label_then_value(want, labels, [o[1] for o in options]))
    if i < 0:
        raise _no_option(want, i, labels)
    try:
        loc.first.select_option(value=options[i][1], force=True, timeout=apply_click.ACTION_TIMEOUT_MS)
    except Exception:       # noqa: BLE001  (a select the page keeps out of reach)
        loc.first.evaluate(_SET_SELECT_JS, options[i][1], timeout=apply_click.ACTION_TIMEOUT_MS)


_SELECT_ALL_JS = """el => {
  el.focus();
  const r = document.createRange();
  r.selectNodeContents(el);
  const s = window.getSelection();
  s.removeAllRanges();
  s.addRange(r);
}"""


def _fill_editable(page, loc, value: str) -> None:
    """A rich-text box: focused, its content selected, the text
    inserted as typing would."""
    loc.first.click(timeout=apply_click.ACTION_TIMEOUT_MS)
    loc.first.evaluate(_SELECT_ALL_JS, timeout=apply_click.ACTION_TIMEOUT_MS)
    page.keyboard.insert_text(value)


def _date_parts(value: str) -> dict[str, str]:
    iso = _date_value(value)
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError as e:
        raise LookupError(f"not a date: {value!r}") from e
    return {"M": f"{d.month:02d}", "D": f"{d.day:02d}", "Y": f"{d.year:04d}"}


def _fill_date_parts(page, pf: PlannedField, value: str) -> None:
    """Month / Day / Year boxes: each part typed into its own box, in
    the order the widget names ("date:MDY")."""
    parts = _date_parts(value)
    order = pf.widget.split(":", 1)[1] if ":" in pf.widget else ""
    for kind, css in zip(order, pf.option_locators):
        target = _clicked(page, pf.locator[0], css)
        if (target.evaluate("el => el.tagName", timeout=apply_click.ACTION_TIMEOUT_MS) or "") == "INPUT":
            target.fill(parts[kind], timeout=apply_click.ACTION_TIMEOUT_MS)
        else:
            target.click(timeout=apply_click.ACTION_TIMEOUT_MS)
            page.keyboard.insert_text(parts[kind])


def _upload(loc, path: str) -> None:
    """Put the file at `path` in the file box `loc` names (the control
    `_same_control` confirmed, found through `apply_form.resolve`)."""
    loc.first.set_input_files(path, timeout=apply_click.ACTION_TIMEOUT_MS)


def _take_out_typed(loc) -> None:
    """Empty the text box of a list (the control itself or the input inside
    it) when it holds words: the ones typed to bring its options."""
    try:
        tag = loc.first.evaluate("el => el.tagName", timeout=apply_click.ACTION_TIMEOUT_MS) or ""
        box = loc.first if tag == "INPUT" else loc.first.locator("input").first
        if box.count() and str(box.input_value(timeout=apply_click.ACTION_TIMEOUT_MS) or ""):
            box.fill("", timeout=apply_click.ACTION_TIMEOUT_MS)
    except Exception:       # noqa: BLE001  (the read-back reports what stays)
        pass


def _pick_unread(pf: PlannedField, loc, want: str, pick: Callable[[], None]) -> None:
    """`pick` `want` among a list's options. The misses that leave no typed
    guess behind, the words typed to bring the options
    taken out of the box first:

    - a yes or a no (`apply_judge.yes_no`) that no option matches exactly
      or by its alias set, whether the list showed nothing (`NothingShown`),
      only other options, or options that tie: the error
      is `OptionsUnread` on a list whose options the plan never read
      (`pf.options` empty), else its own (a tie's `OptionTie`; a plain miss
      on a list read ahead, whose empty box then fails its check);
    - any other value on a list whose options the plan never read that
      shows options none of which code matches: `OptionsUnread`.

    A refused popup, and any other value's tie or list that shows nothing,
    keep their own errors (a place typed into a list that shows nothing
    stays and is checked, as before)."""
    try:
        pick()
    except LookupError as e:
        if isinstance(e, PopupRefused):
            raise
        if not apply_judge.yes_no(want) and (
                pf.options or isinstance(e, (OptionTie, NothingShown))):
            raise
        _take_out_typed(loc)
        if pf.options or isinstance(e, OptionTie):
            raise
        raise OptionsUnread(f"no option of {pf.label!r} is its answer in code") from e


def _act(page, pf: PlannedField, loc, kind: dict[str, str]) -> str:
    want = pf.option if pf.option is not None else pf.value
    tag, typ, role = kind["tag"], kind["type"], kind["role"]
    if pf.action == "upload":
        if upload_shown(page, loc, pf):
            # this run put the file in this box on this page, the chip showed
            # it, and it still does: a second upload would attach it twice
            log.info("apply_fill: %r already holds %s from this run; not uploaded again",
                     pf.label, _file_name(pf.value))
            return "upload already shown"
        # the box's words before this run's upload: a file of the same name
        # the page showed already (a stored resume) never counts as this one
        _before_upload(page)[_box_key(pf)] = _upload_state(loc).get("text", "")
        _upload(loc, pf.value)
        return "upload"
    if pf.action not in ("fill", "select"):
        return ""
    frame = apply_form.frames(page)[int(pf.locator[0])]
    widget = pf.widget or ""
    if widget in ("choice", "checkbox_group"):
        _choose(page, pf, want)
        return "option click"
    if widget == "popup":
        _pick_unread(pf, loc, want, lambda: _pick_listbox(page, frame, loc, want, popup=True))
        return "menu pick"
    if widget == "combo":
        typed = pf.value if pf.action == "fill" else want
        _pick_unread(pf, loc, typed, lambda: _pick_listbox(page, frame, loc, typed))
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
        _pick_unread(pf, loc, want, lambda: _pick_listbox(page, frame, loc, want, face=face))
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
        return Ticked(chosen) if widget == "checkbox_group" else (chosen[0] if chosen else "")
    if widget == "popup":
        return str(loc.first.evaluate(_POPUP_READ_JS, timeout=apply_click.ACTION_TIMEOUT_MS) or "")
    if widget == "combo":
        return str(loc.first.evaluate(_COMBO_READ_JS, timeout=apply_click.ACTION_TIMEOUT_MS) or "")
    if widget == "aria_check":
        return "checked" if _ticked(loc.first) else ""
    if widget == "editable":
        return str(loc.first.evaluate(_EDITABLE_READ_JS, timeout=apply_click.ACTION_TIMEOUT_MS) or "")
    if widget.startswith("date:"):
        order = widget.split(":", 1)[1]
        got = {k: str(_clicked(page, pf.locator[0], css).evaluate(
            "el => (el.value !== undefined ? el.value : el.innerText) || ''",
            timeout=apply_click.ACTION_TIMEOUT_MS)).strip() for k, css in zip(order, pf.option_locators)}
        if not all(got.values()):
            return ""
        return f"{got.get('M', '')}/{got.get('D', '')}/{got.get('Y', '')}"
    return ""


# An upload's read-back: the input's file, else the widget's chip
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
        return dict(loc.first.evaluate(_FILE_STATE_JS, timeout=apply_click.ACTION_TIMEOUT_MS) or {})
    except Exception:       # noqa: BLE001  (gone)
        return {}


def upload_read(state: dict, name: str, before: str | None) -> str:
    """An upload's read-back: the box's own file,
    else the file's name when the box's words show it more often than
    before this run's upload (a chip the upload added), a success note that
    was not there before, or words that changed and still show it with no
    failure among them (a widget that replaced a kept chip of the same
    name); "" when none. With no upload by this run (`before`
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
    """May the upload of `pf` be skipped? Only when this run put
    that file in that box on this page and saw it verified
    (`_verified_uploads`), and the box still shows it. A file of the same
    name the page showed before this run's upload (a resume kept from an
    earlier application) never counts."""
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
        return str(loc.first.evaluate(_READ_JS, timeout=apply_click.ACTION_TIMEOUT_MS) or "")
    except Exception:       # noqa: BLE001
        return ""


# Who the control is now: `apply_form.IDENT_FN_JS`, the extractor's
# own identity, read on the live element; and every element of the frame,
# open shadow roots walked, whose identity names the same control
# (`apply_form.SAME_IDENT_JS`): how a control the page moved is found again.
_FIND_IDENT_JS = r"""(_root, want) => {
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
  // the control whose identity is the planned one exactly comes before one
  // whose label only starts with it ("Phone" before "Phone extension")
  const exact = out.filter((el) => identOf(el) === want);
  const pick = exact.length === 1 ? exact : out;
  if (pick.length === 1) pick[0].setAttribute('data-apply-found', '1');
  return pick.length;
}""".replace("__IDENT__", apply_form.IDENT_FN_JS).replace("__SAME__", apply_form.SAME_IDENT_JS)
_IDENTITY_BLIND = ("choice", "checkbox_group")      # a group's locator names no one control


def _same_control(page, pf: PlannedField, loc):
    """The control `pf` was planned for: the live element at its
    locator when its identity is the one the extractor read; else the one
    element of the frame with exactly that identity (a "Phone" box moved
    under its "Phone extension" neighbour is found again); else the live
    element when its identity is the planned one with more label words
    (a "required" the label gained); else a LookupError: a value is never
    typed into another box. A group's locator names no one control, and a
    radio group's names every radio: neither is checked. An identity that
    cannot be read is refused as well."""
    if not pf.ident or pf.widget in _IDENTITY_BLIND or "[type=radio]" in str(pf.locator[1]):
        return loc
    try:
        live = str(loc.first.evaluate(apply_form.IDENT_FN_JS, timeout=apply_click.ACTION_TIMEOUT_MS))
    except Exception as e:      # noqa: BLE001  (detached, or a frame gone)
        raise LookupError(f"the control at {pf.locator[1]} cannot be read "
                          f"({type(e).__name__})") from None
    if live == pf.ident:
        return loc
    root = apply_form.resolve(page, (pf.locator[0], ":root"))
    found = root.first.evaluate(_FIND_IDENT_JS, pf.ident, timeout=apply_click.ACTION_TIMEOUT_MS)
    if found == 1:
        log.info("apply_fill: %r moved; found again by its identity", pf.label)
        return apply_form.resolve(page, (pf.locator[0], "[data-apply-found='1']"))
    if apply_form.same_ident(live, pf.ident):
        return loc
    raise LookupError(f"the control at {pf.locator[1]} is another one now "
                      f"({found} match its identity)")


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
    is checked to be the one planned for (`_same_control`). The
    uploads go first and the page settles after them (a resume
    parser writes its guesses then, and the planned values go in after
    them); the result keeps the plan's order. `outcomes` takes
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
                settle(page, apply_click.SETTLE_MAX_S)
            except Exception:       # noqa: BLE001  (a page double; the fill goes on)
                pass
        if deadline is not None and clock() >= deadline:
            _say(log, f"apply_fill: deadline passed before {pf.label!r}; {len(done)} filled")
            break
        loc = None
        kind = None
        how, failed = "", ""
        try:
            # `loc` is set once the control is confirmed: a box that is
            # another one now is never read back (its neighbour's value
            # would pass for this field's)
            found = apply_form.resolve(page, pf.locator)
            if found.count() == 0:
                raise LookupError(f"no element at {pf.locator}")
            loc = _same_control(page, pf, found)
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
    """The value a refused box takes on its repair: the planned
    value in the shape the form's message asks: a date in the format the
    message (or the box) names, a phone's national digits when the message
    asks for digits, else the value as planned. Only a phone (its fact, or
    a box the hints call a phone) has its digits joined:
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
    """Put a value the form refused in again: a text box is cleared
    and typed key by key with `repair_value` (a script that checks keys, a
    mask, a format the message names); any other control is acted on its
    own way once more. Returns the read-back."""
    loc = None
    kind = None
    try:
        found = apply_form.resolve(page, pf.locator)
        if found.count() == 0:
            raise LookupError(f"no element at {pf.locator}")
        loc = _same_control(page, pf, found)       # unset when it is another one now
        kind = _kind(loc)
        textish = (kind["tag"] == "TEXTAREA" or (kind["tag"] == "INPUT" and kind["type"] not in (
            "checkbox", "radio", "file", "date", "hidden"))) and not pf.widget \
            and kind["role"] not in ("combobox", "listbox")
        if textish and pf.action == "fill":
            try:
                hints = dict(loc.first.evaluate(_HINTS_JS, timeout=apply_click.ACTION_TIMEOUT_MS) or {})
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
    after its act ("" when it is gone, or when the control at its locator
    is another one now and its own is not found: `_same_control`)."""
    try:
        loc = apply_form.resolve(page, pf.locator)
        if loc.count() == 0:
            return ""
        loc = _same_control(page, pf, loc)
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
    i = int(loc.first.evaluate(_EMPTY_OPTION_JS, timeout=apply_click.ACTION_TIMEOUT_MS))
    if i < 0:
        return False
    try:
        loc.first.select_option(index=i, force=hidden, timeout=apply_click.ACTION_TIMEOUT_MS)
    except Exception:       # noqa: BLE001  (a select the page keeps out of reach)
        value = loc.first.evaluate("(el, i) => el.options[i].value", i,
                                   timeout=apply_click.ACTION_TIMEOUT_MS)
        loc.first.evaluate(_SET_SELECT_JS, value, timeout=apply_click.ACTION_TIMEOUT_MS)
    return bool(loc.first.evaluate(_EMPTY_CHOSEN_JS, i, timeout=apply_click.ACTION_TIMEOUT_MS))


def clear(page, pf: PlannedField) -> bool:
    """Take the answer out of `pf`'s control (an optional
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
                    target.click(timeout=apply_click.ACTION_TIMEOUT_MS)
        elif widget == "aria_check":
            if _ticked(loc.first):
                loc.first.click(timeout=apply_click.ACTION_TIMEOUT_MS)
        elif widget == "editable":
            loc.first.click(timeout=apply_click.ACTION_TIMEOUT_MS)
            loc.first.evaluate(_SELECT_ALL_JS, timeout=apply_click.ACTION_TIMEOUT_MS)
            page.keyboard.press("Delete")
        elif widget == "combo":
            box = loc.first if tag == "INPUT" else loc.first.locator("input").first
            box.fill("", timeout=apply_click.ACTION_TIMEOUT_MS)
        elif widget.startswith("date:"):
            for css in pf.option_locators:
                target = _clicked(page, pf.locator[0], css)
                if target.evaluate(_UNSET_JS, timeout=apply_click.ACTION_TIMEOUT_MS):
                    continue
                target.fill("", timeout=apply_click.ACTION_TIMEOUT_MS)
        elif widget == "hidden_select" or tag == "SELECT":
            return _clear_select(loc, hidden=widget == "hidden_select")
        elif tag == "INPUT" and typ == "checkbox":
            if pf.click_locator:
                if _ticked(loc.first):
                    _clicked(page, pf.click_locator[0],
                             pf.click_locator[1]).click(timeout=apply_click.ACTION_TIMEOUT_MS)
            else:
                loc.first.uncheck(timeout=apply_click.ACTION_TIMEOUT_MS)
        elif tag == "INPUT" and typ == "file":
            loc.first.set_input_files([], timeout=apply_click.ACTION_TIMEOUT_MS)
            return not loc.first.evaluate("el => el.files ? el.files.length : 0",
                                          timeout=apply_click.ACTION_TIMEOUT_MS)
        else:
            loc.first.fill("", timeout=apply_click.ACTION_TIMEOUT_MS)
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


def page_text(page) -> str:
    """The visible text of every frame, main frame first, capped at
    `PAGE_TEXT_CAP` for the record writer."""
    return "\n".join(apply_form.page_texts(page))[:PAGE_TEXT_CAP]
