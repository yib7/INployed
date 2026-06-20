# Codebase explainer

A guided tour of how the pieces fit together — written for someone (you, later)
reopening this repo cold. Operator/runbook details live in `HANDOFF.md`; this doc
is about *how the code is shaped and why*.

## The three subsystems

### 1. Scrape (`scraper.py`)
Async Bright Data client. Triggers keyword × remote-type searches, polls the
snapshot to "ready", downloads rows, dedupes, drops blocklisted companies, and
appends to a cumulative master CSV. Two cost-aware details worth remembering:
- It excludes every job id seen in the last `EXCLUDE_WINDOW_DAYS` (Bright Data
  bills per collected posting, so re-collecting a still-live job wastes money).
- `--snapshot <id>` re-downloads an already-collected (already-billed) snapshot
  without triggering a new collection — the recovery path when a run dies after
  billing.

### 2. Score (`score_jobs.py`)
A two-stage Gemini filter. Stage 1 (cheap flash-lite) does a fast relevance pass;
stage 2 (flash) deep-scores the survivors. A deterministic `min_required_years`
regex pre-filter drops over-senior roles *before* any LLM sees them (this is the
load-bearing, heavily-tested function — see `tests/test_min_required_years.py`).

### 3. Dashboard (`local/ui.py`) + résumé engine (`local/resume_tailor/`)
Tkinter app: high-score triage, an SQLite-backed application tracker
(`local/seen_db.py`) with follow-up nudges, a stats tab, and the **Tailor resume**
button. Heavy operations (tailoring, prep-sheet generation) run on background
threads and marshal status back to the UI via `root.after`, so the window never
freezes.

## The résumé engine in depth (`local/resume_tailor/`)

The whole engine obeys one rule: **select and re-phrase, never invent.** Every
bullet must be traceable to a fact ("atom") the user actually wrote in
`master_experience.yaml`.

| Module | Role |
|--------|------|
| `config.py` | Paths + model tiers (flash-lite / flash / pro), all env-overridable. |
| `assets.py` | Loads/caches `master_experience.yaml` (atoms, blocks, `tailor:` config) and the LaTeX preamble. |
| `compose.py` | The LLM stages: `select` → `rephrase` → `verify` → `compress_skills`, plus the constrained `rephrase_fix`/`refit`/`shrink` fix-ups. |
| `layout.py` | The hard layout spec: per-bullet printed-line budgets and fill floors (single-line ≥75%, multi-line last line ≥50%), all calibrated to the template. |
| `render.py` | Assembles the `.tex` — header + Education + body, all generated from the yaml. |
| `compile.py` | Runs `pdflatex` and enforces one page (drop-weakest-bullet + shrink loop). |
| `latexutil.py` | Escaping, emphasis stripping, date formatting, unicode-math → LaTeX. |
| `output.py` | Where the PDF goes; candidate name from the yaml. |
| `ats.py` | Deterministic ATS keyword-coverage report. |
| `coverletter.py`, `prep.py`, `research.py`, `apply_data.py` | Optional artifacts: cover letter, interview-prep sheet, grounded company research, form-prefill JSON. |
| `master_gaps.py` | The JD-gap suggester: find skills the JD wants that aren't in your file, screen + place them (flash-lite), write back with a reviewable diff + backup. |
| `run.py` | Orchestrates the full pipeline and exposes the CLI. Artifact generation (cover letter / ATS / prep) and tone are now config-driven, default-preserving. |
| `apply.py`, `apply_config.py` | Apply automation: resolve a tailored job's folder, build the apply context, open the posting (never submits); `standard_answers` defaults (work auth, sponsorship, EEO). |

### Why it's config-driven
`compose.py`/`layout.py`/`render.py` deliberately hardcode **no employer names**.
Which blocks are required, the fixed per-block line budgets, and the candidate's
identity all come from the yaml (the `tailor:` section + `basics`/`education`). That
is what lets the same code produce anyone's résumé — see `tests/test_tailor_config.py`.

## Settings & customization (`local/settings.py` + dashboard Settings tab)
`settings.py` is one schema (`SETTINGS_SCHEMA`) describing every user-editable
option (key, type, default, validation, backing file). The dashboard's **Settings**
tab auto-renders it grouped by section (Dashboard / Scraper / Scoring / Résumé /
Apply) inside a scrollable canvas. `load`/`save` read and atomically write (with a
`.bak`) `local/config.json`, plus the git-ignored root-level `search_config.json`
(read by `scraper.py`), `scoring_config.json` (read by `score_jobs.py`), and
`apply_config.json` (read by `apply_data.py`). The VM-standalone scraper/scorer never
import `local/`; they read their own JSON with **env-override > file > built-in-default**
precedence, so an absent file reproduces today's behavior exactly.

## Apply automation (`apply.py` + the `apply-to-job` skill)
`apply_data.write` drops an `apply_data.json` next to each tailored résumé (candidate
basics, education, doc paths, tailored bullets, and a `standard_answers` block). The
dashboard's **Apply** button (and `python -m resume_tailor.apply`) resolves that folder,
opens the posting in Chrome, and surfaces the context; the `.claude/skills/apply-to-job`
playbook drives Claude-in-Chrome to fill the form and **stop for human review — it
never auto-submits.**

### The one-page guarantee
`layout.py` derives a `(min, max)` character window per bullet from empirically
calibrated chars-per-line constants. `rephrase` is told each fixed bullet's window;
a fit loop in `run.py` then `refit`s (grounded) or word-trims (deterministic) any
bullet outside its window, and `compile.enforce_one_page` drops the weakest bullet
and shrinks until it fits one page.

## Data flow, end to end
```
master_experience.yaml ──┐
                         ▼
job (CSV row) ─► select ─► rephrase ─► verify ─► layout fit ─► render ─► pdflatex ─► PDF
                                                                    └► ATS report, cover letter, prep, apply_data
```

## Where the tests live
- `tests/test_min_required_years.py` — the years pre-filter regex.
- `tests/test_tailor_config.py` — config-driven layout + yaml-sourced rendering.
- `tests/test_bullet_length.py` — fill floors + unicode-math conversion.
- `tests/test_master_gaps.py` — JD-gap detection, comment-preserving write, diff.
- `tests/test_seen_reconcile.py`, `tests/test_download_race.py` — registry + scraper edge cases.
- `tests/smoke_ui.py` — full dashboard smoke (run directly, not under pytest).
