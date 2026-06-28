"""LaTeX -> PDF compile primitives + one-page enforcement.

compile_tex/page_count/pdflatex_available are ported from Resume_Tailor's
compiler.py (the proven core). enforce_one_page re-renders the composed data
after each bullet drop instead of injecting into marker blocks.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from pypdf import PdfReader

from . import config, render


@dataclass
class CompileResult:
    ok: bool
    pdf_path: Optional[Path]
    log_tail: str
    error: Optional[str] = None


def pdflatex_available() -> bool:
    return shutil.which(config.PDFLATEX_PATH) is not None


def compile_tex(tex_path: Path, work_dir: Path) -> CompileResult:
    """Run pdflatex twice so refs settle. Returns CompileResult."""
    if not pdflatex_available():
        return CompileResult(False, None, "", f"pdflatex not found at '{config.PDFLATEX_PATH}'.")
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    tex_path = tex_path.resolve()
    cmd = [
        config.PDFLATEX_PATH,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-no-shell-escape",   # defense-in-depth: never let a .tex run shell commands (\write18)
        f"-output-directory={work_dir.as_posix()}",
        tex_path.name,
    ]
    last = ""
    for _ in range(2):
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tex_path.parent))
        last = proc.stdout + "\n" + proc.stderr
        if proc.returncode != 0:
            return CompileResult(False, None, "\n".join(last.splitlines()[-60:]),
                                 f"pdflatex exited with code {proc.returncode}.")
    pdf_out = work_dir / (tex_path.stem + ".pdf")
    if not pdf_out.exists():
        return CompileResult(False, None, "\n".join(last.splitlines()[-60:]),
                             "pdflatex finished but produced no PDF.")
    return CompileResult(True, pdf_out, "\n".join(last.splitlines()[-30:]))


def page_count(pdf_path: Path) -> int:
    with pdf_path.open("rb") as fh:
        return len(PdfReader(fh).pages)


def _drop_weakest_group(sel: dict, bullets: Dict[str, str],
                        keep_projects: bool = False) -> Optional[str]:
    """Remove the weakest project bullet so the page can actually shrink.

    Projects are ordered strongest-first by select(), so trim from the bottom:
    prefer the last group of the last project that still has more than one
    bullet; if every project is down to one, drop the last project's only
    bullet (which removes that project from the render). Experience and
    leadership are never touched. Returns the dropped gkey, or None when there
    is nothing left to drop.

    When `keep_projects` is True ('exact' projects mode) only the first pass
    runs: a project's LAST remaining bullet is never dropped, so no project
    vanishes and the project count holds (best-effort if the page still spills).
    """
    projects = sel.get("projects", [])
    passes = [lambda live: len(live) > 1]
    if not keep_projects:
        passes.append(lambda live: bool(live))
    for keep_one in passes:
        for entry in reversed(projects):
            live = [
                "+".join(ids)
                for ids in entry.get("groups", [])
                if "+".join(ids) in bullets
            ]
            if keep_one(live):
                bullets.pop(live[-1])
                return live[-1]
    return None


def enforce_one_page(
    sel: dict,
    bullets: Dict[str, str],
    skill_lines: List[Dict[str, str]],
    tex_path: Path,
    work_dir: Path,
    jd: str = "",
    on_status: Optional[Callable[[str], None]] = None,
    keep_projects: Optional[bool] = None,
) -> Tuple[CompileResult, Dict[str, str], str]:
    """Render -> compile -> drop the weakest project bullet, looping until the
    resume is one page. `keep_projects` controls the drop policy; when left as
    None it resolves from config.projects_mode() ('exact' -> keep every project).
    """
    if keep_projects is None:
        keep_projects = config.projects_mode() == "exact"

    def log(msg: str) -> None:
        if on_status:
            on_status(msg)

    cur = dict(bullets)
    tex = ""
    while True:
        tex = render.render(sel, cur, skill_lines)
        tex_path.write_text(tex, encoding="utf-8")
        result = compile_tex(tex_path, work_dir)
        if not result.ok:
            return result, cur, tex
        pages = page_count(result.pdf_path)
        log(f"compiled to {pages} page(s)")
        if pages <= config.PAGE_LIMIT:
            return result, cur, tex
        dropped = _drop_weakest_group(sel, cur, keep_projects)
        if not dropped:
            log("over one page but nothing left to drop — returning best effort")
            return result, cur, tex
        log(f"over one page; dropping weakest project bullet [{dropped}]")
