"""Optional extra AI-writing gate for the cover-letter body (default OFF).

A bounded extract of the **avoid-ai-writing** skill (v3.18.0, MIT licence,
author **Conor Bronsdon**), vendored from
``~/.claude/skills/avoid-ai-writing/SKILL.md``. The upstream SKILL.md is ~714
lines: far too large to ship inside a per-call prompt, and most of it covers
registers a cover letter never enters (hashtags, headings, bullet lists, chat
replies). Only the letter-relevant subset lives here, split the same two ways
the résumé engine already splits its style gate:

  * ``RULES_PROMPT`` -- the judgment calls, appended to the generation, refine,
    and repair prompts so the model applies them (compose.BANNED_PHRASING's
    counterpart).
  * ``EXTRA_BANS`` / ``violations()`` -- the deterministic arm, mirroring
    ``compose._STYLE_BANS`` / ``compose.style_violations``.

**What earns a place in EXTRA_BANS.** Same rule as ``compose._STYLE_BANS``: only
phrasing that is ALWAYS slop, because a false positive buys a repair call that
can damage correct text. In a cover letter the load-bearing risk is specific --
the letter must stay grounded in the candidate's own résumé bullets, and it
always names an employer. So a skill word is kept out of the regex arm when it
has a real technical sense a bullet could carry (``realm`` is a Keycloak/Kerberos
term, ``paradigm`` a programming one, ``beacon`` a BLE device, ``landscape`` a
GIS/market analysis, ``best practices`` and ``actionable`` are ordinary
engineering and analytics vocabulary), when the skill itself qualifies it as
context-dependent (``embrace``/``symphony``/``load-bearing`` are tagged
"(metaphor)"; ``genuine``, ``keen``, ``features``, ``presents`` are tagged "(as
intensifier)" / "(inflated)" -- and this package's own prompts deliberately ask
for "genuine but MEASURED interest"), or when it doubles as a common employer
name (``Tapestry``, ``Embark``, ``Vibrant``, ``Thrive``). All of those stay in
RULES_PROMPT, where the model can read context. Anything already covered by
``compose._STYLE_BANS`` (robust, seamless, comprehensive, holistic, leverage,
utilize, harness, streamline, em dashes, ...) is not repeated here.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# The prompt arm: the patterns that need judgment, written as instructions. Kept
# to one block so callers can append it after compose.BANNED_PHRASING verbatim.
# Deliberately free of em dashes, since it is telling the model to avoid them.
RULES_PROMPT = (
    "ALSO STRIP THESE AI-WRITING PATTERNS (avoid-ai-writing v3.18.0, MIT, Conor Bronsdon):\n"
    "1. Tier-1 vocabulary, replaced on sight with the plain word: delve (explore), "
    "landscape (field), tapestry, realm (area), paradigm (model), embark (start), beacon, "
    "testament to (shows), pivotal (key), underscores (highlights), meticulous (careful), "
    "showcasing (showing), deep dive, unpack (explain), intricate (complex), ever-evolving, "
    "daunting (hard), holistic, actionable (practical), impactful (effective), learnings "
    "(lessons), thought leadership, best practices, at its core, synergy, interplay, in "
    "order to (to), due to the fact that (because), serves as (is), features / boasts (has), "
    "commence (start), ascertain (find out), endeavor, keen, embrace, watershed moment, "
    "nestled, vibrant, thriving, bustling.\n"
    "2. No 'not X, it's Y' contrast. That includes the split-sentence form ('The headline "
    "is not the speed. The real story is the tooling.'), the stacked version that negates "
    "two or three options before the reveal, and a bare negation tacked onto the end ('the "
    "options come from the record, no guessing'). State the positive claim once.\n"
    "3. No hollow intensifiers: genuine, genuinely, truly, real (as in 'a real "
    "improvement'), to be honest, quite frankly, let's be clear, worth a look. State the "
    "fact instead.\n"
    "4. No hedging: perhaps, could potentially, may eventually, might ultimately, it is "
    "important to note that, to be clear. Make the point directly.\n"
    "5. No template or slot-fill phrasing: 'a [adjective] step towards [X]', 'Whether you "
    "are X or Y', 'I recently had the pleasure of ...'. If a blank could hold any noun and "
    "the sentence would still read the same, rewrite it.\n"
    "6. No transition scaffolding: Moreover, Furthermore, Additionally, 'In today's ...', "
    "'In an era where', 'It is worth noting that', 'Notably', 'When it comes to', 'At the "
    "end of the day', 'That being said'. Order the paragraphs so the connection is obvious.\n"
    "7. No chatbot artifacts: 'I hope this helps', 'Certainly', 'Feel free to reach out', "
    "'Let me know if you need anything else', 'Let's dive in', and any 'Let's <verb>' "
    "opener used as a transition.\n"
    "8. No sycophancy and no acknowledgment loops: 'Great question', 'You are absolutely "
    "right', 'To answer your question', or restating the posting back at the reader before "
    "answering it. The reader wrote the posting; go straight to the answer.\n"
    "9. No rhetorical-question openers: 'So why does this matter?', 'What does this mean "
    "for your team?'. If you know the answer, write the answer.\n"
    "10. No generic conclusion: 'In conclusion', 'In summary', 'The future looks bright', "
    "'Only time will tell', 'As we move forward'. Close on something specific to this role.\n"
    "11. No significance inflation: 'marking a pivotal moment', 'a watershed moment for the "
    "field'. State what happened and let the reader judge. If the sentence still works "
    "after you delete the inflation clause, delete it.\n"
    "12. No compulsive rule of three: at most one 'adjective, adjective, and adjective' or "
    "three-verb train in the whole letter. Two items or four is fine, and a plain sentence "
    "is usually better.\n"
    "13. Vary the rhythm, which is the strongest AI tell of all. Mix sentence length: some "
    "under eight words, some over twenty, never a whole letter of 15-to-25-word sentences. "
    "Vary paragraph length the same way, so one paragraph is clearly shorter than the rest."
)

# ── the deterministic arm ─────────────────────────────────────────────────────
# Tier 1 words kept ONLY where there is no load-bearing reading in a letter that
# must stay grounded in résumé bullets. See the module docstring for what was
# left out and why.
_TIER1_RE = re.compile(
    r"\b(?:delv(?:e|es|ed|ing)"
    r"|testament to"
    r"|pivotal"
    r"|meticulous(?:ly)?"
    r"|watershed moment"
    r"|nestled"
    r"|bustling"
    r"|intricate|intricacies"
    r"|ever-evolving"
    r"|daunting"
    r"|impactful"
    r"|learnings"
    r"|thought leader(?:ship)?"
    r"|at its core"
    r"|synerg(?:y|ies|istic)"
    r"|interplay"
    r"|in order to"
    r"|due to the fact that"
    r"|boasts?"
    r"|showcas(?:e|es|ed|ing)"
    r")\b", re.I)

EXTRA_BANS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("tier-1 vocabulary", _TIER1_RE),
    # Chat-interface tics. Scoped to the exact sign-off so "I hope to help your
    # team ..." (a normal letter sentence) stays clean.
    ("chatbot artifact", re.compile(r"\bI hope (?:this|that) helps\b", re.I)),
    # The false-collaborative opener. Anchored to the start of a line or sentence:
    # mid-sentence "let's" is ordinary English and is left to RULES_PROMPT.
    ("let's opener", re.compile(r"(?:^|[.!?]\s+)let['’]s\s+\w", re.I | re.M)),
    ("era framing", re.compile(r"\bin today['’]s\b", re.I)),
    ("confidence calibration",
     re.compile(r"\bit(?:['’]s| is)\s+worth\s+noting\b", re.I)),
    ("generic conclusion", re.compile(r"\bin conclusion\b", re.I)),
)


def violations(text: str) -> List[str]:
    """Names of the extra AI-writing patterns present in `text` (empty = clean).

    Mirrors ``compose.style_violations`` so the letter gate can concatenate the
    two lists and keep its single repair call.
    """
    return [name for name, pat in EXTRA_BANS if pat.search(text)]
