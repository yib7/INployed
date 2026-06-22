# CLAUDE.md — project guidance

Job Discovery & Résumé-Tailoring Pipeline. Three subsystems: `scraper.py` (Bright Data scrape) →
`score_jobs.py` (two-stage Gemini scorer) → `local/ui.py` (Tkinter dashboard) + `local/resume_tailor/`
(LaTeX résumé engine). See `docs/ARCHITECTURE.md` for the codebase tour and `docs/HANDOFF.md` for ops.

- Keep `.ps1` scripts pure ASCII (PowerShell 5.1 mangles non-ASCII).
- The résumé engine's rule is **select and re-phrase, never invent** — every bullet traces to an
  atom the user wrote in `resume_tailor_files/master_experience.yaml`.
- Tests: `python -m pytest`; dashboard smoke: `python tests/smoke_ui.py`.
