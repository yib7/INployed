"""The item-level AI-writing sweep: one model call per resume entry.

``compose.enforce_style`` repairs ONE bullet at a time, so it is structurally
blind to every tell that only exists across an item's bullets: one sentence
skeleton reused down the list, every bullet the same length, a three-part series
in each line, one noun cycling through all of them. ``itemcheck`` measures those.
This module is what acts on them, and its unit of work is therefore the ITEM: one
Experience / Projects / Leadership entry together with all of its bullets,
grouped by ``compose._blocks_in_order``. A per-bullet call could not see the
signal at all, which is the whole argument for the feature.

It lives in its own module rather than in ``compose``, which is already large
enough that a reader looking for the per-bullet gate should not have to walk past
a second, differently-shaped one to find it.

── what runs, and when ──────────────────────────────────────────────────────

**Every item, every run.** The toggle is on unconditionally, chosen over cheaper
detect-then-repair gating. The detectors are deterministic and lexical, so they
find what they were written to find; the judgment arm is what they cannot see,
and gating the call on a deterministic hit would only ever send the model the
cases the code had already named. The findings still ride in the payload, because
they make the repair TARGETED. A free rewrite is how a grounded bullet drifts off
its atoms, and every guard below exists to make that drift impossible to commit.

**P0 and P1 are repaired. P2 is reported.** That is this cycle's frozen answer,
and it is why only P1 findings reach the prompt: a P2 finding the model can see
is a P2 finding the model will fix. The P2 material comes back in the result for
the run report to print. The P0 material is lexical and rides in
``aiwriting.RESUME_RULES_PROMPT`` rather than in a finding.

── the acceptance check, which is the layout guarantee ──────────────────────

A rewrite is committed only when ALL of these hold against the text it replaces.
Anything else keeps the original, which was already clean and already fitting.

  1. it is non-empty
  2. ``measure.line_count(new) <= target_lines`` for that bullet
  3. it adds no ``compose.style_violations`` and no ``aiwriting.resume_violations``
     (the two name lists are disjoint by SP1's design, so they concatenate)
  4. ``itemcheck.leading_verb`` is unchanged
  5. it loses no number and no distinctive token the original carried

(2) is the layout invariant. Every committed bullet renders within the same
per-bullet budget ``run._trim_to_caps`` enforces, so the item's total printed line
count is non-increasing across this pass and ``compile.enforce_one_page`` can
never be pushed over by it. Nothing is trimmed here: a clause-boundary trim on
freshly rewritten text is where mid-sentence bullets come from.

(4) pins the opening verb exactly as ``compose.enforce_style`` does.
``compose.dedupe_leading_verbs`` runs earlier and guarantees every opener on the
page is distinct; letting this pass change one would silently break that. It
costs nothing, because opener repetition is already solved and the shape tell
being chased lives downstream of the verb.

(5) is the plan's fifth condition, and it is stated here because the plan named
the guarantee ("a rewrite that drops a number or an atom token is not committed")
while attributing it to the grounding gate, which cannot deliver it.
``verify.enforce_grounded`` reverts a bullet that INTRODUCES a token with no
trace in the atoms. Losing a number is the opposite direction and passes that
gate untouched. So the check is made here, against the text being replaced,
reusing ``verify.unseen_tokens`` with the arguments swapped: the tokens of the
ORIGINAL that have no trace in the REWRITE are exactly the facts the rewrite
dropped. Reusing that function rather than writing a second tokenizer keeps one
calibrated notion of "distinctive token" in the package (numbers on digit
boundaries, words on one word boundary, the opening verb slot skipped).

Condition (5) is deliberately strict, and its false positives are safe by
construction: a rejected bullet keeps text that was already grounded, already
clean and already fitting. A repair for ``noun_cycling`` that drops a repeated
proper noun is the case it will refuse most often, and refusing it costs one
unrepaired P1 finding while accepting it could cost a fact.

── the bounded re-ask ───────────────────────────────────────────────────────

Bullets rejected specifically for overflow buy ONE further call for that item,
naming each one's exact character overage, held to the same five conditions.
Then it stops. Never a third call and never a trim. A bullet that fails twice
keeps its original text.

── failure ──────────────────────────────────────────────────────────────────

Advisory, never fatal, exactly like ``compose.enforce_style``,
``compose.block_briefs`` and ``compose.fill_underfull``. A raised call leaves
every bullet in that item as it was and the sweep moves to the next item.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import aiwriting, compose, config, itemcheck, measure, verify
from .common import _PRINCIPLE
from .llm import as_dict, call

log = logging.getLogger(__name__)

# Why a rewrite was refused. Public, because the run report names them and a
# caller matching on a string literal would drift the first time one is reworded.
REASON_EMPTY = "empty"
REASON_OVERFLOW = "overflow"
REASON_STYLE = "style"
REASON_VERB = "verb"
REASON_FACTS = "facts"

# The order the acceptance conditions are tested in, which is also the order the
# plan states them. It matters for one reason: the reported reason is the FIRST
# condition that failed, and the bounded re-ask fires on REASON_OVERFLOW. Putting
# overflow second means a rewrite that is both too long and otherwise wrong is
# still offered the shortening call, and the same five conditions judge whatever
# comes back.


class Rejection(NamedTuple):
    """One rewrite that was refused, and what the run report says about it."""
    gkey: str
    item: str
    reason: str
    detail: str


class Lingering(NamedTuple):
    """A deterministic phrasing hit that was flagged and is still on the page.

    The sweep names these in the payload and asks for them to be repaired, and the
    model is free to decline. Nothing else in the result records that: a refused
    rewrite lands in `rejected`, but a rewrite that never came back at all looks
    identical to a bullet nobody had anything to say about. So the one case the
    feature exists to catch would have been the one case it reported nothing for.
    Measured on the text that ships, so a repair that worked leaves no entry.
    """
    gkey: str
    item: str
    names: Tuple[str, ...]


class SweepResult(NamedTuple):
    """What SP4's run report is written from.

    ``changed`` and ``rejected`` are the outcome per bullet AFTER the re-ask, so a
    bullet that overflowed and was then shortened successfully appears in
    ``changed`` alone. ``unfixed_p2`` holds the P2 findings measured on the text
    the sweep leaves behind, which is the honest list: a P1 repair occasionally
    clears a P2 tell on its way past.
    """
    changed: Tuple[str, ...]
    rejected: Tuple[Rejection, ...]
    reasked: Tuple[str, ...]
    unfixed_p2: Tuple[itemcheck.Finding, ...]
    unfixed_phrasing: Tuple[Lingering, ...]
    calls: int
    items: int
    failures: Tuple[str, ...]


# ── the prompts ──────────────────────────────────────────────────────────────
# Both are held to the rules they state: no em dash, no spaced double hyphen, and
# none of the phrasings compose.BANNED_PHRASING forbids. A model copies the
# punctuation and the sentence shapes it is shown, and a copied tell costs a
# repair call on a bullet that was already correct.
# tests/test_prompt_hygiene.py scans the whole package; tests/test_item_sweep.py
# runs the same scan directly over the two constants below.
#
# The job description is absent on purpose. This pass repairs phrasing and never
# tailors toward the posting, so the untrusted JD would add tokens, cost and
# injection surface for nothing. `jd` is still a parameter of `sweep_items`,
# unused, because every other bullet stage in this package takes it (including
# `compose.enforce_style`, which ignores it for the same reason) and SP4's pass
# wiring hands the whole context to each stage uniformly.
_SWEEP_SYSTEM = (
    "You clean AI-writing tells out of the bullets of ONE resume entry. You get "
    "the entry and all of its bullets together, because the tells worth catching "
    "live ACROSS an item's bullets: one sentence skeleton reused down the list, "
    "every bullet the same length, a three-part series in each line, one noun "
    "cycling through all of them. A single bullet read alone shows none of that.\n"
    "SCOPE. Repair two things and nothing else. The item's FINDINGS, which are the "
    "tells measured across the whole entry. And each bullet's own PHRASING list, "
    "which names a deterministic rule that bullet broke by itself, so a bullet "
    "carrying one is always worth repairing. A bullet whose findings and phrasing "
    "are both empty comes back exactly as it arrived. Return every bullet you were "
    "given, keyed by its gkey, repaired or unchanged.\n"
    "KEEP, in every bullet you do touch:\n"
    "1. Every fact, number, tool and proper name, spelled the same way. Add "
    "nothing. Drop nothing. A bullet's 'atoms' are the only facts it may state, "
    "and a rewrite that loses one of them is thrown away.\n"
    "2. The OPENING VERB, character for character. An earlier stage already made "
    "every opener on the page distinct, and a swap here breaks that guarantee.\n"
    "3. The length. Each bullet gives 'max_chars'. Stay at or under it. A longer "
    "bullet wraps onto an extra printed line and the resume stops fitting one "
    "page, so an over-long rewrite is discarded and the original ships.\n"
    "4. The register: past tense, one sentence, no first person, no markup.\n"
    "BANNED PHRASING (a bullet using any of these is wrong): "
    + compose.BANNED_PHRASING + "\n"
    + aiwriting.RESUME_RULES_PROMPT + "\n" + _PRINCIPLE
)

# The re-ask. Its opening sentence is also what the transport stub in
# tests/test_item_sweep.py dispatches on, and what a reader greps for.
_REASK_SYSTEM = (
    "Your rewrite of these resume bullets came back too long to print. Each bullet "
    "below carries the text you returned, the character cap it broke, and how many "
    "characters have to come out. Shorten each one so it fits.\n"
    "Cut words, whole clauses and repeated framing. Keep every fact, every number "
    "and every proper name the ORIGINAL bullet carried, keep its opening verb "
    "character for character, and keep the register: past tense, one sentence, no "
    "first person, no markup. A shortened bullet that loses a fact is discarded "
    "and the original ships, so cut phrasing and leave the content alone. This is "
    "the last attempt; a bullet that still overflows keeps its original text.\n"
    "BANNED PHRASING (a bullet using any of these is wrong): "
    + compose.BANNED_PHRASING + "\n"
    + aiwriting.RESUME_RULES_PROMPT + "\n" + _PRINCIPLE
)


# ── measurement helpers ──────────────────────────────────────────────────────
def _fitting_prefix_len(text: str, target_lines: int) -> int:
    """Length of the longest prefix of `text` that still renders within
    `target_lines` printed lines.

    ``measure.line_count`` is monotonic in length, so a binary search is exact.
    The overage the re-ask quotes is ``len(text) - this``, which is a real number
    of characters the model has to remove rather than a guess from a character
    cap that only approximates glyph widths. Same search ``run._fit_to_lines``
    runs before it trims; this one only measures, and never cuts.
    """
    if measure.line_count(text) <= target_lines:
        return len(text)
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure.line_count(text[:mid]) <= target_lines:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _violations(text: str) -> List[str]:
    """Every deterministic style name a bullet trips, from both arms.

    The two lists are disjoint by SP1's design (nothing ``compose._STYLE_BANS``
    matches is repeated in ``aiwriting.RESUME_EXTRA_BANS``), so concatenating
    them cannot double-count a single tell.
    """
    return compose.style_violations(text) + aiwriting.resume_violations(text)


class _Spec(NamedTuple):
    """What one bullet is judged against."""
    gkey: str
    text: str
    target_lines: int
    max_chars: int
    atoms: Dict[str, Any]
    findings: Tuple[str, ...]
    # The deterministic per-bullet hits (aiwriting.resume_violations): tier-1
    # vocabulary, hedging, promotional language, significance inflation, vague
    # attribution, a formulaic opening, a chatbot artifact. Held apart from
    # `findings` because the two have different scope. A finding is measured
    # ACROSS the item and names its bullets through spans; a phrasing hit belongs
    # to one bullet on its own. Both have to reach the model. Carrying only the
    # first is what let a bullet opening "Assisted with" ship: the rule caught it,
    # the prompt said to repair only what the findings flagged, and that bullet
    # arrived carrying none.
    phrasing: Tuple[str, ...]


def _accept(new: str, old: str, spec: _Spec) -> Tuple[str, str]:
    """``("", "")`` when `new` may replace `old`, else ``(reason, detail)``.

    `detail` is written for the run report, so it says what was measured rather
    than which branch fired.
    """
    new = (new or "").strip()
    if not new:
        return REASON_EMPTY, "the model returned no text"
    lines = measure.line_count(new)
    if lines > spec.target_lines:
        over = len(new) - _fitting_prefix_len(new, spec.target_lines)
        return REASON_OVERFLOW, (f"renders on {lines} printed lines against a budget "
                                 f"of {spec.target_lines}, {over} characters too long")
    fresh = [v for v in _violations(new) if v not in _violations(old)]
    if fresh:
        return REASON_STYLE, "introduced " + ", ".join(fresh)
    if itemcheck.leading_verb(new) != itemcheck.leading_verb(old):
        return REASON_VERB, (f"changed the opening verb from "
                             f"{itemcheck.leading_verb(old)!r} to "
                             f"{itemcheck.leading_verb(new)!r}")
    # The tokens of the ORIGINAL with no trace in the rewrite: what was dropped.
    dropped = verify.unseen_tokens(old, new)
    if dropped:
        return REASON_FACTS, "dropped " + ", ".join(dropped)
    return "", ""


# ── payload construction ─────────────────────────────────────────────────────
def _specs(gkeys: Sequence[str], bullets: Dict[str, str],
           targets: Dict[str, int], gm: Dict[str, List[str]],
           findings: Sequence[itemcheck.Finding]) -> List[_Spec]:
    """One `_Spec` per live, non-verbatim bullet of the item, in print order.

    ``_blocks_in_order`` already excludes verbatim groups (it reads
    ``group_map``), and the guard below repeats that check because every other
    pass in this pipeline states it at its own call site: the user opted into
    their exact text, and a future refactor of the grouping must not be able to
    hand it to a rewriter by accident.
    """
    touched: Dict[str, List[str]] = {}
    for finding in findings:
        for span in finding.spans:
            names = touched.setdefault(span.gkey, [])
            if finding.detector not in names:
                names.append(finding.detector)
    out: List[_Spec] = []
    for gk in gkeys:
        if gk not in bullets or compose.is_verbatim_gkey(gk):
            continue
        target = targets.get(gk, config.PROJECT_BULLET_LINES)
        out.append(_Spec(gkey=gk, text=bullets[gk], target_lines=target,
                         max_chars=measure.char_budget(target),
                         atoms={a: compose._atom_payload(a) for a in gm.get(gk, [])},
                         findings=tuple(touched.get(gk, ())),
                         phrasing=tuple(aiwriting.resume_violations(bullets[gk]))))
    return out


def _sweep_body(item: str, specs: Sequence[_Spec],
                findings: Sequence[itemcheck.Finding]) -> Dict[str, Any]:
    """The item as the model sees it.

    Findings are carried at the ITEM level, in full, because each one already
    names the bullets it is about through its spans; each bullet then lists the
    detector names that touch it, as an index into that list. Repeating a
    finding's whole `detail` under every bullet it spans would triple the prompt
    for one sentence of content.
    """
    return {
        "item": item,
        "findings": itemcheck.findings_payload(findings),
        "bullets": [
            {"gkey": s.gkey, "text": s.text, "findings": list(s.findings),
             "phrasing": list(s.phrasing),
             "lines": measure.line_count(s.text), "max_lines": s.target_lines,
             "max_chars": s.max_chars, "atoms": s.atoms}
            for s in specs
        ],
    }


def _reask_body(item: str, entries: Sequence[Tuple[_Spec, str]]) -> Dict[str, Any]:
    """The over-long rewrites, each with the exact number of characters to lose."""
    return {
        "item": item,
        "bullets": [
            {"gkey": s.gkey, "original": s.text, "rewritten": text,
             "max_chars": s.max_chars, "max_lines": s.target_lines,
             "too_long_by": len(text) - _fitting_prefix_len(text, s.target_lines),
             "atoms": s.atoms}
            for s, text in entries
        ],
    }


def _proposals(out: Dict[str, Any], allowed: Sequence[str]) -> Dict[str, str]:
    """``{gkey: text}`` for the entries of a response that name a bullet we sent.

    A BLANK text is kept rather than filtered out, so the acceptance check can
    refuse it as an empty rewrite. A bullet absent from this dict was not answered
    at all, which is a different thing and keeps its text with no verdict recorded.
    """
    result: Dict[str, str] = {}
    for b in out.get("bullets", []) or []:
        if not isinstance(b, dict):
            continue
        gk = b.get("gkey")
        if gk in allowed:
            result[gk] = (b.get("text") or "").strip()
    return result


_SWEEP_CLOSING = (
    "Repair the bullets the FINDINGS name and return the rest as they arrived. Keep "
    "every fact and number, keep each opening verb, and stay within each bullet's "
    "'max_chars'."
)
_REASK_CLOSING = (
    "Return each bullet shortened to fit its 'max_chars', keeping every fact and "
    "number the 'original' carried and keeping its opening verb."
)


def _user_prompt(job_title: str, body: Dict[str, Any], closing: str) -> str:
    return f"""TARGET JOB: {job_title}

{json.dumps(body, ensure_ascii=False, indent=1)}

{closing}

Return ONLY JSON: {{"bullets": [{{"gkey": "<gkey>", "text": "<bullet>"}}, ...]}}"""


# The two calls are written out separately rather than funnelled through one
# helper taking `system` as a parameter, and the duplication is deliberate.
# tests/test_prompt_hygiene.py finds prompts by walking the ARGUMENTS of
# `llm.call`, so a system prompt handed in as a parameter is invisible to it: the
# trace has no way back from a parameter to the constant a caller passed. Naming
# the constant at the call site is what keeps both prompts inside that scan, and
# the scan passing vacuously is the exact failure a prior cycle already found once
# (aiwriting.RULES_PROMPT, invisible for the same reason).
def _ask_sweep(job_title: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return as_dict(call(_SWEEP_SYSTEM,
                        _user_prompt(job_title, body, _SWEEP_CLOSING),
                        config.TIER_FLASH, json_out=True, temperature=0.2), "bullets")


def _ask_reask(job_title: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return as_dict(call(_REASK_SYSTEM,
                        _user_prompt(job_title, body, _REASK_CLOSING),
                        config.TIER_FLASH, json_out=True, temperature=0.2), "bullets")


# ── the pass ─────────────────────────────────────────────────────────────────
def sweep_items(jd: str, job_title: str, sel: Dict[str, Any],
                bullets: Dict[str, str]) -> SweepResult:
    """Sweep every non-verbatim item for the AI-writing tells that live across its
    bullets, committing only rewrites that pass all five acceptance conditions.

    Mutates `bullets` in place and returns what the run report is written from.
    `jd` is accepted and unused; see the note above the prompt constants.
    """
    targets = compose.bullet_line_targets(sel)
    gm = compose.group_map(sel)
    changed: List[str] = []
    rejected: Dict[str, Rejection] = {}
    reasked: List[str] = []
    unfixed: List[itemcheck.Finding] = []
    lingering: List[Lingering] = []
    failures: List[str] = []
    calls = items = 0

    for item, gkeys in compose._blocks_in_order(sel):
        pre = itemcheck.item_findings(item, [(gk, bullets[gk]) for gk in gkeys
                                             if gk in bullets])
        # Only P1 reaches the model. P2 the model can see is P2 the model fixes,
        # and this cycle's frozen answer reports P2 rather than repairing it.
        p1 = [f for f in pre if f.tier == itemcheck.P1]
        specs = _specs(gkeys, bullets, targets, gm, p1)
        if not specs:
            continue
        items += 1
        by_gkey = {s.gkey: s for s in specs}
        body = _sweep_body(item, specs, p1)
        calls += 1
        try:
            out = _ask_sweep(job_title, body)
        except Exception as exc:  # noqa: BLE001 - the sweep is advisory, never fatal
            log.warning("sweep: item %r failed, leaving its bullets as they are: %s",
                        item, exc)
            failures.append(item)
            unfixed.extend(f for f in pre if f.tier != itemcheck.P1)
            continue

        item_changed: List[str] = []
        overflow: List[Tuple[_Spec, str]] = []
        first = _proposals(out, by_gkey)
        for spec in specs:
            text = _commit(spec, first.get(spec.gkey), bullets, item,
                           item_changed, rejected)
            if text is not None:
                overflow.append((spec, text))

        if overflow:
            reasked.append(item)
            calls += 1
            try:
                out2 = _ask_reask(job_title, _reask_body(item, overflow))
            except Exception as exc:  # noqa: BLE001 - same advisory contract
                log.warning("sweep: the re-ask for item %r failed, keeping the "
                            "original bullets: %s", item, exc)
                failures.append(item)
                out2 = {}
            second = _proposals(out2, [s.gkey for s, _ in overflow])
            for spec, _ in overflow:
                # No third call: whatever comes back is judged once and that is the end.
                _commit(spec, second.get(spec.gkey), bullets, item, item_changed,
                        rejected, reask=False)

        changed.extend(item_changed)
        if item_changed:
            # Re-measured on the text that ships, so a P2 tell a P1 repair happened
            # to clear is not reported as still present.
            post = itemcheck.item_findings(item, [(s.gkey, bullets[s.gkey])
                                                  for s in specs])
            unfixed.extend(f for f in post if f.tier != itemcheck.P1)
        else:
            unfixed.extend(f for f in pre if f.tier != itemcheck.P1)

        # Measured on the text that ships. A bullet still carrying a rule hit here
        # was flagged, sent, and came back with the hit intact.
        for spec in specs:
            names = aiwriting.resume_violations(bullets.get(spec.gkey, ""))
            if names:
                lingering.append(Lingering(spec.gkey, item, tuple(names)))

    return SweepResult(changed=tuple(changed),
                       rejected=tuple(rejected[gk] for gk in sorted(rejected)),
                       reasked=tuple(reasked), unfixed_p2=tuple(unfixed),
                       unfixed_phrasing=tuple(lingering),
                       calls=calls, items=items, failures=tuple(failures))


def _commit(spec: _Spec, text: Optional[str], bullets: Dict[str, str], item: str,
            changed: List[str], rejected: Dict[str, Rejection], *,
            reask: bool = True):
    """Apply the acceptance check to one proposed rewrite.

    Returns the text to hand to the bounded re-ask (over-long, and `reask` still
    open), otherwise None. `text` is None when the response did not name this
    bullet at all; an UNCHANGED answer is neither a change nor a rejection,
    because the model was asked to return every bullet it was given and "came
    back as it arrived" is the expected answer for a bullet with no finding.

    `rejected` is keyed by gkey so a bullet's FINAL verdict is the one reported: a
    round-one overflow that the re-ask repairs leaves no rejection behind.
    """
    if text is None or text.strip() == spec.text.strip():
        # Deliberately does NOT clear an existing rejection. On the re-ask an
        # unanswered or unchanged bullet means the shortening did not happen, so
        # the overflow verdict from round one is still what the report should say.
        return None
    reason, detail = _accept(text, spec.text, spec)
    if not reason:
        bullets[spec.gkey] = text
        rejected.pop(spec.gkey, None)
        if spec.gkey not in changed:
            changed.append(spec.gkey)
        return None
    rejected[spec.gkey] = Rejection(spec.gkey, item, reason, detail)
    if reask and reason == REASON_OVERFLOW:
        return text
    return None
