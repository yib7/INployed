"""The composition stages — all bound by SELECT-AND-REPHRASE, NEVER GENERATE.

select()          flash : choose blocks + ordered bullet GROUPS (by atom id) + skill focus
rephrase()        pro   : one bullet per GROUP, faithfully fusing only that group's atoms
compress_skills() flash : exactly 4 fixed-label lines drawn from the taxonomy
verify()          flash : anti-inflation gate — each bullet vs the UNION of its group's atoms
rephrase_fix()    flash : regenerate a flagged bullet once, fixing the cited problems

Only the creative first pass (rephrase) and the cover letter run on the PRO tier.
All other stages (selection, verify, rephrase_fix) use flash for constrained rewrites
of already-grounded text. Length is finalized deterministically downstream.

A "group" is a list of 1-3 closely-related atom ids fused into ONE bullet (e.g. an
accuracy gain + the cost cut). Each bullet's group key is "+".join(ids); every bullet
carries its source atom ids so the verifier and a human can trace it.
"""
from __future__ import annotations

import json
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

from . import assets, config, layout, measure
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


def _render_verb_palette(verbs: Dict[str, List[str]]) -> str:
    """Render the categorized action verbs as a compact grouped block for the prompt:
    one `Category: v1, v2, ...` line per category, in file order. The model picks a
    category-appropriate opener; the no-reuse rule is enforced separately downstream."""
    return "\n".join(f"{cat}: {', '.join(items)}" for cat, items in verbs.items() if items)

# Which blocks must always render and the hard line budgets for the fixed blocks
# are CONFIG-DRIVEN (yaml `tailor:` section) so nothing is tied to one person's
# resume. See _required_blocks below for the schema and defaults.


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


def _experience_guidance() -> str:
    """Per-block selection guidance for the select() prompt, generated from the
    config so it never hardcodes one person's employers."""
    required = set(_required_blocks().get("experience", []))
    lines: List[str] = []
    for b in assets.blocks().get("experience", []):
        name = b["name"]
        n = len(config.block_targets(name))
        tag = "ALWAYS include" if name in required else "include if relevant"
        lines.append(f"  - {name}: {tag}; aim for {n} bullet group(s), densest / most JD-relevant first.")
    return "\n".join(lines)


def _project_guidance() -> str:
    """Per-project selection guidance for select(), generated from the config so it
    honors each project's configured bullet count instead of defaulting weaker projects
    to one group. A project with a custom layout (`config.project_targets`) uses that
    count; an unconfigured project uses the global `PROJECT_BULLETS_MAX`. The engine
    pads/trims to this count deterministically downstream (`_cap_projects`); telling the
    model up front makes it pick that many relevant atoms in strength order."""
    lines: List[str] = []
    for b in assets.blocks().get("projects", []):
        name = b["name"]
        targets = config.project_targets(name)
        n = len(targets) if targets else config.PROJECT_BULLETS_MAX
        lines.append(f"  - {name}: aim for {n} bullet group(s), densest / most JD-relevant first.")
    return "\n".join(lines)


# ── verbatim ("don't tailor — use my exact bullets") ─────────────────────────
# A block the user marked verbatim has its groups replaced (after _normalize_selection)
# with synthetic single-bullet groups whose id is "__verbatim__/<block>/<i>". These
# carry the user's EXACT text: they're excluded from rephrase/cohesion/verify/trim and
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
    LLM stages (rephrase/verify) must never see them."""
    gm: "Dict[str, List[str]]" = {}
    for sec in ("experience", "projects", "leadership"):
        for entry in sel.get(sec, []):
            for ids in entry.get("groups", []):
                gk = _gkey(ids)
                if is_verbatim_gkey(gk):
                    continue
                gm[gk] = ids
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
    """Fail loudly if a required block names a block that isn't in
    master_experience.yaml — otherwise the template's fixed sections silently
    vanish from the output. _required_blocks() already raises for missing names."""
    _required_blocks()  # raises on explicit missing required names


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
        "In the SAME pass, also select the candidate's technical skills into exactly four "
        "lines (Languages / Frameworks / Developer Tools / Libraries): only skills present in "
        "each line's pool. RANK each line's pool by relevance to THIS job and return the BEST "
        "few, most-relevant FIRST. Aim for ~7 Languages, ~7 Frameworks, ~10 Developer Tools, "
        "~10 Libraries; if a pool has fewer than that, just return all of it. Lead with every "
        "skill the JD explicitly mentions or strongly implies, then the strongest complementary "
        "skills a candidate in this role would have (adjacent languages, transferable tools). "
        "Do NOT pad with weak/unrelated filler just to reach the count — a few sharp, relevant "
        "skills beat a long list. Preserve any '(conceptual)' / '(from scratch)' qualifiers "
        "verbatim. You MAY merge closely-related API entries into one compact token (e.g. "
        "'Gemini/OpenAI/Claude API').\n"
        + _PRINCIPLE
    )
    pools = _skill_pools()
    exp_guidance = _experience_guidance()
    proj_guidance = _project_guidance()
    lead_lines = layout.LEADERSHIP_ENTRY_LINES
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

SKILL POOLS (for the "skills" output only — pick each line's items only from its pool, ranked most-relevant-first; aim ~7 Languages, ~7 Frameworks, ~10 Developer Tools, ~10 Libraries, or all of a smaller pool — JD matches first, then complementary skills; don't pad to hit the count):
Languages: {json.dumps(pools["Languages"], ensure_ascii=False)}
Frameworks: {json.dumps(pools["Frameworks"], ensure_ascii=False)}
Developer Tools: {json.dumps(pools["Developer Tools"], ensure_ascii=False)}
Libraries: {json.dumps(pools["Libraries"], ensure_ascii=False)}

Selection guidance — the resume template has FIXED sections; fill them to one full page (~14-18 bullets):
- Work Experience (use the block names exactly as listed in the catalog above):
{exp_guidance}
- Projects: include ALL available projects, ORDERED STRONGEST-FIRST for THIS job; for each project produce the target number of bullet group(s) shown below (densest / most JD-relevant atoms first):
{proj_guidance}
- Leadership: ALWAYS include EVERY leadership entry. {lead_guidance}
- Line density rule: every bullet must fill at least 70% of its printed line. Never write a bullet so short it leaves more than ~30% of the line blank — fuse atoms or pick denser content instead.
- Within a block, order groups by relevance to THIS job.

Return ONLY JSON (use the real block names + atom ids from the catalog; groups is a list of lists of atom ids):
{{
  "experience": [
    {{"name": "<experience block name>", "groups": [["<atom_id>"], ["<atom_id>", "<atom_id>"]]}}
  ],
  "projects":   [{{"name": "<project name>", "groups": [["<atom_id>"], ["<atom_id>", "<atom_id>"]]}}],
  "leadership": [{{"name": "<leadership org>", "groups": [["<atom_id>"]]}}],
  "skill_focus": "one of: ml_research | backend_platform | data_analytics | general",
  "skills": {{"Languages": "Python, SQL, R", "Frameworks": "...", "Developer Tools": "...", "Libraries": "..."}},
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
    """Force each constant block to EXACTLY len(config.block_targets(name)) bullets
    (experience as fused groups, leadership as single-atom bullets), and cap projects.
    Deterministic — the model cannot over/under-fill regardless of select()."""
    used: set[str] = {
        aid
        for sec in ("experience", "projects", "leadership")
        for e in clean.get(sec, [])
        for g in e["groups"]
        for aid in g
    }
    for e in clean.get("experience", []):
        n = len(config.block_targets(e["name"]))
        _resize_to_count(e, "experience", e["name"], n, used, singles=False)
    for e in clean.get("leadership", []):
        n = len(config.block_targets(e["name"]))
        _resize_to_count(e, "leadership", e["name"], n, used, singles=True)
    _cap_projects(clean)


def _cap_projects(clean: Dict[str, Any]) -> None:
    """Keep the top config.projects_max() projects (strength-ordered by select) and fit
    each to its per-project bullet count. A project with a configured layout
    (config.project_targets) is resized to EXACTLY that many groups — padded UP from its
    own unused atoms (like a constant block via _resize_to_count, fused groups preserved)
    as well as trimmed down — so the configured count is a TARGET, not just a ceiling.
    An unconfigured project keeps cap-only behavior (trimmed to the global
    PROJECT_BULLETS_MAX, never padded). Projects are never force-injected; padding draws
    only from the project's own atoms, so it is best-effort when a project has fewer
    atoms than its configured count."""
    projects = clean.get("projects", [])[:config.projects_max()]
    used: set[str] = {
        aid
        for sec in ("experience", "projects", "leadership")
        for e in clean.get(sec, [])
        for g in e["groups"]
        for aid in g
    }
    for entry in projects:
        targets = config.project_targets(entry["name"])
        if targets:  # configured -> honor the count: pad up + trim down (like experience)
            _resize_to_count(entry, "projects", entry["name"], len(targets), used, singles=False)
        else:        # unconfigured -> cap only, never pad
            entry["groups"] = entry["groups"][:config.PROJECT_BULLETS_MAX]
    clean["projects"] = projects


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


def bullet_line_targets(sel: Dict[str, Any]) -> Dict[str, int]:
    """{gkey: target_printed_lines} for EVERY bullet. Constant blocks (experience +
    leadership) use config.block_targets; projects use their per-project
    config.project_targets line targets when configured, else fall back to
    config.PROJECT_BULLET_LINES.
    Feeds the rephrase soft hint and the deterministic trim cap."""
    out: Dict[str, int] = {}
    for sec in ("experience", "leadership"):
        for e in sel.get(sec, []):
            targets = config.block_targets(e["name"])
            for i, ids in enumerate(e["groups"]):
                out[_gkey(ids)] = targets[i] if i < len(targets) else targets[-1]
    for e in sel.get("projects", []):
        targets = config.project_targets(e["name"])
        for i, ids in enumerate(e["groups"]):
            if targets:
                out[_gkey(ids)] = targets[i] if i < len(targets) else targets[-1]
            else:
                out[_gkey(ids)] = config.PROJECT_BULLET_LINES
    return out


# ── Stage 2: rephrase ────────────────────────────────────────────────────────
def _length_hint(target_lines: int) -> str:
    """A soft floor + hard ceiling for one bullet. The ceiling is the trim cap
    (target_lines * MAX_LINE_CHARS); the floor keeps the bullet from sitting
    stubby — a single-line bullet should fill >=90% of its line, and a wrapping
    bullet's last line should fill >=75% (so floor = ((n-1)+0.75)*cap_per_line)."""
    per_line = config.MAX_LINE_CHARS
    cap = target_lines * per_line
    if target_lines <= 1:
        floor = ceil(measure.FULL_LINE_FILL * per_line)
    else:
        floor = ceil(((target_lines - 1) + measure.LAST_LINE_FILL) * per_line)
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
        "read together: the shared theme, the logical order, and — if the block's purpose "
        "is not obvious from the atoms — the high-level context the FIRST bullet should "
        "establish (e.g. what a project is at a glance). Derive the brief ONLY from the "
        "given atoms; never introduce a fact, tool, metric, or claim not present in them. "
        "The brief guides phrasing only; it is not itself a bullet."
    )
    user = f"""TARGET JOB: {job_title}

JOB DESCRIPTION (for emphasis only — never a source of new facts):
{jd[:2000]}

BLOCKS (each holds the atoms selected for one resume entry):
{json.dumps(blocks, ensure_ascii=False, indent=1)}

Return ONLY JSON: {{"briefs": [{{"block": "<block name>", "brief": "<1-2 sentences>"}}, ...]}}"""
    try:
        out = call(system, user, config.TIER_FLASH_LITE, json_out=True, temperature=0.2)
    except Exception:  # noqa: BLE001 - cohesion is advisory; fall back to no briefs
        return {}
    names = {b["block"] for b in blocks}
    result: Dict[str, str] = {}
    for b in out.get("briefs", []) or []:
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
    example = assets.example_text()[:1200]
    system = (
        "You write resume bullets by faithfully RE-PHRASING fact-atoms for a specific job. "
        "Each group is one bullet: if it has multiple atoms, FUSE them into a single dense "
        "line that states only what those atoms say. You are a translator turning structured "
        "facts into one polished line, not a writer inventing content.\n" + _PRINCIPLE + "\n"
        "COHESION: the bullets are grouped BY BLOCK (one job / project / leadership entry). "
        "Within a block, make the bullets read as ONE coherent story — shared framing and "
        "tense, no two bullets making the same point, ordered so they build logically. When "
        "a block carries a 'brief', follow its framing/ordering; if the brief says the block's "
        "purpose isn't obvious, let the FIRST bullet establish that context using ONLY grounded "
        "atom facts. NEVER move a fact from one group's atoms into another bullet — each bullet "
        "still re-phrases ONLY its own group's atoms.\n"
        "STYLE: past tense, no first-person pronouns, no markdown, no LaTeX, NO bold or "
        "italics. One sentence (a fused group may run to ~2 clauses). Each bullet MUST be a "
        "COMPLETE sentence that ends naturally WITHIN its own character budget (the "
        "'length_target' given below) — never write a longer sentence assuming it will be "
        "trimmed; a truncated bullet ending mid-clause is a failure. "
        "Front-load the result/impact that matters for THIS job. Open every bullet with a "
        "strong action verb chosen from the categorized list below, picking a "
        "category-appropriate verb that matches the atom's real ownership. Every bullet's "
        "opening verb MUST be DISTINCT — never reuse a leading verb anywhere on the resume "
        "(the list is large; there is always an unused, fitting choice). Numbers exactly "
        "as written. Write 'greater than or equal to' style comparisons with the symbols "
        ">= and <= (they are converted to proper math notation later).\n"
        "SPACE: a bullet that fits on ONE printed line should fill at least ~90% of it — "
        "never leave a stubby half-empty line (fold in more grounded detail from the atoms "
        "or fuse, but NEVER invent facts to pad). A bullet that wraps to multiple lines may "
        "let its last line run shorter, but it should still be at least ~75% full."
    )
    user = f"""TARGET JOB: {job_title}

JOB DESCRIPTION (for angle/emphasis only — never a source of new facts):
{jd[:2500]}

ACTION VERBS (open each bullet with one of these, grouped by category; pick a
category-appropriate verb matching the atom's real ownership, and use each leading verb at
most ONCE across the whole resume — no two bullets may start with the same verb):
{verbs}

STYLE EXEMPLAR (match this voice, length and density — NEVER copy its facts):
{example}

BLOCKS (write exactly ONE bullet per gkey, re-phrasing ONLY that group's atoms; make
each block's bullets cohere per its 'brief' when present):
{json.dumps(payload, ensure_ascii=False, indent=1)}

LENGTH (hard ceiling): each bullet's "length_target" gives a character cap. Write a
COMPLETE sentence that fits within that cap and ends naturally — a 2-line target
wants a dense, fully-developed line; a 1-line target wants one tight, self-contained
line. Do NOT exceed the cap and do NOT end mid-clause expecting truncation. Never
invent facts to pad and never drop a number to shorten.

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
    # Mechanical constrained edit (fix grounding on one bullet) -> cheapest tier.
    out = call(system, user, config.TIER_FLASH_LITE, json_out=True, temperature=0.0)
    return (out.get("text") or "").strip()


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

JOB CONTEXT (emphasis only): {jd[:1500]}

ALREADY-USED LEADING VERBS (do NOT start the bullet with any of these): {taken}

ACTION VERBS (grouped by category; choose an UNUSED one that fits the atom's ownership):
{palette}

PREVIOUS BULLET (keep the same facts; only change the opening verb): {bad_text}

Return ONLY JSON: {{"text": "<rewritten bullet>"}}"""
    out = call(system, user, config.TIER_FLASH_LITE, json_out=True, temperature=0.0)
    return (out.get("text") or "").strip()


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
        "UNCHANGED -- never pad with filler.\n" + _PRINCIPLE
    )
    user = f"""TARGET JOB: {job_title}

JOB DESCRIPTION (for emphasis only -- never a source of new facts):
{jd[:2000]}

BULLETS TO LENGTHEN (re-phrase each to fill its line using its own atoms PLUS the one extra
atom; keep all existing facts and the opening verb; return the text UNCHANGED if the extra
atom adds nothing that fits):
{json.dumps(payload, ensure_ascii=False, indent=1)}

Return ONLY JSON: {{"bullets": [{{"gkey": "<gkey>", "text": "<lengthened or unchanged bullet>"}}, ...]}}"""
    try:
        out = call(system, user, config.TIER_FLASH, json_out=True, temperature=0.2)
    except Exception:  # noqa: BLE001 - fill is advisory; leave bullets unchanged on any failure
        return bullets

    new_text: Dict[str, str] = {}
    seen = {c["gk"] for c in candidates}
    for b in out.get("bullets", []) or []:
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


def _finalize_skill_lines(out: Dict[str, Any]) -> List[Dict[str, str]]:
    """Resolve the best-N skills per line: take the model's relevance-ranked picks,
    complete from the pool up to the target if it under-returned, then trim from the
    tail (least relevant) to the one-printed-line cap. No fill floor — a short list
    of relevant skills stays short. Always returns the four labeled lines."""
    targets = layout.skill_targets()
    pools = _skill_pools()
    lines: List[Dict[str, str]] = []
    for label, _keys in _SKILL_BUCKETS:
        picked = _complete_to_count(out.get(label) or "", pools.get(label, []),
                                    targets.get(label, 0))
        items = _cap_items(label, ", ".join(picked))
        if items:
            lines.append({"label": label, "items": items})
    return lines


def _complete_to_count(items: str, pool: List[str], target: int) -> List[str]:
    """Best-N selection for one skills line. Start from the model's items in its
    relevance order (kept verbatim — merged tokens like 'Gemini/OpenAI/Claude API'
    and '(conceptual)' qualifiers are preserved); if it returned fewer than
    `target`, append still-unused pool skills in pool order (the least-relevant
    tail) until the line has min(target, len(pool)) items; then cap the count at
    `target`. No char floor — the printed-line cap (applied later) is the only size
    limit, so a genuinely short list is never padded to fill the line."""
    picked: List[str] = []
    seen = set()
    for tok in (t.strip() for t in items.split(",")):
        if tok and tok.lower() not in seen:
            picked.append(tok)
            seen.add(tok.lower())
    if target > 0:
        # atoms already shown: each picked token plus its "/"- and space-delimited
        # parts, so completing the line never re-adds a skill already inside a merged
        # token ('Gemini' in 'Gemini/OpenAI/Claude API') — while single-char skills
        # like 'C'/'R' are NOT falsely matched as substrings of 'JavaScript'.
        present = set()
        for p in picked:
            pl = p.lower()
            present.add(pl)
            present.update(pl.replace("/", " ").split())
        for cand in pool:
            if len(picked) >= target:
                break
            cl = cand.lower()
            if cl in present:
                continue
            picked.append(cand)
            present.add(cl)
            present.update(cl.replace("/", " ").split())
        picked = picked[:target]
    return picked


def _cap_items(label: str, items: str) -> str:
    """Keep whole comma-separated tokens while the rendered skills line (bold label +
    items) fits ONE printed line by real glyph width (measure.skill_line_width) — never
    cut mid-token, never wrap. The first token is always kept (a line is never emptied),
    so a lone over-wide token still renders rather than vanishing."""
    toks = [t.strip() for t in items.split(",") if t.strip()]
    kept: List[str] = []
    for t in toks:
        if kept and measure.skill_line_width(label, ", ".join(kept + [t])) > measure.SKILL_LINE_CAPACITY:
            break
        kept.append(t)
    return ", ".join(kept)


def compress_skills(jd: str, job_title: str, sel: Dict[str, Any]) -> List[Dict[str, str]]:
    """Resolve the 4 fixed skill lines.

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
        "Select the candidate's technical skills into EXACTLY FOUR fixed lines: "
        "'Languages', 'Frameworks', 'Developer Tools', 'Libraries'. "
        "Selection only — only include skills present in that line's pool. "
        "RANK each line's pool by relevance to THIS job and return the BEST few, most-relevant "
        "FIRST: aim ~7 Languages, ~7 Frameworks, ~10 Developer Tools, ~10 Libraries, or all of "
        "a smaller pool. Lead with every skill the JD explicitly mentions or strongly implies, "
        "then the strongest complementary skills (adjacent languages, transferable tools). Do "
        "NOT pad with weak/unrelated filler to reach the count — a few sharp skills beat a long "
        "list. You MAY merge closely-related API entries into one compact token (e.g. "
        "'Gemini/OpenAI/Claude API'). Preserve confidence qualifiers like '(conceptual)' / "
        "'(from scratch)' verbatim."
    )
    user = f"""TARGET JOB: {job_title}  (focus hint: {skill_focus})

JOB DESCRIPTION:
{jd[:4000]}

POOLS (pick each line's items only from its pool):
Languages: {json.dumps(pools["Languages"], ensure_ascii=False)}
Frameworks: {json.dumps(pools["Frameworks"], ensure_ascii=False)}
Developer Tools: {json.dumps(pools["Developer Tools"], ensure_ascii=False)}
Libraries: {json.dumps(pools["Libraries"], ensure_ascii=False)}

Rules:
- Return each line ranked most-relevant-first: aim ~7 Languages, ~7 Frameworks, ~10 Developer Tools, ~10 Libraries — or all of a smaller pool. JD-matching skills first, then adjacent/complementary skills that add signal.
- Don't pad to hit the count with obscure or unrelated items — a few sharp, relevant skills beat a long list. Lead with the items this JOB cares about most.

Return ONLY JSON: {{"Languages": "Python, SQL, R", "Frameworks": "...", "Developer Tools": "...", "Libraries": "..."}}"""
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
        "clauses. Never add anything. Keep each bullet's OPENING VERB unchanged (the "
        "openers are unique across the resume). Plain text, no markup. " + _PRINCIPLE
    )
    user = (
        f"The resume is {pages} pages; it must be 1. Tighten each bullet by ~20-30%% "
        "without losing any fact or number, and keep its first word (the action verb) "
        "exactly as given.\n\nBULLETS:\n"
        + json.dumps([{"gkey": k, "text": v} for k, v in bullets.items()], ensure_ascii=False, indent=1)
        + '\n\nReturn ONLY JSON: {"bullets": [{"gkey": "...", "text": "..."}, ...]}'
    )
    try:
        # Mechanical batch tighten (trim wording to fit one page) -> cheapest tier.
        out = call(system, user, config.TIER_FLASH_LITE, json_out=True, temperature=0.0)
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
