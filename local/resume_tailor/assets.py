"""Load and cache the tailoring inputs from resume_tailor_files/.

- master_experience.yaml -> parsed dict, a flat atom index, block structure, and
                             the optional `tailor:` layout config
- resume_template.tex     -> the LaTeX preamble (candidate-independent), reused
                             verbatim; header/Education/body are rendered from the yaml
- example resume PDF      -> extracted text, used as a style exemplar in prompts
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, List

import yaml

from . import config

# Everything up to and including \begin{document} is the job-AND-candidate-
# independent preamble (page geometry, fonts, the \resume* macros). It is reused
# verbatim. The name/contact header, Education, and every body section are
# generated from master_experience.yaml in render.py, so the tracked template
# carries no personal data and works for any user.
_PREAMBLE_MARKER = "\\begin{document}"


@lru_cache(maxsize=1)
def load_master() -> Dict[str, Any]:
    path = config.MASTER_YAML
    if not path.exists():
        # No personal master configured yet (e.g. a fresh clone before setup.ps1,
        # or CI): fall back to the committed example so the engine and the test
        # suite work with demo data instead of crashing on a missing file.
        example = path.with_name("master_experience.example.yaml")
        if example.exists():
            path = example
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(
            f"{path.name} must be a YAML mapping (got "
            f"{type(data).__name__ if data is not None else 'empty file'}) "
            "- see master_experience.example.yaml")
    return data


@lru_cache(maxsize=1)
def tailor_config() -> Dict[str, Any]:
    """Optional top-level `tailor:` block: which blocks are required to render and
    the hard per-block line budgets for the template's fixed sections. Absent ->
    {} (compose.py then falls back to sensible defaults). See the example yaml for
    the schema. This is what makes the layout config-driven for any user instead of
    hardcoding one person's org names."""
    return load_master().get("tailor") or {}


def _load_alias_map(key: str) -> Dict[str, List[str]]:
    """Parse a top-level alias map (canonical -> [spellings]) from the master. Permissive:
    non-string canonicals are skipped, a scalar alias is promoted to a one-element list,
    blanks dropped. Anchoring (canonical must be a real skill) is enforced downstream in
    ats, so this loader does no anchoring. Absent/malformed -> {}."""
    raw = load_master().get(key) or {}
    out: Dict[str, List[str]] = {}
    if isinstance(raw, dict):
        for canon, aliases in raw.items():
            if not isinstance(canon, str):
                continue
            if isinstance(aliases, str):
                aliases = [aliases]
            if isinstance(aliases, (list, tuple)):
                out[canon] = [str(a).strip() for a in aliases if str(a).strip()]
    return out


@lru_cache(maxsize=1)
def skill_aliases() -> Dict[str, List[str]]:
    """Optional top-level `skill_aliases:` map: canonical skill -> [JD spellings the
    ATS/JD may use for that same concept]. These are the PRINTABLE spelling variants —
    matched by the ATS layer AND surfaced in the JD's own spelling on the page when earned
    (the Methods concepts line, and swapped onto the four technical-skills lines). Use for
    true variants you are happy to see printed (Postgres == PostgreSQL). Each canonical
    SHOULD be a real skill in the taxonomy; anchoring is enforced downstream in
    ats.anchored_alias_groups, so this loader is permissive."""
    return _load_alias_map("skill_aliases")


@lru_cache(maxsize=1)
def skill_aliases_match_only() -> Dict[str, List[str]]:
    """Optional top-level `skill_aliases_match_only:` map: canonical skill -> [broader JD
    synonyms]. These are matched by the ATS report + gap-finder (a JD synonym of an owned
    skill counts as covered and is not proposed as a gap) but are NEVER printed/swapped onto
    the page — the candidate's stronger canonical token stays. Use for broader or weaker
    terms you do NOT want literally on the résumé (e.g. 'Large Language Models' for a specific
    'LLM APIs (Gemini, OpenAI, Claude)' token). Same shape + anchoring as skill_aliases."""
    return _load_alias_map("skill_aliases_match_only")


@lru_cache(maxsize=1)
def atoms_by_id() -> Dict[str, Dict[str, Any]]:
    """Flat {atom_id: atom + provenance}. Atom ids are unique across the file."""
    master = load_master()
    index: Dict[str, Dict[str, Any]] = {}

    def add(section: str, block_name: str, achievements: List[dict]) -> None:
        for atom in achievements or []:
            aid = atom.get("id")
            if not aid:
                continue
            if aid in index:
                raise ValueError(f"Duplicate atom id {aid!r} (in {block_name})")
            index[aid] = {**atom, "_section": section, "_block": block_name}

    for e in master.get("experience", []):
        add("experience", e.get("org", "?"), e.get("achievements", []))
    for p in master.get("projects", []):
        add("projects", p.get("name", "?"), p.get("achievements", []))
    for ld in master.get("leadership", []):
        add("leadership", ld.get("org", "?"), ld.get("achievements", []))
    return index


@lru_cache(maxsize=1)
def blocks() -> Dict[str, List[Dict[str, Any]]]:
    """Ordered block structure with each block's available atom ids."""
    master = load_master()
    out: Dict[str, List[Dict[str, Any]]] = {"experience": [], "projects": [], "leadership": []}
    for e in master.get("experience", []):
        out["experience"].append({
            "name": e.get("org"), "title": e.get("title"), "location": e.get("location"),
            "dates": e.get("dates"),
            "atoms": [a["id"] for a in e.get("achievements", []) if a.get("id")],
        })
    for p in master.get("projects", []):
        out["projects"].append({
            "name": p.get("name"), "dates": p.get("dates"),
            "live_url": p.get("live_url"), "repo": p.get("repo"),
            "atoms": [a["id"] for a in p.get("achievements", []) if a.get("id")],
        })
    for ld in master.get("leadership", []):
        out["leadership"].append({
            "name": ld.get("org"), "dates": ld.get("dates"),
            "atoms": [a["id"] for a in ld.get("achievements", []) if a.get("id")],
        })
    return out


@lru_cache(maxsize=1)
def template_head() -> str:
    """The LaTeX preamble through \\begin{document} (everything candidate-
    independent). The header/Education/body are rendered from the yaml.

    Matches the marker only at the start of a line so a mention inside a comment
    (e.g. the template's own explanatory header) never truncates the preamble."""
    text = config.TEMPLATE_TEX.read_text(encoding="utf-8")
    m = re.search(r"(?m)^" + re.escape(_PREAMBLE_MARKER), text)
    if not m:
        raise ValueError(f"Preamble marker {_PREAMBLE_MARKER!r} not found at a line start.")
    return text[:m.end()].rstrip() + "\n\n"


def _pdf_text(path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages).strip()


@lru_cache(maxsize=1)
def example_text() -> str:
    try:
        return _pdf_text(config.EXAMPLE_PDF)
    except Exception:
        return ""


# A built-in palette used only when active_words.md is missing/unparseable (fresh clone,
# CI, or a user who deleted it) — keeps the engine working with a sane verb set. The real
# source is the curated, categorized resume_tailor_files/active_words.md.
_FALLBACK_VERBS: Dict[str, List[str]] = {
    "Technical Skills": [
        "Built", "Designed", "Engineered", "Developed", "Implemented", "Architected",
        "Automated", "Optimized", "Accelerated", "Reduced", "Improved", "Increased",
        "Streamlined", "Scaled", "Refactored", "Deployed", "Integrated", "Migrated",
        "Launched", "Shipped", "Analyzed", "Modeled", "Forecasted", "Quantified",
        "Evaluated", "Validated", "Diagnosed", "Researched", "Led", "Directed",
        "Coordinated", "Mentored", "Spearheaded", "Drove", "Owned", "Delivered",
        "Resolved", "Standardized", "Consolidated", "Boosted", "Generated", "Produced",
        "Trained", "Benchmarked", "Prototyped", "Instrumented",
    ],
}


@lru_cache(maxsize=1)
def active_verbs() -> Dict[str, List[str]]:
    """The curated résumé action verbs grouped by category, parsed from active_words.md.

    Format: a `## Heading` line opens a category; each following body line lists verbs
    separated by the `·` middot; `---` rules and blanks are ignored. Order (categories and
    verbs) is preserved as written. Falls back to a built-in palette when the file is
    absent or yields nothing (so the engine never loses its openers)."""
    path = config.ACTIVE_WORDS_MD
    out: Dict[str, List[str]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {k: list(v) for k, v in _FALLBACK_VERBS.items()}
    current: str = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            current = s[3:].strip()
            out.setdefault(current, [])
        elif current and s and not s.startswith("#") and s != "---":
            for token in s.split("·"):
                v = token.strip()
                if v:
                    out[current].append(v)
    out = {cat: verbs for cat, verbs in out.items() if verbs}
    return out or {k: list(v) for k, v in _FALLBACK_VERBS.items()}
