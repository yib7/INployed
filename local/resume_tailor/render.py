"""Assemble the tailored resume .tex.

Keeps the template's preamble + header + Education verbatim (via assets.template_head)
and emits Work Experience / Projects / Leadership / Technical Skills from the composed
data, mirroring resume_template.tex's macros and spacing exactly. Bullets are keyed by
group key (gkey = '+'.join(atom_ids)) and rendered in selection order. Bullet and skill
text is plain (no bold/italics) per the "no bolded words" rule; structural bold (names,
titles, project names, skill labels) follows the template.
"""
from __future__ import annotations

from typing import Dict, List

from . import assets
from .latexutil import clean_bullet, escape_latex, fmt_dates


def _block_meta(section: str) -> Dict[str, dict]:
    return {b["name"]: b for b in assets.blocks()[section]}


def _group_bullets(entry: dict, bullets: Dict[str, str]) -> List[str]:
    """Bullets for one block's groups, in selection order, skipping dropped groups."""
    out: List[str] = []
    for ids in entry.get("groups", []):
        gk = "+".join(ids)
        if gk in bullets:
            out.append(clean_bullet(bullets[gk]))
    return out


def _bullet_list(items: List[str]) -> str:
    if not items:
        return ""
    body = "\n".join(f"\\resumeItem{{{b}}}" for b in items)
    return f"\\resumeItemListStart\n{body}\n\\resumeItemListEnd\n"


def _experience(sel: dict, bullets: Dict[str, str]) -> str:
    meta = _block_meta("experience")
    out: List[str] = []
    for entry in sel.get("experience", []):
        b = meta.get(entry["name"])
        items = _group_bullets(entry, bullets)
        if not b or not items:
            continue
        out.append(
            f"\\resumeSubheading\n"
            f"{{{escape_latex(b.get('title',''))}}}{{{fmt_dates(b.get('dates',''))}}}\n"
            f"{{{escape_latex(b.get('name',''))}}}{{{escape_latex(b.get('location',''))}}}\n"
            + _bullet_list(items)
        )
    if not out:
        return ""
    return ("%-----------EXPERIENCE-----------\n\\section{Work Experience}\n"
            "\\resumeSubHeadingListStart\n\n" + "\n".join(out) + "\\resumeSubHeadingListEnd\n\n")


def _projects(sel: dict, bullets: Dict[str, str]) -> str:
    meta = _block_meta("projects")
    out: List[str] = []
    for entry in sel.get("projects", []):
        b = meta.get(entry["name"])
        items = _group_bullets(entry, bullets)
        if not b or not items:
            continue
        name = escape_latex(b.get("name", ""))
        out.append(
            f"\\resumeProjectHeading\n{{\\textbf{{{name}}}}}{{}}\n" + _bullet_list(items)
        )
    if not out:
        return ""
    return ("%-----------PROJECTS-----------\n\\section{Projects}\n"
            "\\resumeSubHeadingListStart\n\n" + "\n".join(out)
            + "\\resumeSubHeadingListEnd\n\n\\vspace{-10pt}\n\n")


def _leadership(sel: dict, bullets: Dict[str, str]) -> str:
    meta = _block_meta("leadership")
    out: List[str] = []
    for entry in sel.get("leadership", []):
        b = meta.get(entry["name"])
        items = _group_bullets(entry, bullets)
        if not b or not items:
            continue
        out.append(
            f"\\resumeProjectHeading\n"
            f"{{\\textbf{{{escape_latex(b.get('name',''))}}}}}{{{fmt_dates(b.get('dates',''))}}}\n"
            + _bullet_list(items)
        )
    if not out:
        return ""
    return ("%-----------Leadership Experience-----------\n\\section{Leadership Experience}\n"
            "\\resumeSubHeadingListStart\n" + "\n".join(out) + "\\resumeSubHeadingListEnd\n\n")


def _skills(skill_lines: List[Dict[str, str]]) -> str:
    if not skill_lines:
        return ""
    rows = " \\\\\n".join(
        f"\\textbf{{{escape_latex(ln['label'])}}}{{: }} {escape_latex(ln['items'])}"
        for ln in skill_lines
    )
    return ("%-----------Technical SKILLS-----------\n\\section{Technical Skills}\n"
            "\\begin{itemize}[leftmargin=0.15in, label={}]\n\\item \\small{\n"
            + rows + " \\\\\n}\n\\end{itemize}\n")


def render(sel: dict, bullets: Dict[str, str], skill_lines: List[Dict[str, str]]) -> str:
    """Build the complete tailored resume .tex."""
    body = (
        _experience(sel, bullets)
        + _projects(sel, bullets)
        + _leadership(sel, bullets)
        + _skills(skill_lines)
    )
    return assets.template_head() + body + "\n%-------------------------------------------\n\\end{document}\n"
