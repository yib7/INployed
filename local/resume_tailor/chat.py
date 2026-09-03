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
  turn, so the JD excerpt, the apply.md excerpt, the master-file fallback and the
  transcript are each capped by a named constant below. Those five numbers are
  the cost ceiling for a chat session; nothing else here grows.

No style gates run on an answer. This is conversation, not résumé copy, so
`compose.enforce_style`, `aiwriting` and the grounding gate all stay out of it —
the grounding rule is carried by the system prompt instead.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import assets, compose, config
from .llm import call

log = logging.getLogger(__name__)

# ── the cost ceiling ─────────────────────────────────────────────────────────
# Chosen against what one turn actually costs: the system prompt is re-sent every
# turn, so (JD + sheet + master) is the per-turn floor. ~16k characters of context
# is roughly 4k tokens — a few tenths of a cent per turn on the flash tier, and
# small enough that a long session cannot quietly run away.
JD_CHAR_CAP = 4000          # same excerpt the cover letter reasons from
APPLY_MD_CHAR_CAP = 12000   # a real tailored apply.md runs ~4-8k; this is headroom, not a squeeze
MASTER_CHAR_CAP = 4000      # only ever used INSTEAD of the sheet, never alongside it
HISTORY_TURN_CAP = 8        # the last 8 exchanges: enough to follow a thread
HISTORY_CHAR_CAP = 6000     # ...and a hard character ceiling under that, for long answers

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
    "paragraph, a bullet), build it only out of facts already in the context, and "
    "say which part you had to leave blank.\n"
    "5. You have no tools and no file access: you cannot open, fetch, or write "
    "anything. Answer in the conversation only.\n"
    "6. Keep it short and plain. No preamble, no restating the question."
)


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
            text = val.strip()
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


def _atom_line(atom: Dict[str, Any]) -> str:
    """One achievement atom flattened to a single readable line."""
    parts: List[str] = []
    for key in ("what", "how", "scope"):
        val = str(atom.get(key) or "").strip()
        if val:
            parts.append(val)
    impact = atom.get("impact")
    if isinstance(impact, (list, tuple)):
        parts += [str(i).strip() for i in impact if str(i or "").strip()]
    elif str(impact or "").strip():
        parts.append(str(impact).strip())
    return "; ".join(parts)


def _entries(master: Dict[str, Any], section: str, *name_keys: str) -> List[str]:
    lines: List[str] = []
    for entry in master.get(section) or []:
        if not isinstance(entry, dict):
            continue
        head = ", ".join(
            s for s in (str(entry.get(k) or "").strip() for k in name_keys) if s)
        dates = str(entry.get("dates") or "").strip()
        if dates:
            head = f"{head} ({dates})" if head else dates
        if head:
            lines.append(f"- {head}")
        for atom in entry.get("achievements") or []:
            if isinstance(atom, dict):
                text = _atom_line(atom)
                if text:
                    lines.append(f"    - {text}")
    return lines


def _master_summary() -> str:
    """A bounded plain-text digest of master_experience.yaml.

    Used ONLY when the job has no apply sheet — an untailored job should still be
    askable from the JD alone, and without this the model would know nothing about
    the candidate. Never sent alongside the sheet: the sheet already carries the
    same facts, so shipping both would double the billed payload every turn.
    Any failure to read the master degrades to "" rather than killing the chat.
    """
    try:
        master = assets.load_master()
    except Exception as exc:  # noqa: BLE001 - a missing/broken master must not kill the chat
        log.warning("chat: master file unavailable for the untailored fallback (%s)", exc)
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
    for section, keys in (("experience", ("org", "title")),
                          ("projects", ("name",)),
                          ("leadership", ("org", "role", "title"))):
        entries = _entries(master, section, *keys)
        if entries:
            lines.append(f"{section.upper()}:")
            lines += entries
    skills = master.get("skills")
    if isinstance(skills, dict) and skills:
        lines.append("SKILLS:")
        for group, items in skills.items():
            if isinstance(items, (list, tuple)) and items:
                lines.append(f"- {group}: {', '.join(str(i) for i in items)}")
            elif str(items or "").strip():
                lines.append(f"- {group}: {items}")
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
        blocks += ["No job description was captured for this posting; say so "
                   "rather than guessing what the role involves.", ""]

    sheet = _read_sheet(folder)
    if sheet:
        blocks += [
            "APPLY SHEET (this job's own apply.md: the candidate's basics and "
            "address, education, the tailored résumé bullets, the standard "
            "application answers, and the cover-letter text):",
            SHEET_BEGIN,
            _cap(sheet, APPLY_MD_CHAR_CAP),
            SHEET_END,
        ]
    else:
        blocks += [
            "This job has not been tailored yet, so there is no apply sheet and "
            "no cover letter for it. The candidate's master experience file is "
            "the only record of their background:",
            MASTER_BEGIN,
            _master_summary(),
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
        log.info("chat: no tailored folder for this job (%s) — JD-only context", exc)
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


def ask(context: str, history: List[Tuple[str, str]], question: str) -> str:
    """One chat turn: `context` as the system prompt, the turns as the user message.

    The context is the cacheable half and must stay byte-identical across a
    session, so nothing volatile may be folded into it — the transcript and the
    new question go in `user`. Returns the model's answer verbatim (stripped);
    no style gate, no grounding gate, no repair pass runs on it.
    """
    parts = _transcript(history)
    if parts:
        parts = ["CONVERSATION SO FAR:", *parts, ""]
    parts.append(f"QUESTION: {question}")
    out = call(system=context, user="\n\n".join(parts), tier=config.TIER_FLASH)
    return out.strip() if isinstance(out, str) else ""
