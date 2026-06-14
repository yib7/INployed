"""Load and cache the four tailoring inputs from resume_tailor_files/.

- master_experience.yaml -> parsed dict, a flat atom index, and block structure
- resume_template.tex     -> the static head (preamble + header + Education),
                             reused verbatim so output matches the example's look
- active-verb-list PDF    -> extracted once to active_verbs.txt
- example resume PDF      -> extracted text, used as a style exemplar in prompts
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List

import yaml

from . import config

# The body sections begin at this marker in resume_template.tex. Everything
# before it (preamble, name/contact header, Education) is job-independent and
# reused verbatim.
_BODY_MARKER = "%-----------EXPERIENCE-----------"


@lru_cache(maxsize=1)
def load_master() -> Dict[str, Any]:
    with config.MASTER_YAML.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


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
    for l in master.get("leadership", []):
        add("leadership", l.get("org", "?"), l.get("achievements", []))
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
    for l in master.get("leadership", []):
        out["leadership"].append({
            "name": l.get("org"), "dates": l.get("dates"),
            "atoms": [a["id"] for a in l.get("achievements", []) if a.get("id")],
        })
    return out


@lru_cache(maxsize=1)
def template_head() -> str:
    text = config.TEMPLATE_TEX.read_text(encoding="utf-8")
    idx = text.find(_BODY_MARKER)
    if idx == -1:
        raise ValueError(f"Body marker {_BODY_MARKER!r} not found in template.")
    return text[:idx].rstrip() + "\n\n"


def _pdf_text(path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages).strip()


@lru_cache(maxsize=1)
def active_verbs() -> str:
    """Extract the action-verb list once, cache to active_verbs.txt, reuse."""
    if config.VERBS_TXT_CACHE.exists():
        return config.VERBS_TXT_CACHE.read_text(encoding="utf-8")
    text = _pdf_text(config.VERBS_PDF)
    try:
        config.VERBS_TXT_CACHE.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return text


@lru_cache(maxsize=1)
def example_text() -> str:
    try:
        return _pdf_text(config.EXAMPLE_PDF)
    except Exception:
        return ""
