"""Write a self-contained apply.md next to each tailored resume.

This is the human-and-Claude-readable "apply sheet" the user pastes into
Claude-in-Chrome to fill out a job application. It carries, at the top, the
fill-it-out playbook (the contract a form-filler must follow — never submit,
never log in, e-sign with the candidate's name + today's date, flag blocking
unknowns), then the candidate basics + mailing address, education, the reusable
standard answers (work auth / EEO / how-did-you-hear), the tailored résumé
highlights, and an electronic-signature section. A hidden HTML-comment meta
marker at the foot carries the job identity for machine lookup (invisible in
rendered markdown). Replaces the old apply_data.json — nothing here ever submits.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from . import apply_answers, apply_config, assets, output

# Structured mailing-address answer ids — rendered in the Address section and
# excluded from the generic Standard-answers list (so they aren't shown twice).
ADDRESS_KEYS = ("address_street", "address_city", "address_state",
                "address_zip", "address_country")

_MARKER_PREFIX = "inployed-apply-meta:"
_MARKER_RE = re.compile(r"inployed-apply-meta:\s*(\{.*?\})\s*-->")

# The embedded fill-it-out playbook (the retired apply-to-job skill's safety
# contract). Paste this whole sheet into Claude-in-Chrome.
PLAYBOOK = """\
## Instructions for the form-filler (read first)

You are filling out **one** job application using ONLY the information in this sheet.
Work through the form page by page, all the way to the end.

- **Never click the final Submit / Apply / Send / Finish button.** Fill every field, reach the
  final review/submit screen, then STOP and hand back to the human to review and submit.
- **Never log in, create an account, or enter a password, payment info, SSN, or any government ID.
  Never solve a CAPTCHA.** At any login / account / verification-code / CAPTCHA wall, stop, say
  exactly what is needed, and wait for the human to clear it — then continue.
- **Upload the résumé PDF** listed under Documents (and the cover letter if one is listed).
- Use the **Standard answers** verbatim for work-authorization / sponsorship / EEO / "how did you
  hear" questions. For "describe your experience"-type boxes, paraphrase the **Résumé highlights** —
  never invent salaries, dates, or essay answers.
- **Electronic signature:** where the form asks you to sign, type the candidate's full name as the
  signature and use **today's date** (the day you are applying). Signing is not submitting — still
  stop before the final Submit.
- **If a REQUIRED field has no answer in this sheet and blocks progress:** enter `XXXXX` (or pick a
  clearly-neutral default option), and add that field to a **"Needs review"** list you report back
  in chat so the human can fix it before submitting. Leave optional unknowns blank.
- At the end, report: what you filled, what still needs the human (placeholders, walls, blanks), and
  any new questions the form asked that aren't covered here."""


def build_marker(job: Dict[str, str]) -> str:
    """A hidden HTML comment carrying the job identity for machine lookup."""
    meta = {
        "job_posting_id": str(job.get("job_posting_id", "")),
        "company": job.get("company_name", "") or "",
        "title": job.get("job_title", "") or "",
        "url": job.get("url", "") or "",
    }
    return f"<!-- {_MARKER_PREFIX} {json.dumps(meta, ensure_ascii=False)} -->"


def parse_marker(text: str) -> Dict[str, str]:
    """Extract the job-identity dict from an apply.md's meta marker ({} if absent)."""
    m = _MARKER_RE.search(text or "")
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _kv(label: str, value: Any, *, always: bool = False) -> str:
    """A `- **label:** value` line, or "" when value is empty (unless always)."""
    text = "" if value is None else str(value).strip()
    if not text and not always:
        return ""
    return f"- **{label}:** {text}\n"


def _address_lines(flat: Dict[str, Any]) -> str:
    """Render the mailing address (combined line + structured components). Falls
    back to apply_config defaults for any key the store doesn't carry."""
    cfg = apply_config.load_apply_config()
    val = {k: (str(flat.get(k) or cfg.get(k, "") or "").strip()) for k in ADDRESS_KEYS}
    street, city, state, zc, country = (val["address_street"], val["address_city"],
                                        val["address_state"], val["address_zip"],
                                        val["address_country"])
    combined_bits = [street, city, f"{state} {zc}".strip(), country]
    combined = ", ".join(b for b in combined_bits if b)
    out = ["### Address\n"]
    out.append(_kv("Full", combined))
    out.append(_kv("Street", street))
    out.append(_kv("City", city))
    out.append(_kv("State / Province", state))
    out.append(_kv("ZIP / Postal", zc))
    out.append(_kv("Country", country))
    return "".join(out)


def _education_lines(education: List[Dict[str, Any]]) -> str:
    out = ["## Education\n"]
    for e in education:
        head = e.get("school", "") or ""
        degree_bits = [e.get("degree", ""), e.get("concentration", "")]
        degree = ", ".join(b for b in degree_bits if b)
        tail_bits = []
        if e.get("minor"):
            tail_bits.append(f"minor: {e['minor']}")
        if e.get("dates"):
            tail_bits.append(str(e["dates"]))
        if e.get("gpa"):
            tail_bits.append(f"GPA {e['gpa']}")
        if e.get("location"):
            tail_bits.append(str(e["location"]))
        line = head
        if degree:
            line += f" — {degree}"
        if tail_bits:
            line += " · " + " · ".join(tail_bits)
        if line.strip():
            out.append(f"- {line}\n")
    if len(out) == 1:
        out.append("- (none listed)\n")
    return "".join(out)


def _standard_answer_lines(answers: List[Dict[str, Any]]) -> str:
    out = ["## Standard answers\n"]
    for e in answers:
        if e.get("status") != "active":
            continue
        eid = e.get("id", "")
        if eid in ADDRESS_KEYS:  # rendered under Address, not here
            continue
        raw = str(e.get("answer", "")).strip()
        if not raw:
            continue
        if eid in apply_answers.BOOL_IDS:
            shown = "Yes" if raw.lower() in {"true", "yes", "1"} else "No"
        else:
            shown = raw
        out.append(f"- **{e.get('question', eid)}** {shown}\n")
    if len(out) == 1:
        out.append("- (none recorded — add them in the Apply Answers tab)\n")
    return "".join(out)


def _highlight_lines(bullets: List[str]) -> str:
    out = ["## Résumé highlights (paraphrase for \"describe your experience\" fields)\n"]
    clean = [b.strip() for b in (bullets or []) if str(b).strip()]
    if clean:
        out.extend(f"- {b}\n" for b in clean)
    else:
        out.append("- (re-tailor this job to include résumé highlights)\n")
    return "".join(out)


def build_markdown(master: Dict[str, Any], job: Dict[str, str], resume_pdf: Path,
                   cover_pdf: Path | None, bullets: List[str],
                   answers: List[Dict[str, Any]]) -> str:
    """Assemble the full apply.md text (pure function — easily testable)."""
    basics = master.get("basics", {}) or {}
    education = master.get("education", []) or []
    flat = apply_answers.as_standard_answers(answers)

    title = job.get("job_title", "") or "this role"
    company = job.get("company_name", "") or "the company"

    parts: List[str] = []
    parts.append(f"# Apply sheet — {title} @ {company}\n")
    parts.append(
        f"Generated {date.today().isoformat()}. **Paste this entire sheet into "
        f"Claude-in-Chrome** to fill out the application, then review and submit it yourself.\n"
    )
    parts.append("\n" + PLAYBOOK + "\n")

    parts.append("\n## Documents (upload these)\n")
    parts.append(_kv("Résumé PDF", f"`{resume_pdf}`", always=True))
    if cover_pdf is not None:
        parts.append(_kv("Cover letter PDF", f"`{cover_pdf}`"))

    parts.append("\n## Candidate\n")
    parts.append(_kv("Name", basics.get("name", ""), always=True))
    parts.append(_kv("Email", basics.get("email", ""), always=True))
    parts.append(_kv("Phone", basics.get("phone", "")))
    parts.append(_kv("Location", basics.get("location", "")))
    parts.append(_kv("LinkedIn", basics.get("linkedin", "")))
    parts.append(_kv("GitHub / Portfolio", basics.get("github", "")))

    parts.append("\n" + _address_lines(flat))
    parts.append("\n" + _education_lines(education))
    parts.append("\n" + _standard_answer_lines(answers))
    parts.append("\n" + _highlight_lines(bullets))

    parts.append("\n## Electronic signature (use at the end, where the form asks — do not submit)\n")
    parts.append(_kv("Signature (type)", basics.get("name", ""), always=True))
    parts.append(_kv("Date", "use today's date (the day you apply)", always=True))

    parts.append("\n" + build_marker(job) + "\n")
    return "".join(parts)


def write(job: Dict[str, str], out_dir: Path, bullets: List[str],
          cover_letter: bool = False) -> Path:
    """Write a self-contained apply.md into out_dir and return its path."""
    master = assets.load_master()
    resume_pdf = out_dir / output.resume_filename()
    cover_pdf = out_dir / output.cover_filename()
    cover = cover_pdf if (cover_letter and cover_pdf.exists()) else None

    answers = apply_answers.load()
    md = build_markdown(master, job, resume_pdf, cover, bullets, answers)
    path = out_dir / "apply.md"
    path.write_text(md, encoding="utf-8")
    return path


def write_from_folder(folder: Path, job: Dict[str, str]) -> Path:
    """Backfill apply.md for an already-tailored folder whose résumé PDF exists but
    whose apply.md is missing (e.g. folders tailored before this format). Bullets are
    unavailable here, so résumé highlights are empty; everything else is rebuilt the
    same way write() does."""
    folder = Path(folder)
    has_cover = (folder / output.cover_filename()).exists()
    return write(job, folder, [], cover_letter=has_cover)
