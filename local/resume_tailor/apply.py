"""Open a tailored job application for human review — never submit it.

Given a job (by id, or by company+title), this finds the folder where its
tailored résumé + apply_data.json landed, loads the form-prefill profile, and
opens the posting's apply URL in the browser. A human reviews every field and
clicks submit; nothing here auto-fills or auto-submits. The apply_data.json's
"standard_answers" block is what a Claude-in-Chrome form-filler skill consumes.

CLI:  python -m resume_tailor.apply --job-id <id> [--company C --title T]
                                    [--print] [--open]
(run from the local/ dir, where the resume_tailor package is importable.)
"""
from __future__ import annotations

import argparse
import json
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

from . import config, output

# ~/Downloads/Generated_Resumes — same root output.resolve_dir writes into.
BASE_DIR = config.OUTPUT_ROOT


def resolve_generated_dir(job_id: Optional[str] = None,
                          company: Optional[str] = None,
                          title: Optional[str] = None) -> Path:
    """Locate the folder holding this job's tailored résumé + apply_data.json.

    With company+title, use output.resolve_dir's deterministic location. Else
    scan BASE_DIR/**/apply_data.json (incl. dated subfolders) for one whose
    job.job_posting_id matches job_id, returning the most-recently-modified on
    ties. Raises FileNotFoundError with guidance when nothing matches.
    """
    if company and title:
        folder = output.resolve_dir(company, title)
        if not (folder / "apply_data.json").exists():
            raise FileNotFoundError(
                f"No apply_data.json under {folder}. Tailor this job first "
                "(Tailor resume), then retry."
            )
        return folder

    if not job_id:
        raise ValueError("Provide either job_id, or both company and title.")

    matches: list[tuple[float, Path]] = []
    base = Path(BASE_DIR)
    if base.is_dir():
        for meta in base.glob("**/apply_data.json"):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str((data.get("job") or {}).get("job_posting_id", "")) == str(job_id):
                try:
                    mtime = meta.stat().st_mtime
                except OSError:
                    mtime = 0.0
                matches.append((mtime, meta.parent))
    if not matches:
        raise FileNotFoundError(
            f"No tailored résumé found for job {job_id} under {base}. "
            "Tailor this job first (Tailor resume), then retry."
        )
    matches.sort(key=lambda t: t[0], reverse=True)
    return matches[0][1]


def build_apply_context(generated_dir: Path) -> Dict[str, Any]:
    """Load apply_data.json from `generated_dir`, verify the résumé PDF exists,
    and return the profile plus resolved absolute paths and the apply URL.

    Raises FileNotFoundError if apply_data.json or the résumé PDF is missing.
    """
    generated_dir = Path(generated_dir)
    meta_path = generated_dir / "apply_data.json"
    if not meta_path.exists():
        raise FileNotFoundError(
            f"apply_data.json missing in {generated_dir} — tailor this job first."
        )
    data: Dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))

    docs = data.get("documents") or {}
    resume_pdf = _abs_path(docs.get("resume_pdf"), generated_dir)
    if not resume_pdf or not resume_pdf.exists():
        raise FileNotFoundError(
            f"Résumé PDF missing for {generated_dir} "
            f"(documents.resume_pdf={docs.get('resume_pdf')!r}) — re-tailor this job."
        )
    cover_pdf = _abs_path(docs.get("cover_letter_pdf"), generated_dir)

    job = data.get("job") or {}
    ctx = dict(data)
    ctx["generated_dir"] = str(generated_dir)
    ctx["resume_pdf"] = str(resume_pdf)
    ctx["cover_letter_pdf"] = str(cover_pdf) if cover_pdf and cover_pdf.exists() else ""
    ctx["apply_url"] = job.get("url", "")
    return ctx


def _abs_path(value: Optional[str], base: Path) -> Optional[Path]:
    """Resolve a stored doc path to an absolute Path; relative paths resolve
    against the generated folder. Empty/None -> None."""
    if not value:
        return None
    p = Path(value)
    return p if p.is_absolute() else (base / p)


def _summary(ctx: Dict[str, Any]) -> str:
    job = ctx.get("job") or {}
    cand = ctx.get("candidate") or {}
    lines = [
        "Apply — review before submitting (this tool never submits for you):",
        f"  Candidate : {cand.get('full_name', '?')}  <{cand.get('email', '')}>",
        f"  Job       : {job.get('title', '?')} @ {job.get('company', '?')} "
        f"(id {job.get('job_posting_id', '?')})",
        f"  Apply URL : {ctx.get('apply_url') or '(none)'}",
        f"  Résumé    : {ctx.get('resume_pdf', '')}",
    ]
    if ctx.get("cover_letter_pdf"):
        lines.append(f"  Cover     : {ctx['cover_letter_pdf']}")
    lines.append(f"  Folder    : {ctx.get('generated_dir', '')}")
    lines.append(
        "  Reminder  : Run the apply-to-job skill in Claude-in-Chrome to fill the "
        "form, then review every field. Submission is left to you."
    )
    return "\n".join(lines)


def _open_url(url: str) -> None:
    """Open the apply URL in Chrome (configured profile) if ui's launcher is
    importable, else the default browser. Opens the posting only — never submits."""
    if not url:
        return
    try:
        from ui import open_in_chrome  # type: ignore
        open_in_chrome(url)
    except Exception:
        webbrowser.open(url)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="resume_tailor.apply",
        description="Open a tailored job application for human review (never submits).",
    )
    ap.add_argument("--job-id", help="job_posting_id to resolve the tailored folder by.")
    ap.add_argument("--company", help="Company name (with --title, resolves the folder directly).")
    ap.add_argument("--title", help="Job title (with --company).")
    ap.add_argument("--print", dest="do_print", action="store_true", default=True,
                    help="Print a human-readable summary (default on).")
    ap.add_argument("--open", dest="do_open", action="store_true",
                    help="Open the apply URL in the browser for review.")
    args = ap.parse_args(argv)

    if not args.job_id and not (args.company and args.title):
        ap.error("provide --job-id, or both --company and --title.")

    try:
        folder = resolve_generated_dir(
            job_id=args.job_id, company=args.company, title=args.title)
        ctx = build_apply_context(folder)
    except (FileNotFoundError, ValueError) as exc:
        print(f"apply: {exc}")
        return 1

    if args.do_print:
        print(_summary(ctx))
    if args.do_open:
        _open_url(ctx.get("apply_url", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
