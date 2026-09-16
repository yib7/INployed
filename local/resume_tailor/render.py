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
from .latexutil import clean_bullet, escape_url, fmt_dates, to_latex


# The contact fields that hold an address rather than a printed value. These render
# as \href so they pick up the template's urlcolor, matching the Projects section's
# repo link; location, phone and email stay plain text.
_LINK_FIELDS = ("linkedin", "github")


def _header(basics: dict) -> str:
    """The PDF info dictionary plus the centered name + contact line, from yaml
    `basics`. Missing fields are simply omitted so the line stays clean for any
    user.

    The \\hypersetup goes here rather than in the template because the title and
    author are the candidate's name, and the preamble is kept free of personal
    data. hyperref writes the info dictionary at the end of the run, so setting
    it after \\begin{document} still lands in the PDF. Without it the file ships
    with an empty Title/Author, which is what an ATS or a recruiter's PDF viewer
    displays for the document.
    """
    name = to_latex(basics.get("name", "") or "")
    contact_bits = [
        _contact_bit(k, str(basics[k]))
        for k in ("location", "phone", "email", "linkedin", "github")
        if basics.get(k)
    ]
    contact = " $|$ ".join(f"\\small{{{b}}}" for b in contact_bits)
    info = (
        f"\\hypersetup{{pdftitle={{{name} Resume}},pdfauthor={{{name}}},"
        "pdfsubject={Resume}}\n\n"
        if name else ""
    )
    return (
        info
        + "\\begin{center}\n"
        f"\\textbf{{\\Huge \\scshape {name}}} \\\\ \\vspace{{1pt}}\n"
        f"{contact}\n"
        "\\end{center}\n\\vspace{-10pt}\n\n"
    )


def _contact_bit(field: str, value: str) -> str:
    """One entry of the contact line. Address fields become a coloured, clickable
    \\href; everything else is printed as-is.

    The two arguments take different escaping and mixing them up is a real bug:
    to_latex is for the printed text (it backslashes `_`, right on the page), and
    escape_url is for the target (it must not, or the link resolves to an address
    with a literal backslash in it). Display text is left exactly as it reads today
    -- only the colour and the clickability change.
    """
    text = to_latex(value)
    if field not in _LINK_FIELDS:
        return text
    href = escape_url(assets.full_url(value))
    return f"\\href{{{href}}}{{{text}}}" if href else text


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


# The GPA is printed over the scale it was earned on ("GPA: 3.7/4.0"): the
# labelled, number-then-scale form is the one resume parsers match. A master
# entry may override the scale (`gpa_scale: 10`) for a non-4.0 system.
_DEFAULT_GPA_SCALE = "4.0"


def _education(edu: List[dict]) -> str:
    """One \\resumeSubheading per entry, laid out for a parser as much as a reader:

        <school bold>, <location>                          <dates>
        <degree line, spelled out from the master's fields>
        GPA: <gpa>/<scale> | Awards & Honors: <honors; ...>

    The location sits with the school (not at the end of the degree row, where a
    parser reads "Minor in Data Science City, ST" as the degree), the
    degree has its row to itself, and the GPA moved off the school row -- "3.7
    GPA" glued to the date column extracted as "GPAAugust 2021" and lost both.
    """
    if not edu:
        return ""
    rows: List[str] = []
    for e in edu:
        school = to_latex(str(e.get("school", "") or ""))
        location = to_latex(str(e.get("location", "") or ""))
        gpa = e.get("gpa")
        # Show GPA only when it's a real, non-zero value (0 / blank means "unset").
        show_gpa = gpa not in (None, "", 0, 0.0, "0")
        # \vspace{2pt} follows the degree line in all cases (matches the template),
        # then one \small line carrying GPA and/or honors, only when either exists.
        row = (
            "\\resumeSubheading\n"
            f"{{{school}}}{{{', ' + location if location else ''}}}"
            f"{{{fmt_dates(str(e.get('dates', '') or ''))}}}\n"
            f"{{{_degree_line(e)}}}\\vspace{{2pt}}"
        )
        bits: List[str] = []
        if show_gpa:
            scale = to_latex(str(e.get("gpa_scale") or _DEFAULT_GPA_SCALE))
            bits.append(f"\\textbf{{GPA:}} {to_latex(str(gpa))}/{scale}")
        honors = e.get("honors") or []
        if honors:
            bits.append("\\textbf{Awards \\& Honors:} "
                        + "; ".join(to_latex(str(h)) for h in honors))
        if bits:
            row += "\n\\item \\small{" + " $|$ ".join(bits) + "}"
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
        # `or ''`, not a .get default: assets.blocks() writes every key with
        # e.get(...), so a master entry with no `title:` has the key present and
        # set to None -- and to_latex(None) prints the literal "None" onto the
        # PDF. master_validate does not require title/location, so a master that
        # passes "Check setup" can reach here. Same guard as _header/_education.
        out.append(
            f"\\resumeSubheadingOneLine\n"
            f"{{{to_latex(b.get('title') or '')}}}{{{to_latex(b.get('name') or '')}}}"
            f"{{{to_latex(b.get('location') or '')}}}{{{fmt_dates(b.get('dates') or '')}}}\n"
            + _bullet_list(items)
        )
    if not out:
        return ""
    return ("%-----------EXPERIENCE-----------\n\\section{Work Experience}\n"
            "\\resumeSubHeadingListStart\n\n" + "\n".join(out) + "\\resumeSubHeadingListEnd\n\n")


def _looks_like_a_host_path(repo: str) -> bool:
    """True when a scheme-stripped `repo` value is an address worth linking.

    The link used to be gated on the literal "github.com", which silently dropped
    a project hosted anywhere else (GitLab, Bitbucket, Hugging Face, a personal
    domain): no href, no printed address, no warning. The field is "Repo", not
    "GitHub repo". The gate is now the shape of an address: a host segment with
    a dot in it and no whitespace anywhere, so "private" or "ask me" still
    render no link, exactly as an empty value does.
    """
    if not repo or any(ch.isspace() for ch in repo):
        return False
    host = repo.split("/", 1)[0]
    return "." in host.strip(".")


def _projects(sel: dict, bullets: Dict[str, str]) -> str:
    meta = _block_meta("projects")
    out: List[str] = []
    for entry in sel.get("projects", []):
        b = meta.get(entry["name"])
        items = _group_bullets(entry, bullets)
        if not b or not items:
            continue
        name = to_latex(b.get("name") or "")            # see _experience on `or ''`
        repo = (b.get("repo") or "").strip()
        # Strip any scheme the yaml already stored -- master_experience.yaml may hold
        # either a bare host+path (github.com/x/y) or a full URL (https://github.com/x/y);
        # without this, prefixing "https://" onto a full URL doubles the scheme.
        repo = repo.removeprefix("https://").removeprefix("http://")
        # The link sits inline after the name as " | github.com/x/y", mirroring the
        # Work Experience header; empty (no trailing pipe) when the project has no
        # repo. The visible text is the repo's own host+path, not the word "Link":
        # an ATS keeps only the extracted text and drops the \href target, so
        # "Link" reached it as a dead word and the address never did. Display text
        # goes through to_latex (a `_` in a repo name must print), the target
        # through escape_url (it must not be backslashed) -- see _contact_bit.
        if _looks_like_a_host_path(repo):
            href = escape_url(f"https://{repo}")
            link = f" $|$ \\href{{{href}}}{{{to_latex(repo.rstrip('/'))}}}"
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
            f"{{\\textbf{{{to_latex(b.get('name') or '')}}}}}"
            f"{{{fmt_dates(b.get('dates') or '')}}}\n"
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
