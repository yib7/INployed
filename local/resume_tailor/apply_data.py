"""Write apply_data.json next to each tailored resume.

This is the machine-readable profile a browser form-fill assistant
(Claude-in-Chrome, etc.) consumes to prefill Greenhouse/Lever/Workday
applications: candidate basics + education from master_experience.yaml, the
exact document paths, the job's identity, and the tailored bullets. The
assistant fills the form; the human reviews before submitting — this file
never auto-submits anything.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from . import apply_answers, assets, output


def write(job: Dict[str, str], out_dir: Path, bullets: List[str],
          cover_letter: bool = False) -> Path:
    master = assets.load_master()
    basics = master.get("basics", {}) or {}
    education = master.get("education", []) or []

    resume_pdf = out_dir / output.resume_filename()
    cover_pdf = out_dir / output.cover_filename()

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "instructions": (
            "Prefill application forms from this profile. Always leave the final "
            "submit to the human (review-before-submit). Never invent answers for "
            "fields not covered here — leave them blank and flag them."
        ),
        "candidate": {
            "full_name": basics.get("name", ""),
            "email": basics.get("email", ""),
            "phone": basics.get("phone", ""),
            "location": basics.get("location", ""),
            "linkedin": basics.get("linkedin", ""),
            "github": basics.get("github", ""),
        },
        "education": [
            {
                "school": e.get("school", ""),
                "degree": e.get("degree", ""),
                "concentration": e.get("concentration", ""),
                "minor": e.get("minor", ""),
                "gpa": e.get("gpa", ""),
                "dates": e.get("dates", ""),
                "location": e.get("location", ""),
            }
            for e in education
        ],
        "documents": {
            "resume_pdf": str(resume_pdf) if resume_pdf.exists() else "",
            "cover_letter_pdf": str(cover_pdf) if cover_letter and cover_pdf.exists() else "",
        },
        "job": {
            "job_posting_id": str(job.get("job_posting_id", "")),
            "company": job.get("company_name", ""),
            "title": job.get("job_title", ""),
            "url": job.get("url", ""),
        },
        # Boilerplate form answers (work auth, sponsorship, EEO, source), flattened
        # from the master answer store (active entries only). Personal + editable
        # from the dashboard; defaults reflect a US citizen / GC who needs no
        # sponsorship. `answer_bank` carries the full rich list so the filler knows
        # which answers are fixed vs. open-ended. The filler still never submits.
        "standard_answers": apply_answers.as_standard_answers(),
        "answer_bank": apply_answers.load(),
        "resume_bullets": bullets,
    }
    path = out_dir / "apply_data.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_from_folder(folder: Path, job: Dict[str, str]) -> Path:
    """Backfill apply_data.json for an already-tailored folder whose resume PDF
    exists but whose apply_data.json is missing (e.g. folders tailored before
    this file existed). Bullets are unavailable here, so resume_bullets is empty;
    everything else (candidate, education, document paths, standard answers) is
    rebuilt the same way write() does."""
    folder = Path(folder)
    has_cover = (folder / output.cover_filename()).exists()
    return write(job, folder, [], cover_letter=has_cover)
