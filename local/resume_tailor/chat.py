"""Per-job "Ask AI" chat — everything the dashboard knows about one job, in one prompt.

The apply folder already holds the whole answer to "how should I word the 'why
this role' box?": the tailored bullets, the cover letter, the standard answers
and the JD all sit together. This module turns that folder into ONE stable
system-prompt payload (`build_context`) and sends only the volatile turns as the
user message (`ask`). That split is the prompt-cache contract `claude_cli.py`
documents, and it is why the chat honours `config.tailor_provider()` with no new
setting: it goes through `llm.call`, so whichever provider is configured answers.

Toolkit-agnostic on purpose — no Qt here. `qt/chat_dialog.py` is the view.

Two things bound this module:

* **The JD is untrusted.** It is arbitrary internet content, so it rides inside
  `compose.fence_jd` exactly as it does in every tailor prompt: explicit markers
  plus an ignore-instructions directive. A crafted posting is data, never
  instructions.
* **Every turn re-sends the whole payload.** That is deliberate (it is what makes
  the prompt cacheable), but the Gemini lane bills the full system prompt each
  turn, so the JD excerpt, the apply.md excerpt, the master-file digest and the
  transcript are each capped by a named constant below. Those five numbers are
  the cost ceiling for a chat session; nothing else here grows.

An answer gets one AI-writing pass. `compose._strip_em_dashes` runs on every
answer, and an answer of `PROSE_WORD_FLOOR` words or more is also checked
against `compose.style_violations` and `aiwriting.violations`, the same two
deterministic checks the résumé and letter arms use; a flash repair call runs
once and is kept only when it strictly lowers the count of findings. See
`_prose_gate`.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import aiwriting, assets, compose, config
from .llm import call

log = logging.getLogger(__name__)

# ── the cost ceiling ─────────────────────────────────────────────────────────
# Chosen against what one turn actually costs: the system prompt is re-sent every
# turn, and the master digest rides alongside the sheet on every turn now, so
# (JD + sheet + master) is the per-turn floor: 4,000 + 12,000 + 80,000 = 96,000
# characters at most, roughly 24k tokens. That runs a few cents per turn on the
# flash tier, small enough that a long session cannot quietly run away. The
# master cap is a ceiling a real master stays under, not a squeeze: at 30,000 a
# 47,000-character digest lost its last projects, leadership and skills.
JD_CHAR_CAP = 4000          # same excerpt the cover letter reasons from
APPLY_MD_CHAR_CAP = 12000   # a real tailored apply.md runs ~4-8k; this is headroom, not a squeeze
MASTER_CHAR_CAP = 80_000    # the full digest, sent alongside the sheet on every turn
HISTORY_TURN_CAP = 8        # the last 8 exchanges: enough to follow a thread
HISTORY_CHAR_CAP = 6000     # ...and a hard character ceiling under that, for long answers

# An answer this short or shorter skips the AI-writing gate: the deterministic
# checks are tuned for a paragraph, and a one-line "the sheet doesn't say" reply
# does not need a second call spent proving it is clean.
PROSE_WORD_FLOOR = 60

TRUNCATED_MARKER = "[... truncated ...]"

JD_PURPOSE = "background on the role being applied to"

SHEET_BEGIN = "=== BEGIN APPLY SHEET ==="
SHEET_END = "=== END APPLY SHEET ==="
MASTER_BEGIN = "=== BEGIN CANDIDATE BACKGROUND ==="
MASTER_END = "=== END CANDIDATE BACKGROUND ==="

APPLY_SHEET = "apply.md"

SYSTEM_RULES = (
    "You are helping ONE person with ONE specific job application. Everything you "
    "know about them and about this job is in the CONTEXT below; treat it as the "
    "whole world.\n"
    "RULES:\n"
    "1. Answer only from the context. It is the complete record; general knowledge "
    "about the company, the role, or the person is not.\n"
    "2. When the context does not hold the answer, say so plainly in one line and "
    "stop. \"The apply sheet doesn't say\" is a good answer; a plausible guess is "
    "not.\n"
    "3. Never invent an experience, a number, a date, an employer, a school, or a "
    "skill. Every claim you make about the candidate must be traceable to something "
    "written in the context.\n"
    "4. When asked to draft text (an answer to an application question, a "
    "paragraph, a bullet), build it only out of facts already in the context, "
    "write it in plain, specific, human prose, and say which part you had to "
    "leave blank.\n"
    "5. You have no tools and no file access: you cannot open, fetch, or write "
    "anything. Answer in the conversation only.\n"
    "6. Keep it short and plain. No preamble, no restating the question."
) + "\n" + aiwriting.RULES_PROMPT


# ── helpers ──────────────────────────────────────────────────────────────────
def _cap(text: str, limit: int) -> str:
    """`text` bounded to `limit` characters, flagged when it was actually cut so
    the model knows it is reading an excerpt rather than the whole document."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n" + TRUNCATED_MARKER


def _first(job: Dict[str, Any], *keys: str) -> str:
    """The first non-blank string among `keys`.

    Two job shapes reach this module: the dashboard's row payload
    (company_name / job_title) and `apply.build_apply_context`'s marker-derived
    dict (company / title). Both must render.
    """
    for key in keys:
        val = job.get(key)
        if isinstance(val, str):
            # One line, bounded: these head the prompt as "Title  : x" /
            # "Company: x" with no fence, so a scraped newline would forge a
            # line of its own (the rule run._line_field applies to the tailor).
            text = re.sub(r"\s+", " ", val).strip()[:200]
            if text and text.lower() not in ("nan", "none"):
                return text
    return ""


def _read_sheet(folder: Optional[Path]) -> str:
    """The folder's apply.md, or "" when there is no folder / no sheet / no read."""
    if folder is None:
        return ""
    try:
        return (Path(folder) / APPLY_SHEET).read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return ""


# The atom and entry flattening lives in assets so the cover letter's background
# block and this digest are built by one copy; these aliases keep the short names
# the callers below use.
_atom_line = assets.atom_line
_entries = assets.entry_lines

# The master's own tailoring configuration: layout budgets and ATS spelling
# maps, none of it a fact about the candidate. A digest that answers "what
# has this person done" has no use for a block-rendering budget, and printing
# one would only burn characters a real fact could have used. Any key
# starting with "_" is skipped the same way, for a user's own scratch notes.
_TAILOR_CONFIG_KEYS = frozenset({"tailor", "skill_aliases", "skill_aliases_match_only",
                                 "project_layout"})

# The sections rendered by name below, in that order. A generic walk covers
# everything else the master holds. "letter" is listed here even though only
# its `seed` key is ever printed (via assets.letter_seed() below): any other
# key under `letter` is deliberately left out of the digest on purpose.
_KNOWN_SECTIONS = frozenset({"basics", "letter", "education", "experience",
                             "projects", "leadership", "skills"})

_ENTRY_SECTIONS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("experience", ("org", "title")),
    ("projects", ("name",)),
    ("leadership", ("org", "role", "title")),
)


def _scalar_text(value: Any) -> str:
    """`value` as printable text, or "" when there is nothing to print.

    Checks `value is None` explicitly. `value or ""` reads a real, printable
    scalar like `0`, `0.0` or `False` as blank and drops it, which is wrong
    for a field that legitimately holds one of those. Only `None` and an
    all-whitespace string count as blank here."""
    if value is None:
        return ""
    return str(value).strip()


def _entry_extra_lines(entry: Dict[str, Any], name_keys: Tuple[str, ...]) -> List[str]:
    """One `    - key: value` line per scalar or list field of `entry` that
    `entry_lines` does not already print: everything but `achievements`, the
    dates and the keys already folded into the header line. A dict-shaped
    field (a free-form notes block, say) is left for a future pass; guessing
    at its shape here would risk printing something the atoms never said."""
    skip = {"achievements", "dates", *name_keys}
    lines: List[str] = []
    for key, value in entry.items():
        if key in skip:
            continue
        if isinstance(value, dict):
            continue
        if isinstance(value, (list, tuple)):
            items = [i for i in (_scalar_text(v) for v in value) if i]
            if items:
                lines.append(f"    - {key}: {', '.join(items)}")
        else:
            text = _scalar_text(value)
            if text:
                lines.append(f"    - {key}: {text}")
    return lines


def _generic_lines(value: Any, indent: int = 0) -> List[str]:
    """`value` as plain-text lines for a top-level key the digest has no
    named section for: a dict's own keys become `key: value` lines, a list
    becomes `- item` lines, a nested dict is indented two spaces per level,
    and a bare scalar is printed inline. Blank values print nothing."""
    pad = "  " * indent
    lines: List[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(sub, dict):
                nested = _generic_lines(sub, indent + 1)
                if nested:
                    lines.append(f"{pad}{key}:")
                    lines += nested
            elif isinstance(sub, (list, tuple)):
                items = [i for i in (_scalar_text(v) for v in sub) if i]
                if items:
                    lines.append(f"{pad}{key}:")
                    lines += [f"{pad}  - {i}" for i in items]
            else:
                text = _scalar_text(sub)
                if text:
                    lines.append(f"{pad}{key}: {text}")
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                nested = _generic_lines(item, indent + 1)
                if nested:
                    lines.append(f"{pad}-")
                    lines += nested
            else:
                text = _scalar_text(item)
                if text:
                    lines.append(f"{pad}- {text}")
    else:
        text = _scalar_text(value)
        if text:
            lines.append(f"{pad}{text}")
    return lines


def master_digest() -> str:
    """A bounded plain-text digest of master_experience.yaml: the known
    sections first (basics, education, experience, projects, leadership,
    skills, the candidate's own voice sample), then every other top-level key
    the master holds, except the file's own tailoring configuration.

    Sent alongside the apply sheet on every turn, and always sent on its own
    when a job has no sheet yet: the sheet is the subset chosen for ONE job,
    and a follow-up question may need a fact that subset left out. Any
    failure to read the master degrades to "" so a broken file cannot kill
    the chat.
    """
    try:
        master = assets.load_master()
    except Exception as exc:  # noqa: BLE001 - a missing/broken master must not kill the chat
        log.warning("chat: master file unavailable for the digest (%s)", exc)
        return ""
    if not isinstance(master, dict):
        return ""

    lines: List[str] = []
    basics = master.get("basics")
    if isinstance(basics, dict) and basics:
        lines.append("BASICS: " + "; ".join(
            f"{k}: {v}" for k, v in basics.items() if str(v or "").strip()))
    education = _entries(master, "education", "degree", "school", "concentration")
    if education:
        lines.append("EDUCATION:")
        lines += education
    for section, keys in _ENTRY_SECTIONS:
        entries = master.get(section) or []
        if not isinstance(entries, list):
            continue
        section_lines: List[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            block = _entries({section: [entry]}, section, *keys)
            if not block:
                continue
            section_lines += block
            section_lines += _entry_extra_lines(entry, keys)
        if section_lines:
            lines.append(f"{section.upper()}:")
            lines += section_lines
    skills = master.get("skills")
    if isinstance(skills, dict) and skills:
        lines.append("SKILLS:")
        for group, items in skills.items():
            if isinstance(items, (list, tuple)) and items:
                lines.append(f"- {group}: {', '.join(str(i) for i in items)}")
            elif str(items or "").strip():
                lines.append(f"- {group}: {items}")
    seed = assets.letter_seed()
    if seed:
        lines.append(f"IN THE CANDIDATE'S OWN WORDS: {seed}")
    for key, value in master.items():
        if not isinstance(key, str):
            continue
        if key in _KNOWN_SECTIONS or key in _TAILOR_CONFIG_KEYS or key.startswith("_"):
            continue
        body = _generic_lines(value)
        if body:
            lines.append(f"{key.upper()}:")
            lines += body
    return _cap("\n".join(lines), MASTER_CHAR_CAP)


# ── the context ──────────────────────────────────────────────────────────────
def build_context(folder: Optional[Path], job: Dict[str, Any]) -> str:
    """The stable system-prompt payload for one job's chat.

    `folder` is the tailored output folder (from `apply.resolve_generated_dir`)
    or None when the job was never tailored. The result is deterministic for a
    given folder + job, which is what makes it worth caching across turns — do
    not fold anything turn-dependent in here.
    """
    job = job or {}
    title = _first(job, "job_title", "title") or "Role"
    company = _first(job, "company_name", "company") or "?"
    url = _first(job, "url", "apply_url")
    jd = _job_description(job)

    blocks = [SYSTEM_RULES, "", "CONTEXT", "",
              "THIS JOB", f"Title  : {title}", f"Company: {company}",
              f"URL    : {url or '(none recorded)'}", ""]

    if jd:
        blocks += [compose.fence_jd(jd, JD_CHAR_CAP, JD_PURPOSE), ""]
    else:
        blocks += ["No job description was captured for this posting; say so and "
                   "never guess what the role involves.", ""]

    sheet = _read_sheet(folder)
    digest = master_digest()
    if sheet:
        blocks += [
            "APPLY SHEET (this job's own apply.md: the candidate's basics and "
            "address, education, the tailored résumé bullets, the standard "
            "application answers, and the cover-letter text):",
            SHEET_BEGIN,
            _cap(sheet, APPLY_MD_CHAR_CAP),
            SHEET_END,
            "CANDIDATE MASTER RECORD (the full experience file the résumé was "
            "tailored from; the apply sheet above is the subset chosen for "
            "this job):",
            MASTER_BEGIN,
            digest,
            MASTER_END,
        ]
    else:
        blocks += [
            "This job has not been tailored yet, so there is no apply sheet and "
            "no cover letter for it. The candidate's master experience file is "
            "the only record of their background:",
            MASTER_BEGIN,
            digest,
            MASTER_END,
        ]
    return "\n".join(blocks)


def _job_description(job: Dict[str, Any]) -> str:
    """The richest JD text on the job row (the same order run.py tailors against)."""
    from .run import _job_description_text  # local import — avoids an import cycle

    try:
        return _job_description_text(job)
    except Exception:  # noqa: BLE001 - a malformed row must not sink the chat
        return ""


def context_for_job(job: Dict[str, Any]) -> str:
    """`build_context` with the folder resolved from the job.

    An unresolvable folder is NOT an error here: a job nobody has tailored yet
    still has a JD and a master file, so the chat degrades to that context rather
    than refusing to open. Does disk I/O (the resolver scans the output root) —
    call it from a worker thread, never the UI thread.
    """
    from . import apply as apply_mod  # local import — apply pulls in output/apply_data

    folder: Optional[Path] = None
    try:
        folder = apply_mod.resolve_generated_dir(
            job_id=_first(job or {}, "job_posting_id"), job=job)
    except (FileNotFoundError, ValueError, OSError) as exc:
        log.info("chat: no tailored folder for this job (%s): JD-only context", exc)
    return build_context(folder, job)


# ── the turn ─────────────────────────────────────────────────────────────────
def _transcript(history: Sequence[Tuple[str, str]]) -> List[str]:
    """The recent exchanges, newest-biased, under both caps.

    Two caps because either one alone leaks: eight one-word turns are free, but
    eight turns of a long drafted cover-letter paragraph are not. The newest
    exchange is trimmed rather than dropped — losing it would break every
    follow-up ("make that shorter"), which is most of what a chat is for.
    """
    recent = list(history or [])[-HISTORY_TURN_CAP:]
    kept: List[str] = []
    total = 0
    for question, answer in reversed(recent):
        turn = f"Q: {question}\nA: {answer}"
        if kept and total + len(turn) > HISTORY_CHAR_CAP:
            break
        turn = _cap(turn, HISTORY_CHAR_CAP)
        total += len(turn)
        kept.append(turn)
    kept.reverse()
    return kept


def _prose_gate(answer: str) -> str:
    """The post-answer AI-writing pass: an answer of `PROSE_WORD_FLOOR` words or
    more is checked against the same two deterministic scans the résumé and
    letter arms use, and a single flash repair call is bought when either
    fires. Committed only when the repair strictly lowers the count of
    findings (a repair that is no better, or blank, or itself fails, leaves
    the original answer standing), exactly like `compose.enforce_style` and
    `coverletter.enforce_body_style`. The em-dash strip runs again on a
    committed repair, since the repair call is free to introduce one of its
    own.

    A long answer that quotes a posting phrase carrying a banned word buys the
    one repair call same as a genuine violation, and that repair is free to
    reword the quote; an accepted cost, since the gate cannot tell a quoted
    phrase from the model's own writing.
    """
    if len(answer.split()) < PROSE_WORD_FLOOR:
        return answer
    names = compose.style_violations(answer) + aiwriting.violations(answer)
    if not names:
        return answer
    system = (
        "You repair a chat answer that slipped into banned AI-writing patterns. "
        "Return the SAME answer: same facts, same structure, roughly the same "
        "length, with every listed pattern removed. Add nothing.\n"
        + aiwriting.RULES_PROMPT
    )
    user = f"ANSWER (findings: {', '.join(names)}):\n{answer}"
    try:
        out = call(system=system, user=user, tier=config.TIER_FLASH, temperature=0.2)
        fixed = out.strip() if isinstance(out, str) else ""
    except Exception:  # noqa: BLE001 - repair is best-effort, like the letter gate
        fixed = ""
    if fixed:
        new_names = compose.style_violations(fixed) + aiwriting.violations(fixed)
        if len(new_names) < len(names):
            return compose._strip_em_dashes(fixed)
    return answer


def ask(context: str, history: List[Tuple[str, str]], question: str) -> str:
    """One chat turn: `context` as the system prompt, the turns as the user message.

    The context is the cacheable half and must stay byte-identical across a
    session, so nothing volatile may be folded into it — the transcript and the
    new question go in `user`. The answer is stripped of em dashes
    unconditionally and passed through `_prose_gate`, which decides on its own
    whether the answer is long enough to be worth the extra checks.
    """
    parts = _transcript(history)
    if parts:
        parts = ["CONVERSATION SO FAR:", *parts, ""]
    parts.append(f"QUESTION: {question}")
    out = call(system=context, user="\n\n".join(parts), tier=config.TIER_FLASH)
    answer = out.strip() if isinstance(out, str) else ""
    answer = compose._strip_em_dashes(answer)
    return _prose_gate(answer)
