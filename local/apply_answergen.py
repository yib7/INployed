"""Free-text answers for the auto-apply runner, with a grounding gate.

A field the judge maps to `needs_generation` (an open-ended prompt the sheet
does not answer word for word) gets ONE draft from the flash-lite tier and
ONE Jev grounding request. The draft prompt carries the sheet excerpt, the
question, the field's character limit and the project's writing rules
(`aiwriting.RULES_PROMPT`); its instruction is select and rephrase, never
invent. The gate splits the draft into sentences and asks
`apply_judge.grounding_questions` whether every claim in each sentence is
supported by the sheet; a sentence below `apply_judge.GROUNDING_MIN` drops
the whole draft. The runner then leaves an optional field blank and flagged,
or parks a required one with the note.

Every draft is a Gemini call and every gate is a Jev call, so a field gets
one attempt and a job gets `apply_run.GENERATE_MAX` drafts.

`resume_tailor` is imported lazily inside `_llm_call` and `_rules_prompt`:
its `config.py` loads `.env` at import, so the runner must be importable
without it and tests inject `llm_call` or monkeypatch `resume_tailor.llm.call`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

import apply_judge

log = logging.getLogger("apply_answergen")

TIER = "flash_lite"
DEFAULT_CHAR_LIMIT = 1500       # when the field states no limit
MIN_CHAR_LIMIT = 40             # a stated limit below this is a parse error, not a budget
TEMPERATURE = 0.3

SYSTEM_PROMPT = (
    "You write one answer to a job application question on behalf of the candidate, in "
    "the first person.\n"
    "The only source of facts is the SHEET that follows the question. Select the facts "
    "that answer the question and rephrase them in plain sentences. Never add an "
    "employer, a project, a number, a date, a skill or a motive the sheet does not state. "
    "When the sheet holds nothing that answers the question, return an empty response.\n"
    "Plain text only: no markdown, no headings, no bullet points, no greeting, no sign-off, "
    "no preamble. Whole sentences, each one a claim the sheet supports. Stay within the "
    "character limit given with the question."
)

_LIMIT_RE = re.compile(r"(\d[\d,]{0,6})\s*(?:characters|chars)\b", re.I)
_MINIMUM_RE = re.compile(r"\b(?:min|minimum|at least)\s*$", re.I)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_LEAD_LABEL_RE = re.compile(r"^(?:answer|response|draft)\s*:\s*", re.I)
_BULLET_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+")
_EMPHASIS_RE = re.compile(r"(?<!\w)(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1(?!\w)")
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")
_BLANK_RUN_RE = re.compile(r"\n\s*\n(?:\s*\n)+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
# A piece ending in an abbreviation joins the next one: dotted initials (B.S.,
# M.S.), the Latin shorthands, and a short list of titles and company suffixes.
_ABBREVIATION_RE = re.compile(
    r"(?:^|\s)(?:[A-Za-z]\.){2,}$"
    r"|(?:^|\s)(?:e\.g|i\.e|etc|vs|Mr|Mrs|Ms|Dr|Prof|Inc|Ltd|Co|Corp|St|Jr|Sr)\.$", re.I)


@dataclass
class Attempt:
    """One field's generation attempt. `text` is the accepted answer, else None."""
    text: str | None
    ok: bool
    note: str
    weakest: float | None = None
    sentences: int = 0


# --- the draft --------------------------------------------------------------------------

def _llm_call(system: str, user: str, tier: str, **kwargs: Any) -> Any:
    from resume_tailor import llm
    return llm.call(system, user, tier, **kwargs)


def _rules_prompt() -> str:
    from resume_tailor import aiwriting
    return aiwriting.RULES_PROMPT


def system_prompt(rules_prompt: str | None = None) -> str:
    """The system prompt: the instruction block, then the writing rules."""
    rules = _rules_prompt() if rules_prompt is None else rules_prompt
    return SYSTEM_PROMPT + "\n\n" + rules


def user_prompt(question: str, sheet_excerpt: str, char_limit: int) -> str:
    """The user prompt: the question, the limit and the sheet."""
    return (f"QUESTION: {question}\n"
            f"CHARACTER LIMIT: {int(char_limit)}\n\n"
            "SHEET (the only source of facts):\n"
            f"{sheet_excerpt}")


def build_prompt(question: str, sheet_excerpt: str, char_limit: int, *,
                 rules_prompt: str | None = None) -> tuple[str, str]:
    """(system, user) for the draft call."""
    return system_prompt(rules_prompt), user_prompt(question, sheet_excerpt, char_limit)


def draft(question: str, sheet_excerpt: str, char_limit: int, *,
          llm_call: Callable[..., Any] | None = None,
          rules_prompt: str | None = None) -> str:
    """One flash-lite call; the reply stripped to plain text and cut to
    `char_limit` (0 means no limit). Empty when the model returned nothing usable.

    The two prompt builders are named at the call site so the prompt census in
    `tests/test_prompt_hygiene.py` can trace every literal that reaches the model."""
    call = llm_call if llm_call is not None else _llm_call
    raw = call(system_prompt(rules_prompt), user_prompt(question, sheet_excerpt, char_limit),
               TIER, json_out=False, temperature=TEMPERATURE)
    if not isinstance(raw, str):
        return ""
    return _fit(plain_text(raw), int(char_limit))


def plain_text(raw: str) -> str:
    """Markdown fences, emphasis, headings, bullets and a leading "Answer:"
    label removed; runs of blank lines folded to one; surrounding quotes dropped."""
    text = _FENCE_RE.sub("", raw.strip())
    text = _HEADING_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _EMPHASIS_RE.sub(r"\2", text)
    text = _LEAD_LABEL_RE.sub("", text.strip())
    text = _BLANK_RUN_RE.sub("\n\n", text).strip()
    if text[:1] in ("{", "["):
        text = _json_string(text)
    if len(text) >= 2 and text[0] in _OPEN_QUOTES and text[-1] in _CLOSE_QUOTES:
        text = text[1:-1].strip()
    return text


_OPEN_QUOTES = "\"'“‘"
_CLOSE_QUOTES = "\"'”’"


def _json_string(text: str) -> str:
    """The first string value inside a JSON reply, else ""."""
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    queue = [data]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, str):
            return cur.strip()
        if isinstance(cur, dict):
            queue.extend(cur.values())
        elif isinstance(cur, list):
            queue.extend(cur)
    return ""


def _fit(text: str, char_limit: int) -> str:
    """`text` within `char_limit`: cut at the last sentence end that fits, else
    the last space, else the limit itself."""
    if char_limit <= 0 or len(text) <= char_limit:
        return text
    head = text[:char_limit + 1]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", head) if m.end() <= char_limit]
    if ends:
        return text[:ends[-1]].rstrip()
    cut = head.rfind(" ")
    return (text[:cut] if cut > 0 else text[:char_limit]).rstrip()


# --- the gate ---------------------------------------------------------------------------

def sentences(text: str) -> list[str]:
    """The draft as sentences: a newline always ends one, end punctuation
    followed by space ends one unless it closes an abbreviation (B.S., Inc.)."""
    out: list[str] = []
    for line in (text or "").splitlines():
        start = 0
        for m in _SENTENCE_END_RE.finditer(line):
            piece = line[start:m.start()]
            if _ABBREVIATION_RE.search(piece):
                continue
            if piece.strip():
                out.append(piece.strip())
            start = m.end()
        tail = line[start:].strip()
        if tail:
            out.append(tail)
    return out


def grounded(text: str, sheet_excerpt: str, jev: Any) -> tuple[bool, float]:
    """(every sentence of `text` grounds at or above `GROUNDING_MIN`, the
    weakest probability). One Jev request; an empty draft asks nothing."""
    parts = sentences(text)
    if not parts:
        return True, 1.0
    state, questions = apply_judge.grounding_questions(parts, sheet_excerpt)
    return apply_judge.read_grounding(jev.judge(state, questions), parts)


# --- the hook ---------------------------------------------------------------------------

def char_limit_for(field: Any) -> int:
    """The limit the field states in its help or placeholder ("Max 1500
    characters."), else `DEFAULT_CHAR_LIMIT`."""
    for text in (getattr(field, "help", ""), getattr(field, "placeholder", "")):
        text = text or ""
        for m in _LIMIT_RE.finditer(text):
            if _MINIMUM_RE.search(text[:m.start()]):
                continue          # "Minimum 100 characters" states a floor
            digits = m.group(1).replace(",", "")
            if digits.isdigit() and int(digits) >= MIN_CHAR_LIMIT:
                return int(digits)
    return DEFAULT_CHAR_LIMIT


def question_for(field: Any) -> str:
    """The field's label, with its help text in parentheses when there is one."""
    label = str(getattr(field, "label", "") or "").strip()
    help_text = str(getattr(field, "help", "") or "").strip()
    return f"{label} ({help_text})" if help_text else label


def attempt(field: Any, catalog: Any, jev: Any, *, budget: int,
            llm_call: Callable[..., Any] | None = None) -> Attempt:
    """One draft and one grounding gate for `field`. No budget, an empty draft,
    a failed call or a rejected draft all come back with `text` None and a
    note the runner writes into the record."""
    if budget <= 0:
        return Attempt(None, False, "generation budget exhausted")
    label = getattr(field, "label", "")
    sheet = catalog.sheet_excerpt()
    try:
        text = draft(question_for(field), sheet, char_limit_for(field), llm_call=llm_call)
    except Exception as e:      # noqa: BLE001  (a model failure parks or flags the field)
        log.warning("generation for %r failed: %s", label, type(e).__name__)
        return Attempt(None, False, f"draft failed: {type(e).__name__}")
    if not text:
        log.info("generation for %r: the model returned nothing", label)
        return Attempt(None, False, "empty draft")
    try:
        ok, weakest = grounded(text, sheet, jev)
    except Exception as e:      # noqa: BLE001  (a judge error can quote the sheet)
        log.warning("grounding for %r failed: %s", label, type(e).__name__)
        return Attempt(None, False, f"grounding failed: {type(e).__name__}")
    count = len(sentences(text))
    if not ok:
        log.info("generation for %r rejected: weakest of %d sentences grounded %.2f",
                 label, count, weakest)
        return Attempt(None, False,
                       f"draft rejected: weakest sentence grounded {weakest:.2f}, "
                       f"below {apply_judge.GROUNDING_MIN:.2f}", weakest, count)
    log.info("generation for %r accepted: %d sentences, weakest %.2f, %d chars",
             label, count, weakest, len(text))
    return Attempt(text, True, f"generated ({count} sentences, weakest {weakest:.2f})",
                   weakest, count)


def answer(field: Any, catalog: Any, jev: Any, *, budget: int,
           llm_call: Callable[..., Any] | None = None) -> str | None:
    """The runner's hook: the accepted draft, else None."""
    return attempt(field, catalog, jev, budget=budget, llm_call=llm_call).text


class Generator:
    """The hook object `apply_run.Runner(answergen=...)` takes. `last` keeps
    the most recent `Attempt` so the runner can write its note."""

    def __init__(self, llm_call: Callable[..., Any] | None = None):
        self.llm_call = llm_call
        self.last: Attempt | None = None

    def answer(self, field: Any, catalog: Any, judge: Any, *, budget: int) -> str | None:
        self.last = attempt(field, catalog, judge, budget=budget, llm_call=self.llm_call)
        return self.last.text
