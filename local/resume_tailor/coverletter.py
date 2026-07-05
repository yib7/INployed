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

# A plain left-aligned business-letter layout (article, not the `letter` class)
# so the header format and the closing gap are under our exact control:
#
#   Name (bold)
#   Phone | Email
#   Date
#   Company
#   Dear Hiring Team,
#   <body>
#   Sincerely,
#   Name            (left-aligned, a small gap under "Sincerely,")
#
# Deliberately NO LinkedIn/GitHub in the header — the user wants phone + email
# only. parskip gives the uniform blank-line spacing between blocks.
_TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage[margin=1in]{geometry}
\usepackage{parskip}
\usepackage{hyperref}
\hypersetup{colorlinks=false, pdfborder={0 0 0}}
\pagenumbering{gobble}

\begin{document}

{\large\textbf{__CANDIDATE_NAME__}}

__CONTACT_LINE__

__DATE__

__COMPANY_NAME__

Dear Hiring Team,

__BODY__

Sincerely,\\[6pt]
__CANDIDATE_NAME__

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


def _contact_values() -> list[str]:
    """The header contact fields, in print order: phone then email. LinkedIn and
    GitHub are intentionally excluded (the user wants them out of the letter).
    Missing/blank fields are dropped. Raw (unescaped) — callers escape as needed."""
    basics = assets.load_master().get("basics", {}) or {}
    return [str(v).strip() for v in (basics.get("phone"), basics.get("email"))
            if str(v or "").strip()]


def _today_str() -> str:
    """Today as 'Month D, YYYY' (no leading zero) — portable across OSes and shared
    verbatim by the PDF header and the plain-text export so they never disagree."""
    t = date.today()
    return f"{calendar.month_name[t.month]} {t.day}, {t.year}"


_SIGNOFF_RE = re.compile(r"^\s*sincerely[,!.]?\s*$", re.I)


def _strip_trailing_signoff(body: str) -> str:
    """Drop any trailing 'Sincerely, / Name' the model appended — the template and
    the .txt export both supply the closing, so a model-added one would double it."""
    lines = body.strip().splitlines()
    if len(lines) >= 2 and _SIGNOFF_RE.match(lines[-2]):
        return "\n".join(lines[:-2]).rstrip()
    return body.strip()


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
        "plain declarative sentences, no clichés. Show genuine but MEASURED interest: "
        "never gush or over-sell — no exclamation-point excitement, no "
        "'thrilled/ecstatic/passionate/love' inflation, no empty superlatives; that "
        "over-eager tone reads as AI-written. " + tone_directive(tone) + " "
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
    # Second (flash) pass: tighten cohesion/flow, strip any invented claim, and dial
    # back over-the-top excitement — THEN the deterministic ban gate runs last, so the
    # refine can never sneak banned phrasing past it.
    body = refine_body(jd, job_title, company, body, bullets, tone=tone)
    return enforce_body_style(jd, job_title, company, body, bullets, tone=tone)


def refine_body(jd: str, job_title: str, company: str, body: str,
                bullets: Dict[str, str], tone: str = "professional") -> str:
    """One flash-tier cohesion/grounding/tone pass over the generated body.

    A final editor polish: make the paragraphs read as one connected argument,
    keep it strictly grounded in the resume bullets (cut anything the draft
    invented — no company/number/skill/claim that isn't supported), and pull an
    over-eager, gushing tone back to measured, genuine interest (that AI-slop
    over-excitement is exactly what the user flagged). Best-effort and advisory:
    an empty result or a failed call leaves the original body untouched, and the
    deterministic style gate still runs after this. Pure aside from the LLM call."""
    body = (body or "").strip()
    if not body:
        return body
    used = "\n".join(f"- {t}" for t in bullets.values())
    system = (
        "You are an editor doing a final polish pass on a cover-letter body. Improve "
        "cohesion and flow so it reads as one connected argument, not stitched-together "
        "sentences. Stay grounded: use ONLY facts already in the draft and the resume "
        "bullets below; never add a company, number, skill, or claim that isn't "
        "supported, and cut anything the draft invented. Keep the meaning and roughly "
        "the same length; no salutation and no sign-off. Show genuine but MEASURED "
        "interest: do NOT be over-the-top or gushing — no exclamation-point enthusiasm, "
        "no 'thrilled/ecstatic/passionate/love' inflation, no empty superlatives; that "
        "over-eager tone reads as AI-written. " + tone_directive(tone) + "\n"
        "BANNED PHRASING (do not introduce any of these): " + compose.BANNED_PHRASING
    )
    user = f"""ROLE: {job_title} at {company}

RESUME BULLETS (the only allowed source of facts):
{used}

COVER-LETTER DRAFT TO POLISH:
{body}

Return ONLY the revised body — same paragraph structure, no preamble, no sign-off."""
    try:
        refined = (compose.call(system, user, config.TIER_FLASH, json_out=False,
                                temperature=0.3) or "").strip()
    except Exception:  # noqa: BLE001 - refine is advisory; the draft still stands
        return body
    return refined or body


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


def cover_letter_text(body: str, company: str) -> str:
    """The cover letter as clean, copy-pasteable plain text (no LaTeX) — saved next
    to the PDF so it can be dropped straight into an application form. Same header
    and left-aligned closing as the PDF, blocks separated by a blank line. Raw text
    (never escaped): reads the master basics for name + phone/email."""
    name = _display_name()
    contact = " | ".join(_contact_values())
    body = _strip_trailing_signoff(body)
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    blocks = [name]
    if contact:
        blocks.append(contact)
    blocks += [_today_str(), company, "Dear Hiring Team,", *paras, "Sincerely,", name]
    return "\n\n".join(blocks) + "\n"


def render_cover_letter(body: str, company: str, tex_path: Path, work_dir: Path) -> Tuple[CompileResult, str]:
    # Drop any trailing "Sincerely, / Name" the model added; the template supplies it.
    body = _strip_trailing_signoff(body)
    contact_line = r" \textbar{} ".join(to_latex(v) for v in _contact_values())
    rendered = (
        _TEMPLATE.replace("__CANDIDATE_NAME__", to_latex(_display_name()))
        .replace("__CONTACT_LINE__", contact_line)
        .replace("__DATE__", to_latex(_today_str()))
        .replace("__COMPANY_NAME__", to_latex(company))
        .replace("__BODY__", _paragraphs(body))
    )
    tex_path.write_text(rendered, encoding="utf-8")
    return compile_tex(tex_path, work_dir), rendered
