"""Optional cover-letter generation + rendering (off by default).

The body is written by the pro tier but stays grounded: it may only use facts
already on the tailored resume (the selected bullets) plus the candidate basics.
Template is self-contained (ported from Resume_Tailor) so there's no file dep.
"""
from __future__ import annotations

import re
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
\address{}
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


# One-line style instruction per Settings tone choice. The body's content rules
# (grounded, 3 short paragraphs, no sign-off) never change — only the voice.
_TONE_DIRECTIVES: Dict[str, str] = {
    "professional": "Use a confident, professional tone.",
    "concise": "Keep it tight and concise — short sentences, no filler.",
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
        "and basics — never invent experience, numbers, or interest you can't support. "
        "No salutation and no sign-off (the template adds them). Plain text, paragraphs "
        "separated by a blank line. Warm but professional; no clichés or buzzword stacks. "
        + tone_directive(tone)
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

Write the body now."""
    return compose.call(system, user, config.TIER_PRO, json_out=False, temperature=0.4)


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
        .replace("__COMPANY_NAME__", to_latex(company))
        .replace("__BODY__", _paragraphs(body))
    )
    tex_path.write_text(rendered, encoding="utf-8")
    return compile_tex(tex_path, work_dir), rendered
