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


@dataclass
class Button:
    """One clickable control. `kind_hint` comes from the DOM (`submit`,
    `button`, `link`, ...) and is only a hint; the judge decides the role."""
    n: int
    locator: tuple[int, str]
    text: str
    kind_hint: str = ""


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
                        id_or_name=str(f.get("id_or_name", "") or ""))
                  for f in (raw.get("fields") or [])]
        buttons = [Button(n=int(b["n"]), locator=_locator(b.get("locator")),
                          text=str(b.get("text", "")),
                          kind_hint=str(b.get("kind_hint", "") or ""))
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
#            button; an input inside a [role=combobox] is part of that widget.
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
    if (!enabled(el)) continue;
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
  for (const el of document.querySelectorAll(bsel)) {
    if (!enabled(el) || !visible(el)) continue;
    if (el.closest('[role=combobox]')) continue;
    const text = norm(el.innerText) || norm(el.value) || norm(el.getAttribute('aria-label')) || norm(el.getAttribute('title'));
    const typeAttr = (el.getAttribute('type') || '').toLowerCase();
    const submits = typeAttr === 'submit' || (el.tagName === 'BUTTON' && !typeAttr && !!el.form);
    let kind = '';
    if (submits || /\b(submit|apply|send|finish)\b/i.test(text)) kind = 'submit';
    else if (/\b(next|continue)\b/i.test(text)) kind = 'advance';
    else if (/\b(back|previous)\b/i.test(text)) kind = 'back';
    buttons.push({ css: locatorFor(el), text: text, kind_hint: kind });
  }

  const text = document.body ? (document.body.innerText || '') : '';
  return { fields: out, buttons: buttons, text: text.slice(0, cap) };
}
""".replace("__OPTION_LABEL__", RADIO_OPTION_LABEL_JS)

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
                id_or_name=str(f.get("id_or_name") or "")))
        for b in raw.get("buttons") or []:
            buttons.append(Button(n=len(buttons), locator=(idx, str(b["css"])),
                                  text=str(b["text"]), kind_hint=str(b.get("kind_hint") or "")))
        if raw.get("text"):
            texts.append(str(raw["text"]))
    text = "\n".join(texts)[:PAGE_TEXT_CAP]
    return FormDigest(url_host=urlparse(page.url).hostname or "", title=page.title(),
                      text=text, fields=fields, buttons=buttons)


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
