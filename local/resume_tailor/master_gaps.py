"""Smarter master_experience.yaml: surface JD skills you might have but forgot to
list, and (on your confirmation) fold them into the skills taxonomy.

The flow (PLAN stage 5 — "smarter master_experience"):

  1. find_gap_keywords()  — deterministic: JD keywords NOT already in your skill
     pool. These are candidates you might own but never wrote down.
  2. screen_candidates()  — flash-lite: keep only genuine, NON-IDENTIFYING,
     resume-worthy skills (drops company names, locations, generic filler).
  3. place_skills()       — flash-lite: map each kept skill to the best-fit
     existing skills bucket (falls back to a sensible default bucket).
  4. preview_additions()  — render the exact yaml change + a unified diff so the
     edit is fully REVIEWABLE before anything is written.
  5. apply_to_file()      — opt-in: insert the confirmed skills into their buckets,
     preserving every comment in the file, after backing it up.

Nothing is written without an explicit apply call: the user confirms first. The
select-and-rephrase rule still holds — we only *suggest*; the human owns the truth.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import assets, ats, config
from .llm import call


@dataclass
class GapProposal:
    """A reviewable proposal: which JD skills are missing, kept, and where they'd go."""
    gap_keywords: List[str] = field(default_factory=list)   # JD terms absent from the pool
    confirmable: List[str] = field(default_factory=list)    # screened, non-identifying skills
    placements: Dict[str, List[str]] = field(default_factory=dict)  # bucket -> [skills]

    def is_empty(self) -> bool:
        return not self.confirmable


# ── Step 1: deterministic gap detection ──────────────────────────────────────
def candidate_skill_terms(master: Optional[dict] = None) -> set[str]:
    """Lowercased set of every skill already in the taxonomy (qualifiers stripped)."""
    master = master if master is not None else assets.load_master()
    out: set[str] = set()
    for pool in (master.get("skills", {}) or {}).values():
        for item in pool or []:
            name = re.sub(r"\(.*?\)", "", str(item)).strip().lower()
            if name:
                out.add(name)
    return out


def find_gap_keywords(jd_text: str, master: Optional[dict] = None) -> List[str]:
    """JD keywords not already present in the candidate's skill pool, in JD-relevance
    order. These are the 'you might have this but didn't list it' candidates."""
    have = candidate_skill_terms(master)
    # A JD synonym of an owned concept (an anchored alias) is already covered — fold the
    # alias spellings in so it is not proposed as a gap the candidate "lacks".
    have |= {sp.lower() for _canon, aliases in ats.anchored_alias_groups() for sp in aliases}
    gaps: List[str] = []
    for kw in ats.extract_keywords(jd_text):
        if kw.lower() not in have:
            gaps.append(kw)
    return gaps


# ── Step 2: flash-lite screen (keep only real, non-identifying skills) ────────
def screen_candidates(candidates: List[str]) -> List[str]:
    """Keep only items that are genuine, resume-worthy, NON-IDENTIFYING skills/tools/
    methods — dropping company names, locations, person names, and generic filler.
    Conservative: on any model error, return [] (suggest nothing rather than junk)."""
    if not candidates:
        return []
    system = (
        "You screen candidate resume SKILLS. Keep only items that are genuine, "
        "resume-worthy technical or professional skills, tools, frameworks, or "
        "methodologies that a person could plausibly possess. DROP anything that is: "
        "a company/employer name, a location, a person's name, a job title, a benefit/"
        "perk, or generic filler (e.g. 'communication' is fine, 'fast-paced' is not). "
        "Never invent new items; only keep or drop the ones given."
    )
    user = (
        "Items:\n" + json.dumps(candidates, ensure_ascii=False)
        + '\n\nReturn ONLY JSON: {"keep": ["..."]}'
    )
    try:
        out = call(system, user, config.TIER_FLASH_LITE, json_out=True, temperature=0.0)
    except Exception:
        return []
    kept = out.get("keep", []) if isinstance(out, dict) else []
    # Only allow items that were actually offered (model must not add new ones).
    offered = {c.lower(): c for c in candidates}
    return [offered[k.lower()] for k in kept if isinstance(k, str) and k.lower() in offered]


# ── Step 3: flash-lite placement into the best-fit bucket ─────────────────────
def place_skills(skills: List[str], buckets: List[str]) -> Dict[str, List[str]]:
    """Map each skill to the best-fit existing bucket. Falls back to the last bucket
    (typically the broad 'concepts_and_methodologies' catch-all) for anything the
    model can't place or that comes back invalid."""
    if not skills or not buckets:
        return {}
    default_bucket = buckets[-1]
    placements: Dict[str, List[str]] = {}
    valid = set(buckets)
    mapping: Dict[str, str] = {}
    system = (
        "Assign each SKILL to the single best-fit BUCKET from the provided list. "
        "Use ONLY bucket names from the list. Return one bucket per skill."
    )
    user = (
        "BUCKETS:\n" + json.dumps(buckets, ensure_ascii=False)
        + "\n\nSKILLS:\n" + json.dumps(skills, ensure_ascii=False)
        + '\n\nReturn ONLY JSON: {"assignments": {"<skill>": "<bucket>"}}'
    )
    try:
        out = call(system, user, config.TIER_FLASH_LITE, json_out=True, temperature=0.0)
        raw = out.get("assignments", {}) if isinstance(out, dict) else {}
        if isinstance(raw, dict):
            mapping = {str(k): str(v) for k, v in raw.items()}
    except Exception:
        mapping = {}
    for skill in skills:
        bucket = mapping.get(skill)
        if bucket not in valid:
            bucket = default_bucket
        placements.setdefault(bucket, []).append(skill)
    return placements


# ── Orchestration ────────────────────────────────────────────────────────────
def propose(jd_text: str, master: Optional[dict] = None) -> GapProposal:
    """Build the full reviewable proposal (no writes)."""
    master = master if master is not None else assets.load_master()
    gaps = find_gap_keywords(jd_text, master)
    confirmable = screen_candidates(gaps)
    buckets = list((master.get("skills", {}) or {}).keys())
    placements = place_skills(confirmable, buckets)
    return GapProposal(gap_keywords=gaps, confirmable=confirmable, placements=placements)


# ── Step 4/5: reviewable insertion that PRESERVES comments ────────────────────
def _find_skills_block(text: str) -> int:
    """Index just after the top-level `skills:` key (start of the skills section)."""
    m = re.search(r"(?m)^skills:\s*$", text)
    if not m:
        raise ValueError("No top-level `skills:` block found in master_experience.yaml")
    return m.end()


def _bucket_span(text: str, skills_start: int, bucket: str) -> Optional[tuple[int, int]]:
    """(start, end) char span of `bucket: [ ... ]` (single- or multi-line flow list)
    located within the skills section. None if the bucket isn't present as a flow list."""
    m = re.search(rf"(?m)^( *){re.escape(bucket)}:\s*\[", text[skills_start:])
    if not m:
        return None
    start = skills_start + m.start()
    # Walk to the matching closing bracket (skill names never contain brackets).
    depth = 0
    i = skills_start + m.end() - 1  # at the opening '['
    while i < len(text):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return start, i  # index OF the closing ']'
        i += 1
    return None


def _safe_item(item: str) -> bool:
    """Reject anything that could break the yaml flow list when inserted as a
    quoted scalar (defense-in-depth — inputs are already lexicon/JD-derived)."""
    return not any(c in item for c in ('"', "]", "[", "\n", "\r", "\\"))


def _insert_items(text: str, close_idx: int, items: List[str]) -> str:
    """Insert quoted items just before the flow list's closing ']' at close_idx.
    Items with yaml-breaking characters are skipped rather than corrupt the file."""
    addition = "".join(f', "{it}"' for it in items if _safe_item(it))
    return text[:close_idx] + addition + text[close_idx:]


def preview_additions(placements: Dict[str, List[str]],
                      master_text: Optional[str] = None) -> tuple[str, str]:
    """Return (new_text, unified_diff) for the proposed skill additions, WITHOUT
    writing. Buckets that aren't flow lists are skipped (reported in the diff header)."""
    if master_text is None:
        master_text = config.MASTER_YAML.read_text(encoding="utf-8")
    new_text = master_text
    skills_start = _find_skills_block(new_text)
    skipped: List[str] = []
    # Apply right-to-left by span so earlier offsets stay valid.
    spans: List[tuple[int, str, List[str]]] = []
    for bucket, items in placements.items():
        if not items:
            continue
        span = _bucket_span(new_text, skills_start, bucket)
        if span is None:
            skipped.append(bucket)
            continue
        spans.append((span[1], bucket, items))
    for close_idx, _bucket, items in sorted(spans, key=lambda s: -s[0]):
        new_text = _insert_items(new_text, close_idx, items)
    diff = "".join(difflib.unified_diff(
        master_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile="master_experience.yaml", tofile="master_experience.yaml (proposed)",
    ))
    if skipped:
        diff = f"# NOTE: buckets not found as flow lists, skipped: {skipped}\n" + diff
    return new_text, diff


def apply_to_file(placements: Dict[str, List[str]], path: Optional[Path] = None,
                  *, backup: bool = True) -> str:
    """Write the confirmed additions into master_experience.yaml, preserving all
    comments. Backs the file up to <name>.bak first. Returns the unified diff."""
    path = path or config.MASTER_YAML
    original = path.read_text(encoding="utf-8")
    new_text, diff = preview_additions(placements, original)
    if new_text == original:
        return diff
    if backup:
        path.with_suffix(path.suffix + ".bak").write_text(original, encoding="utf-8")
    path.write_text(new_text, encoding="utf-8")
    assets.load_master.cache_clear()
    assets.skill_aliases.cache_clear()
    assets.atoms_by_id.cache_clear()
    assets.blocks.cache_clear()
    return diff


# ── CLI ──────────────────────────────────────────────────────────────────────
def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description="Surface JD skills missing from master_experience.yaml and "
                    "(optionally) fold the confirmed ones in."
    )
    ap.add_argument("--jd-file", required=True, help="path to a job-description text file")
    ap.add_argument("--apply", action="store_true",
                    help="write the proposed additions (otherwise just preview the diff)")
    args = ap.parse_args()

    jd = Path(args.jd_file).read_text(encoding="utf-8")
    prop = propose(jd)
    if prop.is_empty():
        print("No non-identifying gap skills found — your master file already covers the JD.")
        return
    print("Skills the JD wants that aren't in your master file (confirm those you truly have):")
    for bucket, items in prop.placements.items():
        print(f"  [{bucket}] {', '.join(items)}")
    _new, diff = preview_additions(prop.placements)
    print("\nProposed change:\n" + (diff or "(nothing to change)"))
    if args.apply:
        apply_to_file(prop.placements)
        print("\nApplied. Backup written to master_experience.yaml.bak — review and re-tailor.")
    else:
        print("\nPreview only. Re-run with --apply to write (a .bak backup is made).")


if __name__ == "__main__":
    main()
