"""Security-code / email-verification gate handling for Playwright-driven applies.

Some ATS (Greenhouse, etc.) email an N-character security code *after* the
candidate clicks Submit and require it before the application is accepted. The
browser driver (a Playwright process) can't read the candidate's inbox — the
orchestrator can, via the Outlook/Gmail MCP. They cooperate through a tiny file
handshake in a per-run directory:

    code_request.json   <- driver:       {"needed", "meta", "requested_at"}
    code_response.json  <- orchestrator:  {"code"}

Flow: the driver detects the gate (`detect_code_gate`), writes a request
(`request_code`), and blocks on `await_code`. The orchestrator watches for
code_request.json, fetches the newest verification email for the run's signup
address, runs `extract_code` on the body, and hands it back with `write_code`.
The driver then `fill_code`s it.

Policy: this module FILLS the code but never clicks the final resubmit/submit —
the caller parks at the button (park-at-review invariant). Auto-resubmit is a
separate, explicit decision for the human to make.
"""
import json
import re
import time
from pathlib import Path

REQUEST_FILE = "code_request.json"
RESPONSE_FILE = "code_response.json"

# Text that signals a verification-code screen is showing (checked in page body).
_CODE_FIELD_HINTS = (
    "security code", "verification code", "confirmation code", "enter the code",
    "one-time", "one time code", "verify your email", "6-digit", "8-character",
)

# All-caps tokens that look code-shaped but are ordinary words — never a code.
_NOT_A_CODE = {
    "HELLO", "GREENHOUSE", "APPLICATION", "SECURITY", "AFTER", "ENTER", "FIELD",
    "YOUR", "THIS", "CODE", "COPY", "PASTE", "INTO", "EMAIL", "SUBMIT", "RESUBMIT",
    "BELOW", "ABOVE", "PLEASE",
}


def extract_code(text, length=None):
    """Extract a security code from an email body.

    Best signal first: a token alone on its own line, then a token right after
    the word 'code', then any standalone ``[A-Z0-9]`` token. Filters out common
    all-caps words and (when ``length`` is given) wrong-length tokens. Returns
    the code string, or None.
    """
    if not text:
        return None
    t = text.replace("\r", "\n")
    # Codes can be mixed-case (e.g. "tffCw7Xp") — match [A-Za-z0-9], not just caps.
    own_line = re.findall(r"(?m)^\s*([A-Za-z0-9]{4,12})\s*$", t)   # alone on a line
    after = re.findall(r"code\W{0,15}?\b([A-Za-z0-9]{4,12})\b", t, re.I)  # after 'code'
    anywhere = re.findall(r"\b([A-Za-z0-9]{4,12})\b", t)          # anywhere

    def _ok(c):
        if length is not None and len(c) != length:
            return False
        if c.isalpha() and c.upper() in _NOT_A_CODE:
            return False
        return True

    # Strongest signal first (own line > after 'code' > anywhere); within each,
    # prefer a token containing a digit — real codes almost always do — before
    # falling back to an all-letter token.
    for group in (own_line, after, anywhere):
        for c in group:
            if _ok(c) and any(ch.isdigit() for ch in c):
                return c
    for group in (own_line, after):
        for c in group:
            if _ok(c):
                return c
    return None


def detect_code_gate(page):
    """Return a Playwright locator for the code input if a code gate is showing, else None."""
    for sel in (
        "#security_code",
        "input[autocomplete='one-time-code']",
        "input[name*='security' i]",
        "input[name*='verification' i]",
        "input[name*='confirmation' i]",
        "input[id*='security' i]",
    ):
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                return loc.first
        except Exception:  # noqa: BLE001
            pass
    try:
        body = page.inner_text("body").lower()
    except Exception:  # noqa: BLE001
        body = ""
    if any(h in body for h in _CODE_FIELD_HINTS):
        try:
            loc = page.locator("input[type='text'], input:not([type])")
            if loc.count() and loc.first.is_visible():
                return loc.first
        except Exception:  # noqa: BLE001
            pass
    return None


def request_code(run_dir, meta):
    """Driver side: signal that a code is needed. Clears any stale response first."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    resp = run_dir / RESPONSE_FILE
    if resp.exists():
        resp.unlink()
    req = run_dir / REQUEST_FILE
    req.write_text(
        json.dumps({"needed": True, "meta": meta, "requested_at": time.time()}),
        encoding="utf-8",
    )
    return req


def await_code(run_dir, timeout=300.0, poll=2.0):
    """Driver side: block until the orchestrator writes a code, or timeout. Returns code or None."""
    resp = Path(run_dir) / RESPONSE_FILE
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if resp.exists():
            try:
                code = str(json.loads(resp.read_text(encoding="utf-8")).get("code", "")).strip()
            except Exception:  # noqa: BLE001
                code = ""
            if code:
                return code
        time.sleep(poll)
    return None


def write_code(run_dir, code):
    """Orchestrator side: hand a fetched code back to the waiting driver."""
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / RESPONSE_FILE).write_text(
        json.dumps({"code": str(code).strip()}), encoding="utf-8"
    )


def fill_code(page, code_input, code):
    """Enter the code into the located field. Handles a single input AND
    per-character OTP widgets (Greenhouse's ``security-input-N`` boxes): click
    the first box, then type char-by-char so the widget auto-advances. Falls
    back to a plain fill. Does NOT submit.
    """
    code = str(code).strip()
    try:
        code_input.click()
    except Exception:  # noqa: BLE001
        pass
    try:
        page.keyboard.type(code, delay=60)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        code_input.fill(code)
    except Exception:  # noqa: BLE001
        pass
