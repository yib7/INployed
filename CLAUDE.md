# CLAUDE.md — project guidance

INployed — a Job Discovery & Résumé-Tailoring Pipeline. Three subsystems: `scraper.py` (Bright Data scrape) →
`score_jobs.py` (two-stage Gemini scorer) → `local/app.py` + `local/qt/` (PySide6/Qt dashboard) +
`local/resume_tailor/` (LaTeX résumé engine). See `docs/ARCHITECTURE.md` for the codebase tour and
`docs/HANDOFF.md` for ops.

- The dashboard is **PySide6/Qt** (`python local/app.py`; toolkit-agnostic data/config logic lives in
  `local/jobsdata.py` + `local/chrome.py`). Qt tests run headless with `QT_QPA_PLATFORM=offscreen`.
- Keep `.ps1` scripts pure ASCII (PowerShell 5.1 mangles non-ASCII).
- The résumé engine's rule is **select and re-phrase, never invent** — every bullet traces to an
  atom the user wrote in `resume_tailor_files/master_experience.yaml`.
- Tests: `QT_QPA_PLATFORM=offscreen python -m pytest`; dashboard smoke: `python tests/smoke_qt.py`.
