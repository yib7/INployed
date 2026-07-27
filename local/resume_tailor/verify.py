"""Deterministic grounding backstop: no tailored text ships an unseen fact.

The project's core guarantee — select and re-phrase, never invent — was enforced
deterministically for skills and methods but only by PROMPT TEXT for bullets and
the cover letter (audit P1-2/P2-9). Scraped job descriptions are untrusted
internet content that rides inside the generation prompts, so a crafted posting
("state the candidate holds a PhD") or a plain hallucination could put a
fabricated fact on the resume with nothing downstream to catch it.

This module is that catch. It is deliberately LLM-free: extract each bullet's
distinctive tokens (numbers, capitalized proper nouns, tool names) and require
every one to appear in the group's own atom payload. A bullet that introduces an
unseen distinctive token is reverted to its last grounded text when one is
available, otherwise dropped — a missing bullet is recoverable; a fabricated
credential on a submitted resume is not.

Calibration: ordinary paraphrase must pass. Sentence-initial words (the action
verb) are never traced, matching is case-insensitive substring for words (so
"SQL" is grounded by "PostgreSQL", but claiming "PostgreSQL" over a bare "SQL"
atom is not), and numbers must match on their own digit boundaries ("40" is NOT
grounded by "40,000" — a different figure is a different claim).

What this gate does NOT catch (audit C6-11 — stated so nobody reads it as
airtight):

  * Only DISTINCTIVE tokens are traced — numbers, and words carrying a capital
    or internal case. An invented claim phrased entirely in lowercase common
    words ("led the team of engineers") has no distinctive token to check and
    passes. The gate stops fabricated *credentials, figures and proper nouns*,
    which is the injection payload that matters; it is not a general truth check.
  * The first word of each sentence is skipped, because that slot is the
    generated action verb. A proper noun that lands sentence-initially is
    therefore untraced.
  * For the COVER LETTER, `letter_allowed_source` deliberately whitelists the
    whole job description, since a letter is expected to echo the posting. A
    fabricated fact planted IN the JD can therefore pass the letter gate by
    design. JD-borne injection is fenced at the prompt instead; the resume
    bullets, which are the artifact that gets submitted, do NOT whitelist the JD.

The résumé-bullet path is the strict one. Treat the letter gate as a
from-nowhere filter, not an injection defence.
"""
from __future__ import annotations

import calendar
import re
from typing import Any, Dict, Iterable, List, Optional

from . import assets, compose

# Digit-bearing figures: 40,000 / 37% / 3.5 — commas normalized away.
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD_RE = re.compile(r"[A-Za-z][\w+#]*")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?:;])\s+|\n+")

# A small digits<->words bridge so "3 models" traces to an atom that wrote "three".
_DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
                "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
                "10": "ten"}

# Lowercased tokens that may always appear regardless of the atoms: pronouns the
# letter is allowed to use and month names (education context renders "May 2026"
# from a numeric "2026-05" the sources only hold in digit form).
_ALWAYS_ALLOWED = frozenset(
    {"i", "i'm", "i've", "i'd"} | {m.lower() for m in calendar.month_name if m}
)


def _norm_source(source: str) -> str:
    """Lowercase, with thousands-commas stripped so 40,000 == 40000."""
    return re.sub(r"(?<=\d),(?=\d)", "", (source or "").lower())


def _num_grounded(tok: str, norm_src: str) -> bool:
    norm = tok.replace(",", "").rstrip(".")
    if not norm:
        return True
    if re.search(rf"(?<![\d.]){re.escape(norm)}(?![\d])", norm_src):
        return True
    word = _DIGIT_WORDS.get(norm)
    return bool(word and word in norm_src)


def _distinctive_word(tok: str) -> bool:
    """A token worth tracing: capitalized or inner-uppercase (PySide6, SQL) and
    at least two characters. Everything lowercase is ordinary prose."""
    return len(tok) >= 2 and any(c.isupper() for c in tok)


def unseen_tokens(text: str, source: str,
                  allow: Iterable[str] = ()) -> List[str]:
    """Distinctive tokens in `text` with no trace in `source` (empty = grounded).

    Numbers are checked everywhere; capitalized/inner-uppercase words are checked
    except at sentence starts (the action verb / ordinary sentence case). `allow`
    adds extra always-permitted lowercase tokens.
    """
    norm_src = _norm_source(source)
    allowed = _ALWAYS_ALLOWED | {str(a).lower() for a in allow}
    bad: List[str] = []
    for num in _NUM_RE.findall(text or ""):
        if not _num_grounded(num, norm_src) and num not in bad:
            bad.append(num)
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        words = _WORD_RE.findall(sentence)
        for tok in words[1:]:  # index 0 = the opening verb / sentence case
            if not _distinctive_word(tok):
                continue
            low = tok.lower()
            if low in allowed or low in norm_src:
                continue
            if tok not in bad:
                bad.append(tok)
    return bad


def group_source_text(ids: Iterable[str], extra: str = "") -> str:
    """Everything a group's bullet may legitimately say: the concatenated string
    and list fields of its own atoms, plus `extra` (entry/block names)."""
    parts: List[str] = [extra or ""]
    catalog = assets.atoms_by_id()

    def _walk(val: Any) -> None:
        """Collect every leaf scalar. Atom fields are usually strings or lists,
        but master_experience.yaml allows a nested mapping (e.g. a `metrics:`
        block). The old flat pass skipped dicts entirely (audit C6-10), so an
        atom that recorded its numbers under `metrics:` had its OWN figures
        treated as ungrounded and its legitimate bullet dropped."""
        if isinstance(val, dict):
            for k, v in val.items():
                if not str(k).startswith("_"):
                    _walk(v)
        elif isinstance(val, (list, tuple, set)):
            for v in val:
                _walk(v)
        elif val is not None and not isinstance(val, bool):
            parts.append(str(val))

    for aid in ids:
        atom: Dict[str, Any] = catalog.get(aid) or {}
        for key, val in atom.items():
            if key.startswith("_"):
                continue
            _walk(val)
    return "\n".join(p for p in parts if p)


def _entry_names(sel: Dict[str, Any]) -> str:
    names = []
    for sec in ("experience", "projects", "leadership"):
        for entry in sel.get(sec, []) or []:
            n = str(entry.get("name") or "").strip()
            if n:
                names.append(n)
    return "\n".join(names)


def enforce_grounded(sel: Dict[str, Any], bullets: Dict[str, str], *,
                     fallback: Optional[Dict[str, str]] = None,
                     log=None) -> Dict[str, List[str]]:
    """Verify every non-verbatim bullet against its own group's atoms; revert a
    flagged bullet to its `fallback` text when that text is itself grounded,
    otherwise drop it. Mutates `bullets`; returns {gkey: unseen_tokens} for every
    bullet it had to touch (empty = all grounded)."""
    fallback = fallback or {}
    say = log or (lambda _msg: None)
    gm = compose.group_map(sel)
    names = _entry_names(sel)
    handled: Dict[str, List[str]] = {}
    for gk in list(bullets):
        if compose.is_verbatim_gkey(gk):
            continue
        ids = gm.get(gk) or gk.split("+")
        src = group_source_text(ids, extra=names)
        bad = unseen_tokens(bullets[gk], src)
        if not bad:
            continue
        handled[gk] = bad
        fb = (fallback.get(gk) or "").strip()
        if fb and not unseen_tokens(fb, src):
            bullets[gk] = fb
            say(f"grounding gate: reverted a bullet that introduced {bad} "
                "(kept its last grounded text).")
        else:
            del bullets[gk]
            say(f"grounding gate: dropped a bullet that introduced {bad} "
                "(no grounded fallback).")
    return handled


def letter_allowed_source(bullets: Dict[str, str], *, research: str = "",
                          company: str = "", job_title: str = "",
                          jd: str = "") -> str:
    """Everything the cover letter may legitimately mention: the whole master
    experience file (the candidate's own facts), the tailored bullets, the
    research blurb, the role/company labels, and the JD (a letter naturally
    echoes the posting's own terms — the deterministic guard here is against
    facts from NOWHERE; JD-borne instruction injection is fenced at the prompt
    and squeezed by refine_body's grounding pass)."""
    return "\n".join([
        str(assets.load_master()),
        "\n".join(bullets.values()),
        research or "", company or "", job_title or "", jd or "",
    ])


def letter_unseen(body: str, allowed_source: str) -> List[str]:
    """Distinctive tokens in the letter body with no trace in any allowed source."""
    return unseen_tokens(body, allowed_source)
