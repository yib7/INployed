"""Stage 1: select -- choose the blocks, the bullet groups and the skill focus.

Moved out of compose.py unchanged (cycle 9), together with the prompt helpers that
only `select` uses (`_catalog`, the two guidance builders, `_required_blocks`) and
the deterministic post-processing that shapes the model's answer (`_normalize_selection`
-> required blocks -> fixed order -> fixed counts -> project cap). `compose` re-exports
every name below, so `compose.select` and friends still resolve.

NOTE for monkeypatching: `call` is bound in THIS module's namespace, so a test that
stubs the LLM for `select` must patch `selection.call`, not `compose.call`. The same
goes for the helpers defined here (`_block_of`, `_block_atoms`, `_required_blocks`).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from . import assets, config, layout
from .common import _PRINCIPLE, _gkey, fence_jd
from .llm import as_dict, call
from .skills import _clean_methods, _methods_pool, _skill_pools


def _block_of(aid: str) -> str:
    return assets.atoms_by_id()[aid].get("_block", "")


def _first_atom(section: str, name: str) -> List[str]:
    """A sensible default group (the block's first atom) for required-block injection."""
    for b in assets.blocks().get(section, []):
        if b["name"] == name and b["atoms"]:
            return [b["atoms"][0]]
    return []


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


# Which blocks must always render and the hard line budgets for the fixed blocks
# are CONFIG-DRIVEN (yaml `tailor:` section) so nothing is tied to one person's
# resume. See _required_blocks below for the schema and defaults.
#
# ── Config-driven layout spec (yaml `tailor:` section) ────────────────────────
# tailor:
#   required:                       # blocks that must always render (default: all)
#     experience: all               #   'all' or a list of block names
#     leadership: [Org A, Org B]
#
# `required` is the whole schema. Per-block bullet counts and printed-line targets
# come from config.json's `resume_layout` / `project_layout` (config.block_targets,
# config.project_targets), not from this file.
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
    count; otherwise, when tiered allotment is configured (`config.project_bullet_tiers`),
    an unconfigured project aims for the LARGEST tier count — select() runs before the
    strength ranking exists, so we can't assign a per-project rank yet; aiming high makes
    the model surface enough relevant atoms for whatever lands in the top slot, and
    `_cap_projects` trims each project to its actual rank's tier count downstream. With no
    tiers, an unconfigured project uses the global `PROJECT_BULLETS_MAX`."""
    tiers = config.project_bullet_tiers()
    tier_max = max(tiers) if tiers else None
    lines: List[str] = []
    for b in assets.blocks().get("projects", []):
        name = b["name"]
        targets = config.project_targets(name)
        if targets:
            n = len(targets)
        elif tier_max is not None:
            n = tier_max
        else:
            n = config.PROJECT_BULLETS_MAX
        lines.append(f"  - {name}: aim for {n} bullet group(s), densest / most JD-relevant first.")
    if tier_max is not None:
        lines.append("  - Final bullet counts taper by project strength: the strongest "
                     "project(s) keep the most groups, weaker ones fewer.")
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
        "This step is PURE SELECTION: you write no prose. Choose which experiences, "
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
        "Do NOT pad with weak/unrelated filler just to reach the count. A few sharp, relevant "
        "skills beat a long list. Preserve any '(conceptual)' / '(from scratch)' qualifiers "
        "verbatim. You MAY merge closely-related API entries into one compact token (e.g. "
        "'Gemini/OpenAI/Claude API'). ALSO rank the candidate's concepts/methodologies (the "
        "METHODS POOL) by relevance to this job, most-relevant first, copying items verbatim "
        "from that pool (selection only, never invent) for the 'methods' output.\n"
        + _PRINCIPLE
    )
    pools = _skill_pools()
    methods_pool = _methods_pool()
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

SKILL POOLS (for the "skills" output only). Pick each line's items only from its pool, ranked most-relevant-first; aim ~7 Languages, ~7 Frameworks, ~10 Developer Tools, ~10 Libraries, or all of a smaller pool. JD matches first, then complementary skills; don't pad to hit the count:
Languages: {json.dumps(pools["Languages"], ensure_ascii=False)}
Frameworks: {json.dumps(pools["Frameworks"], ensure_ascii=False)}
Developer Tools: {json.dumps(pools["Developer Tools"], ensure_ascii=False)}
Libraries: {json.dumps(pools["Libraries"], ensure_ascii=False)}

METHODS POOL (for the "methods" output only, the candidate's concepts/methodologies; RANK by relevance to THIS job, most-relevant FIRST, and return ~8-10. SELECTION ONLY: copy items VERBATIM from this pool, never invent. These become the résumé's concepts line; lead with the concepts this role centers on (e.g. data analysis, ETL, A/B testing, modeling)):
{json.dumps(methods_pool, ensure_ascii=False)}

Selection guidance. The resume template has FIXED sections; fill them to one full page (~14-18 bullets):
- Work Experience (use the block names exactly as listed in the catalog above):
{exp_guidance}
- Projects: include ALL available projects, ORDERED STRONGEST-FIRST for THIS job; for each project produce the target number of bullet group(s) shown below (densest / most JD-relevant atoms first):
{proj_guidance}
- Leadership: ALWAYS include EVERY leadership entry. {lead_guidance}
- Line density rule: every bullet must fill at least 70% of its printed line. Never write a bullet so short it leaves more than ~30% of the line blank; fuse atoms or pick denser content instead.
- Within a PROJECT, LEAD with the bullet that introduces what the project IS (its overview / "what is this at a glance"), THEN order the remaining bullets by relevance to THIS job: a reader should know what a project is before the detail bullets. Within experience/leadership, order by relevance.

Return ONLY JSON (use the real block names + atom ids from the catalog; groups is a list of lists of atom ids):
{{
  "experience": [
    {{"name": "<experience block name>", "groups": [["<atom_id>"], ["<atom_id>", "<atom_id>"]]}}
  ],
  "projects":   [{{"name": "<project name>", "groups": [["<atom_id>"], ["<atom_id>", "<atom_id>"]]}}],
  "leadership": [{{"name": "<leadership org>", "groups": [["<atom_id>"]]}}],
  "skill_focus": "one of: ml_research | backend_platform | data_analytics | general",
  "skills": {{"Languages": "Python, SQL, R", "Frameworks": "...", "Developer Tools": "...", "Libraries": "..."}},
  "methods": ["<concept from the METHODS POOL>", "<next most relevant>", "..."],
  "rationale": "1-2 sentences (incl. why projects are ordered as they are)"
}}

Now select for THIS job, biasing toward the most JD-relevant evidence, most relevant first:
JOB: {job_title} at {company}

{fence_jd(jd, 7000, "relevance ranking and selection")}"""
    out = call(system, user, config.TIER_FLASH, json_out=True, temperature=0.1)
    return _normalize_selection(as_dict(out, "experience"))


def _normalize_selection(sel: Dict[str, Any]) -> Dict[str, Any]:
    """Validate group atom ids, dedupe globally, inject required blocks, fix order.
    Tolerates model shape drift everywhere: non-dict roots/entries are dropped, a
    flat string group is treated as a one-atom group, and a non-dict skills value
    is discarded (it would crash compress_skills' preselected path downstream)."""
    if not isinstance(sel, dict):
        sel = {}
    valid_ids = set(assets.atoms_by_id())
    bl = assets.blocks()
    names = {sec: {b["name"] for b in bl[sec]} for sec in bl}
    used: set[str] = set()

    skills = sel.get("skills")
    clean: Dict[str, Any] = {"skill_focus": sel.get("skill_focus", "general"),
                             "skills": skills if isinstance(skills, dict) else {},
                             "methods": _clean_methods(sel.get("methods")),
                             "rationale": sel.get("rationale", "")}
    for sec in ("experience", "projects", "leadership"):
        clean[sec] = []
        for entry in sel.get(sec, []) or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if name not in names[sec]:
                continue
            groups: List[List[str]] = []
            for g in entry.get("groups", []) or []:
                if isinstance(g, str):
                    g = [g]  # flat id list: each id is its own group
                if not isinstance(g, (list, tuple)):
                    continue
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
    each to its bullet count. Per-project count precedence:

      1. explicit name-keyed layout (config.project_targets) -> EXACTLY len(targets);
      2. else tiered by rank (config.project_rank_bullets) -> that many groups;
      3. else the global PROJECT_BULLETS_MAX.

    For (1) and (2) the project is resized via _resize_to_count — padded UP from its OWN
    unused atoms (fused groups preserved) as well as trimmed down — so the count is a
    TARGET, not just a ceiling. For (3) it keeps cap-only behavior (trimmed, never padded).
    Padding draws only from the project's own atoms and never force-injects, so a count is
    best-effort: a project with fewer atoms than its target stays at its atom count (the
    select-and-rephrase rule — never invent)."""
    projects = clean.get("projects", [])[:config.projects_max()]
    used: set[str] = {
        aid
        for sec in ("experience", "projects", "leadership")
        for e in clean.get(sec, [])
        for g in e["groups"]
        for aid in g
    }
    for rank, entry in enumerate(projects):
        targets = config.project_targets(entry["name"])
        tier_n = config.project_rank_bullets(rank)
        if targets:           # name-keyed -> exact line_targets count (pad up + trim down)
            _resize_to_count(entry, "projects", entry["name"], len(targets), used, singles=False)
        elif tier_n is not None:  # tiered by strength rank -> pad up + trim down to tier_n
            _resize_to_count(entry, "projects", entry["name"], tier_n, used, singles=False)
        else:                 # unconfigured -> cap only, never pad
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
