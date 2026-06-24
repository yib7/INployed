"""Open a tailored job application for human review — never submit it.

Given a job (by id, or by company+title), this finds the folder where its
tailored résumé + apply.md landed, loads the self-contained apply sheet, and
opens the posting's apply URL in the browser. A human reviews every field and
submits; nothing here auto-fills or auto-submits. The apply.md is the sheet the
user pastes into Claude-in-Chrome to fill the form (it never submits).

CLI:  python -m resume_tailor.apply --job-id <id> [--company C --title T]
                                    [--print] [--open]
(run from the local/ dir, where the resume_tailor package is importable.)
"""
from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

from . import apply_data, config, output

APPLY_SHEET = "apply.md"


def _scan_by_job_id(job_id: str) -> Optional[Path]:
    """Most-recently-modified folder whose apply.md meta marker job_posting_id
    matches (scanning dated subfolders too), or None."""
    matches: list[tuple[float, Path]] = []
    base = Path(config.OUTPUT_ROOT)
    if base.is_dir():
        for meta in base.glob("**/" + APPLY_SHEET):
            try:
                text = meta.read_text(encoding="utf-8")
            except OSError:
                continue
            if str(apply_data.parse_marker(text).get("job_posting_id", "")) == str(job_id):
                try:
                    mtime = meta.stat().st_mtime
                except OSError:
                    mtime = 0.0
                matches.append((mtime, meta.parent))
    if not matches:
        return None
    matches.sort(key=lambda t: t[0], reverse=True)
    return matches[0][1]


def resolve_generated_dir(job_id: Optional[str] = None,
                          company: Optional[str] = None,
                          title: Optional[str] = None,
                          job: Optional[Dict[str, Any]] = None) -> Path:
    """Locate the folder holding this job's tailored résumé + apply.md.

    Resolution order:
      1. By job_id: scan for an apply.md whose marker matches (incl. dated
         subfolders), newest wins.
      2. By company+title: the canonical folder. If apply.md is missing but a
         résumé PDF is there (older folders), BACKFILL apply.md from `job` (or
         company/title/id) and use that folder.

    Raises FileNotFoundError with guidance when nothing matches.
    """
    if job and not (company and title):
        company = company or job.get("company_name")
        title = title or job.get("job_title")
        job_id = job_id or job.get("job_posting_id")

    if job_id:
        found = _scan_by_job_id(job_id)
        if found is not None:
            return found

    if company and title:
        folder = output.base_dir(company, title)
        if (folder / APPLY_SHEET).exists():
            return folder
        if (folder / output.resume_filename()).exists():
            backfill_job = job or {
                "job_posting_id": str(job_id or ""),
                "company_name": company, "job_title": title, "url": "",
            }
            apply_data.write_from_folder(folder, backfill_job)
            return folder

    if not job_id and not (company and title):
        raise ValueError("Provide either job_id, or both company and title.")

    where = output.base_dir(company, title) if (company and title) else config.OUTPUT_ROOT
    raise FileNotFoundError(
        f"No tailored résumé found for job {job_id or f'{company} — {title}'} under {where}. "
        "Tailor this job first (Tailor resume), then retry."
    )


def build_apply_context(generated_dir: Path) -> Dict[str, Any]:
    """Load apply.md from `generated_dir`, verify the résumé PDF exists, and return
    the apply sheet text plus resolved absolute paths, the job identity (from the
    sheet's meta marker), and the apply URL.

    Raises FileNotFoundError if apply.md or the résumé PDF is missing.
    """
    generated_dir = Path(generated_dir)
    meta_path = generated_dir / APPLY_SHEET
    if not meta_path.exists():
        raise FileNotFoundError(
            f"{APPLY_SHEET} missing in {generated_dir} — tailor this job first."
        )
    text = meta_path.read_text(encoding="utf-8")

    resume_pdf = generated_dir / output.resume_filename()
    if not resume_pdf.exists():
        raise FileNotFoundError(
            f"Résumé PDF missing for {generated_dir} "
            f"({output.resume_filename()!r} not found) — re-tailor this job."
        )
    cover_pdf = generated_dir / output.cover_filename()

    meta = apply_data.parse_marker(text)
    job = {
        "job_posting_id": str(meta.get("job_posting_id", "")),
        "company": meta.get("company", ""),
        "title": meta.get("title", ""),
        "url": meta.get("url", ""),
    }
    return {
        "generated_dir": str(generated_dir),
        "resume_pdf": str(resume_pdf),
        "cover_letter_pdf": str(cover_pdf) if cover_pdf.exists() else "",
        "apply_url": job["url"],
        "apply_md": text,
        "apply_md_path": str(meta_path),
        "job": job,
    }


def _summary(ctx: Dict[str, Any]) -> str:
    job = ctx.get("job") or {}
    lines = [
        "Apply — review before submitting (this tool never submits for you):",
        f"  Job       : {job.get('title', '?')} @ {job.get('company', '?')} "
        f"(id {job.get('job_posting_id', '?')})",
        f"  Apply URL : {ctx.get('apply_url') or '(none)'}",
        f"  Résumé    : {ctx.get('resume_pdf', '')}",
    ]
    if ctx.get("cover_letter_pdf"):
        lines.append(f"  Cover     : {ctx['cover_letter_pdf']}")
    lines.append(f"  Apply sheet: {ctx.get('apply_md_path', '')}")
    lines.append(f"  Folder    : {ctx.get('generated_dir', '')}")
    lines.append(
        "  Reminder  : This prints/opens the posting only. Paste the apply sheet (apply.md) into "
        "Claude-in-Chrome: it fills every safe field across all pages up to the final Submit screen, "
        "then stops for you to review and send. It never logs in, never creates accounts, never submits."
    )
    return "\n".join(lines)


def _open_url(url: str) -> None:
    """Open the apply URL in Chrome (configured profile) if the launcher is
    importable, else the default browser. Opens the posting only — never submits."""
    if not url:
        return
    try:
        from chrome import open_in_chrome  # type: ignore
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
