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
import re
import weakref
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping
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
    # How the control works when it is no plain native box (SP5): "choice" (a
    # custom radio group or Yes / No buttons, clicked through
    # `option_locators`), "checkbox_group" (one question's boxes, ticked
    # through `option_locators`), "popup" (a dropdown drawn as a button),
    # "typeahead" (a text box that offers matches as it is typed in),
    # "hidden_select" (a hidden <select> behind a styled trigger),
    # "aria_check" (a custom tick box), "editable" (a rich-text box),
    # "date:MDY" (date parts in that order, `option_locators`); "" else.
    widget: str = ""
    # the visible thing to click for a hidden native box (study G6: its label
    # or proxy), in the control's frame; None when the control takes the act
    click_locator: tuple[int, str] | None = None
    option_locators: list[str] = field(default_factory=list)   # per option, in its frame
    section: str = ""           # the heading the control sits under (READ-05)
    ident: str = ""             # who the control is (tag|type|id|name|aria|...): read again
                                # before every act (FILL-02)


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
    posting's entry (INV-03). `chrome`: it sits in the site's header, nav or
    search landmark, a Workday header, or a bar fixed to the top of the page
    (study G4: a header's "Sign In" is no sign-in page)."""
    n: int
    locator: tuple[int, str]
    text: str
    kind_hint: str = ""
    in_form: bool = False
    chrome: bool = False        # in the site's header, nav or top bar (study G4): kept for
                                # the mapping, left out of the page read
    disabled: bool = False      # disabled or aria-disabled now (study G10: a Submit that
                                # waits for the form to validate is kept, flagged)
    primary: bool = False       # styled as the page's main action (a primary or CTA class,
                                # or its form's one submit control)


@dataclass
class FormDigest:
    url_host: str
    title: str
    text: str
    fields: list[Field] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)
    dialog: str = ""            # an open modal's title: its controls are the page's (G9)

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
                        autocomplete=str(f.get("autocomplete", "") or ""),
                        widget=str(f.get("widget", "") or ""),
                        click_locator=_locator(f["click_locator"]) if f.get("click_locator")
                        else None,
                        option_locators=[str(o) for o in (f.get("option_locators") or [])],
                        section=str(f.get("section", "") or ""),
                        ident=str(f.get("ident", "") or ""))
                  for f in (raw.get("fields") or [])]
        buttons = [Button(n=int(b["n"]), locator=_locator(b.get("locator")),
                          text=str(b.get("text", "")),
                          kind_hint=str(b.get("kind_hint", "") or ""),
                          in_form=bool(b.get("in_form", False)),
                          chrome=bool(b.get("chrome", False)),
                          disabled=bool(b.get("disabled", False)),
                          primary=bool(b.get("primary", False)))
                   for b in (raw.get("buttons") or [])]
        return cls(url_host=str(raw.get("url_host", "")), title=str(raw.get("title", "")),
                   text=str(raw.get("text", "")), fields=fields, buttons=buttons,
                   dialog=str(raw.get("dialog", "") or ""))


def _locator(raw: Any) -> tuple[int, str]:
    if not raw:
        return (0, "")
    frame, css = raw
    return (int(frame), str(css))


# --- the extractor ---------------------------------------------------------------

# One pass over a frame's DOM. Returns {"fields": [...], "buttons": [...], "text": str}
# with plain values only; the dataclasses are built in Python. The rules:
#   tree     the composed tree: open shadow roots are walked where their host
#            stands (study G12: UKG's buttons, SAP's header).
#   fields   visible, enabled input (not hidden/submit/button/image/reset), select,
#            textarea, [role=combobox], [role=listbox]; radios collapse into one
#            entry per name group, or per question box when each radio has a
#            name of its own; a file input is kept even when hidden because
#            set_input_files works on it and ATS pages hide it behind a styled
#            button, while its box has a layout and its form (or, outside a
#            form, a box within three levels above it) shows; an input inside
#            a [role=combobox] is part of that widget. SP5 (the study's G3,
#            G6, G7, G8, EXT-02..13) adds, with `widget` naming how each works:
#            a custom radio group of [role=radio] and sibling Yes / No buttons
#            with aria-pressed ("choice"); a question's tick boxes, by their
#            shared name, an id's question prefix or their question box
#            ("checkbox_group"); a dropdown drawn as a button or a box with
#            aria-haspopup ("popup"), and a text box inside one ("combo"); an
#            untyped text box with a results list or a hidden "selected" value
#            beside it ("typeahead"); a hidden checkbox, radio or select behind
#            a visible label or trigger, acted on through `click_locator`
#            ("hidden_select" for a select); a custom tick box ("aria_check");
#            a rich-text box ("editable"); Month / Day / Year boxes ("date:MDY").
#            No field: a read-only box, a honeypot (its words or id, off the
#            page, two pixels or less, under aria-hidden, see-through with
#            tabindex -1), a posting's job-alert, sort or search widget, a
#            placeholder option ("Click here...", "-- No answer --").
#   locator  #id when the id is a plain CSS identifier and unique across the
#            composed tree, else a unique data-automation-id / data-testid /
#            data-qa, else [name="..."] when unique, else an nth-of-type path
#            from the body or the shadow root; inside a shadow root it is
#            `<host> >> <inner>` (a Playwright CSS query pierces the root).
#            `ident` carries who the control is, checked before every act.
#   label    what a person sees (G2): label[for] (only for a unique id whose
#            control the label is), a non-generic aria-label ("Search",
#            "Select...", "textbox" fall through; Workday's "Select One" and
#            "Required" come off), aria-labelledby, an enclosing label, the
#            question box's words (up to five boxes above, holding no other
#            question's control, its visible text before the first control),
#            a fieldset's legend, the nearest preceding text, the words after a
#            tick box, else the placeholder. Visible text only: no display:none
#            or aria-hidden part, no listbox, option, menu or alert, no control,
#            no dropdown's shown value; sr-only text is set apart. A file box
#            takes its group's or fieldset's question, its label or its box's
#            words unless they are the upload's own ("Attach", "Drop your file
#            or upload", "Total 0 file selected"), else its upload button's
#            text; one inside an "Autofill from resume" box is the parser's
#            (`help` "autofill parser", never required). A group takes its
#            radiogroup's or group's name, its legend, its box's words, else
#            the text before its first control.
#   required the attribute, aria-required="true", or a required marker (a
#            star, ✱, "(required)", "*Required", an sr-only "Required", an
#            aria-hidden star) stripped from the label's words.
#   section  the nearest h2 to h4 or heading above the control (READ-05).
#   help     the aria-describedby text, then "Max N characters." from a
#            maxlength (the answer generator's length budget).
#   options  select option texts minus empty or "Select..." placeholders; radio
#            option labels; ["checked"] for a checkbox; for a combobox the
#            [role=option] texts of the listbox it controls when one is in the
#            DOM (apply_fill.open_listbox_options reads it live otherwise).
#   buttons  button, [role=button], input[type=submit|button], a.btn,
#            a[class*=button]; text from innerText, value, aria-label, title
#            (a shadow button's host's words); a widget's own buttons (Yes /
#            No, a dropdown's trigger) are no buttons. A disabled one is kept
#            with `disabled` (G10); `primary` when styled as the main action
#            or its form's one submit.
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
#   modal    an open modal (`dialog[open]`, `aria-modal=true`, Workday's
#            `data-automation-activepopup=true`, a dialog covering over 40% of
#            the viewport, or a dialog of 280 x 200 px or more on top of the
#            page, the element at its centre inside it; never a consent
#            banner or preference center, never a chat window) is
#            the page while it is open (study G9: Workday's "Start Your
#            Application", Teamtailor's form overlay): only its fields and
#            buttons are kept, its text goes first, and its title is
#            returned as `dialog`.
#   top bar  a Workday header (`data-automation-id*=header`) and a bar fixed
#            or sticky at the top of the page (study G4) are chrome like the
#            landmarks: no field is kept there, and its buttons carry
#            `chrome` (the page read leaves them out, the entry and the
#            advance never take one; a footer's button is no chrome, a
#            wizard's Next can live there). A button with no
#            text, value, aria-label or title is dropped.
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
    // label[for] only when the id is the element's alone and the label's
    // control is this element (study G2e: Ashby gives every option one id)
    const root = el.getRootNode();
    const esc = el.id.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    if (root.querySelectorAll('[id="' + esc + '"]').length === 1) {
      const l = Array.from(root.querySelectorAll('label[for="' + esc + '"]')).find((x) => x.control === el);
      if (l) { const t = minus(l); if (t) return t; }
    }
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
  // a consent vendor's own preference center is consent UI whatever it holds
  // (OneTrust's has a vendor search box, which `blocked` would keep out)
  for (const el of document.querySelectorAll('#onetrust-pc-sdk, #onetrust-consent-sdk')) {
    if (el !== document.body) hits.push(el);
  }
  for (const el of found) {
    if (hits.includes(el) || blocked(el)) continue;
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

# Who a control is (FILL-02): its tag, type, id, name, aria-label, stable
# test attributes, placeholder, and the words of its label (the labels that
# name it, else aria-labelledby, else the box it sits in alone), letters only
# so a counter or a count never changes it. One definition for the extractor
# (`Field.ident`) and the filler, which reads it again before every act
# (`apply_fill._same_control`); two idents name the same control when every
# attribute agrees and one label's words start the other's (`SAME_IDENT_JS`).
IDENT_FN_JS = r"""(el) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const words = (s) => norm((s || '').toLowerCase().replace(/[^a-z]+/g, ' ')).slice(0, 60);
  const minus = (node) => {
    const c = node.cloneNode(true);
    c.querySelectorAll('input, select, textarea, button, option, script, style').forEach((n) => n.remove());
    return c.textContent;
  };
  let label = '';
  const labs = el.labels ? Array.from(el.labels) : [];
  if (labs.length) label = words(labs.map(minus).join(' '));
  const by = el.getAttribute('aria-labelledby');
  if (!label && by) {
    const root = el.getRootNode();
    label = words(by.split(/\s+/).map((id) => {
      const n = (root !== document && root.getElementById ? root.getElementById(id) : null)
        || document.getElementById(id);
      return n ? minus(n) : '';
    }).join(' '));
  }
  let p = el.parentElement;
  for (let i = 0; !label && p && i < 3; i++, p = p.parentElement) {
    if (p.querySelectorAll('input:not([type=hidden]), select, textarea, [contenteditable]').length > 1) break;
    label = words(minus(p));
  }
  return [el.tagName.toLowerCase(), (el.getAttribute('type') || '').toLowerCase(), el.id || '',
    el.getAttribute('name') || '', norm(el.getAttribute('aria-label')),
    el.getAttribute('data-automation-id') || '', el.getAttribute('data-testid') || '',
    el.getAttribute('data-qa') || '', norm(el.getAttribute('placeholder')), label].join('|');
}"""
SAME_IDENT_JS = r"""(a, b) => {
  const A = String(a).split('|'), B = String(b).split('|');
  const la = A.pop(), lb = B.pop();
  return A.join('|') === B.join('|') && (la === lb || (!!la && !!lb && (la.startsWith(lb) || lb.startsWith(la))));
}"""


def same_ident(a: str, b: str) -> bool:
    """`SAME_IDENT_JS` in Python: every attribute of the two idents agrees and
    one label's words start the other's."""
    pa, pb = str(a).split("|"), str(b).split("|")
    la, lb = pa.pop(), pb.pop()
    return pa == pb and (la == lb or (bool(la) and bool(lb)
                                      and (la.startswith(lb) or lb.startswith(la))))


# A placeholder text a dropdown shows before a pick ("Select...", "-- Select --",
# "Choose one"), one pattern for the extractor and the filler's read-backs.
PLACEHOLDER_TEXT_JS = r"/^(select|choose|pick|please|--|\u2013|\u2014)/i"


_EXTRACT_JS = r"""
(cap) => {
  const CONTROL = /^(INPUT|SELECT|TEXTAREA|BUTTON)$/;
  const SKIP_INPUT = new Set(['hidden', 'submit', 'button', 'image', 'reset']);
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const q = (s) => '"' + s.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  const cssIdent = /^-?[_a-zA-Z][_a-zA-Z0-9-]*$/;
  const typeAttr = (el) => (el.getAttribute('type') || 'text').toLowerCase();

  // the composed tree (G12): every element in document order, each open
  // shadow root walked where its host stands
  const roots = [document];
  const all = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      all.push(el);
      if (el.shadowRoot) { roots.push(el.shadowRoot); walk(el.shadowRoot); }
    }
  };
  walk(document);
  const order = new Map(all.map((el, i) => [el, i]));
  const up = (n) => n.parentElement || ((n.getRootNode && n.getRootNode().host) || null);
  const closestC = (el, sel) => {
    for (let n = el; n; n = up(n)) { if (n.nodeType === 1 && n.matches(sel)) return n; }
    return null;
  };
  const containsC = (a, b) => { for (let n = b; n; n = up(n)) { if (n === a) return true; } return false; };
  const rootOf = (el) => el.getRootNode();
  const byIdIn = (el, id) => {
    const r = rootOf(el);
    return (r !== document && r.getElementById ? r.getElementById(id) : null)
      || document.getElementById(id);
  };
  const queryIn = (el, sel) => {
    try { return Array.from(rootOf(el).querySelectorAll(sel)); } catch (e) { return []; }
  };

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
    const c = closestC(el, CHROME);
    return !!c && !(up(c) && closestC(up(c), 'form, dialog, [role=dialog]'));
  };
  // a button's chrome: the header, nav and search landmarks only; a footer
  // holds a wizard's Next often enough (review M11)
  const HEAD_CHROME = 'header, nav, search, [role=banner], [role=navigation], [role=search]';
  const inHeadChrome = (el) => {
    const c = closestC(el, HEAD_CHROME);
    return !!c && !(up(c) && closestC(up(c), 'form, dialog, [role=dialog]'));
  };
  const consent = (__CONSENT__)();
  const inConsent = (el) => consent.some((root) => containsC(root, el));
  // an open modal is the page while it is open (G9)
  const vw = window.innerWidth || 1, vh = window.innerHeight || 1;
  const covers = (el) => {
    const r = el.getBoundingClientRect();
    const w = Math.min(r.right, vw) - Math.max(r.left, 0);
    const h = Math.min(r.bottom, vh) - Math.max(r.top, 0);
    return w > 0 && h > 0 && w * h > 0.4 * vw * vh;
  };
  // on top of the page: the element at its centre lies inside it (Workday's
  // "Start Your Application" popup is 442 x 451 px with no aria-modal)
  const onTop = (el) => {
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx < 0 || cy < 0 || cx >= vw || cy >= vh) return false;
    const hit = document.elementFromPoint(cx, cy);
    return !!hit && el.contains(hit);
  };
  // a chat window is a dialog of its own, never the page
  const CHAT = /chat|intercom|drift|messenger|zendesk|livechat|hubspot|olark|tawk|crisp/i;
  const sized = (el) => {
    const r = el.getBoundingClientRect();
    return r.width >= 280 && r.height >= 200;
  };
  const modal = Array.from(document.querySelectorAll(
      'dialog[open], [role=dialog], [role=alertdialog], [aria-modal=true]')).find((el) =>
    visible(el) && !inConsent(el) && !consent.some((root) => el.contains(root))
    && (el.matches('dialog[open]') || el.getAttribute('aria-modal') === 'true'
        || el.getAttribute('data-automation-activepopup') === 'true' || covers(el)
        || (el.matches('[role=dialog], [role=alertdialog]') && sized(el) && onTop(el)
            && !CHAT.test((el.id || '') + ' ' + (el.getAttribute('class') || '')
                          + ' ' + (el.getAttribute('aria-label') || '')))));
  const outsideModal = (el) => !!modal && !containsC(modal, el);
  const modalTitle = (() => {
    if (!modal) return '';
    const by = modal.getAttribute('aria-labelledby');
    if (by) {
      const t = norm(by.split(/\s+/).map((id) => {
        const n = document.getElementById(id); return n ? n.textContent : '';
      }).join(' '));
      if (t) return t;
    }
    const aria = norm(modal.getAttribute('aria-label'));
    if (aria) return aria;
    const h = modal.querySelector('h1, h2, h3, legend');
    return h ? norm(h.textContent) : '';
  })();
  // a Workday header, a bar fixed or sticky at the top of the page (G4)
  const topBar = (el) => {
    const head = closestC(el, '[data-automation-id*=header i]');
    if (head && !closestC(el, 'form, dialog, [role=dialog]')) return true;
    let cur = up(el);
    for (let i = 0; cur && cur !== document.body && i < 10; i++, cur = up(cur)) {
      if (cur.matches('form, dialog, [role=dialog], [aria-modal=true]')) return false;
      const pos = getComputedStyle(cur).position;
      if (pos === 'fixed' || pos === 'sticky') {
        const r = cur.getBoundingClientRect();
        return r.top < 80 && r.height < 200;
      }
    }
    return false;
  };
  // a hidden file box's form, or outside a form a box within three levels
  // above it, still shows (a styled upload hides the input itself)
  const boxShows = (el) => {
    // under a hidden box (display:none above it): kept only while its
    // question shows, a label of its own or the box above the hidden part
    // holding words and no other question (JazzHR hides its upload until
    // "Attach resume" is clicked); a hidden wizard step drops (review I3)
    let hiddenTop = null;
    for (let p = up(el); p && p !== document.body; p = up(p)) {
      if (p.nodeType === 1 && !p.getClientRects().length
          && getComputedStyle(p).display !== 'contents') hiddenTop = p;
    }
    if (hiddenTop) {
      const lab = labelElementFor(el);
      if (!(lab && visible(lab))) {
        const shown = up(hiddenTop);
        if (!shown || shown === document.body || shown === document.documentElement
            || shown.matches('form, main, [role=main], dialog, [role=dialog]')) return false;
        const others = Array.from(shown.querySelectorAll(QUESTION_CTRL)).filter((c) =>
          c !== el && !hiddenTop.contains(c) && visible(c));
        if (others.length || !seen(shown)) return false;
      }
    }
    if (el.form) return visible(el.form);
    let p = up(el);
    for (let i = 0; p && i < 3; i++, p = up(p)) { if (visible(p)) return true; }
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

  // --- locators (EXT-19, G12) ---
  const countC = (sel) => {
    let n = 0;
    for (const r of roots) {
      try { n += r.querySelectorAll(sel).length; } catch (e) { return 99; }
    }
    return n;
  };
  // an nth-of-type path from the body (the document) or from the element's
  // shadow root
  const nthPath = (el) => {
    const parts = [];
    const root = rootOf(el);
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body) {
      let i = 1, sib = cur;
      while ((sib = sib.previousElementSibling)) { if (sib.tagName === cur.tagName) i++; }
      parts.unshift(cur.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      if (root !== document && cur.parentNode === root) break;
      cur = cur.parentElement;
    }
    return (root === document ? 'body > ' : '') + parts.join(' > ');
  };
  const STABLE = ['data-automation-id', 'data-testid', 'data-qa'];
  const ownLocator = (el) => {
    const root = rootOf(el);
    const one = (sel) => {
      try { return root.querySelectorAll(sel).length === 1 && countC(sel) === 1; }
      catch (e) { return false; }
    };
    if (el.id && cssIdent.test(el.id) && one('#' + el.id)) return '#' + el.id;
    for (const a of STABLE) {
      const v = el.getAttribute(a);
      if (v) {
        const sel = el.tagName.toLowerCase() + '[' + a + '=' + q(v) + ']';
        if (one(sel)) return sel;
      }
    }
    const name = el.getAttribute('name');
    if (name && one('[name=' + q(name) + ']')) return '[name=' + q(name) + ']';
    return nthPath(el);
  };
  // `<host> >> <inner>` for an element inside an open shadow root: a
  // Playwright CSS query pierces the host's shadow root
  const hostPrefix = (el) => {
    const parts = [];
    let host = rootOf(el).host;
    while (host) { parts.unshift(ownLocator(host)); host = rootOf(host).host; }
    return parts.length ? parts.join(' >> ') + ' >> ' : '';
  };
  const locatorFor = (el) => hostPrefix(el) + ownLocator(el);
  // who the control is, read again before every act (FILL-02; `IDENT_FN_JS`)
  const identOf = __IDENT__;

  // --- what a person sees (G2) ---
  const SR_CLASS = /(^|[\s_-])(sr-only|visually-?hidden|screen-?reader(-only|-text)?|a11y-hidden|visuallyhidden|assistive-text)([\s_-]|$)/i;
  const srOnly = (el) => {
    if (SR_CLASS.test(el.getAttribute('class') || '')) return true;
    const st = getComputedStyle(el);
    if (st.position !== 'absolute' && st.position !== 'fixed') return false;
    const r = el.getBoundingClientRect();
    return (r.width <= 1 && r.height <= 1) || /rect\(0/.test(st.clip || '')
      || /inset\((50|100)%\)/.test(st.clipPath || '');
  };
  const SKIP_TEXT = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|INPUT|SELECT|TEXTAREA|BUTTON|OPTION|DATALIST|IFRAME|SVG)$/i;
  // a form widget's own words inside a label (review I4): a button, a
  // combobox, listbox or spin box, an editable box, a dropdown drawn as a box;
  // never a link (Oracle's "terms and conditions" opens a dialog)
  const WIDGET_TEXT = 'button, [role=combobox], [role=listbox], [role=spinbutton], '
    + '[role=textbox], [contenteditable=""], [contenteditable=true], '
    + '[aria-haspopup]:not(a):not([role=link])';
  const QUIET_ROLES = /^(listbox|option|menu|menuitem|menuitemradio|menuitemcheckbox|tooltip|alert|status)$/;
  // the visible text of a subtree: never a display:none or visibility:hidden
  // part, an aria-hidden subtree, a listbox, option, menu or alert, a control
  // or an svg; sr-only text goes to `marks` (a marker there still counts:
  // Teamtailor's sr-only "Required"); `stop(el)` ends the walk at that
  // element (a question's text before its first control)
  const REQ_CLASS = /(^|[\s_-])required([\s_-]|$)/i;
  const NOT_REQ_CLASS = /not[\s_-]?required|optional/i;
  const MARK_CHARS = /^\s*(?:[*\u2731\uff0a]+|\(\s*required\s*\)|required)\s*$/i;
  const styledMark = (n) => {
    const cls = n.getAttribute('class') || '';
    if (REQ_CLASS.test(cls) && !NOT_REQ_CLASS.test(cls)) return true;
    for (const pseudo of ['::after', '::before']) {
      const c = getComputedStyle(n, pseudo).content || '';
      if (!c || c === 'none' || c === 'normal') continue;
      if (MARK_CHARS.test(c.replace(/^["']|["']$/g, ''))) return true;
    }
    return false;
  };
  const seen = (node, marks, stop) => {
    let out = '';
    let stopped = false;
    const rec = (n, top) => {
      if (stopped) return;
      if (n.nodeType === 3) {
        const p = n.parentElement;
        if (p && getComputedStyle(p).visibility !== 'hidden') out += n.data;
        return;
      }
      if (n.nodeType !== 1) return;
      if (!top && stop && stop(n)) { stopped = true; return; }
      if (SKIP_TEXT.test(n.tagName)) return;
      if (!top && n.getAttribute('aria-hidden') === 'true') {
        // hidden from the reader, seen by the person: a marker there counts
        if (marks && n.getClientRects().length) marks.push(norm(n.textContent));
        return;
      }
      if (!top && QUIET_ROLES.test(n.getAttribute('role') || '')) return;
      // a required marker drawn in CSS (Ashby's ::after star) or named by a
      // class (Ashby's `_required_`): a marker the person sees (review I2)
      if (marks && styledMark(n)) marks.push('*');
      // a dropdown's shown value inside its label ("State Select a state")
      if (!top && n.matches(WIDGET_TEXT)) return;
      const st = getComputedStyle(n);
      if (st.display === 'none') return;
      if (!top && srOnly(n)) { if (marks) marks.push(norm(n.textContent)); return; }
      const block = !/^inline/.test(st.display) && st.display !== 'contents';
      if (block) out += ' ';
      for (const c of n.childNodes) rec(c, false);
      if (block) out += ' ';
    };
    rec(node, true);
    return norm(out);
  };
  const textMinus = (node) => {
    const clone = node.cloneNode(true);
    clone.querySelectorAll('input, select, textarea, button, script, style').forEach((n) => n.remove());
    return norm(clone.textContent);
  };
  // a label's text: what shows, else (a label the page hides itself) its words
  const labelText = (node, marks) => {
    const t = seen(node, marks);
    return t || node.getClientRects().length ? t : textMinus(node);
  };
  const byIds = (el, ids, marks) => norm(ids.split(/\s+/).map((id) => {
    const n = byIdIn(el, id);
    return n && n !== el ? labelText(n, marks) : '';
  }).join(' '));
  // required markers (G2a): `*`, `✱`, `(required)`, `*Required`; `(optional)`
  const MARK_ONLY = /^\s*(?:[*✱＊]+|\(\s*required\s*\)|required\.?|[*✱＊]\s*required\.?|\(\s*optional\s*\)|optional)\s*$/i;
  const REQ_MARK = /^\s*(?:[*✱＊]+|\(\s*required\s*\)|required\.?|[*✱＊]\s*required\.?)\s*$/i;
  const LEAD_STAR = /^\s*[*✱＊]+\s*/;
  const TRAIL_REQ = /\s*(?:[*✱＊]+\s*(?:required\.?)?|\(\s*required\s*\)\.?)\s*$/i;
  const TRAIL_OPT = /\s*\(\s*optional\s*\)\s*$/i;
  // [the label without its markers, whether a required marker was there]
  const strip = (text, marks) => {
    let t = norm(text), req = !!marks && marks.some((m) => REQ_MARK.test(m));
    for (let i = 0; i < 3; i++) {
      const before = t;
      if (TRAIL_REQ.test(t)) { req = true; t = t.replace(TRAIL_REQ, ''); }
      t = t.replace(TRAIL_OPT, '');
      if (LEAD_STAR.test(t)) { req = true; t = t.replace(LEAD_STAR, ''); }
      if (t === before) break;
    }
    if (MARK_ONLY.test(t)) { req = req || REQ_MARK.test(t); t = ''; }
    return [norm(t), req];
  };
  // an aria-label that names no question (G2d): "Search", "Select...", "textbox"
  const GENERIC = /^(search|select( one| an option| an item)?|choose( one| an option)?|pick one|textbox|text box|combobox|input( \w+)?|type here|start typing|enter text|type to search)\s*(\.{3}|…)?$/i;
  // an aria-label's own words: Workday's "Country Select One Required"
  const ariaWords = (text) => {
    let t = norm(norm(text).replace(/\bselect one\b/ig, ' ')), req = false;
    if (/\s+required\s*$/i.test(t)) { req = true; t = t.replace(/\s+required\s*$/i, ''); }
    return GENERIC.test(t) ? ['', req] : [t, req];
  };
  const srAncestor = (p) => {
    for (let n = p, i = 0; n && i < 3; n = n.parentElement, i++) { if (srOnly(n)) return true; }
    return false;
  };
  const shows = (textNode) => {
    const p = textNode.parentElement;
    if (!p || /^(SCRIPT|STYLE|OPTION|NOSCRIPT|TEMPLATE)$/.test(p.tagName)) return false;
    if (closestC(p, '[aria-hidden=true], [role=listbox], [role=option], [role=menu], [role=alert], '
                    + '[role=status], svg')) return false;
    return !!p.getClientRects().length && getComputedStyle(p).visibility !== 'hidden';
  };
  // the nearest text before the control: marker-only and hidden text is
  // passed over (a marker noted), another control's label or a control ends
  // the walk
  const precedingText = (el, skipWithin, marks) => {
    const root = rootOf(el);
    const walker = document.createTreeWalker(root === document ? document.body : root,
                                             NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
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
      if (!t) continue;
      if (!shows(node)) {
        // an aria-hidden star is still one the person sees
        if (marks && MARK_ONLY.test(t) && p && p.getClientRects().length) marks.push(t);
        continue;
      }
      if (srAncestor(p) || MARK_ONLY.test(t)) { if (marks) marks.push(t); continue; }
      return t.slice(-120);
    }
    return '';
  };
  const optionLabel = __OPTION_LABEL__;
  // label[for] counts only when its id is unique and the label's control is
  // this element (G2e: Ashby reuses a question's id on every option)
  const labelElementFor = (el) => {
    if (!el.id) return null;
    const labs = queryIn(el, 'label[for=' + q(el.id) + ']');
    if (queryIn(el, '[id=' + q(el.id) + ']').length !== 1) return null;
    return labs.find((l) => l.control === el) || null;
  };
  const QUESTION_CTRL = 'input:not([type=hidden]):not([type=submit]):not([type=button])'
    + ':not([type=reset]):not([type=image]), select, textarea, [role=combobox], [role=radio], '
    + '[role=checkbox], [role=switch], [role=spinbutton], [role=textbox], [contenteditable=""], '
    + '[contenteditable=true], button[aria-pressed], [aria-haspopup=listbox]';
  const kept = new Set();       // hidden natives kept behind a visible proxy (G6)
  // the question's box (G2e): up to five boxes above the control, the first
  // that holds all of `own` and no other question's control
  const questionBox = (own) => {
    const list = Array.from(own);
    let p = up(list[0]);
    for (let i = 0; p && i < 5; i++, p = up(p)) {
      if (p === document.body || p === document.documentElement
          || p.matches('form, main, [role=main], dialog, [role=dialog]')) return null;
      const others = Array.from(p.querySelectorAll(QUESTION_CTRL)).filter((c) =>
        !own.has(c) && !list.some((o) => o.contains(c)) && (visible(c) || kept.has(c)));
      if (others.length) return null;
      if (list.every((e) => containsC(p, e))) return p;
    }
    return null;
  };
  const isCtrl = (n) => n.matches(QUESTION_CTRL + ', button, [role=button]');
  // an upload's own words, never its question (G2c): "Attach", "Drop your file
  // or upload", "Total 0 file selected", "file-input", a missing-SVG fallback
  const FACE = /^(attach|upload( an?)?( files?)?|browse|choose( an?)? files?|select( an?)? files?|no file chosen|drop (your )?files?( here)?( or upload)?|drag (and|&) drop.*|click to upload|total \d+ files? selected|file-?input|svgs? (are )?not supported.*)\.?$/i;
  const STEP = /^\s*step\s+\d+\s*(of|\/)\s*\d+\s*$/i;     // a wizard's step marker
  // an upload's own clickable face ("Attach", "ATTACH RESUME/CV") is no question
  const isUpload = (n) => isCtrl(n) || n.matches('a[href], [role=link]')
    || (n.tagName === 'LABEL' && !!n.control && n.control.type === 'file');
  // the question's own words: its box's visible text before the first control
  const boxText = (box, marks, stop) => box ? seen(box, marks, stop || isCtrl).slice(0, 300) : '';
  // the words of the question `own` answers: up to five boxes above it, each
  // holding no other question's control, the first whose words before its
  // first control say something (a select's own wrapper often holds only
  // the select; an upload's face is skipped for a file box)
  const questionText = (own, marks, file) => {
    const list = Array.from(own);
    let p = up(list[0]);
    for (let i = 0; p && i < 5; i++, p = up(p)) {
      if (p === document.body || p === document.documentElement
          || p.matches('form, main, [role=main], dialog, [role=dialog]')) return '';
      const others = Array.from(p.querySelectorAll(QUESTION_CTRL)).filter((c) =>
        !own.has(c) && !list.some((o) => o.contains(c)) && (visible(c) || kept.has(c)));
      if (others.length) return '';
      if (!list.every((e) => containsC(p, e))) continue;
      if (file && p.matches('a, button, [role=button]')) continue;
      const mine = [];
      const t = boxText(p, mine, file ? isUpload : isCtrl);
      if (t && !STEP.test(t) && !(file && FACE.test(strip(t)[0]))) {
        if (marks) marks.push(...mine);
        return t;
      }
    }
    return '';
  };
  const AUTOFILL = /autofill|auto-fill|import (your )?(resume|cv)|parse (your )?(resume|cv)|apply with (your )?resume/i;

  // [label, required] of one control
  // is the question the control answers marked required: its fieldset's
  // own title (a legend, or a label that is none of the control's), by a
  // marker in its words or drawn by CSS (review I2)
  const titleReq = (el) => {
    const fs = el.closest('fieldset');
    if (!fs) return false;
    const title = Array.from(fs.children).find((c) => c.matches('legend, label'));
    if (!title || title.contains(el) || title === labelElementFor(el)) return false;
    const m = [];
    const [, req] = strip(seen(title, m), m);
    return req;
  };
  const labelFor = (el, marks) => {
    const [label, req] = labelWords(el, marks);
    return [label, req || titleReq(el)];
  };
  const labelWords = (el, marks) => {
    const own = new Set([el]);
    const tryText = (t) => { const [s, r] = strip(t, marks); return [s, r]; };
    const lab = labelElementFor(el);
    if (el.tagName === 'INPUT' && typeAttr(el) === 'file') {
      // a file box by its question (G2c): its group, its fieldset, its
      // box's words, and only then its label (an "Attach" button's)
      const grp = closestC(el, '[role=group]');
      if (grp) {
        const aria = ariaWords(grp.getAttribute('aria-label'));
        if (aria[0]) return aria;
        const by = grp.getAttribute('aria-labelledby');
        if (by) { const t = tryText(byIds(grp, by, marks)); if (t[0]) return t; }
      }
      const fs = el.closest('fieldset');
      const legend = fs && fs.querySelector('legend');
      if (legend) { const t = tryText(seen(legend, marks)); if (t[0]) return t; }
      if (lab) { const t = tryText(labelText(lab, marks)); if (t[0] && !FACE.test(t[0])) return t; }
      const q1 = questionText(own, marks, true);
      if (q1) { const t = tryText(q1); if (t[0]) return t; }
      const a1 = ariaWords(el.getAttribute('aria-label'));
      if (a1[0] && !FACE.test(a1[0])) return a1;
      // last, the upload's own button ("Select Resume to Upload", Breezy's
      // "Upload Resume*"), up to four boxes above it
      let box = up(el);
      for (let i = 0; box && i < 4; i++, box = up(box)) {
        const face = Array.from(box.querySelectorAll('button, [role=button], a[href]'))
          .map((b) => norm(b.innerText)).find((t) => /upload|resume|cv|attach|file/i.test(t));
        if (face) return tryText(face);
      }
      return ['', false];
    }
    if (lab) { const t = tryText(labelText(lab, marks)); if (t[0]) return t; }
    const aria = ariaWords(el.getAttribute('aria-label'));
    if (aria[0]) return aria;
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = tryText(byIds(el, by, marks)); if (t[0]) return t; }
    const enclosing = el.closest('label');
    if (enclosing) { const t = tryText(labelText(enclosing, marks)); if (t[0]) return t; }
    const q2 = questionText(own, marks);
    if (q2) { const t = tryText(q2); if (t[0]) return t; }
    const fs = el.closest('fieldset');
    const legend = fs && fs.querySelector('legend');
    if (legend) { const t = tryText(seen(legend, marks)); if (t[0]) return t; }
    const before = tryText(precedingText(el, enclosing, marks));
    if (before[0]) return before;
    // a tick box's words after it; a box whose only words are its placeholder
    if (el.matches('input[type=checkbox], input[type=radio]')) {
      const after = tryText(optionLabel(el));
      if (after[0] && after[0] !== norm(el.value)) return after;
    }
    const ph = norm(el.getAttribute('placeholder'));
    return ph ? [ph, before[1]] : before;
  };
  // [label, required] of a group of controls (radios, checkboxes, choice
  // buttons): its group's name, its fieldset's legend, its box's words,
  // else the text before its first control
  const groupLabelFor = (members, group, marks) => {
    const tryText = (t) => strip(t, marks);
    const first = members[0];
    const rg = group || closestC(first, '[role=radiogroup], [role=group]');
    if (rg) {
      const aria = ariaWords(rg.getAttribute('aria-label'));
      if (aria[0]) return aria;
      const by = rg.getAttribute('aria-labelledby');
      if (by) { const t = tryText(byIds(rg, by, marks)); if (t[0]) return t; }
    }
    const fs = first.closest('fieldset');
    const legend = fs && fs.querySelector('legend');
    if (legend) { const t = tryText(seen(legend, marks)); if (t[0]) return t; }
    const q3 = questionText(new Set(members), marks);
    if (q3) { const t = tryText(q3); if (t[0]) return t; }
    return tryText(precedingText(first, first.closest('label'), marks));
  };

  const typeOf = (el) => {
    const role = el.getAttribute('role') || '';
    if (role === 'combobox' || role === 'listbox') return 'listbox';
    const tag = el.tagName;
    if (tag === 'SELECT') return 'select';
    if (tag === 'TEXTAREA') return 'textarea';
    if (tag === 'INPUT') {
      const t = typeAttr(el);
      if (t === 'text' || t === 'search') return 'text';
      if (['email', 'tel', 'url', 'number', 'file', 'date', 'radio', 'checkbox'].includes(t)) return t;
    }
    return 'other';
  };
  // a placeholder option: an empty-valued "Select..." or "--", and "Click
  // here..." or "-- No answer --" whatever its value (G7)
  const PLACEHOLDER_OPTION = __PLACEHOLDER__;
  const PLACEHOLDER_ANY = /^(click here\b|-+\s*(no answer|none|select)?\s*-+$|\u2013\s*select\s*\u2013$)/i;
  const selectOptions = (el) => Array.from(el.options).map((o) => norm(o.text)).filter((t, i) => {
    const o = el.options[i];
    if (!t || PLACEHOLDER_ANY.test(t)) return false;
    return !(o.value === '' && (o.disabled || PLACEHOLDER_OPTION.test(t)));
  });
  const listboxFor = (el) => {
    if (el.getAttribute('role') === 'listbox') return el;
    const ids = (el.getAttribute('aria-controls') || '') + ' ' + (el.getAttribute('aria-owns') || '');
    for (const id of ids.split(/\s+/).filter(Boolean)) {
      const n = byIdIn(el, id);
      if (n) return n;
    }
    return el.querySelector('[role=listbox]') || (el.parentElement && el.parentElement.querySelector('[role=listbox]'));
  };
  const listboxOptions = (el) => {
    const lb = listboxFor(el);
    if (!lb) return [];
    return Array.from(lb.querySelectorAll('[role=option], [role=menuitemradio]'))
      .map((o) => norm(o.textContent)).filter((t) => t && !PLACEHOLDER_ANY.test(t));
  };
  const optionsFor = (el, type) => {
    if (type === 'select') return selectOptions(el);
    if (type === 'checkbox') return ['checked'];
    if (type === 'listbox') return listboxOptions(el);
    return [];
  };
  const isRequired = (el) => !!el.required || el.getAttribute('aria-required') === 'true';
  // the heading a control sits under (READ-05): the nearest h2 to h4 or
  // heading role before it, outside the site chrome
  const heads = all.filter((e) => e.matches('h2, h3, h4, [role=heading]') && !inChrome(e)
                           && !inConsent(e) && visible(e));
  const sectionOf = (el) => {
    const at = order.has(el) ? order.get(el) : -1;
    let best = null;
    for (const h of heads) {
      if (order.get(h) >= at) break;
      // its own box holds the control (review M7: a parser's box or the
      // posting's headings above the form are no section of it)
      const box = up(h);
      if (box && !box.matches('body, html, main, [role=main]') && containsC(box, el)) best = h;
    }
    return best ? seen(best).slice(0, 80) : '';
  };
  const describe = (el, type, label, required, css, options, extra) => {
    const desc = el.getAttribute('aria-describedby');
    const helps = [desc ? byIds(el, desc) : ''];
    const max = parseInt(el.getAttribute('maxlength') || '', 10);
    if (max > 0) helps.push('Max ' + max + ' characters.');
    const x = extra || {};
    if (x.help) helps.unshift(x.help);
    return {
      css: css, label: label, type: type, required: !!required,
      placeholder: norm(el.getAttribute('placeholder')),
      help: helps.filter(Boolean).join(' '),
      options: options,
      id_or_name: el.id || el.getAttribute('name') || '',
      autocomplete: norm(el.getAttribute('autocomplete')).toLowerCase(),
      click: x.click || '', option_css: x.option_css || [], widget: x.widget || '',
      section: sectionOf(el), ident: identOf(el),
    };
  };

  // --- junk boxes (G3) ---
  // a honeypot's words: "honeypot", "robots only", "if you are human", or a
  // label that is nothing but "leave this field blank" / "do not fill this"
  // (review M4: "Middle name (leave this field blank if none)" is a question)
  const HONEY = /honey[\s_-]?pot|robots? only|for robots|if you('re| are) (a )?human|^\s*(please )?(leave (this )?(field |box )?(blank|empty)|do not (fill|enter)( (in|this)( field| box)?)?)\.?\s*$/i;
  const HONEY_ID = /^hp[_-]|nickname_hp|honey/i;
  const POSTING_WIDGET = /job alerts?\b|receive (an |job )?alerts?\b|newsletter|\bsort by\b|search (for )?jobs\b|^\s*(search( (jobs|roles|positions|openings))?|keywords?|find (a )?jobs?)\s*$/i;
  const junk = (el, t, label) => {
    // a choice or a file box is often hidden behind its label or trigger
    const choice = t === 'checkbox' || t === 'radio' || t === 'file' || t === 'select';
    // a read-only box, never a picker that opens on a click (review M6:
    // react-select without search, a date picker)
    const picker = el.matches('[role=combobox], [aria-haspopup], [aria-autocomplete]')
      || /date|calendar|picker/i.test((el.getAttribute('class') || '') + ' '
                                      + (el.getAttribute('placeholder') || '') + ' ' + (el.id || '')
                                      + ' ' + (el.getAttribute('name') || ''));
    if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.readOnly && !choice && !picker) {
      return 'read-only';
    }
    if (HONEY_ID.test(el.id || '') || HONEY_ID.test(el.getAttribute('name') || '')
        || HONEY.test(label)) return 'honeypot';
    if (POSTING_WIDGET.test(label)) return 'posting widget';
    if (choice) return '';
    if (closestC(el, '[aria-hidden=true]')) return 'aria-hidden';
    const st = getComputedStyle(el);
    if (el.getAttribute('tabindex') === '-1' && parseFloat(st.opacity) < 0.1) return 'hidden';
    const r = el.getBoundingClientRect();
    const x = window.scrollX || 0, y = window.scrollY || 0;
    if (r.right + x < 0 || r.bottom + y < 0 || r.left + x < -500 || r.top + y < -500) return 'offscreen';
    if (r.width <= 2 && r.height <= 2) return 'tiny';
    return '';
  };

  // --- hidden natives behind a visible label or proxy (G6) ---
  const proxyFor = (el) => {
    if (el.tagName === 'SELECT') {
      // a styled trigger beside it (MUI, BambooHR) is what a person clicks
      let p = up(el);
      for (let i = 0; p && i < 2; i++, p = up(p)) {
        const b = Array.from(p.querySelectorAll('button, [role=button], [role=combobox], [aria-haspopup]'))
          .find((x) => x !== el && visible(x));
        if (b) return b;
      }
    }
    const lab = labelElementFor(el);
    if (lab && visible(lab)) return lab;
    const enc = el.closest('label');
    if (enc && visible(enc)) return enc;
    const aria = closestC(up(el) || el, '[role=radio], [role=checkbox], [role=option], '
                                        + '[role=menuitemradio], [role=menuitemcheckbox], [role=switch]');
    if (aria && visible(aria)) return aria;
    return null;
  };
  const faint = (el) => parseFloat(getComputedStyle(el).opacity) < 0.1;
  const usable = (el) => !enabled(el) ? false : !(inChrome(el) || inConsent(el) || outsideModal(el) || topBar(el));

  const items = [];             // {at, rec}: the fields in document order
  const consumed = new Set();   // controls already part of a field
  const asButtons = new Set();  // controls that are a field, never a button
  // a posting's widgets (EXT-16, study G3): the fields of a form whose own
  // buttons only search, filter, alert or subscribe; outside a form, of a box
  // named for filters, a search bar, job alerts or a newsletter, or whose
  // buttons only do those things; a lone picker beside the page's legal
  // links (a footer's language picker). Never a group with a password or a
  // file box, or one beside a button that goes on (submit, apply, next...).
  const WIDGET_BTN = /^(search( jobs| roles)?|find( jobs)?|go|filters?|apply filters?|clear( all| filters)?|reset( filters)?|notify me|alert me|subscribe|get (job )?alerts|create (a )?(job )?alert|set (up )?(an? )?(job )?alert|email me( jobs)?|send me (jobs|alerts))$/i;
  const WAY_ON = /\b(submit|apply|send|next|continue|save|finish|sign in|log in|create account|register)\b/i;
  const WIDGET_BOX = /(^|\s)([\w-]*[-_])?(filters?|job-?alerts?|newsletter|subscribe|search-?(bar|box|form|filters?))(\s|$)/i;
  const LEGAL = /^(terms( of (service|use))?|privacy( policy)?|cookies?( (policy|notice|settings))?|legal( notice)?|accessibility|powered by .+)$/i;
  const btnText = (b) => norm(b.innerText) || norm(b.value) || norm(b.getAttribute('aria-label'));
  const ownButtons = (box) => Array.from(box.querySelectorAll(
    'button, input[type=submit], input[type=button], [role=button]')).filter((b) => visible(b)
      && !b.matches('[aria-haspopup]') && !closestC(b, '[role=combobox]') && !!btnText(b));
  const serious = (box) => !!box.querySelector('input[type=password], input[type=file]');
  const widgetGroup = (el) => {
    const form = el.form || el.closest('form');
    if (form) {
      if (serious(form)) return false;
      if (form.matches('[role=search]')) return true;
      const btns = ownButtons(form);
      return btns.length > 0 && btns.every((b) => WIDGET_BTN.test(btnText(b)));
    }
    const picker = el.matches('select, [role=combobox], [aria-haspopup]') && !isRequired(el);
    let p = up(el);
    for (let i = 0; p && i < 9; i++, p = up(p)) {
      if (p === document.body || p === document.documentElement
          || p.matches('main, [role=main], dialog, [role=dialog], form')) return false;
      if (serious(p)) return false;
      const btns = ownButtons(p);
      if (btns.some((b) => WAY_ON.test(btnText(b)))) return false;
      if (WIDGET_BOX.test((p.id || '') + ' ' + (p.getAttribute('class') || ''))) return true;
      if (btns.length && btns.every((b) => WIDGET_BTN.test(btnText(b)))) return true;
      const legal = Array.from(p.querySelectorAll('a[href]')).filter((a) =>
        !a.closest('label') && LEGAL.test(norm(a.innerText)));
      if (picker && legal.length >= 2) return true;      // a footer's language picker
    }
    return false;
  };
  const push = (anchor, rec) => {
    if (widgetGroup(anchor)) return;
    items.push({ at: order.has(anchor) ? order.get(anchor) : 1e9, rec: rec });
  };
  const textOfOption = (o) => norm(seen(o)) || norm(o.getAttribute('aria-label'))
    || (o.getAttribute('aria-labelledby') ? byIds(o, o.getAttribute('aria-labelledby')) : '');

  // combobox widgets and the listboxes they control
  const controlled = new Set();
  for (const box of all.filter((e) => e.matches('[role=combobox]'))) {
    const lb = listboxFor(box);
    if (lb) controlled.add(lb);
  }

  // custom radio groups (G7): [role=radiogroup] of [role=radio], and
  // [role=radio] siblings with no group
  const ariaRadios = all.filter((e) => e.matches('[role=radio]') && e.tagName !== 'INPUT');
  const radioGroups = new Map();
  for (const r of ariaRadios) {
    const g = closestC(r, '[role=radiogroup]') || up(r);
    if (!radioGroups.has(g)) radioGroups.set(g, []);
    radioGroups.get(g).push(r);
  }
  for (const [g, radios] of radioGroups) {
    const shown = radios.filter(visible);
    if (!shown.length || !usable(shown[0])) continue;
    radios.forEach((r) => { consumed.add(r); asButtons.add(r);
      r.querySelectorAll('input').forEach((i) => consumed.add(i)); });
    const marks = [];
    const [label, req] = groupLabelFor(shown, g.matches('[role=radiogroup]') ? g : null, marks);
    const required = req || g.getAttribute('aria-required') === 'true' || shown.some(isRequired)
      || shown.some((r) => Array.from(r.querySelectorAll('input')).some((i) => i.required));
    push(shown[0], describe(g, 'radio', label, required, locatorFor(g), shown.map(textOfOption),
                            { widget: 'choice', option_css: shown.map(locatorFor) }));
  }

  // choice buttons (G7): sibling buttons that say pressed or checked
  // (Ashby's Yes / No), each a short text
  const pressedByParent = new Map();
  for (const b of all.filter((e) => e.matches('button[aria-pressed], [role=button][aria-pressed], '
                                              + 'button[aria-checked]') && !consumed.has(e))) {
    const p = up(b);
    if (!pressedByParent.has(p)) pressedByParent.set(p, []);
    pressedByParent.get(p).push(b);
  }
  for (const [p, group] of pressedByParent) {
    const shown = group.filter(visible);
    const texts = shown.map((b) => norm(b.innerText) || norm(b.getAttribute('aria-label')));
    if (shown.length < 2 || texts.some((t) => !t || t.length > 30) || !usable(shown[0])) continue;
    const own = new Set(shown);
    const box = questionBox(own);
    const hiddenIn = box ? Array.from(box.querySelectorAll('input[type=checkbox], input[type=radio], '
                                                          + 'input[type=hidden]'))
      .filter((i) => !visible(i)) : [];
    hiddenIn.forEach((i) => consumed.add(i));
    shown.forEach((b) => { consumed.add(b); asButtons.add(b); });
    const marks = [];
    const [label, req] = groupLabelFor(shown, null, marks);
    const required = req || hiddenIn.some((i) => i.required)
      || shown.some((b) => b.getAttribute('aria-required') === 'true');
    push(shown[0], describe(p, 'radio', label, required, locatorFor(p), texts,
                            { widget: 'choice', option_css: shown.map(locatorFor) }));
  }

  // date parts (EXT-12): Month / Day / Year spinbuttons in one box
  const spins = all.filter((e) => e.matches('[role=spinbutton], input[data-automation-id^=dateSection]')
                           && !consumed.has(e) && visible(e));
  const spinBoxes = new Map();
  for (const s of spins) {
    const box = closestC(s, '[data-automation-id*=dateInputWrapper i], [data-automation-id*=dateInput i]')
      || up(s);
    if (!spinBoxes.has(box)) spinBoxes.set(box, []);
    spinBoxes.get(box).push(s);
  }
  const PART = (s) => {
    const t = (norm(s.getAttribute('aria-label')) + ' ' + (s.getAttribute('data-automation-id') || '')
               + ' ' + norm(s.getAttribute('placeholder'))).toLowerCase();
    return /month|\bmm\b/.test(t) ? 'M' : /day|\bdd\b/.test(t) ? 'D' : /year|yyyy/.test(t) ? 'Y' : '';
  };
  for (const [box, parts] of spinBoxes) {
    const kinds = parts.map(PART);
    if (parts.length < 2 || kinds.some((k) => !k) || !usable(parts[0])) continue;
    parts.forEach((s) => consumed.add(s));
    const marks = [];
    const [label, req] = groupLabelFor(parts, closestC(box, '[role=group]'), marks);
    push(parts[0], describe(box, 'date', label, req || parts.some(isRequired), locatorFor(box), [],
                            { widget: 'date:' + kinds.join(''), option_css: parts.map(locatorFor) }));
  }

  // checkbox groups (G8): boxes sharing a name, an id's question prefix, or
  // a question box of their own
  // the question box of options with no shared name (bunq gives each box
  // and each radio a name of its own): up to four boxes above the option,
  // the first holding two or more options of its kind (`pool`), no other
  // question's control and the question's words before its first option;
  // null when the options' set would grow past one question first
  const choiceBox = (el, sel, pool) => {
    let p = up(el), size = 0;
    for (let i = 0; p && i < 4; i++, p = up(p)) {
      if (p === document.body || p.matches('form, main, [role=main], dialog, [role=dialog]')) return null;
      const mine = pool.filter((o) => p.contains(o));
      if (mine.length < 2) continue;
      if (size && mine.length > size) return null;
      size = mine.length;
      const others = Array.from(p.querySelectorAll(QUESTION_CTRL)).filter((c) =>
        !c.matches(sel) && visible(c));
      if (others.length) return null;
      if (boxText(p)) return p;
    }
    return null;
  };
  const boxes = all.filter((e) => e.matches('input[type=checkbox]') && !consumed.has(e)
                           && enabled(e) && (visible(e) || !!proxyFor(e)));
  const byKey = new Map();
  const STATEMENT = /^\s*(yes,\s*)?i\b|\b(agree|consent|confirm|acknowledge|accept|certify|authori[sz]e)\b/i;
  const keyOf = (b) => {
    const name = b.getAttribute('name') || '';
    if (name && boxes.filter((o) => o.getAttribute('name') === name).length > 1) return 'name:' + name;
    const m = (b.id || '').match(/^(.*\[\])/);
    if (m && boxes.filter((o) => (o.id || '').startsWith(m[1])).length > 1) return 'id:' + m[1];
    // a question's box of options with its question above them: short
    // options ("He/Him", "Python"), or any options under a question that
    // asks ("...?", "...:", "select all that apply"); two consent boxes side
    // by side stay two fields
    const box = choiceBox(b, 'input[type=checkbox]', boxes);
    if (box) {
      const mine = boxes.filter((o) => box.contains(o));
      const words = strip(boxText(box))[0];
      const asks = /\?\s*$|all that apply|select|choose|which/i.test(words);
      // boxes that each state something ("I agree to ...", "I consent to
      // ...") under a line that asks nothing stay apart (review M5)
      const statements = mine.every((o) => STATEMENT.test(optionLabel(o)));
      if (statements && !asks) return 'one:' + order.get(b);
      if (asks || /:\s*$/.test(words) || mine.every((o) => optionLabel(o).length <= 60)) {
        return 'box:' + order.get(box);
      }
    }
    return 'one:' + order.get(b);
  };
  for (const b of boxes) {
    const k = keyOf(b);
    if (!byKey.has(k)) byKey.set(k, []);
    byKey.get(k).push(b);
  }
  const clickFor = (el) => {
    if (visible(el) && !faint(el)) return '';
    const p = proxyFor(el);
    return p ? locatorFor(p) : '';
  };
  for (const [k, group] of byKey) {
    if (!usable(group[0])) continue;
    group.forEach((b) => consumed.add(b));
    if (group.length === 1) {
      const b = group[0];
      const marks = [];
      const [label, req] = labelFor(b, marks);
      if (junk(b, 'checkbox', label)) continue;
      if (!visible(b)) kept.add(b);
      push(b, describe(b, 'checkbox', label, req || isRequired(b), locatorFor(b), ['checked'],
                       { click: clickFor(b) }));
      continue;
    }
    const marks = [];
    const [label, req] = groupLabelFor(group, null, marks);
    const name = group[0].getAttribute('name') || '';
    const shared = name && group.every((b) => b.getAttribute('name') === name);
    const css = shared ? hostPrefix(group[0]) + 'input[type=checkbox][name=' + q(name) + ']'
      : locatorFor(questionBox(new Set(group)) || up(group[0]));
    group.forEach((b) => { if (!visible(b)) kept.add(b); });
    push(group[0], describe(group[0], 'checkbox', label, req || group.some(isRequired), css,
                            group.map(optionLabel),
                            { widget: 'checkbox_group',
                              option_css: group.map((b) => clickFor(b) || locatorFor(b)) }));
  }

  // hidden selects behind a styled trigger (G6): the trigger is the select's
  // face, never a field or a button of its own
  // (hidden: not shown, see-through, or a few pixels across)
  const hiddenish = (el) => {
    if (!visible(el) || faint(el)) return true;
    const r = el.getBoundingClientRect();
    return r.width <= 4 && r.height <= 4;
  };
  const proxied = new Map();
  for (const s of all.filter((e) => e.tagName === 'SELECT' && hiddenish(e))) {
    const p = proxyFor(s);
    if (p && !p.matches('label')) { proxied.set(s, p); asButtons.add(p); consumed.add(p); }
    else if (p) proxied.set(s, p);
  }

  const radios = new Map();     // native radios by root and name, or by their box
  const allRadios = all.filter((e) => e.matches('input[type=radio]') && enabled(e)
                               && (visible(e) || !!proxyFor(e)));
  const FIELD_SEL = 'input, select, textarea, [role=combobox], [role=listbox], [role=checkbox], '
    + '[role=switch], [role=textbox], [contenteditable], button[aria-haspopup], '
    + '[role=button][aria-haspopup], [aria-haspopup=listbox]';
  const POPUP_NOT = /import|autofill|upload|attach|share|menu|more|options|settings|profile|account|language|sign in|log in|filter|sort|apply|submit|next|continue|back|interested/i;
  for (const el of all) {
    if (consumed.has(el) || !el.matches(FIELD_SEL)) continue;
    if (!usable(el)) continue;
    const role = el.getAttribute('role') || '';
    if (role !== 'combobox') {
      const widget = closestC(el, '[role=combobox]');
      if (widget && widget !== el) continue;
    }
    if (role === 'listbox' && controlled.has(el)) continue;
    const marks = [];
    if (el.tagName === 'INPUT') {
      const t = typeAttr(el);
      if (SKIP_INPUT.has(t)) continue;
      if (t === 'radio') {
        if (!visible(el) && !proxyFor(el)) continue;
        const name = el.getAttribute('name') || '';
        const shared = !!name && allRadios.filter((r) => r.getAttribute('name') === name
                                                  && rootOf(r) === rootOf(el)).length > 1;
        // radios with a name each (bunq's "...yes", "...no"): one question by their box
        const box = shared ? null : choiceBox(el, 'input[type=radio]', allRadios);
        const key = shared ? 'name:' + name + '@' + (rootOf(el) === document ? '' : order.get(rootOf(el).host))
          : box ? 'box:' + order.get(box) : 'path:' + locatorFor(el);
        let g = radios.get(key);
        if (!g) {
          g = { first: el, name: shared ? name : '', box: box, radios: [] };
          radios.set(key, g);
          if (!widgetGroup(el)) items.push({ at: order.get(el), group: g });
        }
        g.radios.push(el);
        continue;
      }
      if (t === 'file') {
        if (!visible(el) && !boxShows(el)) continue;
        const [label, req] = labelFor(el, marks);
        // a resume parser's own upload ("Autofill from resume"): its box's words
        // say so, in any box above it that holds no other question
        let parser = AUTOFILL.test(label);
        for (let p = up(el), i = 0; p && !parser && i < 6; p = up(p), i++) {
          if (p === document.body || p.matches('form, main, [role=main]')) break;
          if (Array.from(p.querySelectorAll(QUESTION_CTRL)).some((c) => c !== el && visible(c))) break;
          parser = AUTOFILL.test(seen(p).slice(0, 300));
        }
        if (junk(el, t, label)) continue;
        push(el, describe(el, 'file', label, parser ? false : (req || isRequired(el)), locatorFor(el),
                          [], { help: parser ? 'autofill parser' : '' }));
        continue;
      }
      if (!visible(el)) continue;
      // an untyped typeahead (G7): a text box with a results list beside it
      // (or inside a box beside it) or a hidden "selected" value (Lever's location)
      const RESULTS = '[class*=dropdown-results], [class*=autocomplete-results], [role=listbox]';
      const sibs = el.parentElement ? Array.from(el.parentElement.children) : [];
      const typeahead = (t === 'text' || t === 'search') && !role && (
        /^(list|both)$/i.test(el.getAttribute('aria-autocomplete') || '')
        || sibs.some((s) => s !== el && ((s.matches('input[type=hidden]')
                                          && /^selected/i.test(s.getAttribute('name') || ''))
                                         || s.matches(RESULTS) || !!s.querySelector(RESULTS))));
      // a text box inside a dropdown drawn as a box (Paylocity's Country and
      // State): the dropdown, typed in and picked from
      const combo = (t === 'text' || t === 'search') && !role
        && !!closestC(up(el) || el, '[aria-haspopup=listbox]');
      const [label, req] = labelFor(el, marks);
      if (junk(el, t, label)) continue;
      const type = typeahead || combo ? 'listbox' : typeOf(el);
      push(el, describe(el, type, label, req || isRequired(el), locatorFor(el),
                        type === 'listbox' ? [] : optionsFor(el, type),
                        { widget: combo ? 'combo' : typeahead ? 'typeahead' : '' }));
      continue;
    }
    if (el.tagName === 'SELECT') {
      const proxy = proxied.get(el);
      if (!visible(el) && !proxy) continue;
      const [label, req] = labelFor(el, marks);
      if (junk(el, 'select', label)) continue;
      const behind = !!proxy && hiddenish(el);
      if (behind) kept.add(el);
      push(el, describe(el, 'select', label, req || isRequired(el), locatorFor(el),
                        selectOptions(el), { click: behind ? locatorFor(proxy) : '',
                                             widget: behind ? 'hidden_select' : '' }));
      continue;
    }
    if (el.tagName === 'TEXTAREA') {
      if (!visible(el)) continue;
      const [label, req] = labelFor(el, marks);
      if (junk(el, 'textarea', label)) continue;
      push(el, describe(el, 'textarea', label, req || isRequired(el), locatorFor(el), []));
      continue;
    }
    if (!visible(el)) continue;
    if (role === 'combobox' || role === 'listbox') {
      const [label, req] = labelFor(el, marks);
      push(el, describe(el, 'listbox', label, req || isRequired(el), locatorFor(el),
                        listboxOptions(el)));
      continue;
    }
    if (role === 'checkbox' || role === 'switch') {
      // a custom tick box (EXT-04)
      const [label, req] = labelFor(el, marks);
      if (junk(el, 'checkbox', label)) continue;
      asButtons.add(el);
      push(el, describe(el, 'checkbox', label, req || isRequired(el), locatorFor(el), ['checked'],
                        { widget: 'aria_check' }));
      continue;
    }
    if (role === 'textbox' || (el.isContentEditable && el.hasAttribute('contenteditable'))) {
      // a rich-text box (EXT-13), the outermost editable only
      if (up(el) && closestC(up(el), '[contenteditable=""], [contenteditable=true], [role=textbox]')) continue;
      const [label, req] = labelFor(el, marks);
      push(el, describe(el, 'textarea', label, req || isRequired(el), locatorFor(el), [],
                        { widget: 'editable' }));
      continue;
    }
    if (el.matches('[aria-haspopup]')) {
      // a dropdown drawn as a button (G7, EXT-02): Workday's "Select One",
      // Teamtailor's menu, Paylocity's div
      const pop = (el.getAttribute('aria-haspopup') || '').toLowerCase();
      if (!['listbox', 'menu', 'true'].includes(pop) || asButtons.has(el)) continue;
      // one holding its own text box is that box's (the box is the field)
      if (Array.from(el.querySelectorAll('input:not([type=hidden])')).some(visible)) continue;
      const text = norm(el.innerText) || norm(el.value);
      if (POPUP_NOT.test(text)) continue;
      const [label, req] = labelFor(el, marks);
      const scoped = !!closestC(el, 'form, dialog, [role=dialog], [aria-modal=true], fieldset');
      const placeholder = PLACEHOLDER_OPTION.test(text);
      // named by a label of its own (never only by the words above it)
      const named = !!labelElementFor(el) || !!el.getAttribute('aria-labelledby')
        || !!ariaWords(el.getAttribute('aria-label'))[0];
      if (pop !== 'listbox' && !(placeholder || named)) continue;
      if (pop !== 'listbox' && POPUP_NOT.test(label)) continue;
      if (pop === 'listbox' && !(scoped || named || placeholder)) continue;
      asButtons.add(el);
      push(el, describe(el, 'listbox', label || text, req || isRequired(el), locatorFor(el),
                        listboxOptions(el), { widget: 'popup' }));
      continue;
    }
  }
  const fields = items.sort((a, b) => a.at - b.at).map((it) => {
    if (it.rec) return it.rec;
    const g = it.group;
    const shown = g.radios;
    const css = g.name ? hostPrefix(g.first) + 'input[type=radio][name=' + q(g.name) + ']'
      : g.box ? locatorFor(g.box) + ' input[type=radio]' : locatorFor(g.first);
    const marks = [];
    const [label, req] = groupLabelFor(shown, null, marks);
    const required = req || shown.some(isRequired);
    const clicks = shown.map(clickFor);
    shown.forEach((r) => { if (!visible(r)) kept.add(r); });
    const d = describe(g.first, 'radio', label, required, css, shown.map(optionLabel),
                       { option_css: clicks.some(Boolean) ? shown.map((r, i) => clicks[i] || locatorFor(r)) : [] });
    d.id_or_name = g.name || g.first.id || '';
    return d;
  });

  const buttons = [];
  const bsel = 'button, [role=button], input[type=submit], input[type=button], a.btn, a[class*=button]';
  const APPLY = /\bapply\b/i;
  const APPLY_LINK_MAX = 40;
  const applyLink = (el, text) => text.length <= APPLY_LINK_MAX && !inChrome(el)
    && APPLY.test(text + ' ' + norm(el.getAttribute('aria-label')));
  const PRIMARY = /(^|[\s_-])(primary|cta)([\s_-]|$)/i;
  const submitsOf = new Map();    // a form -> its visible submit controls
  const formSubmits = (form) => {
    if (!form) return 0;
    if (!submitsOf.has(form)) {
      submitsOf.set(form, Array.from(form.querySelectorAll(
        'button:not([type]), button[type=submit], input[type=submit]')).filter(visible).length);
    }
    return submitsOf.get(form);
  };
  for (const el of all) {
    if (!el.matches(bsel + ', a[href]') || asButtons.has(el)) continue;
    if (!visible(el)) continue;
    // a disabled control is kept, flagged (G10): a Submit that waits for the
    // form to validate is the page's way on once it is filled
    const disabled = !enabled(el);
    if (closestC(el, '[role=combobox]') || inConsent(el) || outsideModal(el)) continue;
    if (Array.from(asButtons).some((f) => f !== el && containsC(f, el))) continue;
    // a shadow root's button shows its host's words through a slot (G12)
    const host = rootOf(el).host;
    const text = norm(el.innerText) || norm(el.value) || norm(el.getAttribute('aria-label'))
      || norm(el.getAttribute('title')) || (host ? norm(host.innerText) : '');
    if (!text) continue;        // an icon with no name the judge could read (G4)
    if (!el.matches(bsel) && !applyLink(el, text)) continue;
    // a button drawn under a click filter of the same words (Workday's
    // "Create Account"): the filter takes the click, the button is no
    // second button
    const rb = el.getBoundingClientRect();
    const cx = rb.left + rb.width / 2, cy = rb.top + rb.height / 2;
    const over = cx >= 0 && cy >= 0 && cx < vw && cy < vh ? document.elementFromPoint(cx, cy) : null;
    if (over && over !== el && !el.contains(over) && !over.contains(el) && over.matches(bsel)
        && (norm(over.innerText) || norm(over.getAttribute('aria-label'))) === text) continue;
    const typeB = (el.getAttribute('type') || '').toLowerCase();
    const submits = typeB === 'submit' || (el.tagName === 'BUTTON' && !typeB && !!el.form);
    const aria = norm(el.getAttribute('aria-label'));
    let kind = '';
    if (NEVER_SUBMIT.test(text + ' ' + aria)) kind = '';
    else if (submits || /\b(submit|apply|send|finish)\b/i.test(text)) kind = 'submit';
    else if (/\b(next|continue)\b/i.test(text)) kind = 'advance';
    if (!kind && /\b(back|previous)\b/i.test(text)) kind = 'back';
    const owner = el.form || el.closest('form');
    const styled = (el.getAttribute('class') || '') + ' ' + (el.getAttribute('data-variant') || '')
      + ' ' + (el.getAttribute('data-type') || '');
    const primary = PRIMARY.test(styled) || (submits && !!owner && formSubmits(owner) === 1);
    buttons.push({ css: locatorFor(el), text: text, kind_hint: kind,
                   in_form: holdsControls(owner), chrome: inHeadChrome(el) || topBar(el),
                   disabled: disabled, primary: primary });
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
  if (modal) {
    // the open modal's text first: it is what the page asks now (G9)
    const own = (modal.innerText || '').trim();
    if (own) text = [own, text.replace(own, '').trim()].filter(Boolean).join('\n');
  }
  return { fields: fields, buttons: buttons, text: text.slice(0, cap), dialog: modalTitle.slice(0, 160),
           modal: !!modal };
}
""".replace("__OPTION_LABEL__", RADIO_OPTION_LABEL_JS).replace("__CONSENT__", CONSENT_ROOTS_JS).replace(
    "__IDENT__", IDENT_FN_JS).replace("__PLACEHOLDER__", PLACEHOLDER_TEXT_JS)

_TEXT_JS = "() => document.body ? (document.body.innerText || '') : ''"


# The frame URLs of the last `extract` per page (study G14): a locator's frame
# index can shift when an ad or tracker frame detaches between the read and
# the act; `resolve` finds the frame by the URL it had at the read first.
_FRAME_URLS: "weakref.WeakKeyDictionary[Any, list[str]]" = weakref.WeakKeyDictionary()
CONTENT_FRAME_MIN = (600, 300)    # px: a child frame this big is the content (G9)
CONTENT_FRAME_ANY = (300, 150)    # px: or this big with fields or an Apply-worded control
_APPLY_WORD = re.compile(r"\bapply\b", re.I)


def frames(page) -> list:
    """The page's frames with the main frame first: index 0 is `page.main_frame`,
    1.. are the child frames in `page.frames` order. The digest's locators and
    `resolve` share this numbering."""
    main = page.main_frame
    return [main] + [f for f in page.frames if f is not main]


def extract(page, *, content_site: Callable[[str], bool] | None = None) -> FormDigest:
    """Read one page of an application into a `FormDigest`: every frame in one
    JS pass each, fields and buttons numbered across frames in document order,
    the visible text of every frame joined (a content frame's first,
    `_content_frame`) and capped at `apply_judge.PAGE_TEXT_CAP`. A frame whose
    evaluate fails (detached, cross-origin) is skipped and keeps its index.

    `content_site(frame_url)`: may that child frame be read first (R2-M1: an
    embedded video or an ad stays in place)? The runner passes the page's
    site and the ATS platforms (`apply_run.content_frame_site`); the default
    takes only a frame of the page's own host, or a blank or srcdoc one."""
    from apply_judge import PAGE_TEXT_CAP      # lazy: apply_judge imports this module

    fields: list[Field] = []
    buttons: list[Button] = []
    texts: list[tuple[int, str]] = []     # (order, text): a content frame's first (G9)
    dialog = ""
    all_frames = frames(page)
    if content_site is None:
        page_host = (urlparse(str(page.url)).hostname or "").lower()

        def content_site(url: str) -> bool:
            host = (urlparse(str(url or "")).hostname or "").lower()
            return not host or host == page_host
    try:
        _FRAME_URLS[page] = [str(getattr(f, "url", "") or "") for f in all_frames]
    except TypeError:           # a page double that takes no weak reference
        pass
    for idx, frame in enumerate(all_frames):
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
                autocomplete=str(f.get("autocomplete") or ""),
                widget=str(f.get("widget") or ""),
                click_locator=(idx, str(f["click"])) if f.get("click") else None,
                option_locators=[str(o) for o in (f.get("option_css") or [])],
                section=str(f.get("section") or ""), ident=str(f.get("ident") or "")))
        for b in raw.get("buttons") or []:
            buttons.append(Button(n=len(buttons), locator=(idx, str(b["css"])),
                                  text=str(b["text"]), kind_hint=str(b.get("kind_hint") or ""),
                                  in_form=bool(b.get("in_form")), chrome=bool(b.get("chrome")),
                                  disabled=bool(b.get("disabled")),
                                  primary=bool(b.get("primary"))))
        if raw.get("dialog") and not dialog:
            dialog = str(raw["dialog"])
        if raw.get("text"):
            own = bool(raw.get("fields")) or any(_APPLY_WORD.search(str(b.get("text") or ""))
                                                 for b in raw.get("buttons") or [])
            first = (idx > 0 and content_site(str(getattr(frame, "url", "") or ""))
                     and _content_frame(frame, CONTENT_FRAME_ANY if own else CONTENT_FRAME_MIN))
            texts.append((0 if first else 1, str(raw["text"])))
    text = "\n".join(t for _, t in sorted(texts, key=lambda row: row[0]))[:PAGE_TEXT_CAP]
    return FormDigest(url_host=urlparse(page.url).hostname or "", title=page.title(),
                      text=text, fields=fields, buttons=buttons, dialog=dialog)


def _content_frame(frame, size: tuple[int, int] = CONTENT_FRAME_MIN) -> bool:
    """A child frame big enough to be the page's content: at least
    `CONTENT_FRAME_MIN` on the page by its size alone (an iCIMS posting's
    frame holds no field), or `CONTENT_FRAME_ANY` when it holds fields or an
    Apply-worded control (a Greenhouse embed)."""
    try:
        box = frame.frame_element().bounding_box()
    except Exception:       # noqa: BLE001  (a detached frame)
        return False
    return bool(box) and box["width"] >= size[0] and box["height"] >= size[1]


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
    frame. The frame is found by the URL it had at the last `extract` first
    (study G14: a frame that detached since shifts the indexes after it),
    then by its index. Raises `IndexError` when the frame no longer
    exists.

    The URLs are the page's last `extract`'s (`_FRAME_URLS`), not carried on
    the locator: that holds while no other extract runs between a read and
    its act, as in the loop today. Two frames at one URL (two `about:blank`
    frames, two copies of one widget) cannot be told apart this way; the
    first frame at the URL is taken, so for them the index is the better
    guide, and it is used whenever the frame at the index still has the
    URL."""
    idx, css = int(locator[0]), str(locator[1])
    all_frames = frames(page)
    try:
        urls = _FRAME_URLS.get(page) or []
    except TypeError:           # a page double that takes no weak reference
        urls = []
    if 0 < idx < len(urls) and urls[idx]:
        want = urls[idx]
        here = str(getattr(all_frames[idx], "url", "") or "") if idx < len(all_frames) else ""
        if here != want:
            moved = [f for f in all_frames[1:] if str(getattr(f, "url", "") or "") == want]
            if moved:
                return moved[0].locator(css)
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
# an error (a success note, `alert-success`, `alert-info` or a `success`
# class, is none). An error text beside a control
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
  const SUCCESS = /alert-success|alert-info|\bsuccess\b/i;
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
