"""Read verification mail in a separate tab of the signed-in browser profile."""
from __future__ import annotations

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
TIMEOUT_MS = 5_000


@dataclass
class Message:
    n: int
    sender: str
    subject: str
    preview: str
    open_locator: str


def list_messages(page, inbox_url: str, limit: int = 15) -> list[Message]:
    """Read the newest visible rows from the provider's inbox order."""
    page.goto(inbox_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    for selectors in SELECTORS.values():
        rows = page.locator(selectors["row"])
        messages = []
        for i in range(rows.count()):
            if len(messages) >= max(0, limit):
                break
            row = rows.nth(i)
            if not row.is_visible():
                continue

            def text(key):
                loc = row.locator(selectors[key]).first
                if not loc.count():
                    return ""
                return (loc.get_attribute("email") if key == "sender" else None) or loc.inner_text()

            messages.append(Message(len(messages), text("sender"), text("subject"),
                                    text("preview") or row.inner_text(),
                                    f'{selectors["row"]} >> nth={i}'))
        if messages:
            return messages
    return []


def open_message(page, msg: Message) -> str:
    """Open a row and read its rendered message body."""
    page.locator(msg.open_locator).click(timeout=TIMEOUT_MS)
    bodies = page.locator(", ".join(s["body"] for s in SELECTORS.values())).filter(visible=True)
    bodies.first.wait_for(state="visible", timeout=TIMEOUT_MS)
    return "\n".join(bodies.all_inner_texts())


def _candidates(body: str) -> list[str]:
    candidates = []
    remaining = body
    for _ in range(15):
        code = apply_verify.extract_code(remaining)
        if not code or code in candidates:
            break
        candidates.append(code)
        remaining = remaining.replace(code, "")
    return candidates


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
                    candidates = _candidates(body)
                    if candidates:
                        state, questions = apply_judge.code_pick_questions(candidates, body)
                        code = apply_judge.read_code_pick(jev.judge(state, questions))
                        if code in candidates and clock() < end:
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
