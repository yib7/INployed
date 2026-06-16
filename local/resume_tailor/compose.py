"""The composition stages — all bound by SELECT-AND-REPHRASE, NEVER GENERATE.

select()          flash : choose blocks + ordered bullet GROUPS (by atom id) + skill focus
rephrase()        pro   : one bullet per GROUP, faithfully fusing only that group's atoms
compress_skills() flash : exactly 3 fixed-label lines drawn from the taxonomy
verify()          flash : anti-inflation gate — each bullet vs the UNION of its group's atoms
rephrase_fix()    flash : regenerate a flagged bullet once, fixing the cited problems
refit()           flash : nudge a bullet to its target printed-line length

Only the creative first pass (rephrase) and the cover letter run on the PRO tier
— the fix-up passes (rephrase_fix/refit) are constrained rewrites of already-good
text, so flash handles them at a fraction of the cost without touching quality.

A "group" is a list of 1-3 closely-related atom ids fused into ONE bullet (e.g. an
accuracy gain + the cost cut). Each bullet's group key is "+".join(ids); every bullet
carries its source atom ids so the verifier and a human can trace it.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from . import assets, config, layout
from .llm import call

_PRINCIPLE = (
    "ABSOLUTE RULE — select and re-phrase, never invent. You may ONLY restate facts "
    "that are present in the provided atom(s). Never add a metric, number, tool, "
    "technology, company, or claim that is not literally in the atom. Copy every "
    "number/metric VERBATIM. Never upgrade the verb beyond the atom's stated ownership "
    "(if the atom says 'contributed to' or 'helped', do NOT write 'led' or 'owned'). "
    "Inflation here surfaces in the interview, not the application, so it is the worst "
    "possible failure. When unsure, say less."
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

# Which blocks must always render and the hard line budgets for the fixed blocks
# are CONFIG-DRIVEN (yaml `tailor:` section) so nothing is tied to one person's
# resume. See _required_blocks / _fixed_experience_specs / _leadership_entry_lines
# below for the schema and defaults.


# ── helpers ──────────────────────────────────────────────────────────────────
def _gkey(ids: List[str]) -> str:
    return "+".join(ids)


def _atom_payload(aid: str) -> Dict[str, Any]:
    atom = dict(assets.atoms_by_id()[aid])
    atom.pop("_section", None)
    atom.pop("_block", None)
    return atom


def _block_of(aid: str) -> str:
    return assets.atoms_by_id()[aid].get("_block", "")


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


def _first_atom(section: str, name: str) -> List[str]:
    """A sensible default group (the block's first atom) for required-block injection."""
    for b in assets.blocks().get(section, []):
        if b["name"] == name and b["atoms"]:
            return [b["atoms"][0]]
    return []


# ── Config-driven layout spec (yaml `tailor:` section) ────────────────────────
# tailor:
#   required:                       # blocks that must always render (default: all)
#     experience: all               #   'all' or a list of block names
#     leadership: [Org A, Org B]
#   fixed_blocks:                   # hard per-bullet line budgets (default: none)
#     Side Gig: {line_targets: [2, 1]}   # EXACTLY len(line_targets) bullets
#   leadership_entry_lines: 2       # each leadership org forced to N printed lines
def _required_blocks() -> Dict[str, List[str]]:
    """Section -> block names that must always render. Default: every block in
    experience and leadership (projects are selected, never force-injected).
    Explicitly-listed names that don't exist raise, to fail loud on a typo."""
    cfg = assets.tailor_config().get("required") or {}
    bl = assets.blocks()
    out: Dict[str, List[str]] = {}
    for sec in ("experience", "leadership"):
        present = [b["name"] for b in bl.get(sec, [])]
        spec = cfg.get(sec, "all")
        if spec in (None, "all"):
            out[sec] = present
            continue
        # A single block name may be written as a bare scalar (e.g. `experience: Globex`);
        # treat it as a one-element list instead of iterating its characters.
        names = [spec] if isinstance(spec, str) else list(spec)
        missing = [n for n in names if n not in present]
        if missing:
            raise RuntimeError(
                f"tailor.required.{sec} names block(s) not in master_experience.yaml: "
                f"{missing} (present: {present})"
            )
        out[sec] = names
    return out


def _fixed_experience_specs() -> Dict[str, List[int]]:
    """{experience_block_name: [per-bullet printed-line targets]}. Blocks not listed
    are free (model chooses the count, no hard line budget)."""
    fb = assets.tailor_config().get("fixed_blocks") or {}
    exp_names = {b["name"] for b in assets.blocks().get("experience", [])}
    out: Dict[str, List[int]] = {}
    for name, spec in fb.items():
        targets = (spec or {}).get("line_targets")
        if name not in exp_names or not targets:
            continue
        if isinstance(targets, (str, bytes)) or not isinstance(targets, (list, tuple)):
            raise RuntimeError(
                f"tailor.fixed_blocks.{name}.line_targets must be a list of integers "
                f"(e.g. [2, 1]); got {targets!r}"
            )
        try:
            out[name] = [int(t) for t in targets]
        except (TypeError, ValueError):
            raise RuntimeError(
                f"tailor.fixed_blocks.{name}.line_targets must contain only integers; "
                f"got {targets!r}"
            )
    return out


def _leadership_entry_lines() -> int:
    """Printed lines each leadership org is forced to (0/absent -> not enforced)."""
    cfg = assets.tailor_config()
    if "leadership_entry_lines" not in cfg:
        return layout.LEADERSHIP_ENTRY_LINES
    raw = cfg.get("leadership_entry_lines") or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"tailor.leadership_entry_lines must be an integer; got {raw!r}"
        )


def _experience_guidance() -> str:
    """Per-block selection guidance for the select() prompt, generated from the
    config so it never hardcodes one person's employers."""
    specs = _fixed_experience_specs()
    required = set(_required_blocks().get("experience", []))
    lines: List[str] = []
    for b in assets.blocks().get("experience", []):
        name = b["name"]
        if name in specs:
            t = specs[name]
            lines.append(
                f"  - {name}: EXACTLY {len(t)} bullet group(s) "
                f"(printed-line targets {t}); keep it tight."
            )
        else:
            tag = "ALWAYS include" if name in required else "include if relevant"
            lines.append(
                f"  - {name}: {tag}; choose the number of groups that best fits, "
                f"densest / most JD-relevant first."
            )
    return "\n".join(lines)


def group_map(sel: Dict[str, Any]) -> Dict[str, List[str]]:
    """Ordered {gkey: [atom_ids]} across experience -> projects -> leadership."""
    gm: "Dict[str, List[str]]" = {}
    for sec in ("experience", "projects", "leadership"):
        for entry in sel.get(sec, []):
            for ids in entry.get("groups", []):
                gm[_gkey(ids)] = ids
    return gm


def _catalog() -> str:
    """Compact id/what/angles catalog of every atom, grouped by block, for select()."""
    bl = assets.blocks()
    idx = assets.atoms_by_id()
    lines: List[str] = []
    for section in ("experience", "projects", "leadership"):
        lines.append(f"== {section.upper()} ==")
        for b in bl[section]:
            lines.append(f"[{section}] {b['name']}  (dates: {b.get('dates','')})")
            for aid in b["atoms"]:
                a = idx[aid]
                lines.append(f"   - {aid}: {a.get('what','')}  angles={a.get('angles',[])}")
    return "\n".join(lines)


def _check_required_blocks() -> None:
    """Fail loudly if a required block (or a fixed-block spec) names a block that
    isn't in master_experience.yaml — otherwise the template's fixed sections
    silently vanish from the output. _required_blocks() already raises for explicit
    missing names; here we also validate fixed_blocks."""
    _required_blocks()  # raises on explicit missing required names
    exp_names = {b["name"] for b in assets.blocks().get("experience", [])}
    missing = [n for n in _fixed_experience_specs() if n not in exp_names]
    if missing:
        raise RuntimeError(
            f"tailor.fixed_blocks names experience block(s) not in "
            f"master_experience.yaml: {missing} (present: {sorted(exp_names)})"
        )


# ── Stage 1: select ──────────────────────────────────────────────────────────
def select(jd: str, job_title: str, company: str) -> Dict[str, Any]:
    _check_required_blocks()
    system = (
        "You are tailoring a one-page resume for an early-career data/SWE candidate. "
        "This step is PURE SELECTION — you write no prose. Choose which experiences, "
        "projects, and leadership entries best match the job, and group their atoms (by "
        "id) into bullet GROUPS. Each group is 1-3 atom ids fused into ONE bullet; group "
        "atoms only when they describe the SAME achievement and read naturally as a single "
        "dense line (e.g. an accuracy gain + the cost cut). Prefer single-atom groups "
        "unless fusing clearly improves density. Bias toward the most JD-relevant evidence. "
        "In the SAME pass, also select the candidate's technical skills "
        "into exactly three lines (Languages / Tools & Infrastructure / Libraries & Frameworks): "
        "only skills present in each line's pool. STRATEGY: first lock in every skill the JD "
        "explicitly mentions or strongly implies; then fill remaining slots with complementary "
        "skills that a strong candidate in this role would also have — adjacent languages, "
        "transferable tools, or broadly valued skills (e.g. Python on a Java role, SQL on a "
        "backend role). Goal: show depth in the asked stack AND breadth beyond it, without "
        "padding with unrelated filler. Avoid obscure niche items that add no signal. "
        "Most JD-relevant items first. Preserve any '(conceptual)' / '(from scratch)' "
        "qualifiers verbatim. You MAY merge closely-related API entries into one compact token "
        "(e.g. 'Gemini/OpenAI/Claude API').\n"
        + _PRINCIPLE
    )
    pools = _skill_pools()
    exp_guidance = _experience_guidance()
    lead_lines = _leadership_entry_lines()
    lead_guidance = (
        f"Each entry = EXACTLY {lead_lines} printed line(s), normally as "
        f"{lead_lines} tight single-line bullet(s) (one per atom)."
        if lead_lines else
        "Choose the number of groups per entry that best fits."
    )
    # Static blocks first (catalog/pools/guidance/schema are identical every run),
    # the per-job JOB/JD last — so Gemini's implicit prefix cache can discount the
    # large static prefix across back-to-back tailor runs. JSON mode fixes the
    # output shape regardless of where the schema sits.
    user = f"""ATOM CATALOG (choose atom ids from here only; an atom belongs to the block it is listed under):
{_catalog()}

SKILL POOLS (for the "skills" output only — pick each line's items only from its pool; Languages must have AT LEAST 4 (aim ~6-8), ~7-10 for the others — JD matches first, then complementary skills that show breadth):
Languages: {json.dumps(pools["Languages"], ensure_ascii=False)}
Tools & Infrastructure: {json.dumps(pools["Tools & Infrastructure"], ensure_ascii=False)}
Libraries & Frameworks: {json.dumps(pools["Libraries & Frameworks"], ensure_ascii=False)}

Selection guidance — the resume template has FIXED sections; fill them to one full page (~14-18 bullets):
- Work Experience (use the block names exactly as listed in the catalog above):
{exp_guidance}
- Projects: include ALL available projects, ORDERED STRONGEST-FIRST for THIS job. Give the strongest ~2-3 groups and weaker ones ~1 group.
- Leadership: ALWAYS include EVERY leadership entry. {lead_guidance}
- Line density rule: every bullet must fill at least half its printed line. Never write a bullet so short it leaves more than half the line blank — fuse atoms or pick denser content instead.
- Within a block, order groups by relevance to THIS job.

Return ONLY JSON (use the real block names + atom ids from the catalog; groups is a list of lists of atom ids):
{{
  "experience": [
    {{"name": "<experience block name>", "groups": [["<atom_id>"], ["<atom_id>", "<atom_id>"]]}}
  ],
  "projects":   [{{"name": "<project name>", "groups": [["<atom_id>"], ["<atom_id>", "<atom_id>"]]}}],
  "leadership": [{{"name": "<leadership org>", "groups": [["<atom_id>"]]}}],
  "skill_focus": "one of: ml_research | backend_platform | data_analytics | general",
  "skills": {{"Languages": "Python, SQL, R", "Tools & Infrastructure": "...", "Libraries & Frameworks": "..."}},
  "rationale": "1-2 sentences (incl. why projects are ordered as they are)"
}}

Now select for THIS job — bias toward the most JD-relevant evidence, most relevant first:
JOB: {job_title} at {company}

JOB DESCRIPTION:
{jd[:7000]}"""
    out = call(system, user, config.TIER_FLASH, json_out=True, temperature=0.1)
    return _normalize_selection(out)


def _normalize_selection(sel: Dict[str, Any]) -> Dict[str, Any]:
    """Validate group atom ids, dedupe globally, inject required blocks, fix order."""
    valid_ids = set(assets.atoms_by_id())
    bl = assets.blocks()
    names = {sec: {b["name"] for b in bl[sec]} for sec in bl}
    used: set[str] = set()

    clean: Dict[str, Any] = {"skill_focus": sel.get("skill_focus", "general"),
                             "skills": sel.get("skills") or {},
                             "rationale": sel.get("rationale", "")}
    for sec in ("experience", "projects", "leadership"):
        clean[sec] = []
        for entry in sel.get(sec, []) or []:
            name = entry.get("name")
            if name not in names[sec]:
                continue
            groups: List[List[str]] = []
            for g in entry.get("groups", []) or []:
                ids = []
                for aid in g:
                    if aid in valid_ids and aid not in used and _block_of(aid) == name:
                        ids.append(aid)
                        used.add(aid)
                if ids:
                    groups.append(ids)
            if groups:
                clean[sec].append({"name": name, "groups": groups})

    _ensure_required_blocks(clean, used)
    _order_fixed_blocks(clean)
    _enforce_fixed_counts(clean)
    return clean


def _ensure_required_blocks(clean: Dict[str, Any], used: set[str]) -> None:
    """Guarantee the template's fixed blocks render, even if the model omitted them."""
    for sec, required_names in _required_blocks().items():
        present = {e["name"] for e in clean.get(sec, [])}
        for name in required_names:
            if name in present:
                continue
            default = [a for a in _first_atom(sec, name) if a not in used]
            if not default:  # fall back to the first atom even if "used" elsewhere
                default = _first_atom(sec, name)
            if default:
                used.update(default)
                clean.setdefault(sec, []).append({"name": name, "groups": [default]})


def _order_fixed_blocks(clean: Dict[str, Any]) -> None:
    """Experience/leadership follow the template's fixed order; projects keep model (strength) order."""
    order = {sec: [b["name"] for b in assets.blocks()[sec]] for sec in ("experience", "leadership")}
    for sec in ("experience", "leadership"):
        rank = {name: i for i, name in enumerate(order[sec])}
        clean[sec].sort(key=lambda e: rank.get(e["name"], 99))


# ── Hard-coded bullet counts for the fixed blocks (layout.py owns the spec) ───
def _block_atoms(section: str, name: str) -> List[str]:
    for b in assets.blocks().get(section, []):
        if b["name"] == name:
            return list(b.get("atoms", []))
    return []


def _enforce_fixed_counts(clean: Dict[str, Any]) -> None:
    """Force each configured fixed experience block to EXACTLY len(line_targets)
    bullets, and each Leadership org to the bullet count its line-budget needs
    (2 single-atom bullets when it has >=2 atoms, else 1). Deterministic — the
    model cannot over/under-fill the fixed blocks regardless of what select()
    returned. All driven by the yaml `tailor:` config (no hardcoded org names)."""
    used: set[str] = {
        aid
        for sec in ("experience", "projects", "leadership")
        for e in clean.get(sec, [])
        for g in e["groups"]
        for aid in g
    }
    specs = _fixed_experience_specs()
    for e in clean.get("experience", []):
        targets = specs.get(e["name"])
        if targets:
            _resize_to_count(e, "experience", e["name"], len(targets), used, singles=False)
    lead_lines = _leadership_entry_lines()
    if lead_lines:
        for e in clean.get("leadership", []):
            avail = _block_atoms("leadership", e["name"])
            target = lead_lines if len(avail) >= 2 else 1
            _resize_to_count(e, "leadership", e["name"], target, used, singles=True)


def _resize_to_count(entry: Dict[str, Any], section: str, name: str, n: int,
                     used: set[str], *, singles: bool) -> None:
    """Make `entry` have exactly `n` bullet groups. Trim extra groups from the
    end; pad from this block's still-unused atoms. With singles=True every bullet
    is one atom (splitting any fused group), matching the leadership "one tight
    bullet per atom" plan."""
    avail = _block_atoms(section, name)
    if singles:
        ordered = [a for g in entry["groups"] for a in g]  # flatten, keep order
        for a in avail:  # then any unused atoms from the block, in file order
            if a not in ordered:
                ordered.append(a)
        seen: List[str] = []
        for a in ordered:
            if a not in seen:
                seen.append(a)
        chosen = seen[:n]
        for a in chosen:
            used.add(a)
        entry["groups"] = [[a] for a in chosen] or entry["groups"]
        return

    groups = entry["groups"]
    if len(groups) > n:
        for g in groups[n:]:
            for a in g:
                used.discard(a)
        entry["groups"] = groups[:n]
    while len(entry["groups"]) < n:
        extra = next((a for a in avail if a not in used), None)
        if not extra:
            break
        used.add(extra)
        entry["groups"].append([extra])


def layout_budgets(sel: Dict[str, Any]) -> Dict[str, int]:
    """{gkey: target_printed_lines} for every bullet under a hard line budget
    (configured fixed experience blocks + each Leadership bullet). Bullets not
    listed are free. All targets come from the yaml `tailor:` config."""
    budgets: Dict[str, int] = {}
    specs = _fixed_experience_specs()
    for e in sel.get("experience", []):
        targets = specs.get(e["name"])
        if not targets:
            continue
        for i, ids in enumerate(e["groups"]):
            budgets[_gkey(ids)] = targets[i] if i < len(targets) else 1
    lead_lines = _leadership_entry_lines()
    if lead_lines:
        for e in sel.get("leadership", []):
            plan = layout.plan_leadership_lines(len(e["groups"]), lead_lines)
            for ids, tgt in zip(e["groups"], plan):
                budgets[_gkey(ids)] = tgt
    return budgets


# ── Stage 2: rephrase ────────────────────────────────────────────────────────
def _length_hint(target_lines: int) -> str:
    lo, hi = layout.body_line_budget(target_lines)
    return f"{lo}-{hi} chars ({target_lines} printed line{'s' if target_lines > 1 else ''})"


def rephrase(jd: str, job_title: str, sel: Dict[str, Any],
             budgets: Dict[str, int] | None = None) -> Dict[str, str]:
    """Return {gkey: bullet_text} — one bullet per selected group.

    `budgets` (gkey -> target printed lines) carries the hard layout spec for the
    fixed blocks; those bullets are given an explicit character window to hit so
    they render to the intended line count (a fit loop in run.py then verifies).
    """
    gm = group_map(sel)
    budgets = budgets or {}
    payload = []
    for gk, ids in gm.items():
        item: Dict[str, Any] = {"gkey": gk, "atoms": {a: _atom_payload(a) for a in ids}}
        if gk in budgets:
            item["length_target"] = _length_hint(budgets[gk])
        payload.append(item)
    verbs = _CORE_VERBS
    example = assets.example_text()[:1200]
    system = (
        "You write resume bullets by faithfully RE-PHRASING fact-atoms for a specific job. "
        "Each group is one bullet: if it has multiple atoms, FUSE them into a single dense "
        "line that states only what those atoms say. You are a translator turning structured "
        "facts into one polished line, not a writer inventing content.\n" + _PRINCIPLE + "\n"
        "STYLE: past tense, no first-person pronouns, no markdown, no LaTeX, NO bold or "
        "italics. One sentence (a fused group may run to ~2 clauses), <= ~300 characters. "
        "Front-load the result/impact that matters for THIS job. Open with a strong action "
        "verb from the provided list that matches the atom's real ownership. Numbers exactly "
        "as written. Write 'greater than or equal to' style comparisons with the symbols "
        ">= and <= (they are converted to proper math notation later).\n"
        "SPACE: a bullet that fits on ONE printed line should fill at least ~75% of it — "
        "never leave a stubby half-empty line (fold in more grounded detail from the atoms "
        "or fuse, but NEVER invent facts to pad). A bullet that wraps to multiple lines may "
        "let its last line run shorter (down to ~50% full)."
    )
    user = f"""TARGET JOB: {job_title}

JOB DESCRIPTION (for angle/emphasis only — never a source of new facts):
{jd[:2500]}

ACTION VERBS (open each bullet with one of these; match the atom's real ownership):
{verbs}

STYLE EXEMPLAR (match this voice, length and density — NEVER copy its facts):
{example}

GROUPS (write exactly ONE bullet per gkey, re-phrasing ONLY the atoms in that group):
{json.dumps(payload, ensure_ascii=False, indent=1)}

LAYOUT (HARD requirement): any group with a "length_target" MUST land inside that
character range — this controls how many printed lines the bullet takes and is not
optional. For a 2-line target, write a dense, fully-developed line that uses all the
group's facts; for a 1-line target, write one tight line. Stay within the atoms'
facts either way (never invent to pad, never drop a number to shorten).

Return ONLY JSON: {{"bullets": [{{"gkey": "<gkey>", "text": "<one bullet>"}}, ...]}}"""
    out = call(system, user, config.TIER_PRO, json_out=True, temperature=0.25)
    result: Dict[str, str] = {}
    for b in out.get("bullets", []):
        gk, text = b.get("gkey"), (b.get("text") or "").strip()
        if gk in gm and text:
            result[gk] = text
    return result


def rephrase_fix(jd: str, ids: List[str], bad_text: str, problems: List[str]) -> str:
    """Regenerate one flagged bullet, deterministically, fixing the problems."""
    atoms = {a: _atom_payload(a) for a in ids}
    system = (
        "Rewrite ONE resume bullet to fix grounding problems. If multiple atoms are given, "
        "fuse them into one line. " + _PRINCIPLE + "\n"
        "Plain text, past tense, no pronouns, no markup, <= ~300 chars."
    )
    user = f"""ATOMS (the only allowed source of facts):
{json.dumps(atoms, ensure_ascii=False, indent=1)}

JOB CONTEXT (emphasis only): {jd[:1500]}

PREVIOUS BULLET: {bad_text}
PROBLEMS TO FIX: {problems}

Return ONLY JSON: {{"text": "<corrected bullet>"}}"""
    out = call(system, user, config.TIER_FLASH, json_out=True, temperature=0.0)
    return (out.get("text") or "").strip()


def refit(jd: str, ids: List[str], text: str, target_lines: int) -> str:
    """Rewrite one bullet to a STRICT character window so it renders to exactly
    `target_lines` printed lines. Same grounding rule: re-phrase the atoms only —
    lengthen with real detail, shorten by cutting filler; never invent or drop a fact."""
    lo, hi = layout.body_line_budget(target_lines)
    cur = layout._visible_len(text)
    atoms = {a: _atom_payload(a) for a in ids}
    direction = (
        "It is TOO SHORT — develop it with more concrete detail drawn FROM THE ATOMS "
        "(more specifics, fuller phrasing); never invent facts or numbers to pad."
        if cur < lo else
        "It is TOO LONG — tighten wording and cut filler/adjectives; keep every number, "
        "tool, and claim."
    )
    system = (
        f"Rewrite ONE resume bullet so it occupies EXACTLY {target_lines} printed "
        f"line(s) on the resume. " + _PRINCIPLE + "\n"
        "Plain text, past tense, no pronouns, no markup."
    )
    user = f"""ATOMS (the only allowed source of facts):
{json.dumps(atoms, ensure_ascii=False, indent=1)}

JOB CONTEXT (emphasis only): {jd[:1200]}

CURRENT BULLET ({cur} chars): {text}
TARGET: rewrite to between {lo} and {hi} characters. {direction}

Return ONLY JSON: {{"text": "<rewritten bullet>"}}"""
    out = call(system, user, config.TIER_FLASH, json_out=True, temperature=0.1)
    return (out.get("text") or "").strip()


# ── Stage 3: skills (exactly 3 fixed categories) ─────────────────────────────
_SKILL_BUCKETS = (
    ("Languages", ("languages",)),
    ("Tools & Infrastructure", ("developer_tools",)),
    ("Libraries & Frameworks", ("frameworks", "libraries")),
)
# Per-line item-char caps/floors are derived in layout.py from the calibrated
# skills-column width so the section lands at 3-4 printed lines (Libraries may
# wrap to a 2nd) and no line sits >half empty. Languages also carries a hard
# minimum item count (layout.MIN_LANGUAGES).


def _pool(skills: Dict[str, Any], keys: Tuple[str, ...]) -> List[str]:
    out: List[str] = []
    for k in keys:
        out.extend(skills.get(k, []) or [])
    return out


def _skill_pools() -> Dict[str, List[str]]:
    """The three fixed skill lines mapped to their candidate-skill pools."""
    skills = assets.load_master().get("skills", {})
    return {label: _pool(skills, keys) for label, keys in _SKILL_BUCKETS}


def _finalize_skill_lines(out: Dict[str, Any]) -> List[Dict[str, str]]:
    """Backfill each line from its pool so it is robust (>=4 languages, no
    >half-empty line), then cap it to its printed-line budget. Always returns all
    three lines, filled, regardless of how little the model selected."""
    caps = layout.skill_caps()
    floors = layout.skill_floors()
    pools = _skill_pools()
    lines: List[Dict[str, str]] = []
    for label, _keys in _SKILL_BUCKETS:
        items = (out.get(label) or "").strip()
        min_items = layout.MIN_LANGUAGES if label == "Languages" else 0
        items = _backfill_skills(items, pools.get(label, []), caps[label],
                                 floors[label], min_items)
        items = _cap_items(items, caps[label])
        if items:
            lines.append({"label": label, "items": items})
    return lines


def _backfill_skills(items: str, pool: List[str], cap: int, floor: int,
                     min_items: int) -> str:
    """Append still-unused pool skills (in pool order) until the line has at least
    `min_items` items AND fills its floor of characters — without overflowing `cap`
    once the minimum is met. Guarantees a robust, non-empty line even if the model
    under-selected (or returned nothing)."""
    toks = [t.strip() for t in items.split(",") if t.strip()]
    seen = {t.lower() for t in toks}

    def clen(ts: List[str]) -> int:
        return sum(len(t) for t in ts) + 2 * max(0, len(ts) - 1)

    for cand in pool:
        if len(toks) >= min_items and clen(toks) >= floor:
            break
        if cand.lower() in seen:
            continue
        would = clen(toks) + len(cand) + (2 if toks else 0)
        if would > cap and len(toks) >= min_items:
            continue  # don't blow the cap once the hard minimum is satisfied
        toks.append(cand)
        seen.add(cand.lower())
    return ", ".join(toks)


def _cap_items(items: str, max_chars: int) -> str:
    """Keep whole comma-separated tokens up to max_chars (never cut mid-token)."""
    toks = [t.strip() for t in items.split(",") if t.strip()]
    kept: List[str] = []
    total = 0
    for t in toks:
        add = len(t) + (2 if kept else 0)
        if kept and total + add > max_chars:
            break
        kept.append(t)
        total += add
    return ", ".join(kept)


def compress_skills(jd: str, job_title: str, sel: Dict[str, Any]) -> List[Dict[str, str]]:
    """Resolve the 3 fixed skill lines.

    Reuses the skills chosen by select() in the same pass when present; only falls
    back to a dedicated flash call if that selection is missing/empty.
    """
    pre = sel.get("skills") if isinstance(sel, dict) else None
    if pre:
        lines = _finalize_skill_lines(pre)
        if lines:
            return lines

    skill_focus = sel.get("skill_focus", "general") if isinstance(sel, dict) else "general"
    pools = _skill_pools()
    system = (
        "Select the candidate's technical skills into EXACTLY THREE fixed lines: "
        "'Languages', 'Tools & Infrastructure', 'Libraries & Frameworks'. "
        "Selection only — only include skills present in that line's pool. "
        "STRATEGY: first lock in every skill the JD explicitly mentions or strongly implies; "
        "then fill remaining slots with complementary skills a strong candidate in this role "
        "would also have — adjacent languages, transferable tools, broadly valued skills "
        "(e.g. Python on a Java role, SQL on a backend role). Show depth in the asked stack "
        "AND breadth beyond it, without padding with unrelated filler. "
        "You MAY merge closely-related API entries into one compact token (e.g. 'Gemini/OpenAI/Claude API'). "
        "Preserve confidence qualifiers like '(conceptual)' / '(from scratch)' verbatim. "
        "Put the most JD-relevant items first."
    )
    user = f"""TARGET JOB: {job_title}  (focus hint: {skill_focus})

JOB DESCRIPTION:
{jd[:4000]}

POOLS (pick each line's items only from its pool):
Languages: {json.dumps(pools["Languages"], ensure_ascii=False)}
Tools & Infrastructure: {json.dumps(pools["Tools & Infrastructure"], ensure_ascii=False)}
Libraries & Frameworks: {json.dumps(pools["Libraries & Frameworks"], ensure_ascii=False)}

Rules:
- Languages must have AT LEAST 4 items (aim ~6-8); ~7-10 for the others. JD-matching skills first, then adjacent/complementary skills that add signal.
- Avoid obscure niche items that recruiters won't recognise. Lead with the items this JOB cares about most.

Return ONLY JSON: {{"Languages": "Python, SQL, R", "Tools & Infrastructure": "...", "Libraries & Frameworks": "..."}}"""
    try:
        out = call(system, user, config.TIER_FLASH, json_out=True, temperature=0.1)
    except Exception:
        out = {}
    return _finalize_skill_lines(out)


# ── Shrink (one-page enforcement helper) ─────────────────────────────────────
def shrink(jd: str, bullets: Dict[str, str], pages: int) -> Dict[str, str]:
    """Shorten bullets to fit one page, preserving every metric and claim.

    Same grounding rule: this only TRIMS wording (adjectives, filler, redundant
    clauses) — it never drops a number or adds anything. Returns {gkey: text};
    any key the model omits keeps its previous text.
    """
    system = (
        "Shorten resume bullets so the resume fits on one page. Keep EVERY number, "
        "metric, tool, and claim — only cut filler words, adjectives, and redundant "
        "clauses. Never add anything. Plain text, no markup. " + _PRINCIPLE
    )
    user = (
        f"The resume is {pages} pages; it must be 1. Tighten each bullet by ~20-30%% "
        "without losing any fact or number.\n\nBULLETS:\n"
        + json.dumps([{"gkey": k, "text": v} for k, v in bullets.items()], ensure_ascii=False, indent=1)
        + '\n\nReturn ONLY JSON: {"bullets": [{"gkey": "...", "text": "..."}, ...]}'
    )
    try:
        out = call(system, user, config.TIER_FLASH, json_out=True, temperature=0.0)
    except Exception:
        return bullets
    result = dict(bullets)
    for b in out.get("bullets", []):
        gk, text = b.get("gkey"), (b.get("text") or "").strip()
        if gk in result and text:
            result[gk] = text
    return result


# ── Stage 4: verify (anti-inflation gate) ────────────────────────────────────
def verify(bullets: Dict[str, str], gm: Dict[str, List[str]]) -> Dict[str, Dict[str, Any]]:
    """Return {gkey: {ok, problems}} checking each bullet against its group's atom UNION."""
    payload = [
        {"gkey": gk, "bullet": text, "atoms": [_atom_payload(a) for a in gm.get(gk, [])]}
        for gk, text in bullets.items()
    ]
    system = (
        "You are a strict fact-grounding auditor for resume bullets. For each bullet, "
        "compare it ONLY to the union of its atoms. Mark ok=false if the bullet: (a) states "
        "a number/metric not in the atoms, (b) names a tool/tech/skill/company not in the "
        "atoms, (c) uses a verb that overstates the atoms' ownership (e.g. 'led' when the "
        "atom says contributed/helped), or (d) adds any claim the atoms do not support. "
        "Fusing multiple atoms into one line is fine. Be conservative: a faithful paraphrase "
        "with the same numbers is ok=true."
    )
    user = (
        "BULLETS + ATOMS:\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + '\n\nReturn ONLY JSON: {"results": [{"gkey": "...", "ok": true, "problems": []}, ...]}'
    )
    out = call(system, user, config.TIER_FLASH, json_out=True, temperature=0.0)
    results: Dict[str, Dict[str, Any]] = {}
    for r in out.get("results", []):
        gk = r.get("gkey")
        if gk in bullets:
            results[gk] = {"ok": bool(r.get("ok", True)), "problems": r.get("problems", []) or []}
    return results
