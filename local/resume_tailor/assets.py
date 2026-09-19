"""Load and cache the tailoring inputs from resume_tailor_files/.

- master_experience.yaml -> parsed dict, a flat atom index, block structure, the
                             optional `tailor:` layout config, the optional
                             `letter.seed` voice sample, and the plain-text
                             flattening of its entries that the cover letter and
                             the per-job chat both read
- resume_template.tex     -> the LaTeX preamble (candidate-independent), reused
                             verbatim; header/Education/body are rendered from the yaml
- style_exemplar.txt      -> the curated one-bullet-per-line voice sample used in
                             prompts; falls back to the example resume PDF's
                             extracted text, then to "" (see example_text)
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from . import config

# Everything up to and including \begin{document} is the job-AND-candidate-
# independent preamble (page geometry, fonts, the \resume* macros). It is reused
# verbatim. The name/contact header, Education, and every body section are
# generated from master_experience.yaml in render.py, so the tracked template
# carries no personal data and works for any user.
_PREAMBLE_MARKER = "\\begin{document}"


def master_source() -> Path:
    """The file `load_master` will actually read.

    Exists so a caller can tell the user's own master apart from the committed
    example, which `load_master` silently falls back to. That fallback is what
    keeps a fresh clone and CI working, but it also means a brand-new user gets
    a résumé built entirely from demo data with nothing saying so — and the whole
    contract of this engine is that every bullet traces to a fact the USER wrote.
    """
    path = config.MASTER_YAML
    if not path.exists():
        example = path.with_name("master_experience.example.yaml")
        if example.exists():
            return example
    return path


def using_example_master() -> bool:
    """True when no personal master exists and the committed example is standing in."""
    return master_source() != config.MASTER_YAML


@lru_cache(maxsize=1)
def load_master() -> Dict[str, Any]:
    # No personal master configured yet (e.g. a fresh clone before setup.ps1, or
    # CI): master_source falls back to the committed example so the engine and
    # the test suite work with demo data instead of crashing on a missing file.
    path = master_source()
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        # Neither the personal master nor the committed example is on disk.
        raise ValueError(
            f"No resume master file found at {path}. Run scripts/setup.ps1, or "
            f"copy resume_tailor_files/master_experience.example.yaml to "
            f"resume_tailor_files/master_experience.yaml and fill it in."
        ) from None
    except yaml.YAMLError as exc:
        # The user hand-edits this file, so a broken edit is the likeliest way
        # the tailor fails. Say which file, where, and what fixes it -- a raw
        # ParserError traceback in the dashboard's error dialog does not.
        where = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            where = f" at line {mark.line + 1}, column {mark.column + 1}"
        raise ValueError(
            f"{path.name} is not valid YAML{where}: "
            f"{getattr(exc, 'problem', None) or exc}. Fix that line (indentation "
            f"and unclosed brackets are the usual causes) or compare against "
            f"master_experience.example.yaml."
        ) from None
    if not isinstance(data, dict):
        raise ValueError(
            f"{path.name} must be a YAML mapping (got "
            f"{type(data).__name__ if data is not None else 'empty file'}) "
            "- see master_experience.example.yaml")
    return data


# ── the cover letter's two master-file inputs ────────────────────────────────
# The seed is the candidate's own two-to-four sentences on what they want next
# (`letter.seed`), and the background is their notes behind the bullets that made
# the page. Both are bounded here, once, so neither prompt can grow with the
# master file: a seed is a voice sample, and the background is an excerpt.
LETTER_SEED_CAP = 1200
LETTER_BACKGROUND_CAP = 6000

# Appended when a flattened excerpt was cut, so the model knows it is reading a
# part of the record. chat.py carries the same marker for its own excerpts.
TRUNCATED_MARKER = "[... truncated ...]"


def letter_seed() -> str:
    """The optional `letter.seed` text from the master, trimmed and capped.

    Blank when the block is absent, empty, or the wrong shape (a non-mapping
    `letter:` or a non-string `seed`): the letter then runs without a seed, and
    master_validate reports the malformed block as a warning. A seed over
    LETTER_SEED_CAP characters is cut at the last whitespace before the cap, so
    the cut never lands mid-word (a seed with no whitespace at all is cut at
    the cap)."""
    letter = load_master().get("letter")
    if not isinstance(letter, dict):
        return ""
    seed = letter.get("seed")
    if not isinstance(seed, str):
        return ""
    seed = seed.strip()
    if len(seed) <= LETTER_SEED_CAP:
        return seed
    head = seed[:LETTER_SEED_CAP]
    if not seed[LETTER_SEED_CAP].isspace():
        cut = max(head.rfind(ch) for ch in (" ", "\n", "\t"))
        if cut > 0:
            head = head[:cut]
    return head.rstrip()


def atom_line(atom: Dict[str, Any]) -> str:
    """One achievement atom flattened to a single readable line: `what; how;
    scope; impact...`, blank fields dropped, a list impact joined in order."""
    parts: List[str] = []
    for key in ("what", "how", "scope"):
        val = str(atom.get(key) or "").strip()
        if val:
            parts.append(val)
    impact = atom.get("impact")
    if isinstance(impact, (list, tuple)):
        parts += [str(i).strip() for i in impact if str(i or "").strip()]
    elif str(impact or "").strip():
        parts.append(str(impact).strip())
    return "; ".join(parts)


def entry_lines(master: Dict[str, Any], section: str, *name_keys: str,
                entry_atoms: Optional[Iterable[str]] = None) -> List[str]:
    """One section of the master as indented plain-text lines: a `- header`
    line per entry (the named keys joined, then the dates in parentheses) and a
    `    - atom` line per achievement.

    `entry_atoms` is an ENTRY-level filter: with it given, an entry is listed,
    in full, when it owns at least one of those atom ids, and left out
    altogether otherwise. With it None (chat's use) every entry is listed and
    the output is what chat._entries always produced."""
    marks = None if entry_atoms is None else set(entry_atoms)
    lines: List[str] = []
    entries = master.get(section) or []
    if not isinstance(entries, list):
        return lines
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        achievements = [a for a in (entry.get("achievements") or []) if isinstance(a, dict)]
        if marks is not None and not any(a.get("id") in marks for a in achievements):
            continue
        head = ", ".join(
            s for s in (str(entry.get(k) or "").strip() for k in name_keys) if s)
        dates = str(entry.get("dates") or "").strip()
        if dates:
            head = f"{head} ({dates})" if head else dates
        if head:
            lines.append(f"- {head}")
        for atom in achievements:
            text = atom_line(atom)
            if text:
                lines.append(f"    - {text}")
    return lines


def flatten_entries(master: Dict[str, Any], *, entry_atoms: Optional[Iterable[str]] = None,
                    cap: int = LETTER_BACKGROUND_CAP) -> str:
    """The master's experience, projects and leadership entries as one bounded
    plain-text block, in that order, each section under its own label.

    This is the cover letter's BACKGROUND: the notes behind the bullets. The
    tailor run passes the ids of the atoms that made the page and gets every
    entry that owns one of them, in full (the letter tells an employer's story,
    so it gets all the notes on that employer); the standalone letter passes
    None and gets every entry. The result never exceeds `cap` characters: when
    the text is longer it is cut on the last line boundary that fits and
    TRUNCATED_MARKER is appended, so no atom line is ever sent half-finished (a
    cut number is the one thing an excerpt must not invent)."""
    lines: List[str] = []
    for section, keys in (("experience", ("org", "title")),
                          ("projects", ("name",)),
                          ("leadership", ("org", "role", "title"))):
        block = entry_lines(master, section, *keys, entry_atoms=entry_atoms)
        if block:
            lines.append(f"{section.upper()}:")
            lines += block
    text = "\n".join(lines)
    if len(text) <= cap:
        return text
    room = max(0, cap - len(TRUNCATED_MARKER))
    cut = text.rfind("\n", 0, room)
    kept = text[:cut + 1] if cut > 0 else text[:room]
    return kept + TRUNCATED_MARKER


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


def full_url(value) -> str:
    """A clickable absolute URL from a stored link value. The master yaml may hold
    either a bare host+path (linkedin.com/in/x) or a full URL — strip and return
    scheme-prefixed values as-is (case-insensitive), otherwise prepend https://.
    Empty/None -> ""."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith(("http://", "https://")):
        return text
    return f"https://{text}"


def _pdf_text(path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages).strip()


def _exemplar_lines(path) -> str:
    """The curated style exemplar as bullet lines: one bullet per line, `#` comment
    lines and blanks dropped, every line stripped of trailing whitespace.

    Returns "" for a file that is missing, unreadable, or holds nothing but comments —
    the caller then falls back to the PDF rather than sending the model an empty (or
    comment-only) exemplar."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    kept = [line.strip() for line in text.splitlines()]
    return "\n".join(ln for ln in kept if ln and not ln.startswith("#"))


@lru_cache(maxsize=1)
def example_text() -> str:
    """The style exemplar injected (bounded) into the rephrase prompt, resolved in
    priority order: the curated style_exemplar.txt, else the sample PDF's extracted
    text, else "".

    The PDF arm is the original source and stays so an existing install that never
    writes the .txt behaves exactly as before; the curated arm exists because that
    extract is a whole résumé page — name/contact/education before the first bullet,
    next-section headings glued onto bullet tails, column collisions — of which the
    prompt could only afford the first slice. See config.STYLE_EXEMPLAR_TXT.

    Swallows everything on purpose: the exemplar is a nice-to-have, and no tailoring
    run may die because a personal file is absent or malformed."""
    try:
        curated = _exemplar_lines(config.STYLE_EXEMPLAR_TXT)
    except Exception:  # noqa: BLE001 - e.g. a non-UTF-8 file; fall through to the PDF
        curated = ""
    if curated:
        return curated
    try:
        return _pdf_text(config.EXAMPLE_PDF)
    except Exception:
        return ""


# A built-in palette used only when active_words.md is missing/unparseable (fresh clone,
# CI, or a user who deleted it) — keeps the engine working with a sane verb set. The real
# source is the curated, categorized resume_tailor_files/active_words.md.
#
# Curated rather than extracted, and that was a measured decision: the openers used to
# come from the 6KB raw résumé-PDF dump (jumbled multi-column OCR — weak signal AND
# expensive) and the model only needs a clean set of verbs, so this is both cheaper and
# better. `compose._CORE_VERBS` used to record that here; it was a dead duplicate of this
# list and was deleted, so the note lives with the list it describes.
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
