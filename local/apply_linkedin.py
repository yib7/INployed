"""LinkedIn's pages, read without the judge.

Every queued job starts on LinkedIn, and LinkedIn is never the application:
the run takes the job page's offsite Apply (the company's site) and nothing
else. This module tells the run what a LinkedIn page is from its URL and its
DOM, so the answer never rests on a page-state read:

- `url_kind(url)`: "redirector" (`/safety/go/`, the hop an offsite Apply goes
  through), "signed_out" (the login page, a checkpoint, the authwall, the
  sign-up page), "job" (`/jobs/view/<id>`, the email-alert `/comm/jobs/view/`
  and the two-pane `?currentJobId=<id>` views; `/jobs/view/externalApply/` is
  LinkedIn's hop to the company's site, no job page), "other" for the rest of
  LinkedIn, "" off LinkedIn (`is_linkedin`: linkedin.com and every
  `*.linkedin.com` host, the country subdomains too).
- `job_id(url)`: the job a job page shows (its `/jobs/view/<id>` or
  `currentJobId`), whatever tracking parameters the URL carries.
- `read(page)`: one JS pass over the main frame (`View`): the offsite Apply
  controls and the Easy Apply ones outside any dialog, the site's chrome,
  list items and the right rail (a top card's Apply laid out in a list
  counts when its aria-label names the company's website),
  a dialog that holds form fields (LinkedIn's own Easy Apply form), a sign-in
  surface (a sign-in dialog or a visible password box), and the top card's
  "Applied" and "No longer accepting applications" marks (list cards and the
  right rail left out: another job's card can say "Applied").
- `decide(view)`: the page's `Decision`, in this order: an open form dialog
  (Easy Apply's form: the job parks), already applied, closed, an offsite
  Apply to click, Easy Apply only (parks), signed out, nothing found.
  `Decision.final` says whether waiting longer could change it (a top card
  that renders late turns "nothing" or "signed out" into an Apply).
- `continue_control(page)`: the visible "Continue" of the job-search safety
  interstitial, which does not send the tab on by itself.

The Easy Apply rule: an Easy Apply job is applied to
on LinkedIn by the user, so the run stops with `EASY_APPLY_REASON` and never
clicks, fills, ticks, picks or uploads anything on LinkedIn.

Playwright is reached only through the `page` argument.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from apply_form import LOCATOR_FN_JS
from apply_form_js import VISIBLE_AREA_FN_JS

LINKEDIN_SITE = "linkedin.com"
EASY_APPLY_REASON = "Easy Apply: apply on LinkedIn"
EASY_APPLY_NOTE = "an Easy Apply job: apply on LinkedIn yourself, then Mark applied"
SIGNED_OUT_REASON = "LinkedIn is signed out"
APPLIED_REASON = "already applied: LinkedIn shows the job as applied"
APPLIED_NOTE = "LinkedIn shows this job as applied; Mark applied if you sent it"
CLOSED_REASON = "closed: LinkedIn says the job no longer accepts applications"
CLOSED_NOTE = "the posting is closed"
NO_APPLY_REASON = "no offsite Apply on the LinkedIn page"

_SIGNED_OUT_PATHS = ("/login", "/uas/login", "/checkpoint", "/authwall", "/signup", "/reg")
_REDIRECTOR = "/safety/go"
# a job page; `/jobs/view/externalApply/<id>` is LinkedIn's hop to the company's site
_JOB_PATH = re.compile(r"^/(comm/)?jobs/view/(?!externalApply(/|$))([^/?#]+)")
_JOB_ID = re.compile(r"(\d{5,})$")

# What the kinds of `decide` are, and which of them end the wait for a late
# top card.
FINAL_KINDS = frozenset(("form_dialog", "applied", "closed", "offsite", "easy_apply"))


def is_linkedin(url_or_host: str) -> bool:
    """Is this LinkedIn: linkedin.com or any `*.linkedin.com` host?"""
    raw = str(url_or_host or "").strip()
    host = (urlsplit(raw).hostname or "") if "://" in raw else raw.split("/")[0].split(":")[0]
    host = host.lower().rstrip(".")
    return host == LINKEDIN_SITE or host.endswith("." + LINKEDIN_SITE)


def url_kind(url: str) -> str:
    """The kind of LinkedIn page `url` is by its shape (see the module
    docstring); "" off LinkedIn."""
    if not is_linkedin(url):
        return ""
    parts = urlsplit(str(url or ""))
    path = parts.path or "/"
    if path.startswith(_REDIRECTOR):
        return "redirector"
    if any(path == p or path.startswith(p + "/") for p in _SIGNED_OUT_PATHS):
        return "signed_out"
    if _JOB_PATH.match(path):
        return "job"
    if path.startswith("/jobs/") and parse_qs(parts.query).get("currentJobId"):
        return "job"
    return "other"


def job_id(url: str) -> str:
    """The LinkedIn job a job page shows: the `/jobs/view/<id>` segment (a
    slug's trailing number) or the two-pane view's `currentJobId`; "" when
    the URL names none. Tracking parameters and slugs do not change it."""
    parts = urlsplit(str(url or ""))
    current = parse_qs(parts.query).get("currentJobId")
    if current and current[0].strip():
        return current[0].strip()
    m = _JOB_PATH.match(parts.path or "")
    if not m:
        return ""
    segment = m.group(3)
    num = _JOB_ID.search(segment)
    return num.group(1) if num else segment


def signed_out_evidence(url: str) -> str:
    """What a signed-out URL is, in words, for the park reason."""
    path = urlsplit(str(url or "")).path or "/"
    for prefix, words in (("/checkpoint", "a checkpoint page"), ("/authwall", "the authwall"),
                          ("/signup", "the sign-up page"), ("/reg", "the sign-up page")):
        if path.startswith(prefix):
            return words
    return "the login page"


@dataclass
class Control:
    """One Apply-like control of the page: `css` locates it in the main frame."""
    css: str
    text: str
    aria: str = ""
    href: str = ""
    target: str = ""

    @property
    def label(self) -> str:
        return self.text or self.aria


@dataclass
class View:
    """What `read` found on a LinkedIn page."""
    offsite: list[Control] = field(default_factory=list)
    easy: list[Control] = field(default_factory=list)
    form_dialog: int = 0            # form fields inside an open dialog (not a sign-in)
    signin_dialog: bool = False
    password_box: bool = False
    applied: str = ""
    closed: str = ""
    title: str = ""
    offsite_listed: bool = False    # the offsite Apply is a list item's beside the title

    def to_dict(self) -> dict[str, Any]:
        return {"offsite": [c.label for c in self.offsite], "easy": [c.label for c in self.easy],
                "form_dialog": self.form_dialog, "signin_dialog": self.signin_dialog,
                "password_box": self.password_box, "applied": self.applied,
                "closed": self.closed, "title": self.title,
                "offsite_listed": self.offsite_listed}


@dataclass
class Decision:
    kind: str                       # form_dialog | applied | closed | offsite | easy_apply
    #                                 | signed_out | none
    why: str
    control: Control | None = None
    # an offsite Apply found in a list item: the handler reads the page again
    # after a settle before it clicks it (the top card may still be rendering)
    tentative: bool = False

    @property
    def final(self) -> bool:
        return self.kind in FINAL_KINDS and not self.tentative


def decide(view: View) -> Decision:
    """The page's decision (see the module docstring for the order)."""
    if view.form_dialog:
        return Decision("form_dialog", f"a dialog with {view.form_dialog} form field(s) is "
                                       "open on LinkedIn (its Easy Apply form)")
    if view.applied:
        return Decision("applied", f"the top card says {view.applied!r}")
    if view.closed:
        return Decision("closed", f"the top card says {view.closed!r}")
    if view.offsite:
        c = view.offsite[0]
        if view.offsite_listed:
            return Decision("offsite", f"the top card's offsite Apply {c.label!r}, in a list "
                                       f"beside the title {view.title!r}", c,
                            tentative=True)
        return Decision("offsite", f"the top card's offsite Apply {c.label!r}", c)
    if view.easy:
        c = view.easy[0]
        return Decision("easy_apply", f"the only Apply is Easy Apply ({c.label!r})", c)
    if view.signin_dialog or view.password_box:
        return Decision("signed_out", "a sign-in dialog" if view.signin_dialog
                        else "a password box on the page")
    return Decision("none", "no Apply, Easy Apply, applied or closed mark on the page")


# One pass over the main frame. The offsite Apply is "Apply", "Apply now",
# "Apply on company website" (text) or an aria-label naming the company's
# website; Easy Apply by its words in the text or the aria-label ("Easy Apply
# to <title> at <company>"). A control longer than 40 characters is a card
# (a similar job's link carries "Easy Apply" inside a long text). An Easy
# Apply in a list item or the right rail belongs to another job or to the
# search's filters (the two-pane view's "Easy Apply" filter pill); so does a
# plain "Apply" there. The top card's own Apply comes first; one in a list
# item (a top card that lays its actions out as a list) counts when its
# aria-label names the company's website and the page shows no other Apply or
# Easy Apply; the rail's never.
_READ_JS = r"""
() => {
  const locatorFor = __LOCATOR__;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const visible = __VISIBLE_AREA__;
  const DIALOG = 'dialog, [role=dialog], [role=alertdialog], [aria-modal=true]';
  const CHROME = 'header, footer, nav, search, [role=banner], [role=contentinfo], '
    + '[role=navigation], [role=search]';
  const CARD = 'li, aside, [role=listitem], [role=complementary]';
  const RAIL = 'aside, [role=complementary]';
  const PLAIN = /^apply(\s+(now|here|online|for\s+this\s+(job|position|role)|on\s+(the\s+)?company(['’]s)?\s+(website|site)))?$/i;
  const COMPANY = /\bapply\b.*\bcompany(['’]s)?\s+(website|site)\b/i;
  const EASY = /easy\s*apply/i;
  const out = {offsite: [], easy: [], form_dialog: 0, signin_dialog: false,
               password_box: false, applied: '', closed: '', title: '', listed: false};
  // the job's title: the first visible h1 outside dialogs, chrome, lists and the rail
  const h1 = Array.from(document.querySelectorAll('h1')).find(
    (h) => visible(h) && !h.closest(DIALOG) && !h.closest(CHROME) && !h.closest(CARD));
  out.title = h1 ? norm(h1.innerText).slice(0, 120) : '';
  // a list item's control belongs to the top card when it and the title share a
  // container at most three levels above the title, below the page's main
  // column, and its list item carries nothing but its controls (a job card
  // carries its own title and company)
  const inTopCard = (el, text) => {
    if (!h1) return false;
    let box = h1.parentElement;
    for (let i = 0; box && i < 3; i += 1, box = box.parentElement) {
      if (box === document.body || box.matches('main, [role=main]')) return false;
      if (box.contains(el)) {
        const item = el.closest('li, [role=listitem]');
        if (!item || item.querySelector('h1, h2, h3, h4, h5, h6')) return false;
        return norm(item.innerText).length - text.length <= 20;
      }
    }
    return false;
  };
  const sel = 'a[href], button, [role=button], input[type=button], input[type=submit]';
  const listed = [];
  for (const el of document.querySelectorAll(sel)) {
    if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    if (el.closest(DIALOG) || el.closest(CHROME)) continue;
    const text = norm(el.innerText) || norm(el.value);
    const aria = norm(el.getAttribute('aria-label'));
    if (text.length > 40) continue;
    const words = text + ' ' + aria;
    const c = {css: locatorFor(el), text: text, aria: aria.slice(0, 160),
               href: el.getAttribute('href') || '', target: el.getAttribute('target') || ''};
    const inCard = !!el.closest(CARD);
    if (EASY.test(words)) { if (!inCard) out.easy.push(c); continue; }
    if (!inCard) {
      if (PLAIN.test(text) || COMPANY.test(aria) || (!text && PLAIN.test(aria))) out.offsite.push(c);
    } else if (COMPANY.test(aria) && !el.closest(RAIL) && inTopCard(el, text)) {
      listed.push(c);
    }
  }
  // a top card that lays its actions out as a list: its offsite Apply names
  // the company's website in its aria-label and sits beside the job's title;
  // taken only when the page shows no other Apply and no Easy Apply (never
  // the rail's), and marked so the handler reads the page again first
  if (!out.offsite.length && !out.easy.length && listed.length) {
    out.offsite = listed;
    out.listed = true;
  }
  const SIGNIN = /\b(sign\s*in|log\s*in|join\s+(now|linkedin)|welcome back)\b/i;
  for (const d of document.querySelectorAll(DIALOG)) {
    if (!visible(d)) continue;
    const boxes = Array.from(d.querySelectorAll('input, select, textarea')).filter((el) => {
      const t = (el.getAttribute('type') || '').toLowerCase();
      return !['hidden', 'submit', 'button', 'image', 'reset'].includes(t) && visible(el);
    });
    if (!boxes.length) continue;
    const pw = boxes.some((el) => (el.getAttribute('type') || '').toLowerCase() === 'password');
    if (pw || SIGNIN.test(norm(d.innerText).slice(0, 400))) out.signin_dialog = true;
    else out.form_dialog += boxes.length;
  }
  out.password_box = Array.from(document.querySelectorAll('input[type=password]')).some(visible);
  const APPLIED = /^(you\s+)?applied(\s+on\s+(the\s+)?company(['’]s)?\s+(website|site))?(\s+(\d+\s+(second|minute|hour|day|week|month|year)s?|an?\s+(minute|hour|day|week|month|year))\s+ago)?(\s*[·•]\s*see application)?$|^application\s+(submitted|sent)\b.{0,40}$/i;
  const CLOSED = /no longer accepting applications|this job is no longer available/i;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const t = norm(node.data);
    if (!t || t.length > 160) continue;
    const p = node.parentElement;
    if (!p || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|OPTION)$/.test(p.tagName)) continue;
    if (p.closest(DIALOG) || p.closest(CHROME) || p.closest(CARD) || !visible(p)) continue;
    if (!out.closed && CLOSED.test(t)) out.closed = t.slice(0, 80);
    else if (!out.applied && APPLIED.test(t)) out.applied = t.slice(0, 80);
  }
  return out;
}
""".replace("__LOCATOR__", LOCATOR_FN_JS).replace("__VISIBLE_AREA__", VISIBLE_AREA_FN_JS)

# The job-search safety interstitial's "Continue" (a link or a button).
_CONTINUE_JS = r"""
() => {
  const locatorFor = __LOCATOR__;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const visible = __VISIBLE_AREA__;
  for (const el of document.querySelectorAll('a[href], button, [role=button], input[type=button]')) {
    if (!visible(el)) continue;
    const text = norm(el.innerText) || norm(el.value) || norm(el.getAttribute('aria-label'));
    if (/^continue\b/i.test(text) && text.length <= 40) {
      return {css: locatorFor(el), text: text, aria: norm(el.getAttribute('aria-label')),
              href: el.getAttribute('href') || '', target: el.getAttribute('target') || ''};
    }
  }
  return null;
}
""".replace("__LOCATOR__", LOCATOR_FN_JS).replace("__VISIBLE_AREA__", VISIBLE_AREA_FN_JS)


def _control(raw: dict) -> Control:
    return Control(css=str(raw.get("css") or ""), text=str(raw.get("text") or ""),
                   aria=str(raw.get("aria") or ""), href=str(raw.get("href") or ""),
                   target=str(raw.get("target") or ""))


_WORD = re.compile(r"[a-z0-9]+")
_COMMON = frozenset(("and", "the", "for", "with", "job", "jobs", "senior", "junior", "lead",
                     "remote", "hybrid", "inc", "llc", "ltd", "corp", "company"))


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(str(text or "").lower()) if len(w) >= 3} - _COMMON


def same_job(title: str, job_title: str = "", company: str = "") -> bool:
    """Does the page's title name the queued job, loosely: a word of three
    letters or more (common ones left out) that the queued title or company
    also has? Without a queued title or company there is nothing to hold
    it to, and it passes."""
    wanted = _words(job_title) | _words(company)
    if not wanted:
        return True
    return bool(_words(title) & wanted)


def read(page, *, job_title: str = "", company: str = "") -> View:
    """The page's `View` (an empty one when the page cannot be read). A list
    item's offsite Apply is kept only when the page's title names the queued
    job (`same_job`, with the entry's `job_title` and `company`)."""
    try:
        raw = page.main_frame.evaluate(_READ_JS)
    except Exception:       # noqa: BLE001  (a page mid-navigation reads as nothing yet)
        return View()
    title = str(raw.get("title") or "")
    offsite = [_control(c) for c in raw.get("offsite") or []]
    listed = bool(raw.get("listed"))
    if listed and not (title and same_job(title, job_title, company)):
        offsite, listed = [], False
    return View(offsite=offsite, offsite_listed=listed,
                easy=[_control(c) for c in raw.get("easy") or []],
                form_dialog=int(raw.get("form_dialog") or 0),
                signin_dialog=bool(raw.get("signin_dialog")),
                password_box=bool(raw.get("password_box")),
                applied=str(raw.get("applied") or ""), closed=str(raw.get("closed") or ""),
                title=title)


def continue_control(page) -> Control | None:
    """The safety interstitial's visible "Continue", or None."""
    try:
        raw = page.main_frame.evaluate(_CONTINUE_JS)
    except Exception:       # noqa: BLE001
        return None
    return _control(raw) if raw else None
