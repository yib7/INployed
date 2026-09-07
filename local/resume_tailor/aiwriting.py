"""The vendored avoid-ai-writing extract: one arm for the letter, one for bullets.

A bounded extract of the **avoid-ai-writing** skill (v3.18.0, MIT licence,
author **Conor Bronsdon**), vendored from
``~/.claude/skills/avoid-ai-writing/SKILL.md``. The upstream SKILL.md is ~714
lines: far too large to ship inside a per-call prompt, and most of it covers
registers this pipeline never enters (hashtags, headings, chat replies).

Two arms live here, kept visibly separate below. They share ONE module on
purpose: the vendored attribution and the tier-1 vocabulary regex are common to
both, and a second copy is how the two would drift apart.

  * The **cover-letter arm** (``RULES_PROMPT`` / ``EXTRA_BANS`` /
    ``violations()``), opt-in and default OFF.
  * The **résumé arm** (``RESUME_PROFILE`` / ``RESUME_RULES_PROMPT`` /
    ``RESUME_EXTRA_BANS`` / ``resume_violations()``), which serves the
    item-level sweep over Experience, Projects and Leadership bullets.

── the cover-letter arm ──────────────────────────────────────────────────────

Only the letter-relevant subset lives here, split the same two ways
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

── the résumé arm ────────────────────────────────────────────────────────────

A résumé matches none of the skill's six context profiles. All six assume prose
with sentences and paragraphs; a bullet is a subjectless fragment that opens
with a past-tense action verb ("Built a scraper that cut per-run cost 65%.").
Run the skill unmodified over that and two of its rules fire on CORRECT résumé
grammar, so ``RESUME_PROFILE`` is a seventh column of the upstream tolerance
matrix with a written reason on every deviation. The same technical carve-out as
above applies to ``RESUME_EXTRA_BANS``, for the same reason: a false positive
buys a repair call that can damage a bullet that was already right.
"""
from __future__ import annotations

import re
from typing import Dict, List, NamedTuple, Tuple

# ══ the cover-letter arm ══════════════════════════════════════════════════════
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


# ══ the résumé arm ════════════════════════════════════════════════════════════
# Everything below serves résumé BULLETS. It is kept separate from the letter arm
# above because the two registers disagree about grammar: a letter is prose with
# a first-person narrator, a bullet is a subjectless fragment with no narrator at
# all. The one thing they share is the tier-1 vocabulary regex, reused by object
# below so the word list cannot drift into two copies.

# ── (a) the tolerance profile ────────────────────────────────────────────────
# The five levels. The first four are the upstream matrix's own cell values; the
# fifth is this profile's addition.
#
# DELEGATED is not a softer SKIP. It means the signal is still audited, by a
# component that can see the unit of text the rule actually needs. "Uniform
# paragraph length" is the clearest case: there are no paragraphs on a résumé, so
# skipping it would throw away the skill's strongest structural signal, while
# applying it here would ask a one-sentence bullet a question about a paragraph.
# The right unit is the ITEM, so the rule is reinterpreted as uniform bullet
# length and measured across an item's bullets by the item-level detectors.
SKIP = "skip"
RELAXED = "relaxed"
STRICT = "strict"
EXTRA_STRICT = "extra strict"
DELEGATED = "delegated"


class Tolerance(NamedTuple):
    """One cell of the tolerance matrix: how hard to apply a rule, and why.

    ``why`` is not decoration. Several of these rows deviate from the upstream
    skill and one of them contradicts it outright, so the reason has to live next
    to the value; a bare "skip" with no argument becomes folklore in one cycle.
    """
    level: str
    why: str


# The seventh column of the skill's tolerance matrix, over that matrix's own 22
# rows, plus one row the matrix does not carry ("missing first-person
# perspective", which the skill files under Rhythm and uniformity). The upstream
# convention holds: a rule absent from this table applies at full strength, which
# is what ``resume_profile_level`` returns for anything unlisted.
RESUME_PROFILE: Dict[str, Tolerance] = {
    # ---- the two rules that fire on correct résumé grammar -------------------
    "subjectless fragments and agentless passives": Tolerance(
        SKIP,
        "The subjectless fragment IS the register. Every bullet on the page is "
        "one by design ('Built a scraper that cut per-run cost 65%'), the "
        "rephrase prompt asks for exactly that shape, and dedupe_leading_verbs "
        "guarantees each one opens on a distinct past-tense action verb. "
        "Upstream already carves this out for docs and casual, where a fragment "
        "list is the correct form; a résumé is the extreme case of that "
        "carve-out. Applying the rule would name the actor on every line and "
        "turn 18 correct bullets into 18 sentences that no longer fit their "
        "line budget."),
    "missing first-person perspective": Tolerance(
        SKIP,
        "Skipped and INVERTED, and this is the one row where the profile "
        "deliberately contradicts the upstream skill. Upstream reads the absence "
        "of 'I think' or 'in my experience' as an AI tell, on the assumption "
        "that the piece is supposed to have a voice. A résumé forbids first "
        "person outright: no 'I', no 'my', no 'we'. So the finding is worse than "
        "irrelevant here, it points the wrong way, and a repair acting on it "
        "would damage every bullet it touched. Written down because a future "
        "reader diffing this profile against SKILL.md will otherwise think the "
        "row was dropped by accident."),

    # ---- what the résumé is actually at risk of -----------------------------
    "promotional language": Tolerance(
        EXTRA_STRICT,
        "The document as a whole is a pitch, so brochure vocabulary is the "
        "failure mode that arrives first and most often. Upstream reserves extra "
        "strict for investor-email on the same argument: one 'thriving "
        "ecosystem' undermines the rest of the page."),
    "significance inflation": Tolerance(
        EXTRA_STRICT,
        "Same argument. A bullet that calls its own work pivotal has stopped "
        "stating the fact and started grading it, which is the reader's job. If "
        "the bullet still reads after the inflation clause is deleted, it goes."),
    "bullet-np lists": Tolerance(
        EXTRA_STRICT,
        "Upstream fires at five consecutive verbless items, a threshold tuned "
        "for prose that happens to contain a list. Here every line is a list "
        "item, so the threshold drops to one: a single bullet with no finite "
        "verb asserts nothing and is a résumé defect on its own."),
    "hedge-stacked predictions": Tolerance(
        EXTRA_STRICT,
        "A register that bans the single hedge bans the stack a fortiori. In "
        "practice _HEDGE_RE catches the first modal before a stack can form, so "
        "this row records the ordering; it adds no check of its own."),
    "hedging": Tolerance(
        STRICT,
        "A bullet reports work the candidate finished, so 'may', 'could', "
        "'potentially' and 'generally' read as doubt about their own record. "
        "Upstream relaxes this for technical-blog because 'may' is accurate when "
        "describing a system; a bullet describes a completed action instead."),
    "word table (full list)": Tolerance(
        STRICT,
        "Partial, in the sense the matrix gives technical-blog. Tier-1 "
        "vocabulary applies, minus the technical carve-out the module docstring "
        "argues for: realm, paradigm, beacon, landscape, best practices and "
        "actionable have engineering senses a bullet can legitimately carry, so "
        "they stay in the prompt arm where the model can read the context."),
    "real/actual inflation": Tolerance(
        STRICT,
        "Prompt arm only. 'real-time streaming', 'a real user session' and 'the "
        "actual runtime of a query' are ordinary engineering usage, and the "
        "skill's own named-contrast carve-out needs context to apply, so a regex "
        "here would fire on grounded text."),

    # ---- rules the format makes unreachable ---------------------------------
    "copula avoidance": Tolerance(
        SKIP,
        "Bullets lead with an action verb by design, and dedupe_leading_verbs "
        "enforces distinct openers across an item. 'is' and 'has' are what this "
        "register avoids on purpose, so a rule pushing text back toward them "
        "fights the format. Upstream already skips it for linkedin, docs and "
        "casual on the same reasoning."),
    "transition phrases": Tolerance(
        SKIP,
        "Bullets do not connect to one another. Each is a standalone claim in a "
        "list whose order is set by relevance to the job, so there is nothing "
        "for 'Moreover' to join and no argument for it to carry."),
    "generic conclusions": Tolerance(
        SKIP,
        "A bullet list has no closing move to be generic about, and the rule is "
        "P2 in any case, which this cycle reports without fixing."),
    "excessive bullets": Tolerance(
        SKIP,
        "The document IS a bullet list. How many bullets an item gets is fixed "
        "upstream by bullet_line_targets and _enforce_fixed_counts, from the "
        "page budget, so it is a layout decision and style has no vote in it."),
    "hashtag stuffing": Tolerance(
        SKIP,
        "There are no hashtags in a LaTeX résumé. Listed so the row is visibly "
        "considered, since a missing row reads as an oversight and upstream "
        "ranks this one P0."),
    "bold overuse": Tolerance(
        SKIP,
        "The template owns every weight on the page. A bullet is plain text "
        "with no markup, so there is no bold available to overuse."),
    "emoji in headers": Tolerance(
        SKIP,
        "The renderer is LaTeX and tests/test_resume_latex_ascii.py already "
        "fails the build on a non-ASCII character, which is a harder gate than "
        "any style rule."),
    "numbered list inflation": Tolerance(
        SKIP,
        "Nothing in the rendered résumé is numbered. Section items and bullets "
        "are both unordered."),
    "rhetorical questions": Tolerance(
        SKIP,
        "A bullet has no interrogative form. The pattern cannot occur, and a "
        "rule that can never fire only costs prompt space."),
    "future-narrative closers": Tolerance(
        SKIP,
        "Bullets state completed work in the past tense. There is no closing "
        "prediction for a modal to hedge, which is the shape upstream targets."),
    "social endorsement closers": Tolerance(
        SKIP,
        "The résumé has no reader-facing call to action anywhere in it, so the "
        "share-post tell has nothing to attach to."),

    # ---- audited elsewhere ---------------------------------------------------
    "em dashes": Tolerance(
        DELEGATED,
        "compose._STYLE_BANS matches an em dash and compose._strip_em_dashes "
        "removes any that survive the repair call, unconditionally, so one can "
        "never print. Re-auditing it here would only buy a second repair call on "
        "text that is already handled."),
    "uniform paragraph length": Tolerance(
        DELEGATED,
        "Reinterpreted and still audited. There are no paragraphs, but the signal "
        "behind the rule is real and upstream calls structure the strongest tell "
        "of all. The résumé equivalent is uniform BULLET length, which needs "
        "every bullet of an item at once and so belongs to the item-level "
        "detectors, one level up from a per-bullet gate."),
    "tier 3 phrase clustering": Tolerance(
        DELEGATED,
        "A density rule needs a paragraph-sized unit; one bullet is one "
        "sentence, so the cluster threshold can never be met inside it. The "
        "per-item equivalent is noun and synonym cycling across an item's "
        "bullets, which the item-level detectors measure."),
}


def resume_profile_level(rule: str) -> str:
    """Tolerance for `rule` under the résumé profile.

    Anything the table does not list comes back STRICT, which is the upstream
    convention ("Rules not listed in the table apply at full strength across all
    profiles"). Lookup is case-insensitive so a caller can pass the matrix's own
    row label.
    """
    tol = RESUME_PROFILE.get(rule.strip().lower())
    return tol.level if tol else STRICT


# ── (b) the judgment arm ─────────────────────────────────────────────────────
# Appended after compose.BANNED_PHRASING at the call site, so nothing that list
# already covers is repeated (em dashes, contrast framing, participial tails,
# buzz adjectives, buzzword verbs, vague quantifiers, stacked adjectives, the
# rule-of-three verb train). What is left is the P0 and P1 material the shared
# list misses. P2 polish is deliberately out of scope for auto-fixing this cycle:
# it gets detected and reported, never rewritten.
#
# The REGISTER preamble comes first and is the reason this constant exists at
# all. Without it a repair call is free to "fix" a subjectless fragment into a
# first-person sentence, which is what the upstream skill would ask for and what
# RESUME_PROFILE turns off. Stating it to the model closes the same hole the
# profile closes for the code.
#
# Item 5 overlaps BANNED_PHRASING by one example: that list names "the real X"
# in passing, under grandiosity. The full rule with its carve-out is worth the
# repetition, because "real-time" is the exact usage a bare ban would break.
#
# Like every prompt in this package, the text below is itself free of the
# characters and the sentence shapes it forbids. A model copies the punctuation
# it is shown; tests/test_prompt_hygiene.py holds that line package-wide, and
# tests/test_aiwriting_resume.py runs the same scan directly over this constant.
RESUME_RULES_PROMPT = (
    "ALSO STRIP THESE AI-WRITING PATTERNS (avoid-ai-writing v3.18.0, MIT, Conor "
    "Bronsdon; resume profile, P0 and P1 tiers only):\n"
    "REGISTER: a resume bullet is a subjectless fragment that opens with a past-tense "
    "action verb ('Built a scraper that cut per-run cost 65%'). That form is correct "
    "and you must preserve it. Never add a subject. Never write in the first person "
    "('I', 'my', 'we', 'our', 'I think', 'in my experience'). Never expand a fragment "
    "into a full sentence with a named actor. Keep the opening verb exactly as "
    "written.\n"
    "1. TIER-1 VOCABULARY. Replace on sight with the plain word: showcasing (showing), "
    "delve (explore), landscape (field), realm (area), tapestry, paradigm (model), "
    "embark (start), beacon, testament to (shows), pivotal (key), underscores "
    "(highlights), meticulous (careful), deep dive, unpack (explain), intricate "
    "(complex), ever-evolving, daunting (hard), actionable (practical), impactful "
    "(effective), learnings (lessons), thought leadership, best practices, at its "
    "core, synergy, interplay, in order to (to), due to the fact that (because), "
    "serves as (is), features or boasts (has), commence (start), ascertain (find "
    "out), endeavor, keen, embrace, watershed moment, nestled, vibrant, thriving, "
    "bustling. Keep the word when the bullet means it literally: a Keycloak realm, an "
    "event-driven paradigm, a BLE beacon, a GIS landscape layer, a documented best "
    "practice, an actionable alert in a monitoring system.\n"
    "2. TEMPLATE AND SLOT-FILL PHRASING. If a blank could hold any noun and the "
    "bullet would still read the same, rewrite it around the system, the number and "
    "the change: 'a key step towards [X]', 'contributed to [team] initiatives', "
    "'played a role in [project]', 'worked closely with cross-functional "
    "stakeholders', 'delivered high-quality results on time'.\n"
    "3. FORMULAIC OPENINGS. Open on the action verb and the thing it acted on. Cut "
    "the wind-up: 'Responsible for', 'Tasked with', 'Involved in', 'Helped to', "
    "'Assisted with', 'Worked on', 'In a fast-paced environment', 'As part of a team "
    "of engineers', 'In the rapidly evolving world of X'. Broad context ahead of the "
    "fact has no place on one line of a resume.\n"
    "4. HEDGING. The bullet reports work that is finished, so a modal reads as doubt "
    "about the candidate's own record: may, might, could, potentially, generally, "
    "presumably, arguably, and the stacked forms ('could potentially reduce', 'may "
    "eventually improve', 'might ultimately raise'). State what happened. An "
    "approximate measurement keeps its own qualifier ('about 40%', 'roughly 2x').\n"
    "5. REAL AND ACTUAL INFLATION. Do not prop up an abstract noun with real, actual, "
    "genuine or true ('real business value', 'actual production traffic', 'genuine "
    "user impact'). Either name what the weak version was, or drop the adjective and "
    "give the measurement. Literal uses stay: real-time streaming, a real user "
    "session, the actual runtime of a query.\n"
    "6. BARE-NOUN BULLETS. Every bullet needs a finite verb and something checkable. "
    "A verbless label ('Stable pipeline throughput', 'Reliable data quality', "
    "'Effective team collaboration') asserts nothing and reads as a marketing "
    "one-pager. Rewrite it as a claim with a number or a named system.\n"
    "7. CHATBOT ARTIFACTS. A bullet is never a chat reply. No 'Certainly', 'Sure', "
    "'Great question', 'I hope this helps', 'Here is the rewritten bullet', 'Let me "
    "know if you need anything else', and no \"Let's <verb>\" opener. Return the "
    "bullet text alone.\n"
    "8. VAGUE ATTRIBUTION. Never credit an unnamed authority: 'experts believe', "
    "'studies show', 'research suggests', 'industry leaders agree', 'widely "
    "recognized as', 'praised by stakeholders'. Name the source, the benchmark or "
    "the number, or drop the claim.\n"
    "9. SIGNIFICANCE INFLATION. A shipped feature is a shipped feature. Cut 'marking "
    "a pivotal moment', 'a watershed moment for the team', 'a paradigm shift in how "
    "the company works', 'transformed the entire organization'. If the bullet still "
    "works after you delete the inflation clause, delete it and keep the measurement."
)


# ── (c) the deterministic arm ────────────────────────────────────────────────
# Same standard as compose._STYLE_BANS and EXTRA_BANS above: only phrasing that is
# ALWAYS slop in a résumé bullet, because a false positive buys a repair call that
# can damage correct text. Words with a real engineering sense a bullet could
# legitimately carry (realm, paradigm, beacon, landscape, best practices,
# actionable, real-time, actual) are absent by design and live in
# RESUME_RULES_PROMPT instead. Nothing compose._STYLE_BANS already matches is
# repeated, so the two lists can be concatenated without double-counting.

# Subordinators that open a clause a modal can sit inside without hedging the
# bullet's own claim. See _HEDGE_RE.
_SUBORDINATORS = (r"so|that|which|who|whom|whose|where|when|while|if|whether"
                  r"|because|until|unless|though|although|before|after")

# Hedging, in the position where it is always wrong.
#
# Two care points, both of which a flat r"\b(may|could|potentially|generally)\b"
# gets wrong on real bullets:
#
# 1. NO re.I, deliberately. "May" is a month, and "Launched the rewrite in May
#    2024" is a legitimate bullet. Bullets are sentence-case and open on a verb,
#    so a hedging modal is always lowercase while a capitalised "May" is always
#    the calendar one. Case is the cheapest reliable separator available here.
# 2. The modal branch is anchored at the start and refuses to cross a
#    subordinator. "Built a viewer so reviewers could compare runs side by side"
#    is ordinary English: the modal governs a purpose clause, leaving the bullet's
#    own claim unhedged. The tempered pattern consumes characters only while the
#    next token is not a subordinator, so it can reach a modal in the main clause
#    and no further. The adverbs need no such guard, since "potentially" and
#    "generally" hedge whatever they touch.
# 3. Sentence-initial capitals are handled separately, because (1) drops them.
#    "Might improve throughput on larger inputs" is a hedge in the position the
#    pipeline reserves for an action verb, and lowercase openers already match
#    through the tempered branch at zero width, so only the capitals were missing.
_HEDGE_RE = re.compile(
    r"\b(?:potentially|generally|presumably|arguably)\b"
    # 3. A CAPITALISED modal is the gap case (2) leaves open. Case separates the
    #    hedge from the month everywhere except the one position where a bullet
    #    capitalises its first word anyway, so that position is named on its own.
    #    "May 2024" keeps its exemption by declining a following number; "Might"
    #    and "Could" need no such guard, having no calendar sense at all.
    r"|^\s*(?:Might|Could)\b"
    r"|^\s*May\b(?!\s+\d)"
    r"|^(?:(?!\b(?:" + _SUBORDINATORS + r")\b)[\s\S])*?\b(?:may|might|could)\b")

# Tourism-brochure vocabulary, which the profile marks EXTRA STRICT because it is
# the résumé's own failure mode. Kept to words with no honest reading in a
# bullet: "award-winning" can be a grounded fact, "next-generation sequencing" is
# a real technique and "unmatched records" a real data term, so none of those are
# here.
_PROMO_RE = re.compile(
    r"\b(?:vibrant|thriving|breathtaking|industry[- ]leading"
    r"|first[- ]of[- ]its[- ]kind|bleeding[- ]edge|unrivall?ed)\b", re.I)

# Significance inflation beyond what _TIER1_RE already carries (pivotal,
# watershed moment). "paradigm shift" earns a regex where bare "paradigm" cannot:
# the two-word phrase has no programming sense.
_SIGNIFICANCE_RE = re.compile(
    r"\bparadigm[- ]shift\w*"
    r"|\bmarking a(?: \w+)? moment\b"
    r"|\ba (?:defining|watershed|historic|landmark) moment\b"
    r"|\bsea change\b", re.I)

# An authority with no name attached. Scoped to the finite-verb forms, so
# "Published research showing a 12% gain" (a real, checkable claim) stays clean
# while "studies show" does not.
_VAGUE_ATTRIBUTION_RE = re.compile(
    r"\b(?:experts?|studies|research|analysts|scientists|industry leaders)\s+"
    r"(?:believes?|shows?|suggests?|agrees?|indicates?|confirms?|says?)\b"
    r"|\bwidely (?:recognized|regarded|considered|acknowledged) as\b", re.I)

# The dead openers. Anchored to the start of the bullet, which is where the
# pipeline requires an action verb from the palette, so these can only ever be a
# defect there. The era framings are not anchored, being slop anywhere in a
# bullet.
_FORMULAIC_OPENING_RE = re.compile(
    r"^\s*(?:responsible for|tasked with|involved in|assisted with"
    r"|charged with|helped to|worked on)\b"
    r"|\bin (?:the )?(?:rapidly|ever)[- ](?:evolving|changing|growing)\b"
    r"|\bin an era (?:where|of)\b"
    r"|\bin today['’]s\b", re.I)

# Chat-interface tics that ride along when a repair call answers in prose instead
# of returning bullet text. Anchored at the start for the same reason as above.
_CHATBOT_RE = re.compile(
    r"^\s*(?:certainly|sure|absolutely|of course|great question"
    r"|here (?:is|are)|let['’]s\s+\w)\b"
    r"|\bI hope (?:this|that) helps\b"
    r"|\blet me know if you\b", re.I)

RESUME_EXTRA_BANS: Tuple[Tuple[str, re.Pattern], ...] = (
    # The SAME compiled object the letter arm uses, referenced and never
    # copied: every word in it is always slop in a bullet too, and one object
    # means the vocabulary cannot drift between the two arms.
    ("tier-1 vocabulary", _TIER1_RE),
    ("hedge", _HEDGE_RE),
    ("promotional language", _PROMO_RE),
    ("significance inflation", _SIGNIFICANCE_RE),
    ("vague attribution", _VAGUE_ATTRIBUTION_RE),
    ("formulaic opening", _FORMULAIC_OPENING_RE),
    ("chatbot artifact", _CHATBOT_RE),
)


def resume_violations(text: str) -> List[str]:
    """Names of the résumé AI-writing patterns present in a bullet (empty = clean).

    Mirrors ``violations`` and ``compose.style_violations`` so a caller can
    concatenate the lists and keep a single repair call. A correct bullet in the
    résumé register returns ``[]``: that is the guarantee the profile above
    exists to provide, and tests/test_aiwriting_resume.py pins it.
    """
    return [name for name, pat in RESUME_EXTRA_BANS if pat.search(text)]
