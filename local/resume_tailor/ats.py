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


def extract_keywords(jd_text: str, top_n: int = 30) -> List[str]:
    """The JD's top keywords: lexicon hits ranked by frequency, then repeated
    acronyms the lexicon missed (e.g. an in-house tool or niche platform)."""
    scored: list[tuple[int, str]] = []
    seen_lower: set[str] = set()
    lexicon = list(dict.fromkeys([*_candidate_terms(), *BUILTIN_TERMS]))
    for term in lexicon:
        if term.lower() in seen_lower:
            continue
        n = len(_term_pattern(term).findall(jd_text))
        if n:
            scored.append((n, term))
            seen_lower.add(term.lower())
    scored.sort(key=lambda t: (-t[0], t[1].lower()))
    keywords = [t for _, t in scored]

    counts: dict[str, int] = {}
    for acro in re.findall(r"\b[A-Z][A-Z0-9]{1,5}\b", jd_text):
        counts[acro] = counts.get(acro, 0) + 1
    extras = [
        a for a, n in sorted(counts.items(), key=lambda kv: -kv[1])
        if n >= 2 and a not in _ACRONYM_NOISE and a.lower() not in seen_lower
    ]
    return (keywords + extras)[:top_n]


def coverage(keywords: List[str], resume_text: str) -> Tuple[float, List[str], List[str]]:
    """(fraction_present, present, missing) of keywords in the resume text."""
    present = [k for k in keywords if _term_pattern(k).search(resume_text)]
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
        "and re-tailor.",
    ]
    report = out_dir / "ats_report.txt"
    report.write_text("\n".join(lines), encoding="utf-8")
    return frac
