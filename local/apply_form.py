"""The form digest: what one page of an application looks like to the loop.

`FormDigest` is the extractor's output and the judge's input. It carries the
page's host and title, its visible text (capped by the extractor), every visible
form control as a `Field` and every clickable control as a `Button`. The judge
(`apply_judge`) turns a digest into Jev questions; the filler (`apply_fill`)
acts on the plan by the `locator` each field and button carries.

A `locator` is `(frame_index, css)`: the index of the frame the control lives
in (0 is the main frame) and a CSS selector that is stable for the page's
lifetime. `to_dict()` / `from_dict()` round-trip through JSON so tests can build
digests without a browser and the record writer can store one.

`extract(page) -> FormDigest` is the Playwright extractor: one JS pass per
frame (`_EXTRACT_JS`) returns plain dicts and Python builds the dataclasses.
`resolve(page, locator)` turns a stored locator back into a Playwright
`Locator`. Both take a live `Page`; nothing here imports Playwright, so the
module stays importable without it.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

log = logging.getLogger(__name__)

FIELD_TYPES = ("text", "email", "tel", "url", "number", "textarea", "select",
               "radio", "checkbox", "file", "date", "listbox", "other")


@dataclass
class Field:
    """One visible form control. `options` is filled for select, radio,
    checkbox and listbox controls; `id_or_name` is the DOM id, else name."""
    n: int
    locator: tuple[int, str]
    label: str
    type: str
    required: bool
    placeholder: str = ""
    help: str = ""
    options: list[str] = field(default_factory=list)
    id_or_name: str = ""
    autocomplete: str = ""      # the control's autocomplete token, when it has one


PASSWORD_WORDS = ("pass", "pwd", "secret")
PASSWORD_AUTOCOMPLETE = ("current-password", "new-password")


def is_password_field(type_: str, id_or_name: str = "", label: str = "",
                      autocomplete: str = "") -> bool:
    """A password-shaped control: an `other` control (the extractor's type for
    a password input) whose id, name or label carries `pass`, `pwd` or
    `secret`, or any control whose autocomplete token is `current-password` /
    `new-password`.

    One definition for the planner (which never puts a fact in such a field),
    the accounts hook (the only writer) and the record (which hides it), so the
    three cannot drift apart. `Passport number` is a text control and stays an
    ordinary field."""
    if str(autocomplete or "").lower() in PASSWORD_AUTOCOMPLETE:
        return True
    if str(type_ or "") != "other":
        return False
    blob = f"{id_or_name or ''} {label or ''}".lower()
    return any(w in blob for w in PASSWORD_WORDS)


@dataclass
class Button:
    """One clickable control. `kind_hint` comes from the DOM (`submit`,
    `button`, `link`, ...) and is only a hint; the judge decides the role.
    `in_form`: the button's form holds a control a person fills (an input
    other than hidden or a button, a select, a textarea, an editable box, a
    custom control): an Apply there is the form's own button, never a
    posting's entry (INV-03)."""
    n: int
    locator: tuple[int, str]
    text: str
    kind_hint: str = ""
    in_form: bool = False


@dataclass
class FormDigest:
    url_host: str
    title: str
    text: str
    fields: list[Field] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FormDigest:
        fields = [Field(n=int(f["n"]), locator=_locator(f.get("locator")),
                        label=str(f.get("label", "")), type=str(f.get("type", "other")),
                        required=bool(f.get("required", False)),
                        placeholder=str(f.get("placeholder", "") or ""),
                        help=str(f.get("help", "") or ""),
                        options=[str(o) for o in (f.get("options") or [])],
                        id_or_name=str(f.get("id_or_name", "") or ""),
                        autocomplete=str(f.get("autocomplete", "") or ""))
                  for f in (raw.get("fields") or [])]
        buttons = [Button(n=int(b["n"]), locator=_locator(b.get("locator")),
                          text=str(b.get("text", "")),
                          kind_hint=str(b.get("kind_hint", "") or ""),
                          in_form=bool(b.get("in_form", False)))
                   for b in (raw.get("buttons") or [])]
        return cls(url_host=str(raw.get("url_host", "")), title=str(raw.get("title", "")),
                   text=str(raw.get("text", "")), fields=fields, buttons=buttons)


def _locator(raw: Any) -> tuple[int, str]:
    if not raw:
        return (0, "")
    frame, css = raw
    return (int(frame), str(css))


# --- the extractor ---------------------------------------------------------------

# One pass over a frame's DOM. Returns {"fields": [...], "buttons": [...], "text": str}
# with plain values only; the dataclasses are built in Python. The rules:
#   fields   visible, enabled input (not hidden/submit/button/image/reset), select,
#            textarea, [role=combobox], [role=listbox]; radios collapse into one
#            entry per name group; a file input is kept even when hidden because
#            set_input_files works on it and ATS pages hide it behind a styled
#            button, while its form (or, outside a form, a box within three
#            levels above it) shows: a form hidden after its send (Greenhouse's
#            embed shows its thanks in place) leaves no field behind; an input
#            inside a [role=combobox] is part of that widget.
#   locator  #id when the id is a plain CSS identifier and unique, else
#            [name="..."] when unique, else a body-rooted nth-of-type path.
#   label    label[for], aria-label, aria-labelledby, an enclosing label (minus
#            the control's own text), a fieldset's legend, else the nearest
#            preceding text node; a radio group takes its radiogroup's aria
#            name, its fieldset's legend, else the text before the first radio.
#   required the attribute, aria-required="true", or a trailing "*" in the
#            label (stripped).
#   help     the aria-describedby text, then "Max N characters." from a
#            maxlength (the answer generator's length budget).
#   options  select option texts minus empty or "Select..." placeholders; radio
#            option labels; ["checked"] for a checkbox; for a combobox the
#            [role=option] texts of the listbox it controls when one is in the
#            DOM (apply_fill.open_listbox_options reads it live otherwise).
#   buttons  button, [role=button], input[type=submit|button], a.btn,
#            a[class*=button]; text from innerText, value, aria-label, title.
#            `kind_hint` is `submit` for a submit control or a send word, never
#            for "Apply with LinkedIn / Indeed", "Submit a general
#            application", "Cancel", "Apply later" or "Save for later" (study
#            G4); `in_form` when the button's form holds a control a person
#            fills (`Button.in_form`).
#            A plain link joins them when its text or aria-label says "apply"
#            and its text is short: LinkedIn's Apply entry is
#            <a aria-label="Apply on company website">Apply</a> under hashed
#            class names, and a similar job's card link carries "Easy Apply"
#            inside a long text.
#   chrome   a control inside the site's header, footer, nav or search
#            landmark is skipped (a job board's search box, its footer
#            language picker), unless that landmark sits inside a form or a
#            dialog, where it is the form's own. Buttons are all kept: a
#            wizard's Next can live in a footer outside its form.
#   text     the body's innerText with the site chrome above the first visible
#            main landmark (a header, nav or search outside any form or
#            dialog) moved to the end: what else sits above main (a "no
#            longer accepting applications" banner) stays in front of it. The
#            judge reads the first characters (`apply_judge.HEADLINE_CHARS`),
#            and LinkedIn's skip links, header and upsell filled 597 of 600 of
#            them on the 2026-09-22 run, so its posting read as a form.
#   consent  a cookie or consent banner (`CONSENT_ROOTS_JS`) is chrome as
#            well: its fields and buttons are dropped and its text goes to
#            the end (the study's G1: 11 ATSs, the banner text first on 3).
# The label of one radio or checkbox option, self-contained so `apply_fill` can
# run the same rule on a live locator: label[for], aria-label, an enclosing
# label (minus the control's own text), the text that follows it, else its
# value. Spliced into `_EXTRACT_JS` at __OPTION_LABEL__.
RADIO_OPTION_LABEL_JS = r"""(el) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const minus = (node) => {
    const c = node.cloneNode(true);
    c.querySelectorAll('input, select, textarea, button, script, style').forEach((n) => n.remove());
    return norm(c.textContent);
  };
  if (el.id) {
    const l = document.querySelector('label[for="' + el.id.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]');
    if (l) { const t = minus(l); if (t) return t; }
  }
  const aria = norm(el.getAttribute('aria-label'));
  if (aria) return aria;
  const enc = el.closest('label');
  if (enc) { const t = minus(enc); if (t) return t; }
  let n = el.nextSibling;
  while (n) {
    if (n.nodeType === 3) { const t = norm(n.data); if (t) return t; }
    else if (n.nodeType === 1) {
      if (/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(n.tagName)) break;
      const t = minus(n); if (t) return t;
    }
    n = n.nextSibling;
  }
  return norm(el.value);
}"""

# The cookie and consent banners of a document, outermost first: an element
# whose id, class or Workday `data-automation-id` names a consent vendor or a
# cookie banner (onetrust, cookie, cc-banner, privacy-banner, cookiebot,
# usercentrics, TRUSTe or TrustArc as whole words, didomi, osano, termly,
# iubenda); one whose names say only consent, gdpr or legalNotice (Workday's
# banner), or a dialog or a fixed or sticky element, when its first 300
# characters mention cookies (a form's own "I consent
# to..." block does not). The application's own content is never one: no box
# inside a form, none holding a text, email, password or phone box or a
# textarea (a sign-in dialog that mentions cookies), the page's main content,
# its h1 or a file input; neither is the body, a control or a style or script
# element. A banner is found whether or not its own box has a size (OneTrust's
# wrapper has none: its banner inside is fixed); its controls are checked
# for visibility one by one.
CONSENT_ROOTS_JS = r"""() => {
  const STRONG = /onetrust|cookie|cc-banner|privacy-banner|cookiebot|usercentrics|\btruste\b|trustarc|didomi|osano|termly|iubenda/i;
  const WEAK = /consent|gdpr|legalnotice/i;
  const COOKIE = /cookie/i;
  const DIALOG = 'dialog, [role=dialog], [role=alertdialog], [aria-modal=true]';
  const TYPING = 'input:not([type]), input[type=text], input[type=email], input[type=password], '
    + 'input[type=tel], textarea';
  const head = (el) => (el.innerText || el.textContent || '').slice(0, 300);
  const names = (el) => (el.id || '') + ' ' + (el.getAttribute('class') || '') + ' '
    + (el.getAttribute('data-automation-id') || '');
  const blocked = (el) => el === document.body || el === document.documentElement
    || el.matches('main, [role=main], form, input, select, textarea, button, label, option, a, '
                  + 'style, script, noscript, template, link, meta')
    || !!el.closest('form')
    || !!el.querySelector('main, [role=main], h1, input[type=file], ' + TYPING);
  const found = new Set();
  const query = ['onetrust', 'cookie', 'consent', 'gdpr', 'cc-banner', 'privacy-banner',
                 'cookiebot', 'usercentrics', 'truste', 'trustarc', 'didomi', 'osano', 'termly',
                 'iubenda']
    .map((w) => '[id*=' + w + ' i], [class*=' + w + ' i]').join(', ')
    + ', [data-automation-id*=cookie i], [data-automation-id=legalNotice], ' + DIALOG;
  for (const el of document.querySelectorAll(query)) found.add(el);
  const fixed = (el, depth) => {
    for (const c of el.children) {
      const pos = getComputedStyle(c).position;
      if (pos === 'fixed' || pos === 'sticky') found.add(c);
      if (depth < 3) fixed(c, depth + 1);
    }
  };
  if (document.body) fixed(document.body, 0);
  const hits = [];
  for (const el of found) {
    if (blocked(el)) continue;
    const s = names(el);
    let hit = false;
    if (STRONG.test(s)) hit = true;
    else if (WEAK.test(s)) hit = COOKIE.test(head(el));
    else {
      const floating = el.matches(DIALOG)
        || ['fixed', 'sticky'].includes(getComputedStyle(el).position);
      hit = floating && COOKIE.test(head(el));
    }
    if (hit) hits.push(el);
  }
  return hits.filter((el) => !hits.some((o) => o !== el && o.contains(el)));
}"""

# The control the loop may click on a consent banner: a visible button,
# `[role=button]` or `input[type=button|submit]` (a link only when it goes
# nowhere: no href, `#` or `javascript:`), the first whose text (innerText,
# an input's value, else its aria-label or title) reads as reject, decline,
# refuse, deny or necessary / essential only, else the first close or
# dismiss control; never one whose text or aria-label says accept, allow or
# agree. Returns {css, text, kind, banner} or null.
_CONSENT_CONTROL_JS = r"""() => {
  const consentRoots = __CONSENT__;
  const locatorFor = __LOCATOR__;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const REJECT = /^(reject|decline|refuse|deny)\b|\b(necessary|essential) only\b|\bonly (strictly )?(necessary|essential)\b|\b(necessary|essential) cookies only\b/i;
  const CLOSE = /^(close|dismiss|×|✕|x)$/i;
  const CLOSE_ARIA = /\b(close|dismiss)\b/i;
  const ACCEPT = /accept|allow|agree/i;
  let close = null;
  const goesNowhere = (el) => {
    const href = (el.getAttribute('href') || '').trim();
    return !href || href === '#' || /^javascript:/i.test(href);
  };
  for (const root of consentRoots()) {
    const banner = norm((root.id || '') + ' ' + (root.getAttribute('class') || '')).slice(0, 80);
    const sel = 'button, [role=button], input[type=button], input[type=submit], a';
    for (const el of root.querySelectorAll(sel)) {
      if (!visible(el) || el.disabled) continue;
      if (el.tagName === 'A' && !goesNowhere(el)) continue;
      const text = norm(el.innerText) || norm(el.value);
      const aria = norm(el.getAttribute('aria-label')) || norm(el.getAttribute('title'));
      const label = text || aria;
      if (!label || ACCEPT.test(label) || ACCEPT.test(aria)) continue;
      if (REJECT.test(label)) {
        return {css: locatorFor(el), text: label.slice(0, 80), kind: 'reject', banner: banner};
      }
      if (!close && (CLOSE.test(label) || CLOSE_ARIA.test(aria))) {
        close = {css: locatorFor(el), text: label.slice(0, 80), kind: 'close', banner: banner};
      }
    }
  }
  return close;
}"""

# The locator of an element: `#id` when unique, else a body-rooted
# nth-of-type path.
LOCATOR_FN_JS = r"""(el) => {
  const cssIdent = /^-?[_a-zA-Z][_a-zA-Z0-9-]*$/;
  if (el.id && cssIdent.test(el.id) && document.querySelectorAll('#' + el.id).length === 1) {
    return '#' + el.id;
  }
  const parts = [];
  let cur = el;
  while (cur && cur !== document.body && cur.nodeType === 1) {
    let i = 1, sib = cur;
    while ((sib = sib.previousElementSibling)) { if (sib.tagName === cur.tagName) i++; }
    parts.unshift(cur.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
    cur = cur.parentElement;
  }
  return 'body > ' + parts.join(' > ');
}"""

_CONSENT_CONTROL_JS = _CONSENT_CONTROL_JS.replace("__CONSENT__", CONSENT_ROOTS_JS).replace(
    "__LOCATOR__", LOCATOR_FN_JS)

_EXTRACT_JS = r"""
(cap) => {
  const CONTROL = /^(INPUT|SELECT|TEXTAREA|BUTTON)$/;
  const SKIP_INPUT = new Set(['hidden', 'submit', 'button', 'image', 'reset']);
  const STAR = /\s*\*$/;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const q = (s) => '"' + s.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  const cssIdent = /^-?[_a-zA-Z][_a-zA-Z0-9-]*$/;

  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const enabled = (el) => !el.disabled && el.getAttribute('aria-disabled') !== 'true'
    && !el.closest('fieldset:disabled');
  const CHROME = 'header, footer, nav, search, [role=banner], [role=contentinfo], '
    + '[role=navigation], [role=search]';
  const inChrome = (el) => {
    const c = el.closest(CHROME);
    return !!c && !(c.parentElement && c.parentElement.closest('form, dialog, [role=dialog]'));
  };
  const consent = (__CONSENT__)();
  const inConsent = (el) => consent.some((root) => root.contains(el));
  // a hidden file box's form, or outside a form a box within three levels
  // above it, still shows (a styled upload hides the input itself)
  const boxShows = (el) => {
    if (el.form) return visible(el.form);
    let p = el.parentElement;
    for (let i = 0; p && i < 3; i++, p = p.parentElement) { if (visible(p)) return true; }
    return false;
  };
  const FILLABLE = 'input:not([type=hidden]):not([type=submit]):not([type=button])'
    + ':not([type=reset]):not([type=image]), select, textarea, [contenteditable=""], '
    + '[contenteditable=true], [role=textbox], [role=combobox], [role=listbox], [role=radio], '
    + '[role=checkbox], [role=switch], [role=spinbutton]';
  // a form holds a control a person fills: a visible one outside the site
  // chrome (a page-wide form's header search does not count), or a
  // visible custom control with a shadow root
  const held = new Map();
  const holdsControls = (form) => {
    if (!form) return false;
    if (!held.has(form)) {
      held.set(form, Array.from(form.querySelectorAll('*')).some(
        (n) => (n.matches(FILLABLE) || !!n.shadowRoot) && visible(n) && !n.closest(CHROME)));
    }
    return held.get(form);
  };
  const NEVER_SUBMIT = /linkedin|indeed|general application|\bcancel\b|apply later|save for later/i;

  const nthPath = (el) => {
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body && cur.nodeType === 1) {
      let i = 1, sib = cur;
      while ((sib = sib.previousElementSibling)) { if (sib.tagName === cur.tagName) i++; }
      parts.unshift(cur.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      cur = cur.parentElement;
    }
    return 'body > ' + parts.join(' > ');
  };
  const unique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  const locatorFor = (el) => {
    if (el.id && cssIdent.test(el.id) && unique('#' + el.id)) return '#' + el.id;
    const name = el.getAttribute('name');
    if (name && unique('[name=' + q(name) + ']')) return '[name=' + q(name) + ']';
    return nthPath(el);
  };

  const textMinusControls = (node) => {
    const clone = node.cloneNode(true);
    clone.querySelectorAll('input, select, textarea, button, script, style').forEach((n) => n.remove());
    return norm(clone.textContent);
  };
  const byIds = (ids) => norm(ids.split(/\s+/).map((id) => {
    const n = document.getElementById(id);
    return n ? textMinusControls(n) : '';
  }).join(' '));
  const precedingText = (el, skipWithin) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    walker.currentNode = el;
    let node;
    while ((node = walker.previousNode())) {
      if (skipWithin && skipWithin.contains(node)) continue;
      if (node.nodeType === 1) {
        if (CONTROL.test(node.tagName)) return '';
        continue;
      }
      const p = node.parentElement;
      if (p && /^(SCRIPT|STYLE|OPTION|NOSCRIPT)$/.test(p.tagName)) continue;
      const lab = p && p.closest('label');
      if (lab && (lab.control || lab.hasAttribute('for')) && lab.control !== el) return '';
      const t = norm(node.data);
      if (t) return t.slice(-120);
    }
    return '';
  };
  const optionLabel = __OPTION_LABEL__;
  const labelElementFor = (el) => {
    if (el.id) {
      const l = document.querySelector('label[for=' + q(el.id) + ']');
      if (l) return l;
    }
    return null;
  };
  const labelFor = (el) => {
    const forLabel = labelElementFor(el);
    if (forLabel) { const t = textMinusControls(forLabel); if (t) return t; }
    const aria = norm(el.getAttribute('aria-label'));
    if (aria) return aria;
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = byIds(by); if (t) return t; }
    const enclosing = el.closest('label');
    if (enclosing) { const t = textMinusControls(enclosing); if (t) return t; }
    const fs = el.closest('fieldset');
    const legend = fs && fs.querySelector('legend');
    if (legend) { const t = norm(legend.textContent); if (t) return t; }
    return precedingText(el, enclosing);
  };
  const groupLabel = (first) => {
    const rg = first.closest('[role=radiogroup]');
    if (rg) {
      const aria = norm(rg.getAttribute('aria-label'));
      if (aria) return aria;
      const by = rg.getAttribute('aria-labelledby');
      if (by) { const t = byIds(by); if (t) return t; }
    }
    const fs = first.closest('fieldset');
    const legend = fs && fs.querySelector('legend');
    if (legend) { const t = norm(legend.textContent); if (t) return t; }
    return precedingText(first, first.closest('label'));
  };

  const typeOf = (el) => {
    const role = el.getAttribute('role') || '';
    if (role === 'combobox' || role === 'listbox') return 'listbox';
    const tag = el.tagName;
    if (tag === 'SELECT') return 'select';
    if (tag === 'TEXTAREA') return 'textarea';
    if (tag === 'INPUT') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'text' || t === 'search') return 'text';
      if (['email', 'tel', 'url', 'number', 'file', 'date', 'radio', 'checkbox'].includes(t)) return t;
    }
    return 'other';
  };
  const PLACEHOLDER_OPTION = /^(select|choose|please|pick|-{2,})/i;
  const selectOptions = (el) => Array.from(el.options).map((o) => norm(o.text)).filter((t, i) => {
    const o = el.options[i];
    if (!t) return false;
    return !(o.value === '' && (o.disabled || PLACEHOLDER_OPTION.test(t)));
  });
  const listboxFor = (el) => {
    if (el.getAttribute('role') === 'listbox') return el;
    const ids = (el.getAttribute('aria-controls') || '') + ' ' + (el.getAttribute('aria-owns') || '');
    for (const id of ids.split(/\s+/).filter(Boolean)) {
      const n = document.getElementById(id);
      if (n) return n;
    }
    return el.querySelector('[role=listbox]') || (el.parentElement && el.parentElement.querySelector('[role=listbox]'));
  };
  const listboxOptions = (el) => {
    const lb = listboxFor(el);
    if (!lb) return [];
    return Array.from(lb.querySelectorAll('[role=option]')).map((o) => norm(o.textContent)).filter(Boolean);
  };
  const optionsFor = (el, type) => {
    if (type === 'select') return selectOptions(el);
    if (type === 'checkbox') return ['checked'];
    if (type === 'listbox') return listboxOptions(el);
    return [];
  };
  const isRequired = (el) => !!el.required || el.getAttribute('aria-required') === 'true';
  const describe = (el, type, label, required, css, options) => {
    if (STAR.test(label)) { required = true; label = label.replace(STAR, ''); }
    const desc = el.getAttribute('aria-describedby');
    const helps = [desc ? byIds(desc) : ''];
    const max = parseInt(el.getAttribute('maxlength') || '', 10);
    if (max > 0) helps.push('Max ' + max + ' characters.');
    return {
      css: css, label: label, type: type, required: !!required,
      placeholder: norm(el.getAttribute('placeholder')),
      help: helps.filter(Boolean).join(' '),
      options: options,
      id_or_name: el.id || el.getAttribute('name') || '',
      autocomplete: norm(el.getAttribute('autocomplete')).toLowerCase(),
    };
  };

  const fields = [];
  const groups = new Map();
  const controlled = new Set();
  for (const box of document.querySelectorAll('[role=combobox]')) {
    const lb = listboxFor(box);
    if (lb) controlled.add(lb);
  }
  for (const el of document.querySelectorAll('input, select, textarea, [role=combobox], [role=listbox]')) {
    if (!enabled(el) || inChrome(el) || inConsent(el)) continue;
    const role = el.getAttribute('role') || '';
    if (role !== 'combobox') {
      const widget = el.closest('[role=combobox]');
      if (widget && widget !== el) continue;
    }
    if (role === 'listbox' && controlled.has(el)) continue;
    const type = typeOf(el);
    if (el.tagName === 'INPUT') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (SKIP_INPUT.has(t)) continue;
      if (t !== 'file' && !visible(el)) continue;
      if (t === 'file' && !visible(el) && !boxShows(el)) continue;
      if (t === 'radio') {
        const name = el.getAttribute('name') || '';
        const key = name ? 'name:' + name : 'path:' + nthPath(el);
        let g = groups.get(key);
        if (!g) {
          g = { group: true, first: el, name: name, radios: [] };
          groups.set(key, g);
          fields.push(g);
        }
        g.radios.push(el);
        continue;
      }
    } else if (!visible(el)) {
      continue;
    }
    fields.push(describe(el, type, labelFor(el), isRequired(el), locatorFor(el), optionsFor(el, type)));
  }
  const out = fields.map((f) => {
    if (!f.group) return f;
    const css = f.name ? 'input[type=radio][name=' + q(f.name) + ']' : nthPath(f.first);
    const required = f.radios.some(isRequired);
    const options = f.radios.map(optionLabel);
    const d = describe(f.first, 'radio', groupLabel(f.first), required, css, options);
    d.id_or_name = f.name || f.first.id || '';
    return d;
  });

  const buttons = [];
  const bsel = 'button, [role=button], input[type=submit], input[type=button], a.btn, a[class*=button]';
  const APPLY = /\bapply\b/i;
  const APPLY_LINK_MAX = 40;
  const applyLink = (el, text) => text.length <= APPLY_LINK_MAX && !inChrome(el)
    && APPLY.test(text + ' ' + norm(el.getAttribute('aria-label')));
  for (const el of document.querySelectorAll(bsel + ', a[href]')) {
    if (!enabled(el) || !visible(el)) continue;
    if (el.closest('[role=combobox]') || inConsent(el)) continue;
    const text = norm(el.innerText) || norm(el.value) || norm(el.getAttribute('aria-label')) || norm(el.getAttribute('title'));
    if (!el.matches(bsel) && !applyLink(el, text)) continue;
    const typeAttr = (el.getAttribute('type') || '').toLowerCase();
    const submits = typeAttr === 'submit' || (el.tagName === 'BUTTON' && !typeAttr && !!el.form);
    const aria = norm(el.getAttribute('aria-label'));
    let kind = '';
    if (NEVER_SUBMIT.test(text + ' ' + aria)) kind = '';
    else if (submits || /\b(submit|apply|send|finish)\b/i.test(text)) kind = 'submit';
    else if (/\b(next|continue)\b/i.test(text)) kind = 'advance';
    if (!kind && /\b(back|previous)\b/i.test(text)) kind = 'back';
    const owner = el.form || el.closest('form');
    buttons.push({ css: locatorFor(el), text: text, kind_hint: kind,
                   in_form: holdsControls(owner) });
  }

  let text = document.body ? (document.body.innerText || '') : '';
  const main = Array.from(document.querySelectorAll('main, [role=main]')).find(visible);
  const lead = main ? (main.innerText || '').trim() : '';
  const at = lead ? text.indexOf(lead) : -1;
  if (at >= 0) {
    let above = text.slice(0, at);
    const moved = [];
    for (const el of document.querySelectorAll(CHROME)) {
      if (!inChrome(el) || (el.parentElement && el.parentElement.closest(CHROME))) continue;
      if (!(el.compareDocumentPosition(main) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
      const t = (el.innerText || '').trim();
      if (t && above.includes(t)) { above = above.replace(t, ''); moved.push(t); }
    }
    text = [above.trim(), lead, text.slice(at + lead.length).trim(), ...moved]
      .filter(Boolean).join('\n');
  }
  const banners = [];
  for (const root of consent) {
    const t = (root.innerText || '').trim();
    if (t && text.includes(t)) { text = text.replace(t, ''); banners.push(t); }
  }
  if (banners.length) text = [text.trim(), ...banners].filter(Boolean).join('\n');
  return { fields: out, buttons: buttons, text: text.slice(0, cap) };
}
""".replace("__OPTION_LABEL__", RADIO_OPTION_LABEL_JS).replace("__CONSENT__", CONSENT_ROOTS_JS)

_TEXT_JS = "() => document.body ? (document.body.innerText || '') : ''"


def frames(page) -> list:
    """The page's frames with the main frame first: index 0 is `page.main_frame`,
    1.. are the child frames in `page.frames` order. The digest's locators and
    `resolve` share this numbering."""
    main = page.main_frame
    return [main] + [f for f in page.frames if f is not main]


def extract(page) -> FormDigest:
    """Read one page of an application into a `FormDigest`: every frame in one
    JS pass each, fields and buttons numbered across frames in document order,
    the visible text of every frame joined and capped at
    `apply_judge.PAGE_TEXT_CAP`. A frame whose evaluate fails (detached,
    cross-origin) is skipped and keeps its index."""
    from apply_judge import PAGE_TEXT_CAP      # lazy: apply_judge imports this module

    fields: list[Field] = []
    buttons: list[Button] = []
    texts: list[str] = []
    for idx, frame in enumerate(frames(page)):
        try:
            raw = frame.evaluate(_EXTRACT_JS, PAGE_TEXT_CAP)
        except Exception as e:      # noqa: BLE001  (a detached or cross-origin frame)
            log.info("apply_form: frame %d skipped: %s", idx, e)
            continue
        for f in raw.get("fields") or []:
            fields.append(Field(
                n=len(fields), locator=(idx, str(f["css"])), label=str(f["label"]),
                type=str(f["type"]), required=bool(f["required"]),
                placeholder=str(f.get("placeholder") or ""), help=str(f.get("help") or ""),
                options=[str(o) for o in (f.get("options") or [])],
                id_or_name=str(f.get("id_or_name") or ""),
                autocomplete=str(f.get("autocomplete") or "")))
        for b in raw.get("buttons") or []:
            buttons.append(Button(n=len(buttons), locator=(idx, str(b["css"])),
                                  text=str(b["text"]), kind_hint=str(b.get("kind_hint") or ""),
                                  in_form=bool(b.get("in_form"))))
        if raw.get("text"):
            texts.append(str(raw["text"]))
    text = "\n".join(texts)[:PAGE_TEXT_CAP]
    return FormDigest(url_host=urlparse(page.url).hostname or "", title=page.title(),
                      text=text, fields=fields, buttons=buttons)


def consent_control(page, allow=None) -> tuple[int, dict] | None:
    """(frame index, {css, text, kind, banner}) of the control the loop may
    click to dismiss a visible cookie or consent banner: its reject,
    decline or necessary-only control, else its close (`_CONSENT_CONTROL_JS`);
    None when no banner shows or it offers neither. `allow(index, frame)`
    says which frames may be looked in (the runner's: the page's own frames
    on the allowed sites, never a bot check's)."""
    for idx, frame in enumerate(frames(page)):
        if allow is not None and not allow(idx, frame):
            continue
        try:
            found = frame.evaluate(_CONSENT_CONTROL_JS)
        except Exception:       # noqa: BLE001  (a detached or cross-origin frame)
            continue
        if found:
            return idx, dict(found)
    return None


def page_texts(page) -> list[str]:
    """The `innerText` of every frame, main frame first, uncapped. A frame that
    cannot be read is skipped."""
    out: list[str] = []
    for frame in frames(page):
        try:
            t = frame.evaluate(_TEXT_JS)
        except Exception:       # noqa: BLE001
            continue
        if t:
            out.append(str(t))
    return out


def resolve(page, locator: tuple[int, str]):
    """A digest locator `(frame_index, css)` as a Playwright `Locator` on that
    frame. Raises `IndexError` when the frame no longer exists."""
    idx, css = int(locator[0]), str(locator[1])
    all_frames = frames(page)
    if not 0 <= idx < len(all_frames):
        raise IndexError(f"frame {idx} is gone (page has {len(all_frames)})")
    return all_frames[idx].locator(css)


# --- live reads of the page (the submit gate and every click, SP3) ------------------------

# A control's text as the extractor reads a button's (innerText, an input's
# value, aria-label, title), with its aria-label and type apart.
LIVE_TEXT_JS = r"""el => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const text = norm(el.innerText) || norm(el.value) || norm(el.getAttribute('aria-label'))
    || norm(el.getAttribute('title'));
  return {text: text.slice(0, 200), aria: norm(el.getAttribute('aria-label')).slice(0, 200),
          type: (el.getAttribute('type') || '').toLowerCase(), tag: el.tagName.toLowerCase()};
}"""

# The same, for every visible control of the frame that reads `want` (the
# digest's text): how a control that changed under a stored locator is found
# again (INV-04).
_FIND_BY_TEXT_JS = r"""(want) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const locatorFor = __LOCATOR__;
  const out = [];
  const sel = 'button, [role=button], input[type=submit], input[type=button], a';
  for (const el of document.querySelectorAll(sel)) {
    const st = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (st.display === 'none' || st.visibility === 'hidden' || (r.width <= 0 && r.height <= 0)) continue;
    const text = norm(el.innerText) || norm(el.value) || norm(el.getAttribute('aria-label'))
      || norm(el.getAttribute('title'));
    if (text === want) out.push(locatorFor(el));
  }
  return out;
}""".replace("__LOCATOR__", LOCATOR_FN_JS)


def live_text(loc) -> dict[str, str]:
    """The live element's {text, aria, type, tag} (`LIVE_TEXT_JS`), read just
    before a click; {} when it cannot be read."""
    try:
        return dict(loc.first.evaluate(LIVE_TEXT_JS, timeout=2_000))
    except Exception:       # noqa: BLE001  (gone, detached, or a page double)
        return {}


def find_by_text(page, frame_index: int, text: str) -> list[str]:
    """The CSS of every visible control in frame `frame_index` whose text is
    `text` (the way a control is found again when its stored locator now
    names another control)."""
    try:
        return [str(c) for c in frames(page)[int(frame_index)].evaluate(
            _FIND_BY_TEXT_JS, " ".join(str(text or "").split()))]
    except Exception:       # noqa: BLE001  (a frame gone)
        return []


# Where a button sits beside the fields, as a verdict: "same" when its form
# owner holds one of the fields, or, outside any form, when the lowest box
# above it holding one of them is smaller than the page (not the body, not
# `main`, holding no h1); "apart" when it has a form that holds none of
# them and they all sit in other forms, or, outside any form, when every
# field sits in another form that has a button of its own (a job-alert box
# beside a posting's Apply); "unclear" otherwise (a page whose fields and
# button share only the body). `fields` are CSS selectors in the button's
# frame. Returns {verdict, why}.
_SAME_SCOPE_JS = r"""([bcss, fcss]) => {
  let btn = null;
  try { btn = document.querySelector(bcss); } catch (e) {}
  if (!btn) return {verdict: 'unclear', why: 'the button is gone'};
  const fields = [];
  for (const c of fcss) {
    try { const f = document.querySelector(c); if (f) fields.push(f); } catch (e) {}
  }
  if (!fields.length) return {verdict: 'unclear', why: 'no field of this page in its frame'};
  const owner = (x) => x.form || x.closest('form');
  const bf = owner(btn);
  const owners = fields.map(owner);
  if (bf) {
    if (owners.some((o) => o === bf)) return {verdict: 'same', why: 'the fields form'};
    return owners.every((o) => !!o)
      ? {verdict: 'apart', why: 'another form than the fields'}
      : {verdict: 'unclear', why: 'a form without the fields'};
  }
  const BTN = 'button, input[type=submit], input[type=button], [role=button]';
  if (owners.every((o) => !!o && !!o.querySelector(BTN))) {
    return {verdict: 'apart', why: 'the fields sit in a form of their own, with its own button'};
  }
  let box = btn.parentElement;
  while (box && !fields.some((f) => box.contains(f))) box = box.parentElement;
  if (!box || box === document.body || box === document.documentElement
      || box.matches('main, [role=main]') || box.querySelector('h1')) {
    return {verdict: 'unclear', why: 'the page is the only box holding the button and the fields'};
  }
  return {verdict: 'same', why: 'the fields box (' + (box.id || box.tagName.toLowerCase()) + ')'};
}"""


def same_scope(page, button_locator: tuple[int, str],
               field_locators: list[tuple[int, str]]) -> tuple[str, str]:
    """("same" | "apart" | "unclear", why): does the button sit with these
    fields (`_SAME_SCOPE_JS`)? Only fields in the button's frame count."""
    idx = int(button_locator[0])
    css = [str(loc[1]) for loc in field_locators if int(loc[0]) == idx]
    try:
        out = frames(page)[idx].evaluate(_SAME_SCOPE_JS, [str(button_locator[1]), css])
    except Exception as e:      # noqa: BLE001  (a frame gone)
        return "unclear", f"unreadable ({type(e).__name__})"
    return str(out.get("verdict") or "unclear"), str(out.get("why") or "")


# The validity of the controls a submit sends, read from `validity` (no
# `invalid` event fires): its form's elements; for a submit outside any form
# (a wizard's footer), the forms that own the fields this page filled
# (`fcss`) and the controls outside a form in the lowest box above the button
# that holds one of them (the page when none); with no button, every control
# of the frame outside a form. The site chrome and a consent banner are never
# read; in a `novalidate` form (the site validates in its own script) a
# hidden control is skipped. Then the visible error texts of the frame:
# [role=alert], an assertive live region, and short boxes whose class names
# an error (a success or info note is none). An error text beside a control
# (its box within two levels holds one) is a field's; the rest are banners;
# `tied` when a control names it (`aria-describedby`, `aria-errormessage`).
# Without a button, the forms of the filled fields (`fcss`) when there are
# any. Returns {invalid: [{label, message, reason}], errors: [{text, field,
# tied}]}.
_VALIDITY_JS = r"""({bcss, fcss}) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const CHROME = 'header, footer, nav, search, [role=banner], [role=contentinfo], '
    + '[role=navigation], [role=search]';
  const consent = (__CONSENT__)();
  const outside = (el) => consent.some((r) => r.contains(el)) || !!el.closest(CHROME);
  const owner = (x) => x.form || x.closest('form');
  const find = (css) => { try { return css ? document.querySelector(css) : null; } catch (e) { return null; } };
  const labelOf = (el) => {
    const byLabel = el.labels && el.labels.length ? norm(el.labels[0].innerText) : '';
    let by = '';
    const ids = el.getAttribute('aria-labelledby');
    if (ids) by = norm(ids.split(/\s+/).map((i) => { const n = document.getElementById(i);
      return n ? n.innerText : ''; }).join(' '));
    const legend = el.closest('fieldset') && el.closest('fieldset').querySelector('legend');
    return (byLabel || norm(el.getAttribute('aria-label')) || by
      || (el.type === 'radio' && legend ? norm(legend.innerText) : '')
      || norm(el.getAttribute('placeholder')) || el.name || el.id || el.tagName.toLowerCase())
      .replace(/\s*\*$/, '').slice(0, 80);
  };
  const btn = find(bcss);
  const filled = (fcss || []).map(find).filter(Boolean);
  const form = btn ? owner(btn) : null;
  let controls;
  let inScope;
  if (form) {
    controls = Array.from(form.elements);
    inScope = (el) => form.contains(el);
  } else if (btn) {
    const forms = new Set(filled.map(owner).filter(Boolean));
    let box = btn.parentElement;
    while (box && filled.length && !filled.some((f) => box.contains(f))) box = box.parentElement;
    if (!box || !filled.length) box = document.body;
    const loose = Array.from(box.querySelectorAll('input, select, textarea'))
      .filter((el) => !owner(el) && !outside(el));
    controls = [...Array.from(forms).flatMap((f) => Array.from(f.elements)), ...loose];
    inScope = (el) => Array.from(forms).some((f) => f.contains(el))
      || (box.contains(el) && !owner(el) && !outside(el));
  } else if (filled.some((f) => owner(f))) {
    const forms = new Set(filled.map(owner).filter(Boolean));
    controls = Array.from(forms).flatMap((f) => Array.from(f.elements));
    inScope = (el) => Array.from(forms).some((f) => f.contains(el));
  } else {
    controls = Array.from(document.querySelectorAll('input, select, textarea'))
      .filter((el) => !owner(el) && !outside(el));
    inScope = (el) => !owner(el) && !outside(el);
  }
  const REASONS = ['valueMissing', 'typeMismatch', 'patternMismatch', 'tooShort', 'tooLong',
                   'rangeUnderflow', 'rangeOverflow', 'stepMismatch', 'badInput', 'customError'];
  const invalid = [];
  const seen = new Set();
  for (const el of controls) {
    if (!el.willValidate || !el.validity || el.validity.valid) continue;
    const f = owner(el);
    if (f && f.noValidate && !visible(el)) continue;
    const key = el.type === 'radio' && el.name ? 'radio:' + el.name : el;
    if (seen.has(key)) continue;
    seen.add(key);
    invalid.push({label: labelOf(el), message: norm(el.validationMessage).slice(0, 160),
                  reason: REASONS.find((r) => el.validity[r]) || 'invalid'});
  }
  for (const el of document.querySelectorAll('[aria-invalid=true]')) {
    if (seen.has(el) || outside(el) || !visible(el) || !inScope(el)) continue;
    seen.add(el);
    const desc = el.getAttribute('aria-describedby') || el.getAttribute('aria-errormessage') || '';
    const msg = norm(desc.split(/\s+/).map((i) => { const n = document.getElementById(i);
      return n ? n.innerText : ''; }).join(' '));
    invalid.push({label: labelOf(el), message: msg.slice(0, 160), reason: 'aria-invalid'});
  }
  const errors = [];
  const texts = new Set();
  const esel = '[role=alert], [aria-live=assertive], [class*=error i], [class*=invalid i], '
    + '[class*=danger i]';
  const SUCCESS = /\b(success|succeeded|info|notice)\b|alert-(success|info)/i;
  const namedBy = (el) => !!el.id && Array.from(document.querySelectorAll(
    '[aria-describedby], [aria-errormessage]')).some((c) => (
      (c.getAttribute('aria-describedby') || '') + ' ' + (c.getAttribute('aria-errormessage') || ''))
      .split(/\s+/).includes(el.id));
  for (const el of document.querySelectorAll(esel)) {
    if (outside(el) || !visible(el)) continue;
    if (el.matches('input, select, textarea, button, form, body')) continue;
    const cls = el.getAttribute('class') || '';
    if (SUCCESS.test(cls) && !/error|invalid|danger/i.test(cls)) continue;
    const text = norm(el.innerText);
    if (!text || text.length > 240 || texts.has(text)) continue;
    if (Array.from(texts).some((t) => t.includes(text) || text.includes(t))) continue;
    texts.add(text);
    // a field's box: within two levels above, a box holding one to six
    // controls that is no page, form or main region
    let field = false;
    let p = el.parentElement;
    for (let i = 0; p && i < 2 && !field; i++, p = p.parentElement) {
      if (p.matches('body, html, form, main, [role=main]')) break;
      const n = p.querySelectorAll('input:not([type=hidden]), select, textarea').length;
      field = n >= 1 && n <= 6;
    }
    errors.push({text: text.slice(0, 200), field: field, tied: namedBy(el)});
  }
  return {invalid: invalid.slice(0, 20), errors: errors.slice(0, 10)};
}""".replace("__CONSENT__", CONSENT_ROOTS_JS)


_VALUES_JS = r"""(css) => css.map((c) => {
  let el = null;
  try { el = document.querySelector(c); } catch (e) { return null; }
  if (!el) return null;
  const t = (el.getAttribute('type') || '').toLowerCase();
  if (el.tagName === 'INPUT' && ['file', 'password', 'hidden'].includes(t)) return null;
  if (el.tagName === 'INPUT' && (t === 'checkbox' || t === 'radio')) return el.checked ? 'on' : '';
  if (el.tagName === 'SELECT') return el.selectedIndex >= 0 && el.value ? el.value : '';
  return el.value === undefined ? null : String(el.value);
})"""


def box_values(page, locators: list[tuple[int, str]]) -> list[str | None]:
    """What each box holds now (None for a file, password or hidden box, or
    one that is gone), in `locators` order: the run compares them before and
    after its submit click; they are never written to the trace or the
    record."""
    out: list[str | None] = [None] * len(locators)
    by_frame: dict[int, list[int]] = {}
    for i, loc in enumerate(locators):
        by_frame.setdefault(int(loc[0]), []).append(i)
    all_frames = frames(page)
    for idx, rows in by_frame.items():
        if not 0 <= idx < len(all_frames):
            continue
        try:
            got = all_frames[idx].evaluate(_VALUES_JS, [str(locators[i][1]) for i in rows])
        except Exception:       # noqa: BLE001  (a frame mid-navigation)
            continue
        for i, v in zip(rows, got or []):
            out[i] = v
    return out


def validity_report(page, button_locator: tuple[int, str] | None = None,
                    field_locators: list[tuple[int, str]] | None = None) -> dict[str, list]:
    """The controls that would not validate and the visible error texts
    (`_VALIDITY_JS`): with `button_locator`, that button's form in its frame,
    or for a button outside any form the forms of `field_locators` (the
    fields this page filled) and the controls outside a form beside them;
    without, every frame's controls outside a form and every frame's error
    texts. {invalid: [{label, message, reason, frame}], errors: [{text,
    field, frame}]}."""
    out: dict[str, list] = {"invalid": [], "errors": []}
    targets = [(int(button_locator[0]), str(button_locator[1]))] if button_locator \
        else [(i, "") for i in range(len(frames(page)))]
    # without a button, the filled fields' forms: a button no longer found
    # (a server's answer whose inserted summary shifts a path) is looked for
    # through them
    all_frames = frames(page)
    for idx, css in targets:
        if not 0 <= idx < len(all_frames):
            continue
        fcss = [str(loc[1]) for loc in field_locators or [] if int(loc[0]) == idx]
        try:
            got = all_frames[idx].evaluate(_VALIDITY_JS, {"bcss": css or None, "fcss": fcss})
        except Exception:       # noqa: BLE001  (a detached or cross-origin frame)
            continue
        for key in ("invalid", "errors"):
            out[key] += [{**row, "frame": idx} for row in got.get(key) or []]
    return out


# The controls the extractor does not see as fields, in the composed tree
# (open shadow roots walked): a native control inside a shadow root, an ARIA
# textbox / radio / checkbox / switch / spinbutton that is no native control,
# a contenteditable box; outside the site chrome, a consent banner and a
# combobox widget (each read across shadow boundaries, so a shadow header's
# search box is chrome too), visible only. Each with its label, whether it is
# required (the attribute, aria-required, or a required radiogroup) and
# whether it is empty (no value, nothing checked, no text). `requiredOnly`
# keeps the required empty ones; at most 40 rows, cut after that filter.
_SCAN_JS = r"""(requiredOnly) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const CHROME = 'header, footer, nav, search, [role=banner], [role=contentinfo], '
    + '[role=navigation], [role=search]';
  const consent = (__CONSENT__)();
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  // the parent across a shadow boundary: a shadow root's child goes up to its host
  const up = (n) => n.parentElement || ((n.getRootNode && n.getRootNode().host) || null);
  const closestComposed = (el, sel) => {
    for (let n = el; n; n = up(n)) { if (n.nodeType === 1 && n.matches(sel)) return n; }
    return null;
  };
  const insideComposed = (el, root) => {
    for (let n = el; n; n = up(n)) { if (n === root) return true; }
    return false;
  };
  const NATIVE = /^(INPUT|SELECT|TEXTAREA)$/;
  const SKIP = new Set(['hidden', 'submit', 'button', 'image', 'reset']);
  const ROLES = /^(textbox|radio|checkbox|switch|spinbutton)$/;
  const out = [];
  const groups = new Set();
  const radioNames = new Map();
  const labelOf = (el) => norm(el.getAttribute('aria-label') || (el.labels && el.labels[0]
    && el.labels[0].innerText) || el.getAttribute('placeholder') || el.getAttribute('name')
    || el.id || el.tagName.toLowerCase()).slice(0, 80);
  const walk = (root, shadow) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot, true);
      const role = el.getAttribute('role') || '';
      let kind = '';
      if (shadow && NATIVE.test(el.tagName)) {
        if (el.tagName === 'INPUT' && SKIP.has((el.getAttribute('type') || 'text').toLowerCase())) continue;
        kind = el.tagName.toLowerCase();
      } else if (ROLES.test(role) && !NATIVE.test(el.tagName)) kind = role;
      else if (el.isContentEditable && el.hasAttribute('contenteditable')) kind = 'contenteditable';
      if (!kind) continue;
      const box = closestComposed(el, '[role=combobox]');
      if (closestComposed(el, CHROME) || consent.some((r) => insideComposed(el, r))
          || (box && box !== el)) continue;
      if (!visible(el)) continue;
      let required = el.required === true || el.getAttribute('aria-required') === 'true';
      let empty;
      if (kind === 'radio') {
        const group = el.closest('[role=radiogroup]');
        if (group) {
          if (groups.has(group)) continue;
          groups.add(group);
          required = required || group.getAttribute('aria-required') === 'true';
          empty = !group.querySelector('[role=radio][aria-checked=true]');
        } else {
          empty = el.getAttribute('aria-checked') !== 'true';
        }
      } else if (kind === 'input' && el.type === 'radio') {
        const rootNode = el.getRootNode();
        const key = el.name || labelOf(el);
        const names = radioNames.get(rootNode) || new Set();
        radioNames.set(rootNode, names);
        if (names.has(key)) continue;
        names.add(key);
        const mates = el.name
          ? Array.from(rootNode.querySelectorAll('input[type=radio]')).filter((r) => r.name === el.name)
          : [el];
        required = required || mates.some((r) => r.required);
        empty = !mates.some((r) => r.checked);
      } else if (kind === 'checkbox' || kind === 'switch') {
        empty = el.getAttribute('aria-checked') !== 'true';
      } else if (kind === 'input' && el.type === 'checkbox') {
        empty = !el.checked;
      } else if (kind === 'spinbutton') {
        empty = !el.getAttribute('aria-valuenow') && !norm(el.getAttribute('aria-valuetext'))
          && !norm(el.innerText);
      } else if (NATIVE.test(el.tagName)) {
        empty = !norm(el.value);
      } else {
        empty = !norm(el.innerText);
      }
      if (requiredOnly && !(required && empty)) continue;
      out.push({label: labelOf(el), kind: kind, required: !!required, empty: !!empty,
                shadow: !!shadow});
    }
  };
  walk(document, false);
  return out.slice(0, 40);
}""".replace("__CONSENT__", CONSENT_ROOTS_JS)


def control_scan(page, frame_indexes: list[int] | None = None, *,
                 required_only: bool = False) -> list[dict[str, Any]]:
    """The controls the extractor leaves out (`_SCAN_JS`), in the given
    frames (every frame when None), each {label, kind, required, empty,
    shadow, frame}; `required_only` keeps the required empty ones (the
    filter runs before the 40-row cut)."""
    out: list[dict[str, Any]] = []
    for idx, frame in enumerate(frames(page)):
        if frame_indexes is not None and idx not in frame_indexes:
            continue
        try:
            got = frame.evaluate(_SCAN_JS, bool(required_only))
        except Exception:       # noqa: BLE001  (a detached or cross-origin frame)
            continue
        out += [{**row, "frame": idx} for row in got or []]
    return out


# The CAPTCHA widgets of a document by their frames' `src` (read before a
# frame loads): reCAPTCHA (google.com/recaptcha, recaptcha.net), hCaptcha
# and Cloudflare Turnstile (challenges.cloudflare.com), visible or not, their
# size (reCAPTCHA's and hCaptcha's `size=` parameter, Turnstile's path
# segment), and whether each response token of the document
# (`g-recaptcha-response`, `h-captcha-response`, `cf-turnstile-response`) is
# set.
_CAPTCHA_WIDGETS_JS = r"""() => {
  const widgets = [];
  for (const f of document.querySelectorAll('iframe')) {
    const src = f.getAttribute('src') || '';
    let host = '', path = '';
    try { const u = new URL(src, location.href); host = u.hostname.toLowerCase(); path = u.pathname; }
    catch (e) { continue; }
    const recaptcha = /(^|\.)recaptcha\.net$/.test(host)
      || (/(^|\.)google\.com$/.test(host) && path.startsWith('/recaptcha'));
    const hcaptcha = /(^|\.)hcaptcha\.com$/.test(host);
    const turnstile = host === 'challenges.cloudflare.com' && /turnstile/.test(path);
    if (!recaptcha && !hcaptcha && !turnstile) continue;
    const st = getComputedStyle(f);
    const r = f.getBoundingClientRect();
    const shown = st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
    let size = '';
    if (turnstile) {
      const m = path.match(/\/(normal|compact|flexible|invisible)(\/|$)/i);
      size = m ? m[1].toLowerCase() : 'normal';
    } else {
      const m = src.match(/[?&#]size=([a-z]+)/i);
      size = m ? m[1].toLowerCase() : '';
    }
    widgets.push({provider: recaptcha ? 'recaptcha' : hcaptcha ? 'hcaptcha' : 'turnstile',
                  visible: shown, size: size, height: Math.round(r.height)});
  }
  const tokens = Array.from(document.querySelectorAll(
    '[name="g-recaptcha-response"], [name="h-captcha-response"], [name="cf-turnstile-response"]'))
    .map((t) => !!(t.value || '').trim());
  return {widgets: widgets, tokens: tokens};
}"""


def captcha_widgets(page) -> list[dict[str, Any]]:
    """Every frame document's CAPTCHA widgets (`_CAPTCHA_WIDGETS_JS`): one
    row per document that has one, {widgets, tokens, frame}."""
    out: list[dict[str, Any]] = []
    try:
        all_frames = frames(page)
    except Exception:       # noqa: BLE001  (a page double)
        return out
    for idx, frame in enumerate(all_frames):
        evaluate = getattr(frame, "evaluate", None)
        if evaluate is None:
            continue
        try:
            got = evaluate(_CAPTCHA_WIDGETS_JS)
        except Exception:       # noqa: BLE001  (a detached or cross-origin frame)
            continue
        if isinstance(got, Mapping) and got.get("widgets"):
            out.append({**got, "frame": idx})
    return out


_CHECKBOX_SIZES = ("normal", "compact", "flexible")


def unsolved_checkbox(page) -> str:
    """A visible reCAPTCHA, hCaptcha or Turnstile checkbox (its frame's size
    `normal`, whatever its height: study G11; Turnstile's compact and
    flexible too) whose document holds an empty response token, or none:
    the provider's name, else "". The invisible badge (`size=invisible`)
    never counts."""
    for row in captcha_widgets(page):
        normal = [w for w in row.get("widgets") or [] if w.get("visible")
                  and (w.get("size") == "normal" or (w.get("provider") == "turnstile"
                                                     and w.get("size") in _CHECKBOX_SIZES))]
        if not normal:
            continue
        tokens = row.get("tokens") or []
        if not tokens or not all(tokens):
            return str(normal[0].get("provider") or "captcha")
    return ""
