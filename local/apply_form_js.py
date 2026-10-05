"""The page scripts the form digest runs: the extractor and the live reads.

Every constant here is a JavaScript function in a Python string that
`apply_form` (and through it `apply_fill`, `apply_linkedin` and `apply_run`)
hands to Playwright's `evaluate`. Shared snippets are spliced into the larger
scripts with `.replace("__TOKEN__", SNIPPET)` when this module loads. The
Python that calls them, the dataclasses and the password helpers stay in
`apply_form`.

The extractor's text and the other scripts feed the page digest, and the
digest feeds the judge's requests, which are the replay cache's keys: a
change here can move a recorded key (`scripts/replay_check.py`).
"""
import apply_send_words


# The page scripts' visibility test, in its two forms. `VISIBLE_FN_JS` takes a
# control as shown when its box has any width or height (a zero-height row or
# a zero-width line still counts); `VISIBLE_AREA_FN_JS` asks for both (the
# consent banner's buttons and LinkedIn's Apply controls, where a collapsed
# box is a hidden one). Both refuse display:none and visibility:hidden.
# Spliced at `__VISIBLE__` / `__VISIBLE_AREA__`.
VISIBLE_FN_JS = """(el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  }"""
VISIBLE_AREA_FN_JS = """(el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }"""


# --- the extractor ---------------------------------------------------------------

# One pass over a frame's DOM. Returns {"fields": [...], "buttons": [...], "text": str}
# with plain values only; the dataclasses are built in Python. The rules:
#   tree     the composed tree: open shadow roots are walked where their host
#            stands (UKG's buttons, SAP's header).
#   fields   visible, enabled input (not hidden/submit/button/image/reset), select,
#            textarea, [role=combobox], [role=listbox]; radios collapse into one
#            entry per name group, or per question box when each radio has a
#            name of its own; a file input is kept even when hidden because
#            set_input_files works on it and ATS pages hide it behind a styled
#            button, while its box has a layout and its form (or, outside a
#            form, a box within three levels above it) shows; an input inside
#            a [role=combobox] is part of that widget. Widgets join
#            them, with `widget` naming how each works:
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
#   label    what a person sees: label[for] (only for a unique id whose
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
#   section  the nearest h2 to h4 or heading above the control.
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
#            with `disabled`; `primary` when styled as the main action
#            or its form's one submit.
#            `kind_hint` is `submit` for a submit control or a send word, never
#            for "Apply with LinkedIn / Indeed", "Submit a general
#            application", "Cancel", "Apply later" or "Save for later";
#            `in_form` when the button's form holds a control a person
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
#            them on one run, so its posting read as a form.
#   consent  a cookie or consent banner (`CONSENT_ROOTS_JS`) is chrome as
#            well: its fields and buttons are dropped and its text goes to
#            the end (11 ATSs, the banner text first on 3).
#   modal    an open modal (`dialog[open]`, `aria-modal=true`, Workday's
#            `data-automation-activepopup=true`, a dialog covering over 40% of
#            the viewport, or a dialog of 280 x 200 px or more on top of the
#            page, the element at its centre inside it; never a consent
#            banner or preference center, never a chat window) is
#            the page while it is open (Workday's "Start Your
#            Application", Teamtailor's form overlay): only its fields and
#            buttons are kept, its text goes first, and its title is
#            returned as `dialog`.
#   top bar  a Workday header (`data-automation-id*=header`) and a bar fixed
#            or sticky at the top of the page are chrome like the
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
    // control is this element (Ashby gives every option one id)
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
# its h1 or a file input, and none holding a question the application asks:
# a control marked required (the attribute, aria-required, a star in its
# label) or a tick box whose words state an agreement ("I agree to the
# processing of my data and the use of cookies"); neither is the body, a
# control or a style or script element. A banner is found whether or not its own box has a size (OneTrust's
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
  const QUESTION = 'input:not([type=hidden]):not([type=submit]):not([type=button]), select, '
    + 'textarea, [role=checkbox], [role=switch], [role=radio], [role=combobox], [role=textbox]';
  const STAR = /^\s*[*✱＊]|[*✱＊]\s*(?:required\.?)?\s*$|\(\s*required\s*\)/i;
  const AGREE = /\bI\s+(?:have\s+read\s+and\s+)?(?:hereby\s+)?(?:agree|consent|acknowledge|accept|certify|confirm|understand|authori[sz]e|attest)\b/i;
  // the words that name a control: its labels, an enclosing label, its
  // aria-label, and for a tick box drawn as an element its own text
  const said = (c) => {
    const labs = c.labels ? Array.from(c.labels).map((l) => l.textContent || '') : [];
    const enc = c.closest('label');
    if (enc && !labs.includes(enc.textContent)) labs.push(enc.textContent || '');
    labs.push(c.getAttribute('aria-label') || '');
    if (!/^(INPUT|SELECT|TEXTAREA)$/.test(c.tagName)) labs.push(c.textContent || '');
    return labs.map((t) => t.replace(/\s+/g, ' ').trim()).filter(Boolean);
  };
  const tick = (c) => (c.tagName === 'INPUT' && /^(checkbox|radio)$/i.test(c.type || ''))
    || /^(checkbox|switch|radio)$/.test(c.getAttribute('role') || '');
  const asks = (el) => Array.from(el.querySelectorAll(QUESTION)).some((c) =>
    c.required === true || c.getAttribute('aria-required') === 'true'
    || said(c).some((t) => STAR.test(t) || (tick(c) && AGREE.test(t))));
  const names = (el) => (el.id || '') + ' ' + (el.getAttribute('class') || '') + ' '
    + (el.getAttribute('data-automation-id') || '');
  const blocked = (el) => el === document.body || el === document.documentElement
    || el.matches('main, [role=main], form, input, select, textarea, button, label, option, a, '
                  + 'style, script, noscript, template, link, meta')
    || !!el.closest('form')
    || !!el.querySelector('main, [role=main], h1, input[type=file], ' + TYPING)
    || asks(el);
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
  const visible = __VISIBLE_AREA__;
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
}""".replace("__VISIBLE_AREA__", VISIBLE_AREA_FN_JS)

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

# Who a control is: its tag, type, id, name, aria-label, stable
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

  // the composed tree: every element in document order, each open
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

  const visible = __VISIBLE__;
  const enabled = (el) => !el.disabled && el.getAttribute('aria-disabled') !== 'true'
    && !el.closest('fieldset:disabled');
  const CHROME = 'header, footer, nav, search, [role=banner], [role=contentinfo], '
    + '[role=navigation], [role=search]';
  const inChrome = (el) => {
    const c = closestC(el, CHROME);
    return !!c && !(up(c) && closestC(up(c), 'form, dialog, [role=dialog]'));
  };
  // a button's chrome: the header, nav and search landmarks only; a footer
  // holds a wizard's Next often enough
  const HEAD_CHROME = 'header, nav, search, [role=banner], [role=navigation], [role=search]';
  const inHeadChrome = (el) => {
    const c = closestC(el, HEAD_CHROME);
    return !!c && !(up(c) && closestC(up(c), 'form, dialog, [role=dialog]'));
  };
  const consent = (__CONSENT__)();
  const inConsent = (el) => consent.some((root) => containsC(root, el));
  // an open modal is the page while it is open
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
  // a Workday header, a bar fixed or sticky at the top of the page
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
    // "Attach resume" is clicked); a hidden wizard step drops
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

  // --- locators ---
  const countC = (sel) => {
    let n = 0;
    for (const r of roots) {
      try { n += r.querySelectorAll(sel).length; } catch (e) { return 99; }
    }
    return n;
  };
  // an nth-of-type path from the body (the document) or from the element's
  // shadow root, anchored there (`:scope >` under the host's `>>`): an
  // unanchored path also names a deeper element of the same shape
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
    return (root === document ? 'body > ' : ':scope > ') + parts.join(' > ');
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
  // who the control is, read again before every act (`IDENT_FN_JS`)
  const identOf = __IDENT__;

  // --- what a person sees ---
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
  // a form widget's own words inside a label: a button, a
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
  // noted in `marks` where a label's words were left out or cut (a button's or
  // a dropdown's words skipped inside it, a box's words past their cap): such a
  // label is read in part (`label_partial`)
  const CUT = '\u0000cut';
  // a skipped subtree whose shown words are more than a required marker
  const wordsLeft = (n) => {
    const t = norm(n.textContent);
    return /\p{L}/u.test(t) && !MARK_CHARS.test(t);
  };
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
      if (SKIP_TEXT.test(n.tagName)) {
        if (marks && !top && /^(BUTTON|SELECT)$/i.test(n.tagName) && norm(n.textContent)) {
          marks.push(CUT);
        }
        return;
      }
      if (!top && n.getAttribute('aria-hidden') === 'true') {
        // hidden from the reader, seen by the person: a marker there counts;
        // words there are words the label leaves out
        if (marks && n.getClientRects().length) {
          marks.push(norm(n.textContent));
          if (wordsLeft(n)) marks.push(CUT);
        }
        return;
      }
      if (!top && QUIET_ROLES.test(n.getAttribute('role') || '')) {
        if (marks && n.getClientRects().length && wordsLeft(n)) marks.push(CUT);
        return;
      }
      const st = getComputedStyle(n);
      // (a hidden element's mark is none: Breezy's conditional
      // `span.ng-hide.required`)
      if (st.display === 'none') return;
      // a required marker drawn in CSS (Ashby's ::after star) or named by a
      // class (Ashby's `_required_`): a marker the person sees
      if (marks && st.visibility !== 'hidden' && styledMark(n)) marks.push('*');
      // a dropdown's shown value inside its label ("State Select a state")
      if (!top && n.matches(WIDGET_TEXT)) {
        if (marks && norm(n.textContent)) marks.push(CUT);
        return;
      }
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
  // required markers: `*`, `✱`, `(required)`, `*Required`; `(optional)`
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
  // an aria-label that names no question: "Search", "Select...", "textbox"
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
      if (marks && t.length > 120) marks.push(CUT);
      return t.slice(-120);
    }
    return '';
  };
  const optionLabel = __OPTION_LABEL__;
  // label[for] counts only when its id is unique and the label's control is
  // this element (Ashby reuses a question's id on every option)
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
  const kept = new Set();       // hidden natives kept behind a visible proxy
  // the question's box: up to five boxes above the control, the first
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
  // an upload's own words, never its question: "Attach", "Drop your file
  // or upload", "Total 0 file selected", "file-input", a missing-SVG fallback
  const FACE = /^(attach|upload( an?)?( files?)?|browse|choose( an?)? files?|select( an?)? files?|no file chosen|drop (your )?files?( here)?( or upload)?|drag (and|&) drop.*|click to upload|total \d+ files? selected|file-?input|svgs? (are )?not supported.*)\.?$/i;
  const STEP = /^\s*step\s+\d+\s*(of|\/)\s*\d+\s*$/i;     // a wizard's step marker
  // an upload's own clickable face ("Attach", "ATTACH RESUME/CV") is no question
  const isUpload = (n) => isCtrl(n) || n.matches('a[href], [role=link]')
    || (n.tagName === 'LABEL' && !!n.control && n.control.type === 'file');
  // the question's own words: its box's visible text before the first control
  const boxText = (box, marks, stop) => {
    if (!box) return '';
    const t = seen(box, marks, stop || isCtrl);
    if (marks && t.length > 300) marks.push(CUT);
    return t.slice(0, 300);
  };
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
  // own title, by a marker in its words or drawn by CSS. The
  // title is the fieldset's legend, or a label of it that no control in the
  // fieldset owns: another question's label ("First name *" beside Middle
  // name) is no title
  const titleReq = (el) => {
    const fs = el.closest('fieldset');
    if (!fs) return false;
    const kids = Array.from(fs.children);
    const title = kids.find((c) => c.tagName === 'LEGEND')
      || kids.find((c) => c.tagName === 'LABEL' && !(c.control && fs.contains(c.control)));
    if (!title || title.contains(el) || title === labelElementFor(el)) return false;
    const m = [];
    const [, req] = strip(seen(title, m), m);
    return req;
  };
  const cutLabels = new Set();  // controls whose label was read in part
  const labelFor = (el, marks) => {
    const m = marks || [];
    const [label, req] = labelWords(el, m);
    if (m.includes(CUT)) cutLabels.add(el);
    return [label, req || titleReq(el)];
  };
  const labelWords = (el, marks) => {
    const own = new Set([el]);
    const tryText = (t) => { const [s, r] = strip(t, marks); return [s, r]; };
    const lab = labelElementFor(el);
    if (el.tagName === 'INPUT' && typeAttr(el) === 'file') {
      // a file box by its question: its group, its fieldset, its
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
          .map((b) => norm(b.innerText)).find((t) => /upload|resume|\bcv\b|attach|file/i.test(t));
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
  // here..." or "-- No answer --" whatever its value
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
  // the heading a control sits under: the nearest h2 to h4 or
  // heading role before it, outside the site chrome
  const heads = all.filter((e) => e.matches('h2, h3, h4, [role=heading]') && !inChrome(e)
                           && !inConsent(e) && visible(e));
  // The nearest heading before the control whose nearest common ancestor
  // with it is no body, html or main (a posting's headings share only those
  // with the form), taken from a header box beside the fields as
  // well as from the fields' own box (Greenhouse's
  // `div.section-header > h3`). A heading whose own branch under that ancestor holds a
  // control of its own (the resume parser's box, "Autofill from resume") is
  // another block's: it is no section, and it closes off the headings before
  // it. A heading in another column (Ashby's posting details beside the
  // form: the two branches side by side) is no section either.
  const sideBySide = (a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return ra.width > 0 && rb.width > 0 && (ra.right <= rb.left || rb.right <= ra.left);
  };
  const holdsControl = (box, el) => box.nodeType === 1 && Array.from(
    box.querySelectorAll(QUESTION_CTRL + ', input[type=file]')).some((c) => c !== el
      && !c.contains(el));
  const sectionOf = (el) => {
    const at = order.has(el) ? order.get(el) : -1;
    const mine = new Set();
    for (let n = el; n; n = up(n)) mine.add(n);
    let best = null;
    for (const h of heads) {
      if (order.get(h) >= at) break;
      let branch = h, common = up(h);
      while (common && !mine.has(common)) { branch = common; common = up(common); }
      if (!common || common.nodeType !== 1
          || common.matches('body, html, main, [role=main]')) continue;
      let own = el;
      while (own && up(own) !== common) own = up(own);
      if (own && own.nodeType === 1 && branch.nodeType === 1 && sideBySide(branch, own)) continue;
      best = holdsControl(branch, el) ? null : h;
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
      section: sectionOf(el), ident: identOf(el), label_partial: cutLabels.has(el),
      secret: el.tagName === 'INPUT' && typeAttr(el) === 'password',
    };
  };

  // --- junk boxes ---
  // a honeypot's words: "honeypot", "robots only", "if you are human", or a
  // label that is nothing but "leave this field blank" / "do not fill this"
  // ("Middle name (leave this field blank if none)" is a question)
  const HONEY = /honey[\s_-]?pot|robots? only|for robots|if you('re| are) (a )?human|^\s*(please )?(leave (this )?(field |box )?(blank|empty)|do not (fill|enter)( (in|this)( field| box)?)?)\.?\s*$/i;
  const HONEY_ID = /^hp[_-]|nickname_hp|honey/i;
  const POSTING_WIDGET = /job alerts?\b|receive (an |job )?alerts?\b|newsletter|\bsort by\b|search (for )?jobs\b|^\s*(search( (jobs|roles|positions|openings))?|keywords?|find (a )?jobs?)\s*$/i;
  // the words of a name as words: "startDate", "start_date", "datepicker-input"
  const nameWords = (s) => (s || '').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase()
    .split(/[^a-z]+/).filter(Boolean);
  const DATE_WORDS = new Set(['date', 'calendar', 'picker', 'datepicker', 'datetimepicker']);
  // a combobox drawn by its box (react-select without search: a one-pixel
  // input scaled to nothing inside the control a person clicks): that box
  const comboFace = (el) => {
    if (!el.matches('[role=combobox]')) return null;
    let p = up(el);
    for (let i = 0; p && i < 3; i++, p = up(p)) {
      if (p === document.body || p.matches('form, label, fieldset')) return null;
      const r = p.getBoundingClientRect();
      if (visible(p) && r.width >= 20 && r.height >= 10) return p;
    }
    return null;
  };
  // an opacity still moving on `n` itself: a transition or an animation of
  // its opacity, running or about to start
  const fading = (n) => {
    try {
      return n.getAnimations().some((a) => (a.pending || a.playState === 'running')
        && !!a.effect && a.effect.getKeyframes().some((k) => 'opacity' in k));
    } catch (e) { return false; }
  };
  // `box` holds no control but `el` (a trap's own wrapper, with its label)
  const lone = (box, el) => Array.from(box.querySelectorAll(
    'input:not([type=hidden]), textarea, select, [contenteditable=""], [contenteditable=true], '
    + '[role=textbox], [role=combobox]')).every((c) => c === el);
  // `el` shares a box below the body with another visible question
  const amongQuestions = (el) => {
    for (let p = up(el); p && p !== document.body && p !== document.documentElement; p = up(p)) {
      if (p.nodeType !== 1) continue;
      if (Array.from(p.querySelectorAll(QUESTION_CTRL)).some((c) => c !== el && !c.contains(el)
          && !el.contains(c) && visible(c))) return true;
    }
    return false;
  };
  // `req`: the control's question is marked required (a star, "(required)")
  const junk = (el, t, label, req) => {
    // a choice or a file box is often hidden behind its label or trigger
    const choice = t === 'checkbox' || t === 'radio' || t === 'file' || t === 'select';
    // a read-only box, never a picker that opens on a click
    // (react-select without search, a date picker; "date" as a word of the
    // box's names, never inside "candidate" or "update")
    const picker = el.matches('[role=combobox], [aria-haspopup], [aria-autocomplete]')
      || [el.getAttribute('class'), el.getAttribute('placeholder'), el.id,
          el.getAttribute('name')].some((s) => nameWords(s).some((w) => DATE_WORDS.has(w)));
    if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.readOnly && !choice && !picker) {
      return 'read-only';
    }
    // an id or name that starts with hp- is a trap only while nothing marks
    // it required: a trap a person must fill would stop every person
    if (((HONEY_ID.test(el.id || '') || HONEY_ID.test(el.getAttribute('name') || ''))
         && !req && !isRequired(el)) || HONEY.test(label)) return 'honeypot';
    // a required question among the application's other questions is the
    // application's whatever its words name ("How did you hear about us (job
    // board, newsletter, referral)? *"); a posting's lone required alert box
    // beside its Apply stays the posting's
    if (POSTING_WIDGET.test(label) && !((req || isRequired(el)) && amongQuestions(el))) {
      return 'posting widget';
    }
    if (choice) return '';
    // react-select's dummy input: a field through its face
    if (comboFace(el) && !closestC(el, '[aria-hidden=true]')) return '';
    if (closestC(el, '[aria-hidden=true]')) return 'aria-hidden';
    // a box a person cannot see: it nearly transparent, or a box around it
    // that holds no other control, whatever its tabindex
    // (a trap at opacity 0, or in a transparent wrapper, labelled "Website").
    // A box whose opacity is still moving (a fade-in's transition or
    // animation) is read as it will be once it settles, and a transparent
    // box around other controls too is a page or a form held back until it
    // loads: neither hides a field
    for (let n = el; n && n !== document.body; n = up(n)) {
      if (n.nodeType === 1 && parseFloat(getComputedStyle(n).opacity) < 0.1 && !fading(n)
          && (n === el || lone(n, el))) return 'hidden';
    }
    const r = el.getBoundingClientRect();
    const x = window.scrollX || 0, y = window.scrollY || 0;
    if (r.right + x < 0 || r.bottom + y < 0 || r.left + x < -500 || r.top + y < -500) return 'offscreen';
    if (r.width <= 2 && r.height <= 2) return 'tiny';
    return '';
  };

  // --- hidden natives behind a visible label or proxy ---
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
  // a posting's widgets: the fields of a form whose own
  // buttons only search, filter, alert or subscribe; outside a form, of a box
  // named for filters, a search bar, job alerts or a newsletter, or whose
  // buttons only do those things; a lone picker beside the page's legal
  // links (a footer's language picker). Never a group with a password or a
  // file box, or one beside a button that goes on (submit, apply, next...).
  // a search, alert or subscribe action; Go, Clear and Reset go with one
  // and never make a group a widget alone (a phone box's Clear)
  const WIDGET_BTN = /^(search( jobs| roles)?|find( jobs)?|filters?|apply filters?|notify me|alert me|subscribe|get (job )?alerts|create (a )?(job )?alert|set (up )?(an? )?(job )?alert|email me( jobs)?|send me (jobs|alerts))$/i;
  const WIDGET_AID = /^(go|clear( all| filters)?|reset( filters)?)$/i;
  const WAY_ON = /\b(submit|apply|send|next|continue|save|finish|sign in|log in|create account|register)\b/i;
  // a box named for one: a whole class token or id ("filters", "job-alert",
  // "search-bar"; never "location-search-box"), or an id that
  // ends in filters, alerts or a newsletter (Rippling's "open-roles-filters")
  const WIDGET_BOX = /^(filters?|job-?alerts?|newsletter|subscribe|search-?(bar|box|form|filters?))$/i;
  const WIDGET_ID_END = /[-_](filters|job-?alerts?|newsletter)$/i;
  const LEGAL = /^(terms( of (service|use))?|privacy( policy)?|cookies?( (policy|notice|settings))?|legal( notice)?|accessibility|powered by .+)$/i;
  const LANGUAGE = /\b(language|locale|region)\b|english/i;
  const btnText = (b) => norm(b.innerText) || norm(b.value) || norm(b.getAttribute('aria-label'));
  const ownButtons = (box) => Array.from(box.querySelectorAll(
    'button, input[type=submit], input[type=button], [role=button]')).filter((b) => visible(b)
      && !b.matches('[aria-haspopup]') && !closestC(b, '[role=combobox]') && !!btnText(b));
  const serious = (box) => !!box.querySelector('input[type=password], input[type=file]');
  // every button names a search, alert or subscribe action, or goes with one
  const widgetButtons = (btns) => btns.length > 0
    && btns.every((b) => WIDGET_BTN.test(btnText(b)) || WIDGET_AID.test(btnText(b)))
    && btns.some((b) => WIDGET_BTN.test(btnText(b)));
  const widgetBox = (p) => (p.getAttribute('class') || '').split(/\s+/).concat([p.id || ''])
    .some((t) => WIDGET_BOX.test(t)) || WIDGET_ID_END.test(p.id || '');
  const wayOnLink = () => Array.from(document.querySelectorAll('a[href], [role=link]')).some((a) =>
    visible(a) && WAY_ON.test(norm(a.innerText)));
  const widgetGroup = (el, label) => {
    const form = el.form || el.closest('form');
    if (form) {
      if (serious(form)) return false;
      if (form.matches('[role=search]')) return true;
      // its own buttons, those that name it from outside (`form=`), else,
      // when all its buttons sit outside it, the page's
      let btns = ownButtons(form);
      if (form.id) {
        btns = btns.concat(Array.from(document.querySelectorAll('[form=' + q(form.id) + ']'))
          .filter((b) => !form.contains(b) && visible(b) && !!btnText(b)));
      }
      if (!btns.length) {
        if (wayOnLink()) return false;
        btns = ownButtons(document.body);
      }
      return widgetButtons(btns);
    }
    // a footer's language picker: a picker with no question of its own (no
    // label, a language or region, or words taken from the legal links
    // beside it: "Powered by ...") beside the page's legal links; an EEO
    // question beside a privacy note is a question
    const picker = el.matches('select, [role=combobox], [aria-haspopup]') && !isRequired(el);
    const letters = (t) => norm(t).toLowerCase().replace(/[^a-z]/g, '');
    const noQuestion = (legal) => !label || LEGAL.test(label) || LANGUAGE.test(label)
      || letters(legal.map((a) => a.innerText).join(' ')).includes(letters(label));
    let p = up(el);
    for (let i = 0; p && i < 9; i++, p = up(p)) {
      if (p === document.body || p === document.documentElement
          || p.matches('main, [role=main], dialog, [role=dialog], form')) return false;
      if (serious(p)) return false;
      const btns = ownButtons(p);
      if (btns.some((b) => WAY_ON.test(btnText(b)))) return false;
      if (widgetBox(p)) return true;
      if (widgetButtons(btns)) return true;
      const legal = Array.from(p.querySelectorAll('a[href]')).filter((a) =>
        !a.closest('label') && LEGAL.test(norm(a.innerText)));
      if (picker && legal.length >= 2 && noQuestion(legal)) return true;
    }
    return false;
  };
  const push = (anchor, rec) => {
    if (widgetGroup(anchor, rec.label)) return;
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

  // custom radio groups: [role=radiogroup] of [role=radio], and
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

  // choice buttons: sibling buttons that say pressed or checked
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

  // date parts: Month / Day / Year spinbuttons in one box
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

  // one-time code boxes: four or more one-character boxes in one
  // box up to three levels above them (Greenhouse's security-input-N,
  // Oracle's PIN boxes) are one code field, typed from its first box
  const oneChar = (e) => e.tagName === 'INPUT' && ['text', 'tel', 'number'].includes(typeAttr(e))
    && parseInt(e.getAttribute('maxlength') || '', 10) === 1 && !consumed.has(e) && visible(e);
  const singles = all.filter(oneChar);
  const otpBoxes = new Map();
  for (const s of singles) {
    let box = null;
    let p = up(s);
    for (let i = 0; p && i < 3 && !box; i++, p = up(p)) {
      if (singles.filter((o) => containsC(p, o)).length >= 4) box = p;
    }
    if (!box) continue;
    if (!otpBoxes.has(box)) otpBoxes.set(box, []);
    otpBoxes.get(box).push(s);
  }
  for (const [box, parts] of otpBoxes) {
    if (parts.length < 4 || parts.length > 12 || !usable(parts[0])) continue;
    parts.forEach((s) => consumed.add(s));
    const marks = [];
    const [label, req] = groupLabelFor(parts, closestC(box, '[role=group]'), marks);
    push(parts[0], describe(parts[0], 'text', label || 'Verification code',
                            req || parts.some(isRequired), locatorFor(parts[0]), [],
                            { widget: 'otp', option_css: parts.map(locatorFor) }));
  }

  // checkbox groups: boxes sharing a name, an id's question prefix, or
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
      // ...") under a line that asks nothing stay apart
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
      if (junk(b, 'checkbox', label, req)) continue;
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

  // hidden selects behind a styled trigger: the trigger is the select's
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
  // a chrome control drawn as a popup: its whole shown text,
  // or its whole label, is a chrome word ("More", "Apply", "Sort by: Newest",
  // "Import from LinkedIn"); a word inside a question's text or value never
  // is ("Which of these apply to you?", "Back end", "Does not apply")
  const POPUP_CHROME = /^(more( options| actions)?|menu|options|share( this job)?|settings|profile|(my |your )?account|sign in|log in|login|sign up|register|apply( now)?|next|back|continue|submit( application)?|languages?|filters?|sort( by)?|i'?m interested|interested)$/i;
  const POPUP_TOOL = /^(import|autofill|upload|attach)\b|^(sort|filter)( by)?\s*[:\-]/i;
  // a menu popup's label names the site's own chrome; a label that asks is
  // a question, whatever words it holds
  // a label that names the chrome itself, short: "Language", "Change
  // language", "Account settings", "Your account"
  // ("Interview language" and a starred "Preferred language" are questions)
  const POPUP_CHROME_LABEL = /^((change|select|choose|switch|set|your|my|site|display)\s+)?(language|account|profile|settings|share|sort( by)?|filters?)(\s+(settings|preferences|options|menu))?$/i;
  const ASKS = /\?\s*$|\ball that apply\b/i;
  const chromePopup = (text) => POPUP_CHROME.test(text) || POPUP_TOOL.test(text);
  // a send or go-on phrase, as a popup's own name ("More submit options",
  // "Save and continue", "Continue with", "Apply with", "Next step")
  // the send part is `apply_send_words.send_phrase`'s rule:
  // a submit or send leading a short name, or a send or last-step verb
  // followed by nothing or by the application or the send itself; "Finish
  // month", "Expected finish date" and "Willing to submit references" name a
  // question
  const SEND_LEAD = __SEND_LEAD__;
  const SEND_VERB = __SEND_VERB__;
  const SEND_OBJECT = __SEND_OBJECT__;
  const FILLER = /^(your|the|my|this|our|a|an|and|or)$/i;
  const sendName = (t) => {
    const words = (t || '').match(/[a-z]+/gi) || [];
    if (words.length && SEND_LEAD.test(words[0]) && words.length <= 6) return true;
    for (let i = 0; i < words.length; i++) {
      if (!SEND_VERB.test(words[i])) continue;
      const rest = words.slice(i + 1).filter((w) => !FILLER.test(w));
      if (!rest.length || SEND_OBJECT.test(rest[0])) return true;
    }
    return false;
  };
  const POPUP_GO_ON = /\b(continue|apply with|next step)\b/i;
  const POPUP_WAY_ON = { test: (t) => sendName(t) || POPUP_GO_ON.test(t || '') };
  // A form's note about its required marks ("* Required field", "* indicates
  // a required field", "Fields marked with * are required", "Required
  // fields are marked with an asterisk (*)"): neither a question nor a star
  // of any control
  const REQ_NOTE = /^\s*(?:[*✱＊]\s*)?(?:(?:indicates|denotes|marks)\s+(?:an?\s+)?)?(?:required|mandatory)(?:\s+(?:fields?|questions?|information))?(?:\s+(?:are|is)\s+(?:marked|shown|indicated)(?:\s+(?:with|by)(?:\s+an?)?)?(?:\s+(?:asterisk|star|[*✱＊]))?)?(?:\s*\(\s*[*✱＊]\s*\))?\.?\s*$|^\s*(?:all\s+)?(?:fields|questions)\s+(?:marked|shown)\s+(?:with|by)\s+(?:an?\s+)?(?:[*✱＊]|asterisk|star)\s*(?:\(\s*[*✱＊]\s*\)\s*)?(?:are|is)\s+(?:required|mandatory)\.?\s*$/i;
  // a star after the control in its box: a marker's own text node, or one
  // drawn by CSS or a class, outside a note (the words before the control
  // carry their own markers through `seen`)
  const starAfter = (box, el) => {
    for (const n of box.querySelectorAll('*')) {
      if (n === el || el.contains(n) || n.contains(el)) continue;
      if (!(el.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
      if (!n.getClientRects().length) continue;
      let inNote = false;
      for (let a = n; a && a !== box; a = a.parentElement) {
        if (REQ_NOTE.test(norm(a.textContent))) { inNote = true; break; }
      }
      if (inNote) continue;
      const own = norm(Array.from(n.childNodes).filter((c) => c.nodeType === 3)
        .map((c) => c.data).join(' '));
      if (own && REQ_MARK.test(own)) return true;
      if (!own && !norm(n.textContent) && styledMark(n)) return true;
    }
    return false;
  };
  // A popup's question from outside the control: the words
  // of a label[for] or an aria-labelledby target outside it, else of its
  // question box (at most three boxes up, none a form or a fieldset, each
  // holding no other question), and whether that question is starred. Never
  // the control's own words, never a fieldset's legend. The third entry
  // says where the words came from: "label" or "box". A box's words that
  // are only a note about required marks are no question, and its star is
  // taken only from a marker beside the control, never from such a note.
  const popupQuestion = (el) => {
    const marks = [];
    const lab = labelElementFor(el);
    if (lab && !lab.contains(el)) {
      const t = strip(labelText(lab, marks), marks);
      if (t[0]) return [t[0], t[1], 'label'];
    }
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const outside = by.split(/\s+/).map((id) => byIdIn(el, id))
        .filter((n) => n && n !== el && !el.contains(n));
      const t = strip(norm(outside.map((n) => labelText(n, marks)).join(' ')), marks);
      if (t[0]) return [t[0], t[1], 'label'];
    }
    let p = up(el);
    for (let i = 0; p && i < 3; i++, p = up(p)) {
      if (p.matches('body, html, form, fieldset, main, [role=main], dialog, [role=dialog]')) break;
      if (Array.from(p.querySelectorAll(QUESTION_CTRL)).some((c) => c !== el
          && !el.contains(c) && visible(c))) break;
      // the words before the control, and a star beside it (before it, or
      // a marker after it)
      const mine = [];
      const raw = seen(p, mine, (n) => n === el || isCtrl(n));
      const note = REQ_NOTE.test(raw);
      const [words, starBefore] = note ? ['', false] : strip(raw, mine);
      const star = starBefore || starAfter(p, el);
      if (words || star) return [words, star, 'box'];
    }
    return ['', false, ''];
  };
  // A popup's kind: a field when it has a question from
  // outside it that is no chrome name, or when it is required by its own
  // attributes (`aria-required`, `required`, Workday's aria-label ending
  // "Required") or its own question's star; else a button (left among the
  // page's buttons, never opened) when its own name, shown or aria-label,
  // is a send or go-on phrase; else chrome for a chrome word or name; else a
  // field when it names a question of its own (an aria-label) or shows a
  // placeholder, or is a listbox inside a form.
  const chromeName = (t) => !!t && !ASKS.test(t) && (chromePopup(t) || POPUP_CHROME_LABEL.test(t));
  // A menu whose own name is chrome or a way on keeps that name over its
  // box's words: an aria-label that is chrome or a
  // way on ("More submit options"), or a shown text that is a whole chrome
  // word ("More"). A shown text with more words is an answer ("Continue
  // studies"), and a value picker (listbox) shows its answer, so its box's
  // words still ask.
  const MENU_CHROME = /^(more( options| actions)?|menu|options|actions|share( this job)?|settings|profile|(my |your )?account)$/i;
  const popupKind = (el, pop, text, aria, question, required, from) => {
    const ariaName = ariaWords(aria)[0];
    if (from === 'box' && pop !== 'listbox') {
      if (ariaName && POPUP_WAY_ON.test(ariaName)) return 'button';
      if (MENU_CHROME.test(ariaName || text)) {
        return 'chrome';
      }
    }
    if ((question && !chromeName(question)) || required) return 'field';
    if (POPUP_WAY_ON.test(text) || POPUP_WAY_ON.test(aria)) return 'button';
    if (chromePopup(text) || chromeName(ariaWords(aria)[0]) || question) return 'chrome';
    if (ariaWords(aria)[0] || PLACEHOLDER_OPTION.test(text)) return 'field';
    if (pop === 'listbox' && closestC(el, 'form, dialog, [role=dialog], [aria-modal=true], fieldset')) {
      return 'field';
    }
    return 'chrome';
  };
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
        if (junk(el, t, label, req)) continue;
        push(el, describe(el, 'file', label, parser ? false : (req || isRequired(el)), locatorFor(el),
                          [], { help: parser ? 'autofill parser' : '' }));
        continue;
      }
      if (!visible(el)) continue;
      // an untyped typeahead: a text box with a results list beside it
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
      if (junk(el, t, label, req)) continue;
      const type = typeahead || combo ? 'listbox' : typeOf(el);
      // react-select's dummy input takes its clicks through its face
      const face = hiddenish(el) ? comboFace(el) : null;
      if (face) kept.add(el);
      push(el, describe(el, type, label, req || isRequired(el), locatorFor(el),
                        type === 'listbox' ? [] : optionsFor(el, type),
                        { widget: combo ? 'combo' : typeahead ? 'typeahead' : '',
                          click: face ? locatorFor(face) : '' }));
      continue;
    }
    if (el.tagName === 'SELECT') {
      const proxy = proxied.get(el);
      if (!visible(el) && !proxy) continue;
      const [label, req] = labelFor(el, marks);
      if (junk(el, 'select', label, req)) continue;
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
      if (junk(el, 'textarea', label, req)) continue;
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
      // a custom tick box
      const [label, req] = labelFor(el, marks);
      if (junk(el, 'checkbox', label, req)) continue;
      asButtons.add(el);
      push(el, describe(el, 'checkbox', label, req || isRequired(el), locatorFor(el), ['checked'],
                        { widget: 'aria_check' }));
      continue;
    }
    if (role === 'textbox' || (el.isContentEditable && el.hasAttribute('contenteditable'))) {
      // a rich-text box, the outermost editable only
      if (up(el) && closestC(up(el), '[contenteditable=""], [contenteditable=true], [role=textbox]')) continue;
      const [label, req] = labelFor(el, marks);
      push(el, describe(el, 'textarea', label, req || isRequired(el), locatorFor(el), [],
                        { widget: 'editable' }));
      continue;
    }
    if (el.matches('[aria-haspopup]')) {
      // a dropdown drawn as a button: Workday's "Select One",
      // Teamtailor's menu, Paylocity's div
      const pop = (el.getAttribute('aria-haspopup') || '').toLowerCase();
      if (!['listbox', 'menu', 'true'].includes(pop) || asButtons.has(el)) continue;
      // one holding its own text box is that box's (the box is the field)
      if (Array.from(el.querySelectorAll('input:not([type=hidden])')).some(visible)) continue;
      const text = norm(el.innerText) || norm(el.value);
      const aria = norm(el.getAttribute('aria-label'));
      const [question, star, from] = popupQuestion(el);
      const own = isRequired(el) || ariaWords(aria)[1] || star;
      if (popupKind(el, pop, text, aria, question, own, from) !== 'field') continue;
      const [label, req] = labelFor(el, marks);
      asButtons.add(el);
      push(el, describe(el, 'listbox', label || question || text, own || req, locatorFor(el),
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
    // a disabled control is kept, flagged: a Submit that waits for the
    // form to validate is the page's way on once it is filled
    const disabled = !enabled(el);
    if (closestC(el, '[role=combobox]') || inConsent(el) || outsideModal(el)) continue;
    if (Array.from(asButtons).some((f) => f !== el && containsC(f, el))) continue;
    // a shadow root's button shows its host's words through a slot
    const host = rootOf(el).host;
    const text = norm(el.innerText) || norm(el.value) || norm(el.getAttribute('aria-label'))
      || norm(el.getAttribute('title')) || (host ? norm(host.innerText) : '');
    if (!text) continue;        // an icon with no name the judge could read
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
    else if (submits || __SUBMIT_WORDS__.test(text)) kind = 'submit';
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
    // the open modal's text first: it is what the page asks now
    const own = (modal.innerText || '').trim();
    if (own) text = [own, text.replace(own, '').trim()].filter(Boolean).join('\n');
  }
  return { fields: fields, buttons: buttons, text: text.slice(0, cap), dialog: modalTitle.slice(0, 160),
           modal: !!modal };
}
""".replace("__OPTION_LABEL__", RADIO_OPTION_LABEL_JS).replace("__CONSENT__", CONSENT_ROOTS_JS).replace(
    "__IDENT__", IDENT_FN_JS).replace("__PLACEHOLDER__", PLACEHOLDER_TEXT_JS).replace(
    "__SEND_LEAD__", apply_send_words.js_send_lead()).replace(
    "__SEND_VERB__", apply_send_words.js_send_verb()).replace(
    "__SEND_OBJECT__", apply_send_words.js_send_object()).replace(
    "__SUBMIT_WORDS__", apply_send_words.js_submit()).replace("__VISIBLE__", VISIBLE_FN_JS)

_TEXT_JS = "() => document.body ? (document.body.innerText || '') : ''"


# --- live reads of the page (the submit gate and every click) ------------------------

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
# again.
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

_FORM_INDEX_JS = r"""(css) => css.map((c) => {
  let el = null;
  try { el = document.querySelector(c); } catch (e) { return -2; }
  if (!el) return -2;
  const f = el.form || el.closest('form');
  return f ? Array.from(document.forms).indexOf(f) : -1;
})"""

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
# tied}]}. Each invalid row carries the control's identity
# (`IDENT_FN_JS`), its name or id and whether it shows (`shown`); each error
# text the identity and name of the control that names it, or of the one
# control in its box, so the repair can find the field.
_VALIDITY_JS = r"""({bcss, fcss}) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const identOf = __IDENT__;
  const visible = __VISIBLE__;
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
    // a required control in the box beside the button counts wherever it
    // sits (a wizard's footer bar holding the terms box beside its Submit)
    const near = (el) => !outside(el)
      || (box !== document.body && (el.required === true || el.getAttribute('aria-required') === 'true'));
    const loose = Array.from(box.querySelectorAll('input, select, textarea'))
      .filter((el) => !owner(el) && near(el));
    controls = [...Array.from(forms).flatMap((f) => Array.from(f.elements)), ...loose];
    inScope = (el) => Array.from(forms).some((f) => f.contains(el))
      || (box.contains(el) && !owner(el) && near(el));
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
                  reason: REASONS.find((r) => el.validity[r]) || 'invalid', ident: identOf(el),
                  name: el.name || el.id || '', shown: visible(el)});
  }
  for (const el of document.querySelectorAll('[aria-invalid=true]')) {
    if (seen.has(el) || outside(el) || !visible(el) || !inScope(el)) continue;
    seen.add(el);
    const desc = el.getAttribute('aria-describedby') || el.getAttribute('aria-errormessage') || '';
    const msg = norm(desc.split(/\s+/).map((i) => { const n = document.getElementById(i);
      return n ? n.innerText : ''; }).join(' '));
    invalid.push({label: labelOf(el), message: msg.slice(0, 160), reason: 'aria-invalid',
                  ident: identOf(el), name: el.getAttribute('name') || el.id || '', shown: true});
  }
  const errors = [];
  const texts = new Set();
  const esel = '[role=alert], [aria-live=assertive], [class*=error i], [class*=invalid i], '
    + '[class*=danger i]';
  const SUCCESS = /alert-success|alert-info|\bsuccess\b/i;
  // the control that names an error text (aria-describedby, aria-errormessage)
  const namer = (el) => !el.id ? null : Array.from(document.querySelectorAll(
    '[aria-describedby], [aria-errormessage]')).find((c) => (
      (c.getAttribute('aria-describedby') || '') + ' ' + (c.getAttribute('aria-errormessage') || ''))
      .split(/\s+/).includes(el.id)) || null;
  const namedBy = (el) => !!namer(el);
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
    // controls that is no page, form or main region; its control, when it
    // holds one alone (or the control that names the text)
    let field = false;
    let owner = namer(el);
    let p = el.parentElement;
    for (let i = 0; p && i < 2 && !field; i++, p = p.parentElement) {
      if (p.matches('body, html, form, main, [role=main]')) break;
      const ctrls = p.querySelectorAll('input:not([type=hidden]), select, textarea');
      field = ctrls.length >= 1 && ctrls.length <= 6;
      if (!owner && ctrls.length === 1) owner = ctrls[0];
    }
    errors.push({text: text.slice(0, 200), field: field, tied: !!namer(el),
                 ident: owner ? identOf(owner) : '',
                 name: owner ? (owner.getAttribute('name') || owner.id || '') : ''});
  }
  return {invalid: invalid.slice(0, 20), errors: errors.slice(0, 10)};
}""".replace("__CONSENT__", CONSENT_ROOTS_JS).replace("__IDENT__", IDENT_FN_JS).replace("__VISIBLE__", VISIBLE_FN_JS)

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

# The controls the extractor does not see as fields, in the composed tree
# (open shadow roots walked): a native control inside a shadow root, an ARIA
# textbox / radio / checkbox / switch / spinbutton that is no native control,
# a contenteditable box, a custom element the run cannot read into (kind
# "unreadable", `why`: a closed shadow root or a form-associated custom
# element; required by its attributes, empty by its own validity or its
# value, else read as empty); outside the site chrome, a consent banner and a
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
  const visible = __VISIBLE__;
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
  // a custom element the run cannot read into: form-associated (its
  // value lives in the element's internals), or upgraded with a box, no open
  // shadow root and nothing inside in the light DOM, and named or marked as a
  // control (a name, required, a label, a control's role, a tab stop): a
  // closed shadow root
  const unreadable = (el) => {
    if (!el.localName.includes('-')) return '';
    const def = window.customElements && customElements.get(el.localName);
    if (!def) return '';
    if (def.formAssociated) return 'form-associated custom element';
    if (el.shadowRoot || el.querySelector('input, select, textarea, [contenteditable]')) return '';
    if (norm(el.innerText)) return '';
    const named = ['name', 'required', 'aria-required', 'aria-label', 'label', 'placeholder']
      .some((a) => el.hasAttribute(a)) || /^(textbox|combobox|listbox|radiogroup|checkbox|switch|spinbutton)$/
      .test(el.getAttribute('role') || '') || (el.tabIndex >= 0 && el.hasAttribute('tabindex'));
    return named ? 'closed shadow root' : '';
  };
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
      let why = '';
      if (shadow && NATIVE.test(el.tagName)) {
        if (el.tagName === 'INPUT' && SKIP.has((el.getAttribute('type') || 'text').toLowerCase())) continue;
        kind = el.tagName.toLowerCase();
      } else if (ROLES.test(role) && !NATIVE.test(el.tagName)) kind = role;
      else if (el.isContentEditable && el.hasAttribute('contenteditable')) kind = 'contenteditable';
      else if ((why = unreadable(el))) kind = 'unreadable';
      if (!kind) continue;
      const box = closestComposed(el, '[role=combobox]');
      if (closestComposed(el, CHROME) || consent.some((r) => insideComposed(el, r))
          || (box && box !== el)) continue;
      if (!visible(el)) continue;
      let required = el.required === true || el.getAttribute('aria-required') === 'true'
        || (kind === 'unreadable' && el.hasAttribute('required'));
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
      } else if (kind === 'unreadable') {
        // its own validity when it has one (`:invalid` reads a form-associated
        // element's internals), else its value when it shows one, else unknown:
        // read as empty (the run cannot tell it holds an answer)
        let invalid = false;
        try { invalid = el.matches(':invalid'); } catch (e) { invalid = false; }
        if (why === 'form-associated custom element') {
          empty = invalid;
          // one its internals mark invalid blocks its form's send whatever
          // its attributes say: it counts as required
          required = required || invalid;
        } else empty = invalid || !('value' in el) || !norm(String(el.value || ''));
      } else if (NATIVE.test(el.tagName)) {
        empty = !norm(el.value);
      } else {
        empty = !norm(el.innerText);
      }
      if (requiredOnly && !(required && empty)) continue;
      const label = kind === 'unreadable'
        ? norm(el.getAttribute('aria-label') || el.getAttribute('label')
               || (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('placeholder')
               || el.getAttribute('name') || el.localName).slice(0, 80)
        : labelOf(el);
      out.push({label: label, kind: kind, required: !!required, empty: !!empty,
                shadow: !!shadow, why: why});
    }
  };
  walk(document, false);
  return out.slice(0, 40);
}""".replace("__CONSENT__", CONSENT_ROOTS_JS).replace("__VISIBLE__", VISIBLE_FN_JS)

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
