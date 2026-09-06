"""Stage 3: the four fixed skill lines + the optional Methods concepts line.

Moved out of compose.py unchanged (cycle 9): the skills stage shares nothing with
the bullet stages but the prompt-fencing helper, so it was 425 lines of unrelated
code sitting in the middle of the bullet pipeline. `compose` re-exports every name
below, so `compose.compress_skills` and friends still resolve.

NOTE for monkeypatching: `call` is bound in THIS module's namespace, so a test that
stubs the LLM for `compress_skills` must patch `skills.call`, not `compose.call`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import assets, ats, config, layout, measure
from .common import fence_jd
from .llm import as_dict, call

# ── Stage 3: skills (exactly 4 fixed categories) ─────────────────────────────
_SKILL_BUCKETS = (
    ("Languages", ("languages",)),
    ("Frameworks", ("frameworks",)),
    ("Developer Tools", ("developer_tools",)),
    ("Libraries", ("libraries",)),
)
# Each line shows the best-N most JD-relevant skills (layout.skill_targets():
# Languages 7, Frameworks 7, Developer Tools 10, Libraries 10). The model ranks;
# _finalize_skill_lines takes the top N, completes from the pool if the model
# under-returns, and trims from the tail until the rendered line fits ONE printed line
# by real glyph width (measure.skill_line_width / SKILL_LINE_CAPACITY). No fill floor —
# a short list of relevant skills stays short.


def _pool(skills: Dict[str, Any], keys: Tuple[str, ...]) -> List[str]:
    out: List[str] = []
    for k in keys:
        out.extend(skills.get(k, []) or [])
    return out


def _skill_pools() -> Dict[str, List[str]]:
    """The four fixed skill lines mapped to their candidate-skill pools."""
    skills = assets.load_master().get("skills", {})
    return {label: _pool(skills, keys) for label, keys in _SKILL_BUCKETS}


def _methods_pool() -> List[str]:
    """The candidate's concepts/methodologies pool — the source of the (optional) 5th
    'Methods' concepts line. Rendered nowhere among the four tool lines, so this is its
    only path to the page."""
    skills = assets.load_master().get("skills", {}) or {}
    return list(skills.get("concepts_and_methodologies", []) or [])


def _clean_methods(raw: Any) -> List[str]:
    """Validate a model 'methods' ranking against the concepts pool: keep only real pool
    concepts (anchored — never invent), printed in the pool's own spelling, de-duplicated
    by normalized concept, model order preserved."""
    pool_by_norm = {ats._norm_skill(p): p for p in _methods_pool()}
    out: List[str] = []
    seen: set[str] = set()
    for item in (raw or []):
        key = ats._norm_skill(str(item))
        spelling = pool_by_norm.get(key)
        if spelling and key not in seen:
            out.append(spelling)
            seen.add(key)
    return out


def _jd_alias_match(concept: str, aliases: List[str], jd: str) -> Tuple[Optional[str], int]:
    """The spelling to PRINT for `concept` given the JD, plus its JD frequency:
      - a DIRECT JD hit (the concept itself, full or paren-stripped) -> (concept, freq)
      - else the most-frequent anchored ALIAS the JD uses            -> (alias, freq)
      - no hit at all                                                -> (None, 0)
    Shared by the Methods concepts line (Tier 1) and the tech-line swap, so both surface the
    JD's own wording the same way: the concept's spelling on a direct hit, the JD's alias
    spelling when only an alias appears."""
    direct = max(len(ats._term_pattern(concept).findall(jd)),
                 len(ats._term_pattern(_paren_strip(concept)).findall(jd)))
    if direct > 0:
        return concept, direct
    printed, freq = None, 0
    for alias in aliases:
        n = len(ats._term_pattern(alias).findall(jd))
        if n > freq:
            printed, freq = alias, n
    return printed, freq


def _swap_to_jd_spelling(token: str, aliases_by_norm: Dict[str, List[str]], jd: str) -> str:
    """Swap a printed technical-skill token to the JD's spelling when the JD uses a PRINTABLE
    alias of it (and not the canonical) — so a literal keyword ATS sees the JD's exact term.
    A direct JD hit or no JD mention keeps the token unchanged; match-only synonyms are absent
    from `aliases_by_norm` so they never swap (the candidate's stronger token stays)."""
    aliases = aliases_by_norm.get(ats._norm_skill(token))
    if not aliases:
        return token
    printed, _freq = _jd_alias_match(token, aliases, jd)
    # printed == token on a direct hit; an alias only on an alias-only hit; None when no hit.
    return printed if (printed and printed != token) else token


def _finalize_skill_lines(out: Dict[str, Any], jd: str = "") -> List[Dict[str, str]]:
    """Resolve the best-N skills per line: take the model's relevance-ranked picks,
    complete from the pool up to the target if it under-returned, then trim from the
    tail (least relevant) to the one-printed-line cap. No fill floor — a short list
    of relevant skills stays short. Always returns the four labeled lines.

    When `jd` is given and tech aliases are enabled, each picked token is swapped to the JD's
    own spelling if the JD uses a printable alias of it (before the width cap, so the swapped
    width is measured)."""
    if not isinstance(out, dict):  # model shape drift: fall through to pool completion
        out = {}
    targets = layout.skill_targets()
    pools = _skill_pools()
    swap = bool(jd) and config.tech_aliases_enabled()
    aliases_by_norm = ({ats._norm_skill(c): al for c, al in ats.anchored_alias_groups()}
                       if swap else {})
    lines: List[Dict[str, str]] = []
    for label, _keys in _SKILL_BUCKETS:
        raw = out.get(label)
        # A common model shape drift returns the line as a JSON array instead of
        # a comma string ("Languages": ["Python","SQL"]); join it rather than
        # discarding the model's relevance ranking (audit P2-2).
        if isinstance(raw, list):
            raw = ", ".join(str(x) for x in raw if str(x).strip())
        picked = _complete_to_count(raw if isinstance(raw, str) else "",
                                    pools.get(label, []), targets.get(label, 0), jd)
        if swap:
            picked = [_swap_to_jd_spelling(tok, aliases_by_norm, jd) for tok in picked]
        items = _cap_items(label, ", ".join(picked))
        if items:
            lines.append({"label": label, "items": items})
    return lines


def _split_skill_tokens(s: str) -> List[str]:
    """Split a comma-joined skills string into tokens WITHOUT splitting on commas inside
    parentheses, so a merged token like 'LLM APIs (Gemini, OpenAI, Claude)' stays ONE item.
    A flat ``split(",")`` shatters it into 3 fragments — which then miscount toward the
    best-N target (a 10-target line stops at ~8 visual items) and can be cut mid-parenthesis."""
    tokens: List[str] = []
    depth = start = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            tokens.append(s[start:i])
            start = i + 1
    tokens.append(s[start:])
    return [t.strip() for t in tokens if t.strip()]


def _paren_list_items(tok: str) -> List[str]:
    """The comma-separated items enumerated inside a token's LAST parenthetical, but ONLY
    when that parenthetical is a LIST (contains a comma) -- e.g. 'LLM APIs (Gemini, OpenAI,
    Claude)' -> ['Gemini', 'OpenAI', 'Claude']. A single-item qualifier like '(conceptual)'
    or '(from scratch)' is NOT a component list, so it returns [] and the base is what must
    anchor. Empty when there is no parenthetical at all."""
    m = re.search(r"\(([^()]*)\)\s*$", tok.strip())
    if not m or "," not in m.group(1):
        return []
    return [p.strip() for p in m.group(1).split(",") if p.strip()]


def _merged_members(tok: str) -> List[str]:
    """The constituent skills a MERGED token asserts, derived from the RAW token (BEFORE
    ats._norm_skill, which would strip the paren content away): the items of a trailing
    '(a, b, c)' paren LIST, plus the '/'-parts of the pre-paren label when it is a
    slash-join. The un-slashed umbrella label itself ('LLM APIs' in 'LLM APIs (Gemini,
    OpenAI, Claude)') is packaging, not a member -- like a '(conceptual)' qualifier it
    anchors nothing by itself. Empty for a bare / qualifier-only token (not merged)."""
    items = _paren_list_items(tok)
    label = re.sub(r"\([^()]*\)\s*$", "", tok).strip() if items else tok
    members = list(items)
    if "/" in label:
        members.extend(p.strip() for p in label.split("/") if p.strip())
    return members


def _skill_base(tok: str) -> str:
    """A token's anchorable base: ats._norm_skill (lowercased, parens stripped) with any
    ' API'/' APIs' WORD removed ('Gemini API' -> 'gemini') -- word-boundary, so 'Rapids'
    is untouched and 'LLM APIs' becomes 'llm', never 'llms'."""
    return re.sub(r"\bapis?\b", "", ats._norm_skill(tok)).strip()


def _base_anchors(base: str, pool_norms: set) -> bool:
    """One normalized base vs the line's pool: a SHORT base (<=2 chars, 'C'/'R'/'Go')
    requires an EXACT pool match so it never false-anchors as a substring of a longer
    entry ('R' in 'JavaScript'); a longer base matches a pool entry by equality or
    either-direction containment ('Postgres' <-> 'PostgreSQL').

    Sanctioned residual: containment is a deliberate tradeoff, not a gap. It is what
    lets alias canonicals anchor without a hardcoded alias table (e.g. 'Postgres' vs
    a pool entry spelled 'PostgreSQL'), but it also means a fabricated >2-char token
    can false-anchor as a substring of an unrelated longer pool entry (e.g. an
    invented 'Java' would anchor against a pool 'JavaScript'). Accepted on purpose;
    <=2-char parts stay exact-match-only so the riskiest (shortest, most collision-
    prone) tokens never get the containment leniency."""
    if not base:
        return False
    if len(base) <= 2:
        return base in pool_norms
    return any(base == n or base in n or n in base for n in pool_norms)


def _anchored(tok: str, pool_norms: set) -> bool:
    """SELECT-AND-NEVER-INVENT gate for one model-picked skill token, checked against THIS
    line's own pool (its normalized forms). A token is anchored -- allowed onto the page --
    iff EVERY skill it asserts traces to the pool:

      * a BARE token or a single '(conceptual)'/'(from scratch)' QUALIFIER: its base
        (_skill_base) must anchor to a pool skill (_base_anchors);
      * a MERGED token (a '/'-join like 'Gemini/OpenAI/Claude API', or an 'X (a, b, c)'
        paren LIST like 'LLM APIs (Gemini, OpenAI, Claude)'): every MEMBER must anchor --
        each slash-part and each paren-list item. The umbrella label is packaging and need
        not be a pool entry itself; a token that IS a pool entry verbatim (a slashed pool
        skill like 'CI/CD', whose short parts have no own entries) always passes first.

    Everything else -- a bare token with no pool relationship ('Rust'), an invented short
    token ('K'), a '(conceptual)' on an invented base, a fabricated merge ('Rust/Zig API',
    'Fake Tools (Foo, Bar)') -- is dropped; the pool-completion then refills the freed
    slot, so the line never loses a count and never gains a fabricated skill."""
    members = _merged_members(tok)
    if not members:
        return _base_anchors(_skill_base(tok), pool_norms)
    if ats._norm_skill(tok) in pool_norms:
        return True
    return all(_base_anchors(_skill_base(m), pool_norms) for m in members)


def _completion_order(pool: List[str], jd: str) -> List[str]:
    """The pool re-ordered for TOP-UP: skills the JD actually names come first (most
    frequent first), then the user's own pool order as a stable tiebreak. Alias-aware --
    a skill counts as JD-relevant when the JD uses the skill OR any anchored alias of it
    (the same match `ats.coverage` uses). With no JD, or no JD hit, the pool order is
    returned unchanged, so a line the model fully answered stays byte-for-byte what it was.

    This only reorders which UNUSED pool skills fill a line the model under-returned; it
    never touches the model's own picks and never lets a non-pool skill in (that gate is
    `_anchored`, upstream). Paired with `_cap_items` best-fit width packing, a
    short-of-target line ends up carrying the most JD-relevant skills that fit."""
    if not jd:
        return list(pool)
    idx = ats.alias_index()

    def _score(cand: str) -> int:
        group = idx.get(ats._norm_skill(cand))
        spellings = group if group else (cand,)
        return sum(len(ats._term_pattern(sp).findall(jd)) for sp in spellings)

    scored = {c: _score(c) for c in pool}
    if not any(scored.values()):
        return list(pool)
    return sorted(pool, key=lambda c: -scored[c])   # stable: 0-score ties keep pool order


def _complete_to_count(items: str, pool: List[str], target: int, jd: str = "") -> List[str]:
    """Best-N selection for one skills line. Start from the model's items in its
    relevance order, but ANCHOR each first: a picked token is kept only if EVERY skill it
    asserts traces to THIS line's pool (`_anchored` -- a bare/'(conceptual)'-qualified
    token must match a pool skill; a merged '/'-join or 'X (a, b, c)' paren-list token
    needs every named member pool-backed). A token the model invented -- any shape -- is
    dropped BEFORE completion so it never reaches the page, enforcing the project's
    select-and-never-invent rule the way the Methods line already does; anchored merged
    forms like 'Gemini/OpenAI/Claude API' and '(conceptual)' qualifiers on real skills are
    preserved verbatim. If fewer than `target` survive, append still-unused pool skills --
    JD-relevant ones first (`_completion_order`), then the user's pool order -- until the
    line has min(target, len(pool)) items, refilling any slot a dropped hallucination
    freed; then cap the count at `target`. No char floor -- the printed-line cap (applied
    later) is the only size limit, so a short list is never padded to fill the
    line."""
    pool_norms = {ats._norm_skill(c) for c in pool}
    pool_norms.discard("")
    picked: List[str] = []
    seen = set()
    for tok in _split_skill_tokens(items):
        if tok.lower() not in seen and _anchored(tok, pool_norms):
            picked.append(tok)
            seen.add(tok.lower())
    if target > 0:
        # atoms already shown: each picked token plus its "/"- and space-delimited parts
        # AND its paren-LIST members, so completing the line never re-adds a skill already
        # inside a merged token ('Gemini' in 'Gemini/OpenAI/Claude API' or in
        # 'LLM APIs (Gemini, OpenAI, Claude)' -- the anchored members ARE pool entries now)
        # -- while single-char skills like 'C'/'R' are NOT falsely matched as substrings
        # of 'JavaScript'.
        present = set()

        def _mark(tok: str) -> None:
            tl = tok.lower()
            present.add(tl)
            present.update(tl.replace("/", " ").split())
            present.update(m.lower() for m in _paren_list_items(tok))

        for p in picked:
            _mark(p)
        for cand in _completion_order(pool, jd):
            if len(picked) >= target:
                break
            if cand.lower() in present:
                continue
            picked.append(cand)
            _mark(cand)
        picked = picked[:target]
    return picked


def _cap_items(label: str, items: str) -> str:
    """Keep whole comma-separated tokens, in order, that fit the rendered skills line (bold
    label + items) on ONE printed line by real glyph width (measure.skill_line_width) —
    never cut mid-token, never wrap. Best-fit: an over-wide token in the middle is SKIPPED,
    not a hard stop, so a shorter token later in the relevance order still claims the
    leftover space instead of the rest of the line being wasted. Kept tokens stay in their
    incoming (relevance) order — skipping only drops, never reorders. The first token is
    always kept (a line is never emptied), so a lone over-wide token still renders rather
    than vanishing. Tokenization is parenthesis-aware (_split_skill_tokens) so a merged
    'X (a, b, c)' token is kept or dropped whole, never cut to an unclosed '...X (a'."""
    toks = _split_skill_tokens(items)
    kept: List[str] = []
    for t in toks:
        if kept and measure.skill_line_width(label, ", ".join(kept + [t])) > measure.SKILL_LINE_CAPACITY:
            continue
        kept.append(t)
    return ", ".join(kept)


def compress_skills(jd: str, job_title: str, sel: Dict[str, Any]) -> List[Dict[str, str]]:
    """Resolve the 4 fixed skill lines.

    Reuses the skills chosen by select() in the same pass when present; only falls
    back to a dedicated flash call if that selection is missing/empty.
    """
    pre = sel.get("skills") if isinstance(sel, dict) else None
    if pre:
        lines = _finalize_skill_lines(pre, jd)
        if lines:
            return lines

    skill_focus = sel.get("skill_focus", "general") if isinstance(sel, dict) else "general"
    pools = _skill_pools()
    system = (
        "Select the candidate's technical skills into EXACTLY FOUR fixed lines: "
        "'Languages', 'Frameworks', 'Developer Tools', 'Libraries'. "
        "Selection only: only include skills present in that line's pool. "
        "RANK each line's pool by relevance to THIS job and return the BEST few, most-relevant "
        "FIRST: aim ~7 Languages, ~7 Frameworks, ~10 Developer Tools, ~10 Libraries, or all of "
        "a smaller pool. Lead with every skill the JD explicitly mentions or strongly implies, "
        "then the strongest complementary skills (adjacent languages, transferable tools). Do "
        "NOT pad with weak/unrelated filler to reach the count. A few sharp skills beat a long "
        "list. You MAY merge closely-related API entries into one compact token (e.g. "
        "'Gemini/OpenAI/Claude API'). Preserve confidence qualifiers like '(conceptual)' / "
        "'(from scratch)' verbatim."
    )
    user = f"""TARGET JOB: {job_title}  (focus hint: {skill_focus})

{fence_jd(jd, 4000, "relevance ranking")}

POOLS (pick each line's items only from its pool):
Languages: {json.dumps(pools["Languages"], ensure_ascii=False)}
Frameworks: {json.dumps(pools["Frameworks"], ensure_ascii=False)}
Developer Tools: {json.dumps(pools["Developer Tools"], ensure_ascii=False)}
Libraries: {json.dumps(pools["Libraries"], ensure_ascii=False)}

Rules:
- Return each line ranked most-relevant-first: aim ~7 Languages, ~7 Frameworks, ~10 Developer Tools, ~10 Libraries, or all of a smaller pool. JD-matching skills first, then adjacent/complementary skills that add signal.
- Don't pad to hit the count with obscure or unrelated items; a few sharp, relevant skills beat a long list. Lead with the items this JOB cares about most.

Return ONLY JSON: {{"Languages": "Python, SQL, R", "Frameworks": "...", "Developer Tools": "...", "Libraries": "..."}}"""
    try:
        out = as_dict(call(system, user, config.TIER_FLASH, json_out=True, temperature=0.1))
    except Exception:
        out = {}
    return _finalize_skill_lines(out, jd)


# ── Methods line (optional 5th concepts line) ────────────────────────────────
def methods_line(jd: str, sel: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Build the optional 'Methods' concepts line: the buzzwords the candidate
    owns, surfaced so an ATS/reader sees them. Two tiers, anchored to the concepts pool —
    never invents, never empty:

      Tier 1 (ATS keywords first, deterministic): for each pool concept the JD references,
        take it — printing the concept's own spelling on a DIRECT JD hit, or the JD's alias
        spelling when only an anchored alias appears (so the page shows the JD's wording).
        Ranked by JD hit frequency, with equal-frequency hits broken by the model's
        role-relevance order (sel['methods']) so a role-defining buzzword (ETL, feature
        engineering) outranks a generic one (collaboration) — alphabetical only as a last tie.
      Tier 2 (pad to target, role-relevant): if Tier 1 is short of the target, append from
        the model's role-relevance ranking (sel['methods'], pool spelling), skipping any
        concept already chosen (dedup by canonical).

    Returns {'label', 'items'} width-capped to ONE printed line, or None when the pool is
    empty or the target is 0 (the label is omitted entirely — never an empty line)."""
    pool = _methods_pool()
    target = layout.skill_targets().get("Methods", 0)
    if not pool or target <= 0:
        return None
    label = config.methods_line_label()
    aliases_by_norm = {ats._norm_skill(c): al for c, al in ats.anchored_alias_groups()}
    # The model's role-relevance order breaks equal-frequency Tier-1 ties (a concept the
    # model didn't rank falls to the back); same ranking also drives Tier-2 padding.
    model_methods = (sel.get("methods") or []) if isinstance(sel, dict) else []
    methods_rank = {ats._norm_skill(m): i for i, m in enumerate(model_methods)}
    unranked = len(methods_rank) + 1

    # Tier 1 — deterministic JD matches, ranked by (frequency, model relevance, name).
    tier1: List[tuple[int, int, str, str]] = []   # (freq, model_rank, printed, canonical norm)
    chosen_norm: set[str] = set()
    for concept in pool:
        cnorm = ats._norm_skill(concept)
        printed, freq = _jd_alias_match(concept, aliases_by_norm.get(cnorm, []), jd)
        if printed and cnorm not in chosen_norm:
            tier1.append((freq, methods_rank.get(cnorm, unranked), printed, cnorm))
            chosen_norm.add(cnorm)
    tier1.sort(key=lambda t: (-t[0], t[1], t[2].lower()))
    chosen = [printed for _f, _r, printed, _c in tier1]

    # Tier 2 — pad from the model's role-relevance ranking (pool spelling), anchored + deduped.
    pool_by_norm = {ats._norm_skill(p): p for p in pool}
    for concept in model_methods:
        if len(chosen) >= target:
            break
        cnorm = ats._norm_skill(str(concept))
        spelling = pool_by_norm.get(cnorm)
        if spelling and cnorm not in chosen_norm:
            chosen.append(spelling)
            chosen_norm.add(cnorm)

    items = _cap_items(label, ", ".join(chosen[:target]))
    if not items:
        return None
    return {"label": label, "items": items}


def _paren_strip(s: str) -> str:
    """Drop a parenthetical qualifier for JD matching: 'Exploratory Data Analysis (EDA)'
    -> 'Exploratory Data Analysis' (the form a JD is likelier to spell)."""
    return re.sub(r"\(.*?\)", "", s).strip()
