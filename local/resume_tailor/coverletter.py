"""Optional cover-letter generation + rendering (off by default).

The body is written by the pro tier but stays grounded: it may only use facts
already on the tailored resume (the selected bullets) plus the candidate basics.
Like the bullets, the body passes the deterministic style gate before rendering
(enforce_body_style — compose.enforce_style's letter arm), so banned AI-tell
phrasing never reaches the letter.
Template is self-contained (ported from Resume_Tailor) so there's no file dep.
"""
from __future__ import annotations

import calendar
import re
from datetime import date
from pathlib import Path
from typing import Dict, Tuple

from . import assets, compose, config
from .compile import CompileResult, compile_tex
from .latexutil import to_latex

_TEMPLATE = r"""\documentclass[11pt]{letter}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage[margin=1in]{geometry}
\usepackage{parskip}
\usepackage{hyperref}
\hypersetup{colorlinks=false, pdfborder={0 0 0}}

\signature{__CANDIDATE_NAME__}
\address{__CONTACT_BLOCK__}
\date{\today}

\begin{document}
\begin{letter}{Hiring Team \\ __COMPANY_NAME__}

\opening{Dear Hiring Team,}

__BODY__

\closing{Sincerely,}

\end{letter}
\end{document}
"""


def _display_name() -> str:
    return assets.load_master().get("basics", {}).get("name", config.CANDIDATE_NAME.replace("_", " "))


_END_YM_RE = re.compile(r"^(\d{4})-(\d{1,2})$")


def _education_context() -> str:
    """One line of graduation-status facts for the generation prompt.

    Without this the model guesses tense from the JD ("I am completing my
    studies") even after the candidate has graduated. For each master
    `education` entry, parse the END token of its `dates` ("YYYY-MM / YYYY-MM"):
    end <= today's year-month -> graduated (and available immediately); a
    future end -> expected; "Present"/missing end -> still enrolled with no
    date claim; an unparseable end -> just the degree line, no claim at all.
    Entries join with '; '. Pure: reads only load_master() and date.today()."""
    today = date.today()
    lines = []
    for entry in assets.load_master().get("education") or []:
        if not isinstance(entry, dict):
            continue
        label = ", ".join(
            s for s in (str(entry.get("degree") or "").strip(),
                        str(entry.get("school") or "").strip()) if s)
        if not label:
            continue
        raw = str(entry.get("dates") or "").strip()
        end_token = raw.split("/", 1)[1].strip() if "/" in raw else ""
        if not end_token or end_token.lower() in {"present", "current"}:
            lines.append(f"{label}: still enrolled")
            continue
        m = _END_YM_RE.match(end_token)
        if not m or not 1 <= int(m.group(2)) <= 12:
            lines.append(label)  # unparseable end -> no graduation claim
            continue
        year, month = int(m.group(1)), int(m.group(2))
        when = f"{calendar.month_name[month]} {year}"
        if (year, month) <= (today.year, today.month):
            lines.append(f"{label}: graduated {when}; has already graduated "
                         "and is available to start immediately")
        else:
            lines.append(f"{label}: expected {when}; still enrolled")
    return "; ".join(lines)


def _contact_block() -> str:
    """The \\address{} lines (email \\\\ phone \\\\ LinkedIn \\\\ GitHub) from the
    master basics, links made absolute via assets.full_url and every value
    escaped with to_latex, so the letter carries the candidate's contact info
    top-right above the date and stays self-contained when separated from the
    resume. Missing fields are simply skipped."""
    basics = assets.load_master().get("basics", {}) or {}
    values = (basics.get("email"), basics.get("phone"),
              assets.full_url(basics.get("linkedin")),
              assets.full_url(basics.get("github")))
    return " \\\\ ".join(to_latex(v) for v in values if str(v or "").strip())


# One-line style instruction per Settings tone choice. The body's content rules
# (grounded, 3 short paragraphs, no sign-off) never change — only the voice.
_TONE_DIRECTIVES: Dict[str, str] = {
    "professional": "Use a confident, professional tone.",
    "concise": "Keep it tight and concise: short sentences, no filler.",
    "enthusiastic": "Let genuine enthusiasm and energy come through, while staying grounded.",
    "impactful": "Lead with impact and outcomes; make every sentence earn its place.",
}


def tone_directive(tone: str) -> str:
    """Map a Settings tone choice to a one-line style instruction.

    Unknown or empty input falls back to the professional directive so the
    prompt always carries a valid voice cue.
    """
    key = (tone or "").strip().lower()
    return _TONE_DIRECTIVES.get(key, _TONE_DIRECTIVES["professional"])


def generate_body(jd: str, job_title: str, company: str, bullets: Dict[str, str],
                  research: str = "", tone: str = "professional") -> str:
    used = "\n".join(f"- {t}" for t in bullets.values())
    system = (
        "Write a concise, genuine cover-letter body (3 short paragraphs) for an "
        "early-career candidate. Use ONLY facts present in the provided resume bullets "
        "and basics; never invent experience, numbers, or interest you can't support. "
        "No salutation and no sign-off (the template adds them). Plain text, paragraphs "
        "separated by a blank line. Warm but professional; write like a person, in "
        "plain declarative sentences, no clichés. " + tone_directive(tone) + " "
        "Use the correct tense for education, based on the EDUCATION line: if the "
        "candidate has already graduated, NEVER say they are 'completing' or "
        "'finishing' their studies; refer to the degree as completed. "
        "Do NOT open with boilerplate ('I am writing to express my interest...', "
        "'I am writing to apply for...'): the FIRST sentence must lead with "
        "something specific about the candidate or the company. "
        "Never use the same metric or number twice in the letter.\n"
        "BANNED PHRASING (using any of these is wrong): " + compose.BANNED_PHRASING
    )
    research_block = (
        f"""

VERIFIED COMPANY RESEARCH (from web search — use it for one or two SPECIFIC
"why this company" sentences; cite only what is relevant, never the whole blurb):
{research[:1500]}"""
        if research
        else ""
    )
    user = f"""ROLE: {job_title} at {company}

JOB DESCRIPTION:
{jd[:4000]}

FACTS YOU MAY DRAW FROM (the candidate's tailored resume bullets):
{used}{research_block}

Candidate: {_display_name()}, {assets.load_master().get('basics', {}).get('location', '')}.
TODAY'S DATE: {date.today():%B %d, %Y}.
EDUCATION: {_education_context()}

Write the body now."""
    body = compose.call(system, user, config.TIER_PRO, json_out=False, temperature=0.4)
    return enforce_body_style(jd, job_title, company, body, bullets, tone=tone)


def enforce_body_style(jd: str, job_title: str, company: str, body: str,
                       bullets: Dict[str, str], tone: str = "professional") -> str:
    """The letter arm of the deterministic style gate (compose.enforce_style is the
    bullet arm): the generation prompt bans AI-tell phrasing, but a model can still
    slip one through. When the body violates compose._STYLE_BANS, buy ONE repair
    call — same letter, same facts (the resume bullets are the only allowed
    source), committed only on strict improvement so a bad repair can't make it
    worse — then mechanically strip any em dash that survives, so one can never
    print. Best-effort: a failed call just leaves the body to the mechanical pass
    (advisory, never fatal — like the bullet gate)."""
    violations = compose.style_violations(body)
    if violations:
        used = "\n".join(f"- {t}" for t in bullets.values())
        system = (
            "You repair a cover-letter body that slipped into banned AI-tell "
            "phrasing. Rewrite it as the SAME letter: same facts, same paragraph "
            "structure, roughly the same length, no salutation and no sign-off. "
            "Use ONLY facts already in the letter and the resume bullets below; "
            "never add a claim. " + tone_directive(tone) + "\n"
            "BANNED: " + compose.BANNED_PHRASING
        )
        user = f"""ROLE: {job_title} at {company}

RESUME BULLETS (the only allowed source of facts):
{used}

LETTER BODY TO REPAIR (banned patterns found: {", ".join(violations)}):
{body}

Rewrite the body now, removing every banned pattern."""
        try:
            fixed = (compose.call(system, user, config.TIER_FLASH, json_out=False,
                                  temperature=0.2) or "").strip()
            # Commit only strict improvement, so a bad repair can't make it worse.
            if fixed and len(compose.style_violations(fixed)) < len(violations):
                body = fixed
        except Exception:  # noqa: BLE001 - repair is advisory; the mechanical pass still runs
            pass
    # Unconditional backstop: an em dash must never reach the letter.
    return compose._strip_em_dashes(body)


def _paragraphs(body: str) -> str:
    paras = [p.strip() for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    esc = [to_latex(p).replace("\n", " ") for p in paras]
    return "\n\n\\medskip\n\n".join(esc)


def render_cover_letter(body: str, company: str, tex_path: Path, work_dir: Path) -> Tuple[CompileResult, str]:
    # Drop any trailing "Sincerely, / Name" the model added; the template supplies it.
    lines = body.strip().splitlines()
    if len(lines) >= 2 and re.match(r"^\s*sincerely[,!.]?\s*$", lines[-2], re.I):
        body = "\n".join(lines[:-2]).rstrip()
    rendered = (
        _TEMPLATE.replace("__CANDIDATE_NAME__", to_latex(_display_name()))
        .replace("__CONTACT_BLOCK__", _contact_block())
        .replace("__COMPANY_NAME__", to_latex(company))
        .replace("__BODY__", _paragraphs(body))
    )
    tex_path.write_text(rendered, encoding="utf-8")
    return compile_tex(tex_path, work_dir), rendered
