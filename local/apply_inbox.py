"""Read verification mail in a separate tab of the signed-in browser profile."""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
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
        "body": '[role="document"], [aria-label="Message body"]',
    },
    "gmail": {
        "row": "tr.zA",
        "sender": ".yX [email], .yX",
        "subject": ".bog",
        "preview": ".y2",
        "body": ".a3s",
    },
}
PROVIDER_HOSTS = {
    "gmail": ("mail.google.com",),
    "outlook": ("outlook.office.com", "outlook.office365.com", "outlook.live.com",
                "outlook.com"),
}
TIMEOUT_MS = 5_000
SENDER_SCAN = 4              # elements checked for a sender address attribute
CANDIDATES_CAP = 15          # code-shaped tokens read out of one message
BODY_SELECTOR = ", ".join(s["body"] for s in SELECTORS.values())


@dataclass
class Message:
    n: int
    sender: str
    subject: str
    preview: str
    open_locator: str


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
    has drifted."""
    page.goto(inbox_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
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

            subject = text("subject")
            sender = text("sender")
            # a row with no subject and no sender is a foreign widget; a row
            # that names a sender is mail even when the subject hook drifted
            if not subject.strip() and not sender.strip():
                continue
            messages.append(Message(len(messages), sender, subject,
                                    text("preview") or row.inner_text(),
                                    f'{selectors["row"]} >> nth={i}'))
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


def fetch_code(page, site: str, inbox_url: str, *, jev, polls: int = 3,
               wait_s: float = 60, clock=time.monotonic, sleep=time.sleep,
               deadline: float | None = None) -> str | None:
    """Judge message relevance and code candidates; never log mail or codes.

    Polls share a three-minute budget and the caller's remaining job budget.
    Navigation in the temporary tab stays on the configured inbox host.
    """
    parsed = urlsplit(inbox_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or polls <= 0:
        return None
    end = min(clock() + 180, deadline if deadline is not None else float("inf"))
    tab = None
    try:
        if clock() >= end:
            return None
        tab = page.context.new_page()

        def guard(route):
            request = route.request
            if request.is_navigation_request() and urlsplit(request.url).hostname != parsed.hostname:
                route.abort()
            else:
                route.continue_()

        tab.route("**/*", guard)
        for attempt in range(min(polls, 3)):
            if clock() >= end:
                break
            messages = list_messages(tab, inbox_url)
            rows = [asdict(message) for message in messages]
            if rows:
                state, questions = apply_judge.inbox_questions(rows, site)
                chosen = apply_judge.read_inbox(jev.judge(state, questions), rows)
                message = next((m for m in messages if m.n == chosen), None)
                if message:
                    body = open_message(tab, message)
                    picks = candidates(body, message.subject)
                    if picks:
                        state, questions = apply_judge.code_pick_questions(picks, body)
                        code = apply_judge.read_code_pick(jev.judge(state, questions))
                        if code in picks and clock() < end:
                            return code
            if attempt + 1 < min(polls, 3):
                delay = min(max(0, wait_s), max(0, end - clock()))
                if delay:
                    sleep(delay)
    except Exception:  # noqa: BLE001  (browser and judge errors may include private mail)
        return None
    finally:
        if tab is not None:
            try:
                tab.close()
            except Exception:  # noqa: BLE001
                pass
    return None
