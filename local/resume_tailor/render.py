"""Assemble the tailored resume .tex.

Keeps the template's preamble (page geometry, fonts, \\resume* macros) verbatim via
assets.template_head, then generates EVERYTHING candidate-specific — the name/contact
header, Education, Work Experience, Projects, Leadership, Technical Skills — from
master_experience.yaml, mirroring the template's macros and spacing. This keeps the
tracked template free of personal data and makes the layout work for any user. Bullets
are keyed by group key (gkey = '+'.join(atom_ids)) and rendered in selection order.
Bullet and skill text is plain (no bold/italics) per the "no bolded words" rule;
structural bold (names, titles, project names, skill labels) follows the template.
"""
from __future__ import annotations

from typing import Dict, List

from . import assets
from .latexutil import clean_bullet, fmt_dates, to_latex


def _header(basics: dict) -> str:
    """The centered name + contact line, from yaml `basics`. Missing fields are
    simply omitted so the line stays clean for any user."""
    name = to_latex(basics.get("name", "") or "")
    contact_bits = [
        to_latex(str(basics[k]))
        for k in ("location", "phone", "email", "linkedin", "github")
        if basics.get(k)
    ]
    contact = " $|$ ".join(f"\\small{{{b}}}" for b in contact_bits)
    return (
        "\\begin{center}\n"
        f"\\textbf{{\\Huge \\scshape {name}}} \\\\ \\vspace{{1pt}}\n"
        f"{contact}\n"
        "\\end{center}\n\\vspace{-10pt}\n\n"
    )


def _degree_line(e: dict) -> str:
    """'B.S. in Computer Science' + optional concentration/minor, from structured
    fields. A `degree_line` field, if present, is used verbatim (full control)."""
    if e.get("degree_line"):
        return to_latex(str(e["degree_line"]))
    parts = [to_latex(str(e.get("degree", "") or ""))]
    if e.get("concentration"):
        parts.append(f" with a Concentration in {to_latex(str(e['concentration']))}")
    if e.get("minor"):
        parts.append(f", Minor in {to_latex(str(e['minor']))}")
    return "".join(parts).strip()


def _education(edu: List[dict]) -> str:
    if not edu:
        return ""
    rows: List[str] = []
    for e in edu:
        school = to_latex(str(e.get("school", "") or ""))
        gpa = e.get("gpa")
        # Show GPA only when it's a real, non-zero value (0 / blank means "unset").
        show_gpa = gpa not in (None, "", 0, 0.0, "0")
        left = f"{school} $|$ {to_latex(str(gpa))} GPA" if show_gpa else school
        # \vspace{2pt} follows the degree line in all cases (matches the template),
        # then an Honors item only when honors are present.
        row = (
            "\\resumeSubheading\n"
            f"{{{left}}}{{{fmt_dates(str(e.get('dates', '') or ''))}}}\n"
            f"{{{_degree_line(e)}}}{{{to_latex(str(e.get('location', '') or ''))}}}\\vspace{{2pt}}"
        )
        honors = e.get("honors") or []
        if honors:
            row += "\n\\item \\small{\\textbf{Awards \\& Honors:} " \
                   + "; ".join(to_latex(str(h)) for h in honors) + "}"
        rows.append(row)
    return ("%-----------EDUCATION-----------\n\\section{Education}\n"
            "\\resumeSubHeadingListStart\n" + "\n".join(rows)
            + "\n\\resumeSubHeadingListEnd\n\n\\vspace{-10pt}\n\n\n")


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
            f"\\resumeSubheadingOneLine\n"
            f"{{{to_latex(b.get('title',''))}}}{{{to_latex(b.get('name',''))}}}"
            f"{{{to_latex(b.get('location',''))}}}{{{fmt_dates(b.get('dates',''))}}}\n"
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
        name = to_latex(b.get("name", ""))
        repo = (b.get("repo") or "").strip()
        # Strip any scheme the yaml already stored -- master_experience.yaml may hold
        # either a bare host+path (github.com/x/y) or a full URL (https://github.com/x/y);
        # without this, prefixing "https://" onto a full URL doubles the scheme.
        repo = repo.removeprefix("https://").removeprefix("http://")
        # Link sits inline after the name as " | Link" (italic), mirroring the Work
        # Experience header; empty (no trailing pipe) when the project has no repo.
        if "github.com" in repo:
            href = to_latex(f"https://{repo}")
            link = f" $|$ \\href{{{href}}}{{\\textit{{Link}}}}"
        else:
            link = ""
        out.append(
            f"\\resumeProjectHeadingInline\n{{{name}}}{{{link}}}\n" + _bullet_list(items)
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
            f"{{\\textbf{{{to_latex(b.get('name',''))}}}}}{{{fmt_dates(b.get('dates',''))}}}\n"
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
        f"\\textbf{{{to_latex(ln['label'])}}}{{: }} {to_latex(ln['items'])}"
        for ln in skill_lines
    )
    return ("%-----------Technical SKILLS-----------\n\\section{Technical Skills}\n"
            "\\begin{itemize}[leftmargin=0.15in, label={}]\n\\item \\small{\n"
            + rows + " \\\\\n}\n\\end{itemize}\n")


def render(sel: dict, bullets: Dict[str, str], skill_lines: List[Dict[str, str]]) -> str:
    """Build the complete tailored resume .tex."""
    master = assets.load_master()
    body = (
        _header(master.get("basics", {}) or {})
        + _education(master.get("education", []) or [])
        + _experience(sel, bullets)
        + _projects(sel, bullets)
        + _leadership(sel, bullets)
        + _skills(skill_lines)
    )
    return assets.template_head() + body + "\n%-------------------------------------------\n\\end{document}\n"
