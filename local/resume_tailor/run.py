"""Orchestrate the tailor pipeline and expose a single entry point + CLI.

    tailor(job, cover_letter=False, on_status=None) -> Path (output directory)

job is a dict with: company_name, job_title, job_summary, url (job_posting_id optional).

CLI:  python -m resume_tailor.run --job-id <id> [--cover-letter] [--csv <path>]
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional

from . import apply_data, assets, ats, compose, config, coverletter, layout, llm, output, research
from .compile import compile_tex, enforce_one_page, pdflatex_available

StatusFn = Optional[Callable[[str], None]]


def _noop(_msg: str) -> None:
    pass


def _field(job: Dict[str, str], key: str) -> str:
    """Robust string getter: NaN floats / None (pandas rows) become ''."""
    v = job.get(key)
    if not isinstance(v, str):
        return ""
    s = v.strip()
    return "" if s.lower() in ("nan", "none") else s


def _to_plain(text: str) -> str:
    """HTML -> markdown-ish plain text (descriptions are often raw HTML)."""
    if "<" in text and ">" in text:
        try:
            from markdownify import markdownify
            return markdownify(text, heading_style="ATX").strip()
        except ImportError:
            import re
            return re.sub(r"<[^>]+>", " ", text).strip()
    return text


def _job_description_text(job: Dict[str, str]) -> str:
    """The richest available JD text: full description first, summary last.

    LinkedIn's job_summary is often truncated or empty, so tailoring against it
    alone wastes most of the JD signal (and hard-fails when it's blank).
    """
    for key in ("job_description_formatted", "job_description", "job_summary"):
        text = _to_plain(_field(job, key))
        if len(text) >= 40:
            return text
    return ""


def _resolve_bullets(jd: str, job_title: str, sel: dict, log: Callable[[str], None]) -> Dict[str, str]:
    """Rephrase selected groups, then run the anti-inflation gate: fix once, else drop.

    Bullets are keyed by group key (gkey = '+'.join(atom_ids)); each is verified
    against the UNION of its group's atoms.
    """
    gm = compose.group_map(sel)
    budgets = compose.layout_budgets(sel)
    bullets = compose.rephrase(jd, job_title, sel, budgets=budgets)
    log(f"rephrased {len(bullets)} bullet(s); verifying grounding…")
    results = compose.verify(bullets, gm)
    flagged = {gk: r["problems"] for gk, r in results.items() if not r["ok"]}
    if flagged:
        log(f"verifier flagged {len(flagged)} bullet(s); regenerating…")
        for gk, problems in flagged.items():
            try:
                fixed = compose.rephrase_fix(jd, gm.get(gk, []), bullets[gk], problems)
                if fixed:
                    bullets[gk] = fixed
            except Exception:
                pass
        recheck = compose.verify({gk: bullets[gk] for gk in flagged if gk in bullets}, gm)
        for gk, r in recheck.items():
            if not r["ok"]:
                log(f"dropping still-unsupported bullet [{gk}]: {r['problems']}")
                bullets.pop(gk, None)
    return bullets


def _word_trim(text: str, max_visible: int) -> str:
    """Hard-trim to <= max_visible rendered glyphs at a word boundary (clean_bullet
    re-adds the trailing period). Numbers/impact are front-loaded, so trimming the
    tail preserves the metrics — the deterministic last resort when the model can't
    hit an over-length target on its own."""
    text = text.rstrip().rstrip(".")
    budget = max_visible - 1  # leave room for the period clean_bullet appends
    if len(text) <= budget:
        return text
    cut = text[:budget]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip(",; ")


def _enforce_layout(jd: str, sel: dict, bullets: Dict[str, str],
                    log: Callable[[str], None]) -> None:
    """Drive every layout-budgeted bullet (Initech's two, each leadership bullet)
    into its target printed-line window. Tries up to two grounded `refit` rewrites,
    keeping the candidate closest to the window; if still over-length, deterministically
    word-trims as a last resort. Under-length bullets whose atoms can't fill the
    target are logged and left (we never invent facts to pad)."""
    budgets = compose.layout_budgets(sel)

    def dist(text: str, tgt: int) -> int:
        lo, hi = layout.body_line_budget(tgt)
        n = layout._visible_len(text)
        return 0 if lo <= n <= hi else (lo - n if n < lo else n - hi)

    for gk, tgt in budgets.items():
        if gk not in bullets or layout.body_fits(bullets[gk], tgt):
            continue
        lo, hi = layout.body_line_budget(tgt)
        cur = layout._visible_len(bullets[gk])
        if cur < lo:
            # Under-length: padding would mean inventing facts (forbidden), and the
            # content is already grounded — accept it rather than spend a call that
            # can only fail. (Over-length is the case worth a rewrite.)
            log(f"layout: [{gk}] under target ({cur} chars for {tgt} line(s)); "
                f"left as-is (no padding without facts)")
            continue
        # Over-length: one grounded tighten pass (flash), then a deterministic trim.
        try:
            cand = compose.refit(jd, gk.split("+"), bullets[gk], tgt)
        except Exception:
            cand = ""
        if cand and dist(cand, tgt) <= dist(bullets[gk], tgt):
            bullets[gk] = cand
        if not layout.body_fits(bullets[gk], tgt):
            if layout._visible_len(bullets[gk]) > hi:
                bullets[gk] = _word_trim(bullets[gk], hi)
            got = layout.est_body_lines(bullets[gk])
            if got != tgt:
                log(f"layout: [{gk}] wanted {tgt} line(s), best effort ~{got} "
                    f"({layout._visible_len(bullets[gk])} chars)")


def tailor(job: Dict[str, str], *, cover_letter: bool = False, on_status: StatusFn = None) -> Path:
    log = on_status or _noop
    llm.reset_usage()

    company = _field(job, "company_name") or "Unknown Company"
    job_title = _field(job, "job_title") or "Role"
    jd = _job_description_text(job)
    if not pdflatex_available():
        raise RuntimeError(f"pdflatex not found at '{config.PDFLATEX_PATH}'. Install MiKTeX/TeX Live.")
    if len(jd) < 40:
        raise RuntimeError("Job description is empty/too short to tailor against.")

    log(f"selecting evidence for: {job_title} @ {company}")
    sel = compose.select(jd, job_title, company)
    if not sel.get("experience"):
        raise RuntimeError("Selection returned no experience — aborting (check the JD/model).")

    bullets = _resolve_bullets(jd, job_title, sel, log)
    if not bullets:
        raise RuntimeError("No grounded bullets survived verification.")

    _enforce_layout(jd, sel, bullets, log)

    log("compressing skills…")
    skill_lines = compose.compress_skills(jd, job_title, sel)

    out_dir = output.resolve_dir(company, job_title)
    with tempfile.TemporaryDirectory(prefix="resume_tailor_") as tmp:
        tmp_path = Path(tmp)
        tex_path = tmp_path / "resume.tex"
        log("rendering + compiling (one-page enforcement)…")
        result, final_bullets, tex = enforce_one_page(
            sel, bullets, skill_lines, tex_path, tmp_path, jd, on_status=log
        )
        if not result.ok or not result.pdf_path:
            raise RuntimeError(f"LaTeX compile failed: {result.error}\n{result.log_tail}")

        shutil.copyfile(result.pdf_path, out_dir / output.resume_filename())
        shutil.copyfile(tex_path, out_dir / "resume.tex")  # keep source for inspection

        try:
            cov = ats.write_report(jd, out_dir / output.resume_filename(), out_dir)
            log(f"ATS keyword coverage: {cov:.0%} (details in ats_report.txt)")
        except Exception as exc:  # noqa: BLE001 - the report is advisory, never fatal
            log(f"ATS check skipped ({exc})")

        if cover_letter:
            log("writing cover letter…")
            try:
                blurb = ""
                try:
                    log("researching company (grounded search)…")
                    blurb = research.company_blurb(company, job_title)
                except Exception as exc:  # noqa: BLE001 - research is optional
                    log(f"company research unavailable ({exc})")
                body = coverletter.generate_body(jd, job_title, company, final_bullets,
                                                 research=blurb)
                cl_tex = tmp_path / "cover_letter.tex"
                cl_res, _ = coverletter.render_cover_letter(body, company, cl_tex, tmp_path)
                if cl_res.ok and cl_res.pdf_path:
                    shutil.copyfile(cl_res.pdf_path, out_dir / output.cover_filename())
                else:
                    log(f"cover letter compile failed: {cl_res.error}")
            except Exception as exc:  # noqa: BLE001 - cover letter is optional, never fatal
                log(f"cover letter skipped ({exc})")

        try:
            apply_data.write(job, out_dir, list(final_bullets.values()), cover_letter)
            log("apply_data.json written (form-prefill profile)")
        except Exception as exc:  # noqa: BLE001 - advisory artifact, never fatal
            log(f"apply data skipped ({exc})")

    log(f"done -> {out_dir}")
    log("token usage: " + llm.usage_summary())
    return out_dir


# ── CLI ──────────────────────────────────────────────────────────────────────
_DEFAULT_CSV = "E:/My Drive/LinkedInJobs/linkedin_jobs_master.csv.gz"


def _job_from_csv(job_id: str, csv_path: str) -> Dict[str, str]:
    import pandas as pd

    df = pd.read_csv(csv_path, dtype=str)
    row = df.loc[df["job_posting_id"].astype(str) == str(job_id)]
    if row.empty:
        raise SystemExit(f"job_posting_id {job_id} not found in {csv_path}")
    r = row.iloc[0]
    return {
        "job_posting_id": str(job_id),
        "company_name": r.get("company_name", ""),
        "job_title": r.get("job_title", ""),
        "job_description_formatted": r.get("job_description_formatted", ""),
        "job_description": r.get("job_description", ""),
        "job_summary": r.get("job_summary", ""),
        "url": r.get("url", ""),
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Tailor a resume for one scraped job.")
    ap.add_argument("--job-id", required=True, help="job_posting_id from the master CSV")
    ap.add_argument("--csv", default=_DEFAULT_CSV, help="path to the master CSV(.gz)")
    ap.add_argument("--cover-letter", action="store_true", help="also generate a cover letter")
    args = ap.parse_args()

    job = _job_from_csv(args.job_id, args.csv)
    print(f"Tailoring: {job['job_title']} @ {job['company_name']}")
    out = tailor(job, cover_letter=args.cover_letter, on_status=lambda m: print("  ·", m))
    print(f"\nOutput: {out}")


if __name__ == "__main__":
    main()
