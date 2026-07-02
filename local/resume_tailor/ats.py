"""ATS keyword-coverage check: final PDF text vs. the JD's top keywords.

Deterministic (no LLM call): the keyword pool is the candidate's own skill
taxonomy from master_experience.yaml plus a built-in lexicon of common
ATS-screened tech/analytics terms, plus repeated acronyms found in the JD.
The report flags JD terms missing from the tailored PDF so the user can decide
whether a missing term is genuinely true of them (select-and-rephrase rule:
never add a skill just because the JD wants it).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

from . import assets

# Common ATS-screened terms beyond the candidate's own skill pools. Lowercase;
# matching is case-insensitive with word-ish boundaries.
BUILTIN_TERMS = (
    "python", "sql", "r", "java", "javascript", "typescript", "c++", "c#", "go",
    "scala", "rust", "bash", "powershell", "html", "css",
    "machine learning", "deep learning", "data science", "data analysis",
    "data analytics", "statistics", "statistical analysis", "a/b testing",
    "nlp", "natural language processing", "computer vision", "llm",
    "generative ai", "predictive modeling", "regression", "classification",
    "data visualization", "dashboards", "dashboarding", "reporting",
    "etl", "elt", "data pipeline", "data pipelines", "data warehouse",
    "data modeling", "data quality", "data engineering", "big data",
    "tableau", "power bi", "looker", "excel", "spreadsheets",
    "pandas", "numpy", "scikit-learn", "pytorch", "tensorflow", "keras",
    "spark", "hadoop", "kafka", "airflow", "dbt", "snowflake", "databricks",
    "postgresql", "mysql", "mongodb", "redis", "nosql",
    "aws", "azure", "gcp", "google cloud", "cloud", "docker", "kubernetes",
    "terraform", "ci/cd", "git", "github", "linux", "apis", "rest", "graphql",
    "microservices", "agile", "scrum", "jira",
    "stakeholder", "stakeholders", "communication", "cross-functional",
    "problem solving", "problem-solving", "collaboration",
)

# Acronyms that show up in JDs but are not skills.
_ACRONYM_NOISE = {
    "AND", "THE", "FOR", "YOU", "OUR", "ALL", "NOT", "ARE", "WILL", "WITH",
    "US", "USA", "USD", "EEO", "EOE", "LLC", "INC", "PTO", "401K", "EST",
    "PST", "CST", "FTE", "GPA", "ID", "OK", "NEW", "JOB", "PM", "AM", "HR",
    "CEO", "CTO", "CFO", "VP", "FAQ", "ASAP", "DEI", "ADA", "COVID",
}

_WORDISH = r"A-Za-z0-9"


def _term_pattern(term: str) -> re.Pattern:
    """Word-boundary-ish regex for a term that may contain +, #, ., or /."""
    return re.compile(
        rf"(?<![{_WORDISH}]){re.escape(term)}(?![{_WORDISH}])", re.IGNORECASE
    )


def _norm_skill(item: str) -> str:
    """Lowercased skill name with parenthetical qualifiers stripped — the matching key
    shared by the taxonomy and the alias layer: 'Exploratory Data Analysis (EDA)' and
    'exploratory data analysis' both normalize to 'exploratory data analysis'."""
    return re.sub(r"\(.*?\)", "", str(item)).strip().lower()


def _candidate_terms() -> List[str]:
    """The candidate's own skill taxonomy (every pool in master_experience.yaml)."""
    out: List[str] = []
    skills = assets.load_master().get("skills", {}) or {}
    for pool in skills.values():
        for item in pool or []:
            # strip confidence qualifiers like "(conceptual)" for matching
            name = re.sub(r"\(.*?\)", "", str(item)).strip()
            if name:
                out.append(name)
    return out


# ── Anchored skill aliases (canonical -> JD spellings) ────────────────────────
# Two alias maps with different print behavior, but the SAME matching behavior:
#   skill_aliases (printable)        -> matched AND surfaced/swapped onto the page
#   skill_aliases_match_only (broad) -> matched only, never printed
# anchored_alias_groups() is the PRINTABLE source (Methods line + tech-line swap);
# all_alias_groups() is the union used for matching (extract/coverage/gap).
def _anchor(raw: dict) -> List[Tuple[str, List[str]]]:
    """(canonical, [aliases]) for each entry whose canonical is a REAL skill in the taxonomy
    (paren-stripped match). Unanchored groups are dropped so an alias can never inject an
    untethered keyword or drift away from a true skill. Aliases are de-duplicated
    (case-insensitively) and never repeat the canonical."""
    real = {_norm_skill(t) for t in _candidate_terms()}
    groups: List[Tuple[str, List[str]]] = []
    for canon, aliases in raw.items():
        if _norm_skill(canon) not in real:
            continue
        seen = {canon.lower()}
        clean: List[str] = []
        for a in aliases:
            al = str(a).strip()
            if al and al.lower() not in seen:
                clean.append(al)
                seen.add(al.lower())
        groups.append((canon, clean))
    return groups


def anchored_alias_groups() -> List[Tuple[str, List[str]]]:
    """The PRINTABLE alias groups (from skill_aliases): matched and surfaced in the JD's own
    spelling when earned — on the Methods concepts line and swapped onto the four tech lines."""
    return _anchor(assets.skill_aliases())


def match_only_alias_groups() -> List[Tuple[str, List[str]]]:
    """The MATCH-ONLY alias groups (from skill_aliases_match_only): broader synonyms that count
    toward coverage / are not proposed as gaps, but are never printed or swapped onto the page."""
    return _anchor(assets.skill_aliases_match_only())


def all_alias_groups() -> List[Tuple[str, List[str]]]:
    """Every anchored alias group — printable + match-only — for MATCHING (extract_keywords,
    coverage, gap-finder), where both kinds behave identically: a JD synonym of an owned skill
    counts as the same skill regardless of whether it is printable."""
    return anchored_alias_groups() + match_only_alias_groups()


def alias_index() -> dict[str, Tuple[str, ...]]:
    """Normalized spelling -> the full alias group (canonical + aliases) it belongs to,
    for O(1) group lookups in extract_keywords/coverage. Built from BOTH maps so matching is
    alias-aware for printable and match-only alike. Keys are _norm_skill()'d so a paren-stripped
    canonical resolves too; the first group to claim a spelling wins."""
    idx: dict[str, Tuple[str, ...]] = {}
    for canon, aliases in all_alias_groups():
        group = (canon, *aliases)
        for spelling in group:
            idx.setdefault(_norm_skill(spelling), group)
    return idx


def extract_keywords(jd_text: str, top_n: int = 30) -> List[str]:
    """The JD's top keywords: lexicon hits ranked by frequency, then repeated acronyms
    the lexicon missed. Alias-aware: a concept and its JD synonyms count as ONE keyword
    (frequencies summed) and surface in the JD's own spelling (the most frequent spelling
    actually present), so a synonym the candidate genuinely owns is matched, not missed,
    and never double-listed against its canonical."""
    idx = alias_index()
    # Spellings to score: candidate terms + builtin lexicon + every anchored alias spelling,
    # de-duplicated case-insensitively (so a term in two pools isn't counted twice).
    spellings: list[str] = []
    seen: set[str] = set()
    alias_spellings = [a for _canon, aliases in all_alias_groups() for a in aliases]
    for t in [*_candidate_terms(), *BUILTIN_TERMS, *alias_spellings]:
        if t.lower() not in seen:
            spellings.append(t)
            seen.add(t.lower())

    # Bucket by alias-group (canonical norm) when grouped, else by the term itself; sum the
    # group's JD frequency and remember the most-frequent spelling actually present.
    counts: dict[str, int] = {}
    best: dict[str, tuple[int, str]] = {}
    for term in spellings:
        group = idx.get(_norm_skill(term))
        gk = _norm_skill(group[0]) if group else term.lower()
        n = len(_term_pattern(term).findall(jd_text))
        if n <= 0:
            continue
        counts[gk] = counts.get(gk, 0) + n
        cur = best.get(gk)
        if cur is None or n > cur[0]:
            best[gk] = (n, term)
    scored = [(counts[gk], best[gk][1]) for gk in counts]
    scored.sort(key=lambda t: (-t[0], t[1].lower()))
    keywords = [t for _, t in scored]
    seen_lower = {k.lower() for k in keywords}

    acro_counts: dict[str, int] = {}
    for acro in re.findall(r"\b[A-Z][A-Z0-9]{1,5}\b", jd_text):
        acro_counts[acro] = acro_counts.get(acro, 0) + 1
    extras = [
        a for a, n in sorted(acro_counts.items(), key=lambda kv: -kv[1])
        if n >= 2 and a not in _ACRONYM_NOISE and a.lower() not in seen_lower
    ]
    return (keywords + extras)[:top_n]


def coverage(keywords: List[str], resume_text: str) -> Tuple[float, List[str], List[str]]:
    """(fraction_present, present, missing) of keywords in the resume text. Alias-aware:
    a keyword counts present when ANY spelling in its alias group is literally on the page
    (so the canonical or a sibling synonym covers it) — a pure addition that never removes
    a literal match and never fabricates presence when no group spelling is printed."""
    idx = alias_index()
    present: List[str] = []
    for k in keywords:
        group = idx.get(_norm_skill(k))
        spellings = group if group else (k,)
        if any(_term_pattern(sp).search(resume_text) for sp in spellings):
            present.append(k)
    missing = [k for k in keywords if k not in present]
    frac = len(present) / len(keywords) if keywords else 1.0
    return frac, present, missing


def _pdf_text(pdf_path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages)


def write_report(jd_text: str, pdf_path: Path, out_dir: Path) -> float:
    """Write ats_report.txt next to the tailored PDF; return coverage fraction."""
    keywords = extract_keywords(jd_text)
    frac, present, missing = coverage(keywords, _pdf_text(pdf_path))
    lines = [
        "ATS keyword coverage report",
        f"Resume: {pdf_path.name}",
        f"Coverage: {frac:.0%}  ({len(present)} of {len(keywords)} JD keywords found in the PDF)",
        "",
        "PRESENT IN RESUME:",
        *(f"  + {k}" for k in present),
        "",
        "MISSING FROM RESUME:",
        *(f"  - {k}" for k in missing),
        "",
        "Note: a missing term is only worth adding if it is genuinely true of the",
        "candidate (it must exist in master_experience.yaml) — never keyword-stuff.",
        "If a missing skill IS real but absent from the master file, add it there",
        "and re-tailor. Concept buzzwords are matched through skill_aliases: a JD",
        "synonym counts as present when the real skill it maps to is on the page, so",
        "a term still MISSING here is one no owned skill (or its alias) covers.",
    ]
    report = out_dir / "ats_report.txt"
    report.write_text("\n".join(lines), encoding="utf-8")
    return frac
