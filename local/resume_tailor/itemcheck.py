"""Item-level AI-writing detectors: the tells that only exist ACROSS an item's bullets.

``compose.enforce_style`` sees one bullet at a time. That makes it structurally blind
to the signal the avoid-ai-writing skill (v3.18.0, MIT, Conor Bronsdon) calls the
strongest of all: "AI detection tools weight structural regularity higher than
vocabulary ... if you fix every word on the Tier 1 list but leave the rhythm
untouched, the text still reads as AI-generated." Rhythm is not a property of one
bullet. It is a property of an ITEM, meaning one Experience / Projects / Leadership
entry together with all of its bullets, which is the unit this module takes.

``aiwriting.RESUME_PROFILE`` marks three upstream rows DELEGATED, and this module is
what they are delegated to: "uniform paragraph length" reinterpreted as uniform
bullet length, "tier 3 phrase clustering" as noun and synonym cycling, and the
EXTRA_STRICT "bullet-np lists" row whose threshold the profile drops to a single
verbless bullet. Every vocabulary and phrase rule stays in ``aiwriting``; nothing
lexical belongs here.

**Stdlib only, and that is a hard requirement rather than a preference.**
``config.py`` calls ``load_dotenv()`` at import scope, so importing anything else
from this package pulls live credentials into the process and a scratch script can
place a billed API request. Depending on nothing but ``re`` and ``statistics`` means
these detectors can be exercised standalone, the way ``measure.py`` already is. That
property earns its keep: SP1's hedge regex was checked that way and three false
negatives fell out of it that the suite had not covered.

── what every threshold here is fighting ────────────────────────────────────────

A CORRECT resume item has every property these detectors measure, to some degree.
Its bullets are length-matched because ``run._trim_to_caps`` trims each one to a
per-bullet printed-line budget. They share nouns because they describe one job. They
all open on a past-tense action verb because the rephrase prompt demands it. So a
threshold picked by intuition fires on text that was already right, and a repair
call on correct text can only move it in the wrong direction.

Every constant below was therefore calibrated against real output: 57 resumes this
pipeline generated and shipped, holding 445 items and 886 bullets, of which 60 items
carry the three or more bullets the statistical detectors need. Each constant records
its own false-positive count against that corpus. Re-run the calibration if the line
budget, the bullet counts or the rephrase prompt ever change; the numbers are
properties of the output, not of the English language.

── the API ──────────────────────────────────────────────────────────────────────

``item_findings(item_name, [(gkey, text), ...]) -> [Finding, ...]``, with every
detector also callable on its own. Findings are ordered deterministically and do not
depend on the order the bullets arrive in, so the same item always produces the same
payload. ``findings_payload`` converts them to plain dicts for the JSON prompt body
the item sweep sends.
"""
from __future__ import annotations

import re
import statistics
from math import ceil
from typing import Callable, Dict, FrozenSet, List, NamedTuple, Sequence, Tuple

# Severity, from the skill's own tier list, because the frozen answer for this cycle
# is "P0 and P1 are repaired, P2 is reported and left alone". The mapping is not a
# judgment call: upstream files "Synonym cycling within a paragraph" and "Bullet
# lists of bare noun phrases" under P1, and "Uniform paragraph length" and
# "Compulsive rule of three" under P2. Shape repetition is P1 by the same reading
# that puts the other structural rules there: upstream ranks stacked same-shape
# fragments with the obvious tells, and structure is the signal it says outranks
# vocabulary. There is no P0 here, since every P0 row is lexical and lives in
# aiwriting.RESUME_EXTRA_BANS.
P1 = "P1"
P2 = "P2"


class Span(NamedTuple):
    """Where a finding actually is: a half-open slice of one bullet's text.

    ``text`` is carried alongside the offsets so a consumer can put the offending
    words in front of a model without re-slicing, and so a test can prove the
    offsets index the bullet they name.
    """
    gkey: str
    start: int
    end: int
    text: str


class Finding(NamedTuple):
    """One detector's verdict about one item.

    ``detail`` is written to be read by a model as well as a human: it says what was
    measured and what to do about it, in the register the repair prompt uses. It is
    held to the same punctuation rules as every prompt in this package, since it
    rides into one.
    """
    detector: str
    tier: str
    item: str
    detail: str
    spans: Tuple[Span, ...]


Bullets = Sequence[Tuple[str, str]]
Detector = Callable[[str, Bullets], List[Finding]]


# ── shared text plumbing ─────────────────────────────────────────────────────
# Punctuation stripped from the EDGES of a leading token (an inner hyphen in
# "Co-developed" is kept). The palette verbs are capitalized past-tense; matching is
# case-insensitive on this normalized form.
_EDGE_PUNCT = " \t\n\r\"'`()[]{}.,;:!?"


def leading_verb(text: str) -> str:
    """The bullet's opening verb, normalized for comparison: the first whitespace token,
    edge-punctuation-stripped and lowercased. '' for an empty/blank bullet."""
    toks = (text or "").split()
    if not toks:
        return ""
    return toks[0].strip(_EDGE_PUNCT).lower()


# A word for counting purposes: letters, plus the inner hyphen and apostrophe that
# hold "trunk-based" and "on-call" together. Numbers and bare percentages are
# deliberately not words here, since none of the detectors below reason about them.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def _pairs(bullets: Bullets) -> List[Tuple[str, str]]:
    """The item's non-blank bullets, keyed and ORDERED BY GKEY.

    Sorting here is what makes every finding independent of the order the caller
    passes bullets in. The caller already knows the item's print order; a finding
    only has to name the bullets it is about, and a payload that changes shape
    because two bullets swapped places is a payload no test can pin.
    """
    return sorted(((gk, text) for gk, text in bullets if (text or "").strip()),
                  key=lambda pair: pair[0])


def _whole_span(gkey: str, text: str) -> Span:
    """A span covering the bullet's visible text, used by the detectors whose finding
    is about a whole bullet rather than a phrase inside it."""
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    return Span(gkey, start, end, text[start:end])


def _tokens(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def _order(findings: List[Finding]) -> List[Finding]:
    """Findings in a stable order: by the first bullet they touch, then by detail.
    Both keys are already order-independent, so this is a total order over a set that
    does not change when the input is shuffled."""
    return sorted(findings,
                  key=lambda f: (f.spans[0].gkey if f.spans else "", f.detail))


# ── 1. shape_repetition ──────────────────────────────────────────────────────
# The skill's rule is "three or more same-shape fragments in a row". Naming a shape
# needs a parse, and no POS tagger is available in this package (and none is worth a
# dependency for five detectors), so the skeleton below approximates one from four
# features that survive without grammar.
#
# THE FEATURE THE PLAN ASKED FOR AND THIS CANNOT USE: the leading verb itself.
# `compose.dedupe_leading_verbs` runs earlier and GUARANTEES every opener on the page
# is distinct, so a skeleton keyed on the verb's identity could never repeat and the
# detector would be dead code that always returns []. What repeats is the verb's
# FORM, which the dedupe does not touch: an item whose bullets all open on a regular
# "-ed" past tense reads more metronomic than one that mixes "Built" and "Automated".
# The form is a weak feature on its own (the verb palette in
# resume_tailor_files/active_words.md is about 90% regular, measured 801 of 886
# bullets in the corpus), so it discriminates little and costs nothing; the work is
# done by the other three.
_PARTICIPIAL_TAIL_RE = re.compile(r",\s+\w+ing\b", re.I)

# Clause breaks, counted rather than parsed. Commas and semicolons, plus the
# conjunctions and relativizers that start a new predicate. Prepositions are absent
# on purpose: "for the finance team" adds a phrase, not a clause, and counting it
# would make the bucket track bullet length instead of bullet structure.
_CLAUSE_BREAK_RE = re.compile(
    r"[,;]|\b(?:and|or|but|that|which|who|whose|where|when|while|because|so"
    r"|after|before|though|although|using)\b", re.I)

# Bucketed, because the exact count is noise: two bullets with 5 and 6 clause breaks
# are the same shape to a reader. The cap is 4 because that is where the corpus
# saturates (459 of 886 bullets sit at 4 or more), so a higher cap would split one
# natural class into several and cost real detections.
_CLAUSE_BUCKET_CAP = 4

# The trailing-prepositional shape: which preposition heads the phrase the bullet
# ends on. Two bullets that both land on "by 40%" and "by 30%" rhyme in a way that a
# reader registers and a word list does not.
_PREPOSITIONS: FrozenSet[str] = frozenset({
    "across", "after", "against", "among", "around", "at", "before", "behind",
    "below", "beneath", "beside", "between", "beyond", "by", "despite", "down",
    "during", "for", "from", "in", "inside", "into", "near", "of", "off", "on",
    "onto", "outside", "over", "per", "since", "through", "throughout", "to",
    "toward", "towards", "under", "until", "up", "upon", "via", "with", "within",
    "without",
})
# How far back to look for the head of that final phrase. Eight tokens covers the
# tails these bullets actually end on ("across 12 source systems", "by 40% for the
# checkout service") without reaching back into the main clause of a 35-word bullet.
_TAIL_WINDOW = 8

# Three, straight from the skill's own wording. Two bullets that rhyme is a
# coincidence a human writer produces constantly.
_SHAPE_MIN_MATCHES = 3

# Verbs whose past tense does not end in "-ed", seeded from the irregular openers in
# resume_tailor_files/active_words.md (began, drew, found, led, overcame, oversaw,
# spoke, taught, understood, built, sold, wrote) and extended with the irregulars a
# bullet about engineering work actually reaches for. Kept to plausible resume verbs:
# an exhaustive irregular list would add hundreds of entries that can never appear
# and would raise the odds of a noun being read as a verb by bare_noun_bullet.
_IRREGULAR_PAST: FrozenSet[str] = frozenset({
    "began", "beat", "bought", "brought", "broke", "built", "burnt", "caught",
    "chose", "cost", "cut", "dealt", "drew", "drove", "fell", "felt", "fought",
    "found", "gave", "got", "grew", "held", "hit", "kept", "knew", "led", "left",
    "let", "lost", "made", "met", "outgrew", "overcame", "oversaw", "paid", "put",
    "ran", "rebuilt", "redrew", "rewrote", "rose", "said", "sang", "sat", "saw",
    "sent", "set", "shot", "shrank", "shut", "slid", "sold", "sought", "spent",
    "split", "spoke", "spread", "spun", "stood", "struck", "stuck", "swept", "swam",
    "taught", "told", "took", "tore", "thought", "threw", "understood", "undertook",
    "upheld", "went", "withdrew", "won", "wore", "wrote",
})

# Copulas and auxiliaries. A bullet that leans on one is weak, and the rephrase
# prompt steers away from them, but the verb is finite and the claim is asserted, so
# bare_noun_bullet must not flag it.
_AUXILIARIES: FrozenSet[str] = frozenset({
    "is", "are", "was", "were", "be", "been", "am", "has", "have", "had", "do",
    "does", "did", "can", "could", "will", "would", "shall", "should", "may",
    "might", "must",
})


def _verb_form(verb: str) -> str:
    """The opening verb's morphological class. See the block comment above for why
    the class is used and the verb's identity is not."""
    if not verb:
        return ""
    if verb.endswith("ing"):
        return "ing"
    if verb.endswith("ed"):
        return "ed"
    if verb in _IRREGULAR_PAST:
        return "irregular"
    return "other"


def _tail_preposition(text: str) -> str:
    """The preposition heading the phrase the bullet ends on, or '' when the tail is
    not a prepositional phrase."""
    toks = [w.strip(_EDGE_PUNCT).lower() for w in text.split()]
    for tok in [t for t in toks if t][-_TAIL_WINDOW:]:
        if tok in _PREPOSITIONS:
            return tok
    return ""


def _skeleton(text: str) -> Tuple[str, bool, int, str]:
    """A bullet's approximate syntactic shape: opening-verb form, participial tail,
    bucketed clause count, trailing-prepositional head."""
    return (_verb_form(leading_verb(text)),
            bool(_PARTICIPIAL_TAIL_RE.search(text)),
            min(len(_CLAUSE_BREAK_RE.findall(text)), _CLAUSE_BUCKET_CAP),
            _tail_preposition(text))


def _describe_skeleton(shape: Tuple[str, bool, int, str]) -> str:
    form, participial, clauses, prep = shape
    parts = [f"{form or 'no'} opening verb form",
             "a participial tail" if participial else "no participial tail",
             f"{clauses} clause break(s)"]
    parts.append(f"a trailing '{prep}' phrase" if prep else "no trailing phrase")
    return ", ".join(parts)


def shape_repetition(item_name: str, bullets: Bullets) -> List[Finding]:
    """Bullets of one item built to the same syntactic skeleton.

    Fires at three matching skeletons, the skill's own threshold. Measured against
    the corpus: 0 of the 60 real items with three or more bullets trip it, so the
    approximation does not mistake correct variation for a pattern.
    """
    pairs = _pairs(bullets)
    if len(pairs) < _SHAPE_MIN_MATCHES:
        return []
    by_shape: Dict[Tuple[str, bool, int, str], List[Tuple[str, str]]] = {}
    for gkey, text in pairs:
        by_shape.setdefault(_skeleton(text), []).append((gkey, text))
    findings = []
    for shape, members in by_shape.items():
        if len(members) < _SHAPE_MIN_MATCHES:
            continue
        findings.append(Finding(
            detector="shape_repetition", tier=P1, item=item_name,
            detail=(f"{len(members)} bullets are built to one sentence skeleton "
                    f"({_describe_skeleton(shape)}). Rebuild all but one of them on "
                    f"a different construction, keeping the opening verb and every "
                    f"number."),
            spans=tuple(_whole_span(gk, text) for gk, text in members)))
    return _order(findings)


# ── 2. length_uniformity ─────────────────────────────────────────────────────
# The skill's burstiness signal, and the row aiwriting.RESUME_PROFILE marks DELEGATED
# ("uniform paragraph length", reinterpreted as uniform bullet length).
#
# THE TRAP, and the reason a floor cannot be guessed. Every bullet here has already
# been trimmed to a per-bullet printed-line target by run._trim_to_caps, and the
# rephrase prompt asks it to FILL that target (measure.FULL_LINE_FILL). Bullets in
# one item are therefore supposed to come out similar lengths: that is the layout
# engine working, not a model being metronomic. Measured over the 60 real items with
# three or more bullets, word-count spread lands as follows.
#
#   absolute population stdev   < 1.0 words   3 of 60      relative spread (stdev
#                               < 2.0 words   8 of 60      over mean) < 0.02  1 of 60
#                               < 2.5 words  14 of 60                 < 0.03  3 of 60
#                               < 3.0 words  26 of 60                 < 0.04  4 of 60
#                                                                     < 0.05  8 of 60
#
# So the intuitive "three words apart is basically identical" floor would fire on 43%
# of items that were already correct and already shipped. It is not mistuned, it is
# the wrong statistic.
#
# Two consequences, both load-bearing:
#
#  1. The measure is RELATIVE (stdev over mean), not absolute. A one-line item at 17
#     words a bullet and a two-line item at 33 show the same relative variation at
#     half the absolute spread, so an absolute floor would fire on the short items and
#     never on the long ones. The unit under test is regularity, which has no unit.
#  2. The floor sits at the bottom of the MEASURED distribution rather than anywhere
#     a reader would call "similar". 0.03 fires on 3 of 60 real items, and at the
#     corpus median of 33 words a bullet it means a spread under one word: counts of
#     32, 33, 34. That is metronomic by any reading, and it is the only part of the
#     real distribution where the signal is not just the line budget doing its job.
#
# Calibrated against 60 items of three or more bullets from 57 resumes this pipeline
# generated and shipped, rather than against invented examples. That distinction is
# the whole point: invented examples are exactly what make an absolute floor of two
# or three words look reasonable.
_UNIFORMITY_MIN_BULLETS = 3
_UNIFORMITY_CV_FLOOR = 0.03
# Below this, the ratio stops meaning anything: at two words a bullet a single word
# of difference is a 35% swing, so every such item would look "varied" and no item
# would ever be flagged. Nothing this short is a real bullet either, so the guard
# only ever catches degenerate input.
_UNIFORMITY_MIN_MEAN_WORDS = 3.0


def length_uniformity(item_name: str, bullets: Bullets) -> List[Finding]:
    """Bullets of one item that all run to the same length.

    Needs three bullets: a spread computed over two samples is not a measurement of
    rhythm, it is the difference between two numbers.
    """
    pairs = _pairs(bullets)
    if len(pairs) < _UNIFORMITY_MIN_BULLETS:
        return []
    counts = [len(text.split()) for _, text in pairs]
    mean = statistics.fmean(counts)
    if mean < _UNIFORMITY_MIN_MEAN_WORDS:
        return []
    spread = statistics.pstdev(counts) / mean
    if spread >= _UNIFORMITY_CV_FLOOR:
        return []
    return [Finding(
        detector="length_uniformity", tier=P2, item=item_name,
        detail=(f"All {len(counts)} bullets run to nearly one length "
                f"({', '.join(str(c) for c in counts)} words, a spread of "
                f"{spread:.1%} of the mean). Let at least one bullet come out "
                f"clearly shorter than the rest, within its own character cap."),
        spans=tuple(_whole_span(gk, text) for gk, text in pairs))]


# ── 3. rule_of_three ─────────────────────────────────────────────────────────
# "at most one 'adjective, adjective, and adjective' or three-verb train in the whole
# letter" is what the vendored letter arm already tells the model
# (aiwriting.RULES_PROMPT item 12). An item is the resume's unit of the same size, so
# the threshold carries over unchanged: one series is writing, two is a habit.
#
# One regex covers both forms the rule names. A three-verb train ("designed, built,
# and shipped") and a three-noun list ("finance, operations, and support") differ only
# in what fills the slots, and nothing here can tell a verb from a noun anyway.
#
# Each slot takes up to three words so the pattern reaches real list items ("the
# ingestion stage") without running away across a whole clause. The Oxford comma is
# optional, since both spellings appear.
_SERIES_SLOT = r"[A-Za-z0-9][\w\-/+%]*(?:\s+[A-Za-z0-9][\w\-/+%]*){0,2}"
_RULE_OF_THREE_RE = re.compile(
    rf"\b{_SERIES_SLOT},\s+{_SERIES_SLOT},?\s+(?:and|or)\s+{_SERIES_SLOT}\b", re.I)
# A match that swallows a sentence boundary is not a series, it is two sentences that
# happen to have commas in them. Rejecting the match keeps the offsets intact, which
# splitting the bullet first would not.
_SENTENCE_END_RE = re.compile(r"[.!?]\s")
_RULE_OF_THREE_MAX = 1


def _series_matches(text: str) -> List[re.Match]:
    return [m for m in _RULE_OF_THREE_RE.finditer(text)
            if not _SENTENCE_END_RE.search(m.group(0))]


def rule_of_three(item_name: str, bullets: Bullets) -> List[Finding]:
    """More than one three-part series across an item's bullets.

    Fires on 37 of the corpus's 445 items. That rate is not alarming for a P2 finding
    that is reported and never auto-repaired this cycle, and it is what the vendored
    rule says: a resume that stacks two of these in one entry is doing it on reflex.
    """
    spans: List[Span] = []
    for gkey, text in _pairs(bullets):
        for match in _series_matches(text):
            spans.append(Span(gkey, match.start(), match.end(), match.group(0)))
    if len(spans) <= _RULE_OF_THREE_MAX:
        return []
    return [Finding(
        detector="rule_of_three", tier=P2, item=item_name,
        detail=(f"{len(spans)} three-part series in one item. Keep the one that "
                f"earns it and state the others plainly, as two items or as a "
                f"single claim."),
        spans=tuple(spans))]


# ── 4. noun_cycling ──────────────────────────────────────────────────────────
# The skill: "AI either repeats the same word mechanically or cycles through synonyms
# conspicuously. Human writers repeat when the word is right and vary when it's
# natural." Both halves are implemented, because only having one of them would push
# the text straight into the other.
#
# THE TRAP, again a false-positive one. An item is one job or one project, so its
# bullets legitimately share the subject's name and its domain nouns. Measured over
# the 60 real items with three or more bullets, the number of content nouns appearing
# in EVERY bullet of an item comes out: 0 nouns for 16 items, 1 noun for 20, 2 nouns
# for 24, and never 3. So "a noun repeated in every bullet" describes 73% of correct
# items and is worthless as a signal, while three such nouns describes none of them
# and is the point where repetition stops being the subject matter and starts being a
# tic. Both thresholds below are the measured ones: each fires on 0 of 60.
#
# LEADING VERBS ARE EXCLUDED, each bullet's own from its own token set.
# compose.dedupe_leading_verbs already guarantees the openers are distinct, so
# counting them would report a property the pipeline enforces on purpose. Excluding
# only a bullet's OWN opener keeps the real signal: an opener that also turns up in
# the middle of a sibling bullet is repetition, and still counts there.
_NOUN_MIN_LENGTH = 4
_NOUN_CYCLING_MIN_SHARED = 3
# Shared by "most" bullets, spelled as a ratio so the rule does not become
# unreachable on a longer item. At three bullets it rounds to all three, which is
# the only size the corpus contains and therefore the only size the 0-of-60
# measurement covers.
_NOUN_SHARE_RATIO = 0.8

# Only closed-class function words that are long enough to survive the
# _NOUN_MIN_LENGTH filter and would otherwise be counted as content. The length
# filter already removes the short ones (the, a, of, in, to, for, on, at, by), so
# this list stays short on purpose: every entry is a word that can never be the
# subject of a bullet, and a longer list would start hiding real repetition.
_STOPWORDS: FrozenSet[str] = frozenset({
    "about", "across", "after", "against", "also", "although", "among", "around",
    "because", "been", "before", "being", "below", "between", "both", "during",
    "each", "either", "every", "from", "have", "here", "into", "less", "many",
    "more", "most", "much", "neither", "only", "onto", "other", "over", "same",
    "since", "some", "such", "than", "that", "their", "them", "then", "there",
    "these", "they", "this", "those", "through", "throughout", "under", "until",
    "upon", "using", "were", "what", "when", "where", "which", "while", "whose",
    "with", "within", "without", "would",
})

# Near-synonym sets, for the cycling half of the rule. Deliberately small and
# domain-specific: these are the rotations a model reaches for when told to avoid
# repeating itself in a resume, and every member is a word the per-bullet gate lets
# through. Nothing compose._STYLE_BANS already deletes is listed (leverage, utilize,
# harness, streamline), since a banned word never survives long enough to be cycled
# and listing it here would only put a banned string in a module whose text can reach
# a prompt.
_SYNONYM_GROUPS: Dict[str, FrozenSet[str]] = {
    "build": frozenset({"built", "created", "developed", "designed", "constructed",
                        "engineered", "crafted", "assembled", "produced"}),
    "improve": frozenset({"improved", "enhanced", "optimized", "refined", "boosted",
                          "strengthened", "upgraded", "polished"}),
    "reduce": frozenset({"reduced", "cut", "lowered", "decreased", "trimmed",
                         "slashed", "shrank", "minimized"}),
    "increase": frozenset({"increased", "raised", "grew", "expanded", "scaled",
                           "widened"}),
    "manage": frozenset({"managed", "led", "directed", "oversaw", "coordinated",
                         "headed", "supervised", "ran"}),
    "system": frozenset({"system", "systems", "platform", "platforms", "framework",
                         "frameworks", "infrastructure", "stack", "architecture"}),
    "tool": frozenset({"tool", "tools", "utility", "utilities", "service",
                       "services", "application", "applications", "component",
                       "components"}),
    "speed": frozenset({"speed", "latency", "throughput", "performance", "runtime"}),
    "team": frozenset({"team", "teams", "group", "groups", "cohort", "squad"}),
}
# Three distinct members across three distinct bullets. Two members in two bullets is
# ordinary English (2 of the 60 real items do that) and would be noise.
_SYNONYM_MIN_MEMBERS = 3
_SYNONYM_MIN_BULLETS = 3


def _content_sets(pairs: Sequence[Tuple[str, str]]) -> List[FrozenSet[str]]:
    """Per bullet, the content words it contributes: long enough, not a function
    word, and not that bullet's own opening verb."""
    out = []
    for _, text in pairs:
        opener = leading_verb(text)
        out.append(frozenset(
            w for w in _tokens(text)
            if len(w) >= _NOUN_MIN_LENGTH and w not in _STOPWORDS and w != opener))
    return out


def _word_sets(pairs: Sequence[Tuple[str, str]]) -> List[FrozenSet[str]]:
    """Per bullet, every word it contributes minus its own opening verb.

    The synonym arm uses this rather than `_content_sets`: its members are an
    explicit list, so the length filter and the stopword list would only be able to
    delete entries it names on purpose ("cut", "led", "team").
    """
    return [frozenset(_tokens(text)) - {leading_verb(text)} for _, text in pairs]


def _occurrences(gkey: str, text: str, word: str) -> List[Span]:
    pattern = re.compile(rf"\b{re.escape(word)}\b", re.I)
    return [Span(gkey, m.start(), m.end(), m.group(0)) for m in pattern.finditer(text)]


def noun_cycling(item_name: str, bullets: Bullets) -> List[Finding]:
    """Mechanical repetition of content nouns, and conspicuous synonym rotation."""
    pairs = _pairs(bullets)
    if len(pairs) < _NOUN_CYCLING_MIN_SHARED:
        return []
    findings: List[Finding] = []

    # (a) the same word in most bullets of the item.
    needed = max(_NOUN_CYCLING_MIN_SHARED, ceil(_NOUN_SHARE_RATIO * len(pairs)))
    sets = _content_sets(pairs)
    tally: Dict[str, int] = {}
    for content in sets:
        for word in content:
            tally[word] = tally.get(word, 0) + 1
    shared = sorted(word for word, seen in tally.items() if seen >= needed)
    if len(shared) >= _NOUN_CYCLING_MIN_SHARED:
        spans = [span for word in shared
                 for gkey, text in pairs for span in _occurrences(gkey, text, word)]
        findings.append(Finding(
            detector="noun_cycling", tier=P1, item=item_name,
            detail=(f"{len(shared)} words repeat across the item's bullets "
                    f"({', '.join(shared)}). Name the thing once, then use the "
                    f"concrete detail that belongs to each bullet."),
            spans=tuple(sorted(spans, key=lambda s: (s.gkey, s.start)))))

    # (b) three members of one synonym set, spread across three bullets.
    word_sets = _word_sets(pairs)
    for group, members in sorted(_SYNONYM_GROUPS.items()):
        present: Dict[str, List[Span]] = {}
        bullets_hit = 0
        for (gkey, text), content in zip(pairs, word_sets):
            hit = content & members
            if hit:
                bullets_hit += 1
            for word in hit:
                present.setdefault(word, []).extend(_occurrences(gkey, text, word))
        if len(present) < _SYNONYM_MIN_MEMBERS or bullets_hit < _SYNONYM_MIN_BULLETS:
            continue
        spans = [span for word in sorted(present) for span in present[word]]
        findings.append(Finding(
            detector="noun_cycling", tier=P1, item=item_name,
            detail=(f"Conspicuous synonym rotation in this item: "
                    f"{', '.join(sorted(present))} all stand for the same idea "
                    f"({group}). Repeat the right word, or name the specific thing "
                    f"each bullet is about."),
            spans=tuple(sorted(spans, key=lambda s: (s.gkey, s.start)))))
    return _order(findings)


# ── 5. bare_noun_bullet ──────────────────────────────────────────────────────
# aiwriting.RESUME_PROFILE marks "bullet-np lists" EXTRA_STRICT and drops upstream's
# "five consecutive verbless items" threshold to one, on the argument that upstream
# is tuned for prose that happens to contain a list while here every line IS a list
# item. The profile states the repair instruction; this is the detector.
#
# No POS tagger, so "has a finite verb" is approximated as "has a token that is a
# past-tense verb or a copula". The approximation runs in the SAFE direction: it
# claims a verb on thin evidence and so under-reports, because a false positive here
# means telling the model to add a verb to a bullet that already has one.
#
#  * Anything ending in "-ed" counts, adjectives included. "Advanced analytics
#    coverage" is a bare-noun bullet this will miss. Distinguishing the participle
#    from the adjective needs the parse that is not available, and the miss costs
#    nothing while the reverse costs a repair call.
#  * A gerund is NOT finite, so "Building the ingestion layer" is flagged. That is
#    the correct reading of the rule: the bullet still asserts nothing checkable.
#  * The register is assumed to be past tense, which the whole pipeline enforces
#    (the rephrase prompt asks for it and every verb in active_words.md is past
#    tense), so a present-tense "Maintains 40 dashboards" would be flagged. The
#    sweep skips verbatim bullets, which is the only place a user's own present-tense
#    text could enter.
#
# Measured against the corpus: 0 of 886 real bullets are flagged.


def _has_finite_verb(text: str) -> bool:
    for token in text.split():
        word = token.strip(_EDGE_PUNCT).lower()
        if not word:
            continue
        if word.endswith("ed") or word in _IRREGULAR_PAST or word in _AUXILIARIES:
            return True
    return False


def bare_noun_bullet(item_name: str, bullets: Bullets) -> List[Finding]:
    """Bullets with no finite verb, which state a label instead of a claim."""
    findings = []
    for gkey, text in _pairs(bullets):
        if _has_finite_verb(text):
            continue
        findings.append(Finding(
            detector="bare_noun_bullet", tier=P1, item=item_name,
            detail=("This bullet carries no finite verb, so it asserts nothing a "
                    "reader can check. Rewrite it as a past-tense claim about a "
                    "named system, with the number that makes it checkable."),
            spans=(_whole_span(gkey, text),)))
    return _order(findings)


# ── the item pass ────────────────────────────────────────────────────────────
# Order is fixed and public: it is the order findings reach the repair payload, and a
# detector missing from this tuple never runs no matter that it exists.
DETECTORS: Tuple[Tuple[str, Detector], ...] = (
    ("shape_repetition", shape_repetition),
    ("length_uniformity", length_uniformity),
    ("rule_of_three", rule_of_three),
    ("noun_cycling", noun_cycling),
    ("bare_noun_bullet", bare_noun_bullet),
)


def item_findings(item_name: str, bullets: Bullets) -> List[Finding]:
    """Every item-level finding for one entry and its bullets.

    `bullets` is [(gkey, text), ...]. An empty item, a one-bullet item and an item of
    blank strings all come back as [] rather than raising: this runs over whatever
    the selection produced, and a detector that crashes on a thin item takes the
    whole tailor run down with it.
    """
    out: List[Finding] = []
    for _, detector in DETECTORS:
        out.extend(detector(item_name, bullets))
    return out


def findings_payload(findings: Sequence[Finding]) -> List[Dict[str, object]]:
    """The findings as plain JSON-serializable dicts, for the repair prompt's body.

    A NamedTuple survives `repr` but not `json.dumps` in a shape anything can read
    back, and the findings exist to be handed to a model, so the conversion lives
    here next to the type rather than at the call site.
    """
    return [{"detector": f.detector, "tier": f.tier, "item": f.item,
             "detail": f.detail,
             "spans": [{"gkey": s.gkey, "start": s.start, "end": s.end,
                        "text": s.text} for s in f.spans]}
            for f in findings]
