"""Read verification mail in a separate tab of the signed-in browser profile."""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import urlsplit

import apply_judge
import apply_verify

# Provider DOM shapes are kept together so markup changes need one update.
SELECTORS = {
    "outlook": {
        "row": '[role="listbox"] [role="option"], [data-convid]',
        "sender": '[data-testid="sender"], [data-automationid="sender"], .lvHighlightFrom',
        "subject": '[data-testid="subject"], [data-automationid="subject"], .lvHighlightSubject',
        "preview": '[data-testid="preview"], [data-automationid="preview"], .lvHighlightBody',
        "time": '[data-testid="date"], [data-automationid="date"], time, .lvHighlightDate',
        "body": '[role="document"], [aria-label="Message body"]',
    },
    "gmail": {
        "row": "tr.zA",
        "sender": ".yX [email], .yX",
        "subject": ".bog",
        "preview": ".y2",
        "time": "td.xW span, .xW span, time",
        "body": ".a3s",
    },
}
PROVIDER_HOSTS = {
    "gmail": ("mail.google.com",),
    "outlook": ("outlook.office.com", "outlook.office365.com", "outlook.live.com",
                "outlook.com"),
}
TIMEOUT_MS = 5_000
GOTO_TIMEOUT_MS = 20_000     # the inbox page's load (ACC-08: a webmail slower than 5 s)
ROWS_WAIT_MS = 10_000        # for its message rows to render once it loaded (ACC-08)
SENDER_SCAN = 4              # elements checked for a sender address attribute
CANDIDATES_CAP = 15          # code-shaped tokens read out of one message
LINKS_CAP = 12               # verification links offered to the judge from one message
BODY_SELECTOR = ", ".join(s["body"] for s in SELECTORS.values())
ROW_SELECTOR = ", ".join(s["row"] for s in SELECTORS.values())

# a link that checks an address or starts an account: its words or its path
VERIFY_LINK_WORDS = re.compile(r"verif|confirm|activat|validat", re.I)
_LINKS_JS = """els => els.flatMap(e => Array.from(e.querySelectorAll('a[href]')).map(a =>
  [((a.innerText || '') + ' ' + (a.getAttribute('aria-label') || '')).replace(/\\s+/g, ' ').trim(),
   a.href]))"""


@dataclass
class Message:
    n: int
    sender: str
    subject: str
    preview: str
    open_locator: str
    when: str = ""           # the row's time as the provider shows it (ACC-07)


def code_hash(code: str) -> str:
    """A code the run used, as it is remembered (ACC-07): never the code."""
    return hashlib.sha256(str(code).strip().encode("utf-8")).hexdigest()


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _when(text: str, now: datetime) -> tuple[datetime | None, bool]:
    """(the time a row's words name, whether they name a clock time): an ISO
    date, a US M/D/Y date or a month and day, else a weekday (the last one
    before today) or "yesterday", else today when a clock time alone shows;
    (None, False) for words that name no time."""
    t = " ".join(str(text or "").split()).lower()
    if not t:
        return None, False
    day = None
    clock = None
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})(?:[t ](\d{1,2}):(\d{2}))?", t)
    if m:
        day = (int(m[1]), int(m[2]), int(m[3]))
        if m[4]:
            clock = (int(m[4]), int(m[5]))
    else:
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", t)
        if m:
            year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            day = (year, int(m[1]), int(m[2]))
        else:
            m = re.search(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\b(?:,?\s+(\d{4}))?", t)
            if m:
                month, dom = _MONTHS.index(m[1]) + 1, int(m[2])
                year = int(m[3]) if m[3] else now.year
                if not m[3] and (month, dom) > (now.month, now.day):
                    year -= 1           # "Dec 30" read in January is last year's
                day = (year, month, dom)
    if clock is None:
        c = re.search(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap])\.?\s*m\b\.?", t)
        if c:
            hour = int(c[1]) % 12 + (12 if c[3] == "p" else 0)
            clock = (hour, int(c[2]))
        else:
            c = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
            if c:
                clock = (int(c[1]), int(c[2]))
    if day is None:
        w = re.search(r"\b(" + "|".join(_WEEKDAYS) + r")[a-z]*\b", t)
        if w:
            back = (now.weekday() - _WEEKDAYS.index(w[1])) % 7 or 7
            d = (now - timedelta(days=back)).date()
            day = (d.year, d.month, d.day)
        elif "yesterday" in t:
            d = (now - timedelta(days=1)).date()
            day = (d.year, d.month, d.day)
        elif clock is not None:
            day = (now.year, now.month, now.day)
        else:
            return None, False
    try:
        hour, minute = clock or (0, 0)
        return datetime(day[0], day[1], day[2], hour, minute), clock is not None
    except ValueError:
        return None, False


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """The time a row's words name (`_when`), or None."""
    return _when(text, now or datetime.now())[0]


def _stale(message: Message, since: datetime | None) -> bool:
    """A message from before `since` (the job's start, ACC-07): its day
    before `since`'s day, or with a clock time, its minute before `since`'s
    minute. A row whose time cannot be read is not stale."""
    if since is None:
        return False
    when, has_clock = _when(message.when, datetime.now())
    if when is None:
        return False
    if not has_clock:
        return when.date() < since.date()
    return when < since.replace(second=0, microsecond=0)


def provider_for(inbox_url: str) -> str | None:
    """The provider the inbox host names, or None when the host says nothing.

    A host match is the reliable signal; the shape trial in `list_messages` is
    the fallback, and it is what a tenant on its own domain (an Outlook Web
    App at a university) goes through."""
    host = (urlsplit(str(inbox_url or "")).hostname or "").lower()
    for name, hosts in PROVIDER_HOSTS.items():
        # an exact host or a subdomain of one; a prefix match would accept
        # `outlook.com.example.invalid`, which starts with a listed host
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    return None


def list_messages(page, inbox_url: str, limit: int = 15) -> list[Message]:
    """Read the newest visible rows from the provider's inbox order.

    The provider the inbox host names is tried first; the others follow, so a
    webmail on its own domain still works. A row with no subject and no sender
    is skipped, which is how a foreign `role=listbox` (Gmail's own label picker
    matches the Outlook row selector) yields nothing and the next shape gets
    its turn; a row that names a sender is kept when the subject hook alone
    has drifted. A webmail is given `GOTO_TIMEOUT_MS` to load and
    `ROWS_WAIT_MS` more for its rows to render (ACC-08: an inbox slower than
    5 s read as empty); an inbox that shows no row by then is read as it
    is. Each row keeps the time it shows (`Message.when`, ACC-07)."""
    page.goto(inbox_url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
    try:
        page.wait_for_selector(ROW_SELECTOR, state="attached", timeout=ROWS_WAIT_MS)
    except Exception:  # noqa: BLE001  (an empty inbox, or rows of a shape no selector knows)
        pass
    named = provider_for(inbox_url)
    order = ([SELECTORS[named]] if named else []) + [s for n, s in SELECTORS.items()
                                                     if n != named]
    for selectors in order:
        rows = page.locator(selectors["row"])
        messages = []
        for i in range(rows.count()):
            if len(messages) >= max(0, limit):
                break
            row = rows.nth(i)
            if not row.is_visible():
                continue

            def text(key):
                matches = row.locator(selectors[key])
                count = matches.count()
                if not count:
                    return ""
                if key == "sender":
                    # Gmail keeps the address in an `email` attribute on a span
                    # inside the cell, and the cell itself matches first.
                    for j in range(min(count, SENDER_SCAN)):
                        address = matches.nth(j).get_attribute("email")
                        if address:
                            return address
                return matches.first.inner_text()

            def when():
                # the full date a provider keeps in the time's title first
                matches = row.locator(selectors["time"])
                if not matches.count():
                    return ""
                first = matches.first
                for attr in ("title", "datetime"):
                    value = first.get_attribute(attr)
                    if value and value.strip():
                        return value.strip()
                return first.inner_text().strip()

            subject = text("subject")
            sender = text("sender")
            # a row with no subject and no sender is a foreign widget; a row
            # that names a sender is mail even when the subject hook drifted
            if not subject.strip() and not sender.strip():
                continue
            messages.append(Message(len(messages), sender, subject,
                                    text("preview") or row.inner_text(),
                                    f'{selectors["row"]} >> nth={i}', when()))
        if messages:
            return messages
    return []


def open_message(page, msg: Message) -> str:
    """Open a row and read its rendered message body."""
    row = page.locator(msg.open_locator)
    was_selected = row.get_attribute("aria-selected") == "true"
    was_selected = was_selected or row.get_attribute("aria-current") not in (None, "false")
    previous = page.locator(BODY_SELECTOR).filter(visible=True).all_inner_texts()
    row.click(timeout=TIMEOUT_MS)
    if not was_selected:
        page.wait_for_function(
            """({selector, previous}) => {
                const visible = [...document.querySelectorAll(selector)]
                    .filter(element => element.getClientRects().length)
                    .map(element => element.innerText);
                return visible.some(text => text.trim()) &&
                    JSON.stringify(visible) !== JSON.stringify(previous);
            }""",
            arg={"selector": BODY_SELECTOR, "previous": previous},
            timeout=TIMEOUT_MS,
        )
    bodies = page.locator(BODY_SELECTOR).filter(visible=True)
    bodies.first.wait_for(state="visible", timeout=TIMEOUT_MS)
    return "\n".join(bodies.all_inner_texts())


def candidates(body: str, subject: str = "") -> list[str]:
    """Every code-shaped token `apply_verify.extract_code` finds in the subject
    and the body, strongest signal first and each one only once.

    The subject counts because some providers put the code there and nowhere
    else. A token is dropped from the text once it has been collected, so the
    next pass reads the next-strongest one; `extract_code`'s own filters (the
    stop words, bare years, an inline order number without a length hint) do
    the rejecting, so nothing else has to repeat those rules."""
    found: list[str] = []
    remaining = f"{subject}\n{body}" if subject else body
    for _ in range(CANDIDATES_CAP):
        code = apply_verify.extract_code(remaining)
        if not code or code in found:
            break
        if _code_shaped(code, body, subject):
            found.append(code)
        remaining = remaining.replace(code, " ")
    return found


def _code_shaped(token: str, body: str = "", subject: str = "") -> bool:
    """A token the judge is worth asking about.

    A digit or capitals say "code" on their own. An all-letter token in lower
    case is a code too when the message presents it as one: alone on its own
    line in the body, or directly after the word "code" on a single line
    ("Enter code: hunterz", "code is: hunterz"). A sentence word a line below
    the word "code" ("Thanks", "Welcome") matches the extractor's near-`code`
    pattern and is dropped here, so the pick question stays short and carries
    no prose. A title-case word alone on its own line ("Regards",
    "Unsubscribe", "Best") is a sign-off or a footer link, so the own-line form
    takes lower case only. The subject is one line, so only the labelled form
    counts there."""
    if any(ch.isdigit() for ch in token) or token.isupper():
        return True
    quoted = re.escape(token)
    if token.islower() and re.search(rf"(?m)^[ \t]*{quoted}[ \t]*$", body):
        return True
    # only spaces and tabs between the label and the token: a line break means
    # the extractor reached across a sentence
    labelled = rf"code[ \t]*(?:is)?[ \t]*[:=]?[ \t]*{quoted}\b"
    return bool(re.search(labelled, body, re.I) or re.search(labelled, subject, re.I))


def _main_frame(request) -> bool:
    """Whether a request navigates the tab's own page (no parent frame)."""
    try:
        return request.frame.parent_frame is None
    except Exception:  # noqa: BLE001  (a frame already gone: count it as the page's)
        return True


def _message(tab, inbox_url: str, site: str, *, jev, ats: str, company: str, want: str,
             since: datetime | None) -> Message | None:
    """The message the judge takes for the site's, carrying a `want` ("code"
    or "link"), among the rows no older than `since` (ACC-07); None when no
    row qualifies."""
    messages = [m for m in list_messages(tab, inbox_url) if not _stale(m, since)]
    rows = [asdict(message) for message in messages]
    if not rows:
        return None
    state, questions = apply_judge.inbox_questions(rows, site, ats=ats, company=company,
                                                   want=want)
    chosen = apply_judge.read_inbox(jev.judge(state, questions), rows, want=want)
    return next((m for m in messages if m.n == chosen), None)


def _poll(tab, inbox_url: str, site: str, *, jev, ats: str, company: str,
          since: datetime | None = None, used=frozenset()) -> str | None:
    """One read of the inbox: the message the judge takes for the site's,
    and the code it picks from that message's body, never one the run used
    already (`used`, by `code_hash`); None when either is missing."""
    message = _message(tab, inbox_url, site, jev=jev, ats=ats, company=company, want="code",
                       since=since)
    if not message:
        return None
    body = open_message(tab, message)
    picks = [c for c in candidates(body, message.subject) if code_hash(c) not in used]
    if not picks:
        return None
    state, questions = apply_judge.code_pick_questions(picks, body)
    code = apply_judge.read_code_pick(jev.judge(state, questions))
    return code if code in picks else None


def message_links(page) -> list[tuple[str, str]]:
    """(text, absolute URL) of every link in the opened message's body."""
    try:
        rows = page.locator(BODY_SELECTOR).filter(visible=True).evaluate_all(_LINKS_JS)
    except Exception:  # noqa: BLE001  (a body gone mid-read: no links)
        return []
    return [(str(text), str(href)) for text, href in rows or []]


def verification_links(links: list[tuple[str, str]], allowed: Callable[[str], bool],
                       refused: list | None = None) -> list[tuple[str, str]]:
    """The links that verify an address or start an account
    (`VERIFY_LINK_WORDS` in their text or their path), http or https, whose
    host `allowed` takes, each URL once. A verification link on any other
    host (a mail tracker's, a stranger's) is never handed back: its host
    joins `refused`."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for text, href in links:
        parts = urlsplit(href)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host or href in seen:
            continue
        if not (VERIFY_LINK_WORDS.search(text) or VERIFY_LINK_WORDS.search(parts.path)):
            continue
        seen.add(href)
        if allowed(host):
            out.append((text, href))
        elif refused is not None and host not in refused:
            refused.append(host)
    return out[:LINKS_CAP]


def _poll_link(tab, inbox_url: str, site: str, *, jev, ats: str, company: str,
               allowed: Callable[[str], bool], since: datetime | None = None,
               refused: list | None = None) -> str | None:
    """One read of the inbox for a verification link: the message the
    judge takes for the site's account check, and its one verification link
    on an allowed host (the judge picks when it holds several)."""
    message = _message(tab, inbox_url, site, jev=jev, ats=ats, company=company, want="link",
                       since=since)
    if not message:
        return None
    body = open_message(tab, message)
    links = verification_links(message_links(tab), allowed, refused)
    if len(links) <= 1:
        return links[0][1] if links else None
    state, questions = apply_judge.link_pick_questions(
        [(text, urlsplit(href).hostname or "") for text, href in links], body)
    picked = apply_judge.read_link_pick(jev.judge(state, questions), len(links))
    return links[picked][1] if picked is not None else None


def _polling(page, inbox_url: str, one_poll: Callable, *, polls: int, wait_s: float, clock,
             sleep, deadline: float | None, errors: list | None):
    """The poll loop `fetch_code` and `fetch_link` share (see
    `fetch_code`): `one_poll(tab)` is one read of the inbox in a tab of its
    own, guarded onto the inbox host; its first result that is not None."""
    parsed = urlsplit(inbox_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or polls <= 0:
        return None
    end = min(clock() + 180, deadline if deadline is not None else float("inf"))
    tab = None

    def _noted(e: Exception) -> None:
        if errors is not None:
            errors.append(type(e).__name__)
    try:
        if clock() >= end:
            return None
        tab = page.context.new_page()
        stopped: list[str] = []         # navigations the guard stopped

        def guard(route):
            request = route.request
            if request.is_navigation_request() and urlsplit(request.url).hostname != parsed.hostname:
                # the inbox itself leaving its host (a sign-in redirect), never a
                # frame of it on another host (a token renewal, review round 4, M3)
                if _main_frame(request):
                    stopped.append("off the inbox host")
                route.abort()
            else:
                route.continue_()

        tab.route("**/*", guard)
        last = ""
        for attempt in range(min(polls, 3)):
            if clock() >= end:
                break
            try:
                got = one_poll(tab)
            except Exception as e:  # noqa: BLE001  (browser and judge errors may include private mail)
                _noted(e)
                if stopped or type(e).__name__ == last:
                    break
                last, got = type(e).__name__, None
            if got is not None and clock() < end:
                return got
            if attempt + 1 < min(polls, 3):
                delay = min(max(0, wait_s), max(0, end - clock()))
                if delay:
                    sleep(delay)
    except Exception as e:  # noqa: BLE001  (browser and judge errors may include private mail)
        _noted(e)
        return None
    finally:
        if tab is not None:
            try:
                tab.close()
            except Exception:  # noqa: BLE001
                pass
    return None


def fetch_code(page, site: str, inbox_url: str, *, jev, polls: int = 3,
               wait_s: float = 60, clock=time.monotonic, sleep=time.sleep,
               deadline: float | None = None, ats: str = "", company: str = "",
               errors: list | None = None, since: datetime | None = None,
               used=frozenset()) -> str | None:
    """Judge message relevance and code candidates; never log mail or codes.

    `ats` (the queue entry's system) and `company` reach the from-site
    question: the mail comes from the ATS's domain, and `site` is only the
    form's host. Polls share a three-minute budget and the caller's remaining
    job budget. Navigation in the temporary tab stays on the configured inbox
    host. A poll that fails (a list read that timed out on a busy machine, a
    judge error) is that poll's error: the next poll runs after its wait,
    unless the same error came back (a provider outage) or the tab tried to
    leave the inbox host and the guard stopped it (a signed-out inbox's
    sign-in redirect): the polls end there (review round 3, M3). Each error
    is appended to `errors` by its type name alone (its message may quote the
    mail). ACC-07: a message whose time is before `since` (the job's start)
    is never read, and a code in `used` (by `code_hash`: the run typed it
    already) is never picked.
    """
    def one(tab):
        return _poll(tab, inbox_url, site, jev=jev, ats=ats, company=company, since=since,
                     used=used)
    return _polling(page, inbox_url, one, polls=polls, wait_s=wait_s, clock=clock, sleep=sleep,
                    deadline=deadline, errors=errors)


def fetch_link(page, site: str, inbox_url: str, *, jev, allowed: Callable[[str], bool],
               polls: int = 3, wait_s: float = 60, clock=time.monotonic, sleep=time.sleep,
               deadline: float | None = None, ats: str = "", company: str = "",
               errors: list | None = None, since: datetime | None = None,
               refused: list | None = None) -> str | None:
    """ACC-05: the verification link of the site's account check, polled
    as `fetch_code` polls: the message the judge takes for the site's and
    for an account check by link, then its one verification link whose
    host `allowed` takes (the application's own site, a known ATS). A
    verification link on any other host is never handed back and never
    opened: its host joins `refused`. The link is only read here; the
    caller opens it. Never logs mail or links."""
    def one(tab):
        return _poll_link(tab, inbox_url, site, jev=jev, ats=ats, company=company,
                          allowed=allowed, since=since, refused=refused)
    return _polling(page, inbox_url, one, polls=polls, wait_s=wait_s, clock=clock, sleep=sleep,
                    deadline=deadline, errors=errors)


