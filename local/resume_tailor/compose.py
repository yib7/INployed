"""The composition stages — all bound by SELECT-AND-REPHRASE, NEVER GENERATE.

select()          flash : choose blocks + ordered bullet GROUPS (by atom id) + skill focus
rephrase()        pro   : one bullet per GROUP, faithfully fusing only that group's atoms
compress_skills() flash : exactly 4 fixed-label lines drawn from the taxonomy

This module owns the BULLET stages. `select()` lives in selection.py and
`compress_skills()`/`methods_line()` in skills.py; both are re-exported here, so
`compose.select(...)` still works. Patch the defining module, not this one.

Only the creative first pass (rephrase) and the cover letter run on the PRO tier.
Selection uses flash for constrained rewrites of already-grounded text. Length is
finalized deterministically downstream.

A "group" is a list of 1-3 closely-related atom ids fused into ONE bullet (e.g. an
accuracy gain + the cost cut). Each bullet's group key is "+".join(ids); every bullet
carries its source atom ids so a human can trace it back to the master yaml.
"""
from __future__ import annotations

import json
import logging
import re
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

# `ats`/`layout` are no longer used by the bullet stages, but `compose.ats` and
# `compose.layout` are part of this module's historical surface (call sites read and
# monkeypatch them), so they stay bound here alongside the name re-exports below.
from . import assets, ats, config, layout, measure  # noqa: F401
from .common import _PRINCIPLE, _gkey, fence_jd
from .llm import as_dict, call
# Re-exports. The two seams below moved to their own modules in cycle 9; every
# historical `compose.X` call site (run.py, prep.py, verify.py, the Qt layer, the
# tests) keeps resolving through this module. `selection` and `skills` import from
# `common`, never from `compose`, so this edge stays one-way.
#
# A monkeypatch binds a name in the namespace where the function resolves its
# global, so re-exporting does NOT re-attach a patch: stub `selection.call` /
# `skills.call`, not `compose.call`, when the function under test lives there.
from .selection import (_block_atoms, _block_of, _cap_projects,  # noqa: F401
                        _catalog, _check_required_blocks, _enforce_fixed_counts,
                        _ensure_required_blocks, _experience_guidance, _first_atom,
                        _normalize_selection, _order_fixed_blocks, _project_guidance,
                        _required_blocks, _resize_to_count, bullet_line_targets, select)
from .skills import (_SKILL_BUCKETS, _anchored, _base_anchors,  # noqa: F401
                     _cap_items, _clean_methods, _complete_to_count, _completion_order,
                     _finalize_skill_lines, _jd_alias_match, _merged_members,
                     _methods_pool, _paren_list_items, _paren_strip, _pool, _skill_base,
                     _skill_pools, _split_skill_tokens, _swap_to_jd_spelling,
                     compress_skills, methods_line)

log = logging.getLogger(__name__)

# The ONE shared enumeration of banned AI-tell phrasing. Every prompt that asks a
# model to WRITE prose — rephrase, the style-gate repairs (bullets here, the letter
# body in coverletter), the cover-letter generation — embeds this same list, so the
# bans can never drift apart. The deterministic _STYLE_BANS regexes below enforce
# only the always-slop subset; the context-sensitive tells (scalable/dynamic/smart,
# 'drove X', significant, multiple, end-to-end, grandiosity) live only in this
# prompt text, where model judgment can spare the legitimate technical uses.
#
# Every prompt in this package is itself free of the characters listed here (em
# dashes, spaced double hyphens): a model copies the punctuation it is shown, and
# a copied em dash costs an enforce_style repair call on a bullet that was already
# correct. tests/test_prompt_hygiene.py holds that line.
BANNED_PHRASING = (
    "em dashes; contrast framing ('not X, but Y', 'X, not Y', 'not just', "
    "'rather than', 'instead of'); participial tails (', enabling/ensuring/"
    "allowing/driving/resulting in ...'); buzz adjectives used as filler (robust, "
    "seamless, comprehensive, powerful, innovative, cutting-edge, holistic, "
    "world-class, game-changing, very; and scalable/dynamic/smart when not a "
    "literal technical term); buzzword verbs (leverage, utilize, spearhead, "
    "empower, harness, streamline; and 'drove X' with no number after it); vague "
    "quantifiers (various, numerous, multiple, significant, consistently, "
    "regularly; use the number or cut the claim); grandiosity (guarantees, "
    "'eliminates by construction', 'the real X'); decorative marketing frames "
    "('end-to-end', 'one place', 'all-in-one'); stacked adjectives and "
    "rule-of-three verb trains. State each fact once, plainly; prefer nouns and "
    "numbers over adjectives."
)

# A curated palette of strong, role-relevant action verbs. Replaces the 6KB raw
# PDF dump (jumbled multi-column OCR — weak signal AND expensive): the model only
# needs a clean set of openers, so this is both cheaper and higher-quality.
_CORE_VERBS = (
    "Built, Designed, Engineered, Developed, Implemented, Architected, Automated, "
    "Optimized, Accelerated, Reduced, Improved, Increased, Streamlined, Scaled, "
    "Refactored, Deployed, Integrated, Migrated, Launched, Shipped, Analyzed, "
    "Modeled, Forecasted, Quantified, Evaluated, Validated, Diagnosed, Researched, "
    "Led, Directed, Coordinated, Mentored, Spearheaded, Drove, Owned, Delivered, "
    "Resolved, Standardized, Consolidated, Boosted, Generated, Produced, Trained, "
    "Benchmarked, Prototyped, Instrumented"
)


# How much style exemplar the rephrase prompt carries. Against the curated
# style_exemplar.txt (a handful of bullets, well under 1k chars) this never bites: with a
# curated source the cap is no longer rationing a noisy 3.5KB page dump, it is a guard
# against a user pasting an entire résumé — or against the PDF fallback, which still is
# that page dump — inflating every rephrase call.
#
# The cut is on a LINE boundary, never mid-word. A flat `[:1200]` slice of the PDF extract
# ended the exemplar at "• Proc", so the same prompt that calls a bullet ending mid-clause
# "a failure" was showing the model one; a whole bullet dropped is a cost, a fragment
# taught as an example is a defect.
EXEMPLAR_CHAR_CAP = 1200


def _exemplar_for_prompt(text: str, cap: int = EXEMPLAR_CHAR_CAP) -> str:
    """`text` bounded to `cap` characters, keeping whole lines: lines are taken in order
    while they fit and the first that would overflow ends the exemplar. A single opening
    line longer than `cap` is hard-cut (one over-long line is not a bullet list, and
    something has to give)."""
    text = text.strip()
    if len(text) <= cap:
        return text
    kept: List[str] = []
    used = 0
    for line in text.splitlines():
        used += len(line) + (1 if kept else 0)      # +1 for the joining newline
        if used > cap:
            break
        kept.append(line)
    return "\n".join(kept) if kept else text[:cap]


def _render_verb_palette(verbs: Dict[str, List[str]]) -> str:
    """Render the categorized action verbs as a compact grouped block for the prompt:
    one `Category: v1, v2, ...` line per category, in file order. The model picks a
    category-appropriate opener; the no-reuse rule is enforced separately downstream."""
    return "\n".join(f"{cat}: {', '.join(items)}" for cat, items in verbs.items() if items)


# ── helpers ──────────────────────────────────────────────────────────────────
def _atom_payload(aid: str) -> Dict[str, Any]:
    atom = dict(assets.atoms_by_id()[aid])
    atom.pop("_section", None)
    atom.pop("_block", None)
    return atom


def atom_material_len(ids: List[str]) -> int:
    """Rough count of grounded text available across a group's atoms (string +
    list-of-string fields). Used to decide whether a short bullet could be
    expanded FROM FACTS — if the atoms hold no more material than the bullet
    already shows, a 'lengthen' call could only pad, so skip it."""
    total = 0
    for aid in ids:
        for v in _atom_payload(aid).values():
            if isinstance(v, str):
                total += len(v)
            elif isinstance(v, list):
                total += sum(len(str(x)) for x in v)
    return total


# ── verbatim ("don't tailor — use my exact bullets") ─────────────────────────
# A block the user marked verbatim has its groups replaced (after _normalize_selection)
# with synthetic single-bullet groups whose id is "__verbatim__/<block>/<i>". These
# carry the user's EXACT text: they're excluded from rephrase/cohesion/trim and
# rendered as typed (render._group_bullets just reads the bullets dict by gkey).
_VERBATIM_PREFIX = "__verbatim__"


def is_verbatim_gkey(gk: str) -> bool:
    return isinstance(gk, str) and gk.startswith(_VERBATIM_PREFIX)


def inject_verbatim(sel: Dict[str, Any]) -> Dict[str, str]:
    """Replace the groups of any SELECTED verbatim block with one synthetic group per
    user bullet, and return {gkey: exact_text}. Mutates `sel`; call AFTER select()
    (i.e. after _normalize_selection) so the atom-based fixed-count/resize logic is
    untouched. A block only renders verbatim if it is in the selection (experience and
    leadership are required, so always are; a project must have been selected)."""
    vb = config.verbatim_blocks()
    out: Dict[str, str] = {}
    if not vb:
        return out
    for sec in ("experience", "projects", "leadership"):
        for entry in sel.get(sec, []):
            bullets = vb.get(entry.get("name"))
            if not bullets:
                continue
            groups: List[List[str]] = []
            for i, text in enumerate(bullets):
                gk = f"{_VERBATIM_PREFIX}/{entry['name']}/{i}"
                groups.append([gk])
                out[gk] = text
            entry["groups"] = groups
    return out


def group_map(sel: Dict[str, Any]) -> Dict[str, List[str]]:
    """Ordered {gkey: [atom_ids]} across experience -> projects -> leadership.
    Verbatim groups are excluded — they carry the user's exact text, not atoms, so the
    LLM stages (rephrase) must never see them."""
    gm: "Dict[str, List[str]]" = {}
    for sec in ("experience", "projects", "leadership"):
        for entry in sel.get(sec, []):
            for ids in entry.get("groups", []):
                gk = _gkey(ids)
                if is_verbatim_gkey(gk):
                    continue
                gm[gk] = ids
    return gm


# ── Stage 2: rephrase ────────────────────────────────────────────────────────
def _length_hint(target_lines: int) -> str:
    """A soft floor + hard ceiling for one bullet, both in characters (the model cannot
    measure glyph widths, so the printed-line budget has to be stated as a char count).

    The ceiling is `measure.char_budget`: the real, MEASURED capacity of that many printed
    lines. It is deliberately NOT `target_lines * <chars per line>` — greedy word wrap loses
    part of a line at every break, so capacity is sublinear in the line count and the flat
    multiply invited the model to write past what fits (see measure.char_budget). The floor
    keeps the bullet from sitting stubby: a single-line bullet fills FULL_LINE_FILL of its
    budget, a wrapping bullet fills all but its last line and LAST_LINE_FILL of that one."""
    cap = measure.char_budget(target_lines)
    if target_lines <= 1:
        floor = ceil(measure.FULL_LINE_FILL * cap)
    else:
        share = ((target_lines - 1) + measure.LAST_LINE_FILL) / target_lines
        floor = ceil(share * cap)
    unit = "line" if target_lines == 1 else "lines"
    return (f"about {target_lines} {unit} ({floor}-{cap} characters; aim to fill "
            f"the line(s), never exceed {cap})")


def _blocks_in_order(sel: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
    """[(block_name, [gkey, ...]), ...] for non-verbatim groups, in selection order
    (experience -> projects -> leadership). The grouping rephrase/cohesion key off."""
    gm = group_map(sel)  # excludes verbatim
    order: List[str] = []
    by_block: Dict[str, List[str]] = {}
    for sec in ("experience", "projects", "leadership"):
        for entry in sel.get(sec, []):
            name = entry.get("name", "")
            for ids in entry.get("groups", []):
                gk = _gkey(ids)
                if gk not in gm:  # verbatim
                    continue
                if name not in by_block:
                    by_block[name] = []
                    order.append(name)
                by_block[name].append(gk)
    return [(name, by_block[name]) for name in order]


def _overview_group_index(name: str, groups: List[List[str]]) -> int:
    """Deterministic fallback: the index of the group holding the project's earliest-
    AUTHORED atom (master file order). The master lists each project's overview/headline
    atom first, so this floats the natural intro bullet to the front when the model pass
    is unavailable. Verbatim/unknown ids sort last so a real atom always wins."""
    order = {aid: i for i, aid in enumerate(_block_atoms("projects", name))}
    sentinel = len(order) + 1
    best_idx, best_rank = 0, sentinel + 1
    for idx, g in enumerate(groups):
        rank = min((order.get(a, sentinel) for a in g), default=sentinel)
        if rank < best_rank:
            best_idx, best_rank = idx, rank
    return best_idx


def lead_with_overview(jd: str, job_title: str, sel: Dict[str, Any]) -> None:
    """Reorder each PROJECT's bullet GROUPS so the bullet that introduces the project — its
    high-level "what is this project at a glance" overview — LEADS, instead of a detail bullet
    that select() placed first by JD-relevance. A reader should learn what a project IS before
    the implementation bullets make sense.

    A cheap batched model pass picks the lead from each project's OWN selected bullets (it only
    chooses which existing bullet should lead — it writes no prose and invents nothing). When the
    call fails or returns nothing usable for a project, a deterministic file-order fallback floats
    the project's earliest-authored atom's group to the front, so flow is ALWAYS enforced.

    Mutates `sel` in place. Projects only (experience/leadership keep their template/relevance
    order). Verbatim projects (the user's exact bullets, in the user's order) and single-bullet
    projects are left untouched. Runs BEFORE briefs/rephrase so cohesion framing and the
    per-position line budgets build on the corrected order. Advisory: never fatal."""
    candidates: List[Dict[str, Any]] = []
    payload: List[Dict[str, Any]] = []
    for entry in sel.get("projects", []) or []:
        groups = entry.get("groups", []) or []
        if len(groups) < 2:
            continue
        if any(is_verbatim_gkey(_gkey(g)) for g in groups):
            continue
        candidates.append(entry)
        bullets = [
            {"n": n, "summary": " | ".join(
                str(_atom_payload(a).get("what", "")) for a in g)[:300]}
            for n, g in enumerate(groups, start=1)
        ]
        payload.append({"project": entry["name"], "bullets": bullets})
    if not candidates:
        return

    picks: Dict[str, int] = {}
    system = (
        "You order resume bullets for narrative flow. For each project you are given its "
        "selected bullets, numbered. Pick the ONE bullet that best introduces the project: "
        "the high-level overview a reader needs ('what is this project at a glance') BEFORE the "
        "detail bullets make sense. Return its number. This is PURE ORDERING: you write no "
        "prose, you invent nothing, you only choose which EXISTING bullet should lead.\n" + _PRINCIPLE
    )
    user = f"""TARGET JOB: {job_title}

PROJECTS (each with its selected bullets, numbered):
{json.dumps(payload, ensure_ascii=False, indent=1)}

For each project, return the NUMBER of the bullet that should LEAD (its overview / intro).
Return ONLY JSON: {{"projects": [{{"project": "<name>", "lead": <number>}}, ...]}}"""
    try:
        out = as_dict(call(system, user, config.TIER_FLASH_LITE, json_out=True,
                           temperature=0.0), "projects")
        for p in out.get("projects", []) or []:
            if not isinstance(p, dict):
                continue
            lead = p.get("lead")
            if isinstance(lead, int) and not isinstance(lead, bool):
                picks[p.get("project")] = lead
    except Exception as exc:  # noqa: BLE001 - ordering is advisory; fall back to file order
        log.warning("lead_with_overview: LLM ordering failed, falling back to "
                    "file order: %s", exc)
        picks = {}

    for entry in candidates:
        groups = entry["groups"]
        lead = picks.get(entry["name"])
        if isinstance(lead, int) and 1 <= lead <= len(groups):
            j = lead - 1
        else:
            j = _overview_group_index(entry["name"], groups)
        if j > 0:
            groups.insert(0, groups.pop(j))


def block_briefs(jd: str, job_title: str, sel: Dict[str, Any]) -> Dict[str, str]:
    """One cheap batched call: a 1-2 sentence framing brief per non-verbatim block,
    derived ONLY from that block's selected atoms. The brief is a cohesion aid for
    rephrase (how the block's bullets should share framing / progress, and—when the
    block's purpose isn't self-evident—what high-level context the lead bullet should
    establish). It is NEVER a source of new facts. Returns {block_name: brief}; {} on
    any failure (cohesion is advisory, never fatal)."""
    gm = group_map(sel)
    blocks: List[Dict[str, Any]] = []
    for name, gkeys in _blocks_in_order(sel):
        atoms: List[Dict[str, Any]] = []
        for gk in gkeys:
            atoms.extend(_atom_payload(a) for a in gm[gk])
        if atoms:
            blocks.append({"block": name, "atoms": atoms})
    if not blocks:
        return {}
    system = (
        "You frame resume blocks for cohesion. For each block (one job, project, or "
        "leadership entry), write a 1-2 sentence BRIEF describing how its bullets should "
        "read together: the shared theme, the logical order, and the high-level context the "
        "FIRST bullet should establish when the block's purpose is not obvious from the "
        "atoms (e.g. what a project is at a glance). Derive the brief ONLY from the "
        "given atoms; never introduce a fact, tool, metric, or claim not present in them. "
        "The brief guides phrasing only; it is not itself a bullet."
    )
    user = f"""TARGET JOB: {job_title}

{fence_jd(jd, 2000, "emphasis")}

BLOCKS (each holds the atoms selected for one resume entry):
{json.dumps(blocks, ensure_ascii=False, indent=1)}

Return ONLY JSON: {{"briefs": [{{"block": "<block name>", "brief": "<1-2 sentences>"}}, ...]}}"""
    try:
        out = as_dict(call(system, user, config.TIER_FLASH_LITE, json_out=True,
                           temperature=0.2), "briefs")
    except Exception as exc:  # noqa: BLE001 - cohesion is advisory; fall back to no briefs
        log.warning("block_briefs: LLM briefing failed, falling back to no briefs: "
                    "%s", exc)
        return {}
    names = {b["block"] for b in blocks}
    result: Dict[str, str] = {}
    for b in out.get("briefs", []) or []:
        if not isinstance(b, dict):
            continue
        name, brief = b.get("block"), (b.get("brief") or "").strip()
        if name in names and brief:
            result[name] = brief
    return result


def rephrase(jd: str, job_title: str, sel: Dict[str, Any],
             briefs: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return {gkey: bullet_text} — one bullet per selected group. The payload is
    grouped BY BLOCK and each block carries its optional cohesion `brief`, so the
    block's bullets read as one story (shared framing, no redundancy, logical
    progression) instead of glued-together atoms. Each bullet still gets a soft length
    hint; final length is enforced deterministically later (run._trim_to_caps)."""
    briefs = briefs or {}
    gm = group_map(sel)
    targets = bullet_line_targets(sel)

    def _item(gk: str) -> Dict[str, Any]:
        it: Dict[str, Any] = {"gkey": gk, "atoms": {a: _atom_payload(a) for a in gm[gk]}}
        if gk in targets:
            it["length_target"] = _length_hint(targets[gk])
        return it

    payload = []
    for name, gkeys in _blocks_in_order(sel):
        block_entry: Dict[str, Any] = {"block": name, "bullets": [_item(gk) for gk in gkeys]}
        if briefs.get(name):
            block_entry["brief"] = briefs[name]
        payload.append(block_entry)
    verbs = _render_verb_palette(assets.active_verbs())
    example = _exemplar_for_prompt(assets.example_text())
    system = (
        "You write resume bullets by faithfully RE-PHRASING fact-atoms for a specific job. "
        "Each group is one bullet: if it has multiple atoms, FUSE them into a single dense "
        "line that states only what those atoms say. You are a translator: turn structured "
        "facts into one polished line and invent nothing.\n" + _PRINCIPLE + "\n"
        "COHESION: the bullets are grouped BY BLOCK (one job / project / leadership entry). "
        "Within a block, make the bullets read as ONE coherent story: shared framing and "
        "tense, no two bullets making the same point, ordered so they build logically. When "
        "a block carries a 'brief', follow its framing/ordering; if the brief says the block's "
        "purpose isn't obvious, let the FIRST bullet establish that context using ONLY grounded "
        "atom facts. NEVER move a fact from one group's atoms into another bullet. Each bullet "
        "still re-phrases ONLY its own group's atoms.\n"
        "REDUNDANCY (across the WHOLE resume, block boundaries included): a distinctive number "
        "or metric appears ONCE. When two groups' atoms cite the same figure (an accuracy "
        "percentage, a corpus size), state it in the bullet where it lands hardest and let the "
        "other bullet carry its remaining facts. Vary the nouns: a pet word like 'pipeline' "
        "repeated across many bullets reads templated; after two uses, say what the thing "
        "concretely is instead. Don't end several bullets the same way (e.g. test counts); "
        "fold at most one or two test-coverage claims into the page.\n"
        "STYLE: past tense, no first-person pronouns, no markdown, no LaTeX, NO bold or "
        "italics. One sentence (a fused group may run to ~2 clauses). Each bullet MUST be a "
        "COMPLETE sentence that ends naturally WITHIN its own character budget (the "
        "'length_target' given below). Never write a longer sentence assuming it will be "
        "trimmed; a truncated bullet ending mid-clause is a failure. "
        "BANNED PHRASING (a bullet using any of these is wrong): " + BANNED_PHRASING + "\n"
        "Front-load the result/impact that matters for THIS job. Open every bullet with a "
        "strong action verb chosen from the categorized list below, picking a "
        "category-appropriate verb that matches the atom's real ownership. Every bullet's "
        "opening verb MUST be DISTINCT: never reuse a leading verb anywhere on the resume "
        "(the list is large; there is always an unused, fitting choice). Numbers exactly "
        "as written. Write 'greater than or equal to' style comparisons with the symbols "
        ">= and <= (they are converted to proper math notation later).\n"
        # The two percentages are FORMATTED from measure's constants, never written out as
        # literals: they are the same numbers _length_hint's floor is computed from, and a
        # prompt that states its own copy drifts silently the moment a constant is retuned.
        f"SPACE: a bullet that fits on ONE printed line should fill at least "
        f"~{measure.FULL_LINE_FILL:.0%} of it. "
        "Never leave a stubby half-empty line (fold in more grounded detail from the atoms "
        "or fuse, but NEVER invent facts to pad). A bullet that wraps to multiple lines may "
        f"let its last line run shorter, but it should still be at least "
        f"~{measure.LAST_LINE_FILL:.0%} full."
    )
    user = f"""TARGET JOB: {job_title}

{fence_jd(jd, 2500)}

ACTION VERBS (open each bullet with one of these, grouped by category; pick a
category-appropriate verb matching the atom's real ownership, and use each leading verb at
most ONCE across the whole resume, so no two bullets start with the same verb):
{verbs}

STYLE EXEMPLAR (match this voice, length and density; NEVER copy its facts):
{example}

BLOCKS (write exactly ONE bullet per gkey, re-phrasing ONLY that group's atoms; make
each block's bullets cohere per its 'brief' when present):
{json.dumps(payload, ensure_ascii=False, indent=1)}

LENGTH (hard ceiling): each bullet's "length_target" gives a character cap. Write a
COMPLETE sentence that fits within that cap and ends naturally. A 2-line target
wants a dense, fully-developed line; a 1-line target wants one tight, self-contained
line. Do NOT exceed the cap and do NOT end mid-clause expecting truncation. Never
invent facts to pad and never drop a number to shorten.

Return ONLY JSON: {{"bullets": [{{"gkey": "<gkey>", "text": "<one bullet>"}}, ...]}}"""
    out = as_dict(call(system, user, config.TIER_PRO, json_out=True, temperature=0.25),
                  "bullets")
    result: Dict[str, str] = {}
    for b in out.get("bullets") or []:
        if not isinstance(b, dict):
            continue
        gk, text = b.get("gkey"), (b.get("text") or "").strip()
        if gk in gm and text:
            result[gk] = text
    return result


# ── Stage 2b: unique leading verbs (no opener reused across the resume) ───────
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


def _pick_unused_verb(palette: Dict[str, List[str]], current: str, used) -> str:
    """First palette verb whose lowercase isn't in `used`, preferring the category that
    holds the colliding `current` verb (so the swap stays semantically near), then any
    category. '' only if the entire palette is exhausted."""
    cl = (current or "").lower()
    home = [items for items in palette.values() if any(v.lower() == cl for v in items)]
    for items in home + list(palette.values()):
        for v in items:
            if v.lower() not in used:
                return v
    return ""


def _swap_leading_verb(text: str, repl: str) -> str:
    """Replace the bullet's first word with `repl`, preserving the rest verbatim."""
    parts = (text or "").strip().split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    return f"{repl} {rest}".strip()


def reverb(jd: str, ids: List[str], bad_text: str, used) -> str:
    """Regenerate ONE bullet so it opens with a fresh action verb NOT in `used`, keeping
    every fact/number. Deterministic, cheapest tier — the re-roll arm of dedupe_leading_verbs."""
    atoms = {a: _atom_payload(a) for a in ids}
    palette = _render_verb_palette(assets.active_verbs())
    taken = ", ".join(sorted(used)) or "(none)"
    system = (
        "Rewrite ONE resume bullet so it OPENS WITH A DIFFERENT action verb, keeping every "
        "fact and number identical. " + _PRINCIPLE + "\n"
        "Choose a category-appropriate opening verb from the list that is NOT already used; "
        "do not inflate ownership. Plain text, past tense, no pronouns, no markup, <= ~300 chars."
    )
    user = f"""ATOMS (the only allowed source of facts):
{json.dumps(atoms, ensure_ascii=False, indent=1)}

{fence_jd(jd, 1500, "emphasis")}

ALREADY-USED LEADING VERBS (do NOT start the bullet with any of these): {taken}

ACTION VERBS (grouped by category; choose an UNUSED one that fits the atom's ownership):
{palette}

PREVIOUS BULLET (keep the same facts; only change the opening verb): {bad_text}

Return ONLY JSON: {{"text": "<rewritten bullet>"}}"""
    out = as_dict(call(system, user, config.TIER_FLASH_LITE, json_out=True,
                       temperature=0.0), "text")
    text = out.get("text")
    return text.strip() if isinstance(text, str) else ""


def dedupe_leading_verbs(bullets: Dict[str, str], gm: Dict[str, List[str]], jd: str,
                         *, reserved=frozenset()) -> Dict[str, str]:
    """Guarantee every tailored bullet opens with a DISTINCT action verb — none reused, none
    colliding with `reserved` (the openers of verbatim bullets, which are never modified).

    First occurrence of a verb keeps it. A collision is re-rolled once via the LLM (`reverb`,
    constrained to an unused opener); if that still collides or fails, a deterministic
    in-category swap from `active_verbs()` makes the opener unique. Verbatim gkeys are skipped.
    Mutates and returns `bullets`."""
    used = {v for v in (reserved or ()) if v}
    palette = assets.active_verbs()
    for gk, text in list(bullets.items()):
        if is_verbatim_gkey(gk):
            continue
        v = leading_verb(text)
        if v and v not in used:
            used.add(v)
            continue
        ids = gm.get(gk) or gk.split("+")
        try:
            new = reverb(jd, ids, text, used)
        except Exception:  # noqa: BLE001 - re-roll is best-effort; the swap below guarantees uniqueness
            new = ""
        nv = leading_verb(new)
        if new and nv and nv not in used:
            bullets[gk] = new
            used.add(nv)
            continue
        repl = _pick_unused_verb(palette, v, used)
        if repl:
            bullets[gk] = _swap_leading_verb(text, repl)
            used.add(repl.lower())
        elif v:
            used.add(v)  # palette exhausted (pathological) — keep as-is, record the verb
    return bullets


# ── Stage 2c: fill underfull bullets from unused SAME-block atoms ─────────────
def fill_underfull(jd: str, job_title: str, sel: Dict[str, Any],
                   bullets: Dict[str, str]) -> Dict[str, str]:
    """Grow each UNDERFULL tailored bullet toward its configured line target by fusing in one
    UNUSED atom from the SAME block, then re-phrasing it. Strictly grounded: the folded detail
    can come ONLY from a real atom in the same entry, so it can never fabricate; a bullet whose
    block has no spare atom (or that is already full, or whose group already fuses 3 atoms) is
    left exactly as-is. One batched flash call over only the underfull bullets (often none).

    Implemented as group-augmentation: a committed fill appends the borrowed id to that group in
    `sel` and re-keys `bullets[old_gk] -> bullets[new_gk]`, so render / bullet_line_targets /
    one-page drop / fact-trace all key off the same atom ids and the borrowed atom becomes
    genuinely "used". Mutates `sel` and `bullets`; returns `bullets`. Best-effort: any failure
    leaves `bullets` unchanged (advisory, never fatal -- like block_briefs / shrink)."""
    targets = bullet_line_targets(sel)
    used: set[str] = {
        aid
        for sec in ("experience", "projects", "leadership")
        for e in sel.get(sec, [])
        for g in e["groups"]
        for aid in g
    }
    candidates: List[Dict[str, Any]] = []
    for sec in ("experience", "projects", "leadership"):
        for entry in sel.get(sec, []):
            name = entry["name"]
            for gi, ids in enumerate(entry["groups"]):
                gk = _gkey(ids)
                if gk not in bullets or is_verbatim_gkey(gk) or len(ids) >= 3:
                    continue
                target = targets.get(gk, config.PROJECT_BULLET_LINES)
                if not measure.is_underfull(bullets[gk], target):
                    continue
                spare = next((a for a in _block_atoms(sec, name)
                              if a not in used and atom_material_len([a]) > 0), None)
                if not spare:
                    continue
                used.add(spare)  # reserve so two bullets never borrow the same atom
                candidates.append({"entry": entry, "gi": gi, "ids": ids,
                                   "gk": gk, "spare": spare, "target": target})
    if not candidates:
        return bullets  # nothing underfull with spare material -> leave everything as-is

    payload = [
        {
            "gkey": c["gk"],
            "current_text": bullets[c["gk"]],
            "length_target": _length_hint(c["target"]),
            "atoms": {a: _atom_payload(a) for a in (c["ids"] + [c["spare"]])},
        }
        for c in candidates
    ]
    system = (
        "You lengthen UNDERFULL resume bullets that left their printed line half-empty. For "
        "each bullet you get its CURRENT text plus its group's atoms WITH ONE EXTRA atom "
        "appended. Keep EVERY existing fact, number, and the OPENING VERB exactly as written, "
        "and fold in ONE concrete detail drawn ONLY from the newly-added atom so the line fills "
        "toward its 'length_target'. You MAY slightly overshoot the target (it is trimmed back "
        "deterministically). If nothing in the extra atom fits naturally, return the bullet "
        "UNCHANGED; never pad with filler.\n" + _PRINCIPLE
    )
    user = f"""TARGET JOB: {job_title}

{fence_jd(jd, 2000, "emphasis")}

BULLETS TO LENGTHEN (re-phrase each to fill its line using its own atoms PLUS the one extra
atom; keep all existing facts and the opening verb; return the text UNCHANGED if the extra
atom adds nothing that fits):
{json.dumps(payload, ensure_ascii=False, indent=1)}

Return ONLY JSON: {{"bullets": [{{"gkey": "<gkey>", "text": "<lengthened or unchanged bullet>"}}, ...]}}"""
    try:
        out = as_dict(call(system, user, config.TIER_FLASH, json_out=True,
                           temperature=0.2), "bullets")
    except Exception:  # noqa: BLE001 - fill is advisory; leave bullets unchanged on any failure
        return bullets

    new_text: Dict[str, str] = {}
    seen = {c["gk"] for c in candidates}
    for b in out.get("bullets", []) or []:
        if not isinstance(b, dict):
            continue
        gk, text = b.get("gkey"), (b.get("text") or "").strip()
        if gk in seen and text:
            new_text[gk] = text

    for c in candidates:
        gk = c["gk"]
        text = new_text.get(gk, "")
        # Commit only when the model actually folded the extra atom in (text changed). An
        # unchanged / blank return means the atom added nothing, so leave the bullet and let
        # the spare atom stay unused.
        if not text or text == bullets[gk].strip():
            continue
        new_ids = c["ids"] + [c["spare"]]
        c["entry"]["groups"][c["gi"]] = new_ids
        bullets.pop(gk, None)
        bullets[_gkey(new_ids)] = text
    return bullets


# ── style gate: no AI-tell phrasing reaches the page ─────────────────────────
# The rephrase prompt bans this phrasing, but a model can still slip one
# through (observed ~2/18 bullets). This deterministic gate catches offenders,
# buys ONE batched repair call, and mechanically strips any em dash that
# survives even that, so an em dash can never print.
#
# Only phrasing that is ALWAYS slop in a resume bullet lives here: a false positive
# triggers a repair that could damage a correct bullet. Context-sensitive tells
# (dynamic, scalable, smart, multiple, significant, guarantee, decorative
# "end-to-end", "drove X" with no number) collide with real terms — "dynamic
# programming", "method signature", "statistically significant", "multiple
# regression" — so they stay in the PROMPT bans (model judgment), never here.
_STYLE_BANS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("em dash", re.compile(r"—|\s--\s")),
    ("contrast framing",
     re.compile(r",\s*not\s|\bnot just\b|\brather than\b|\binstead of\b", re.I)),
    ("participial tail",
     re.compile(r",\s*(?:enabling|ensuring|allowing|driving|resulting in|empowering"
                r"|showcasing|highlighting|demonstrating)\b", re.I)),
    ("buzzword verb",
     re.compile(r"\b(?:leverag|utiliz|spearhead|harness|empower|streamlin"
                r"|supercharg|turbocharg|revolutioniz|democratiz)\w*", re.I)),
    ("hollow intensifier",
     re.compile(r"\b(?:seamless\w*|robust\w*|comprehensive|cutting-edge|innovative"
                r"|holistic|state-of-the-art|powerful\w*|world-class|best-in-class"
                r"|top-notch|groundbreaking|unparalleled|turnkey|blazing\w*"
                r"|lightning[- ]fast|game[- ]?chang\w*|revolutionar\w*|very"
                r"|successfully)\b", re.I)),
    ("vague quantifier",
     re.compile(r"\b(?:various|numerous|myriad|consistently|regularly)\b"
                r"|\ba (?:wide range|wide variety|plethora) of\b", re.I)),
)


def style_violations(text: str) -> List[str]:
    """Names of the banned-phrasing patterns present in a bullet (empty = clean)."""
    return [name for name, pat in _STYLE_BANS if pat.search(text)]


def _strip_em_dashes(text: str) -> str:
    return re.sub(r"\s*—\s*|\s--\s", ", ", text)


def enforce_style(jd: str, job_title: str, sel: Dict[str, Any],
                  bullets: Dict[str, str]) -> int:
    """Repair IN PLACE any bullet using banned phrasing: one batched call grounded
    in the same atoms (same facts, same opening verb, no longer than the current
    text), then the mechanical em-dash strip as the unconditional backstop.
    Mutates `bullets`; returns how many were changed. Best-effort: a failed call
    leaves the texts for the mechanical pass (advisory, never fatal -- like
    block_briefs / fill_underfull)."""
    gm = group_map(sel)
    offenders = {gk: t for gk, t in bullets.items()
                 if not is_verbatim_gkey(gk) and style_violations(t)}
    changed = 0
    if offenders:
        payload = [
            {
                "gkey": gk,
                "bullet": text,
                "violations": style_violations(text),
                "max_chars": len(text),
                "atoms": {a: _atom_payload(a) for a in gm.get(gk, [])},
            }
            for gk, text in offenders.items()
        ]
        system = (
            "You repair resume bullets that slipped into banned phrasing. Rewrite each "
            "bullet as one plain declarative sentence stating the SAME facts, grounded "
            "ONLY in its atoms. Keep the OPENING VERB exactly as written and stay within "
            "'max_chars'. BANNED: " + BANNED_PHRASING + "\n" + _PRINCIPLE
        )
        user = f"""TARGET JOB: {job_title}

BULLETS TO REPAIR (each lists which banned patterns it hit; rewrite to remove them,
keeping every fact, number, and the opening verb):
{json.dumps(payload, ensure_ascii=False, indent=1)}

Return ONLY JSON: {{"bullets": [{{"gkey": "<gkey>", "text": "<repaired bullet>"}}, ...]}}"""
        try:
            out = as_dict(call(system, user, config.TIER_FLASH, json_out=True,
                               temperature=0.2), "bullets")
        except Exception:  # noqa: BLE001 - repair is advisory; the mechanical pass still runs
            out = {}
        for b in out.get("bullets", []) or []:
            if not isinstance(b, dict):
                continue
            gk, text = b.get("gkey"), (b.get("text") or "").strip()
            # Commit only strict improvement, so a bad repair can't make things worse.
            if (gk in offenders and text
                    and len(style_violations(text)) < len(style_violations(offenders[gk]))):
                bullets[gk] = text
                changed += 1
    # Unconditional backstop: an em dash must never reach the page.
    for gk, text in bullets.items():
        fixed = _strip_em_dashes(text)
        if fixed != text:
            bullets[gk] = fixed
            changed += 1
    return changed
