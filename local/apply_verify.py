"""Security codes from verification mail, and typing one into a form.

`extract_code` reads the code out of a message body; `apply_inbox` uses it
to list a message's code-shaped tokens for the judge to pick from.
`fill_code` types a code into the form's code field, into a single input or
into a row of one-character boxes, and never submits: the caller decides
when the form goes.
"""
import re

# All-caps tokens that look code-shaped but are ordinary words, never a code.
_NOT_A_CODE = {
    "HELLO", "GREENHOUSE", "APPLICATION", "SECURITY", "AFTER", "ENTER", "FIELD",
    "YOUR", "THIS", "CODE", "COPY", "PASTE", "INTO", "EMAIL", "SUBMIT", "RESUBMIT",
    "BELOW", "ABOVE", "PLEASE",
}


def extract_code(text, length=None):
    """Extract a security code from an email body.

    Best signal first: a token alone on its own line, then a token near the word
    'code' (a couple of filler words like "is"/"the" tolerated between them),
    then, ONLY when a ``length`` hint constrains it, any standalone token of
    that length. The bare-``anywhere`` fallback is deliberately gated on a length
    hint: without one, an inline order number / ZIP / confirmation ref
    ([A-Za-z0-9]{4,12} tokens the footer is full of) would be mistaken for a
    code and stall the form. Filters common all-caps words, bare years, and
    (when given) wrong-length tokens. Returns the code string, or None.
    """
    if not text:
        return None
    t = text.replace("\r", "\n")
    # Codes can be mixed-case (e.g. "mkPz3Qra"): match [A-Za-z0-9], not just caps.
    own_line = re.findall(r"(?m)^\s*([A-Za-z0-9]{4,12})\s*$", t)   # alone on a line
    # Near 'code': allow a short run of filler ("is", "the", "your", ":") between
    # the keyword and the token, not only non-word chars. A lookahead, so every
    # 'code' starts a match of its own: "Your code / Your code is MKPZ3QRA"
    # would otherwise be one match ending on the second 'code'.
    after = re.findall(
        r"(?=code(?:\W+(?:is|the|your|to|be|of|below)\b)*\W{0,4}([A-Za-z0-9]{4,12})\b)",
        t, re.I)
    anywhere = re.findall(r"\b([A-Za-z0-9]{4,12})\b", t) if length is not None else []

    def _looks_like_year(c):
        # A bare 4-digit 19xx/20xx token is a copyright/date, not a code.
        return len(c) == 4 and c.isdigit() and c[:2] in ("19", "20")

    def _ok(c):
        if length is not None and len(c) != length:
            return False
        if c.isalpha() and c.upper() in _NOT_A_CODE:
            return False
        if length is None and _looks_like_year(c):
            return False
        return True

    # Strongest signal first (own line > near 'code' > length-gated anywhere);
    # within each, prefer a token containing a digit (real codes almost always
    # do) before falling back to an all-letter token.
    for group in (own_line, after, anywhere):
        for c in group:
            if _ok(c) and any(ch.isdigit() for ch in c):
                return c
    for group in (own_line, after):
        for c in group:
            if _ok(c):
                return c
    return None


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
