# Codebase explainer

A guided tour of how the pieces fit together — written for someone (you, later)
reopening this repo cold. Operator/runbook details live in `HANDOFF.md`; this doc
is about *how the code is shaped and why*.

## The three subsystems

### 1. Scrape (`scraper.py`)
Async Bright Data client. Triggers keyword × remote-type searches, polls the
snapshot to "ready", downloads rows, dedupes, drops blocklisted companies, and
appends to a cumulative master CSV. Two cost-aware details worth remembering:
- It excludes every job id already in the master from re-collection (Bright Data
  bills per collected posting, so re-fetching a job we already have wastes money —
  and a posting still live weeks later is usually stale anyway). See
  `load_exclude_ids()`.
- `--snapshot <id>` re-downloads an already-collected (already-billed) snapshot
  without triggering a new collection — the recovery path when a run dies after
  billing.

### 2. Score (`score_jobs.py`)
A two-stage Gemini filter. Stage 1 (cheap flash-lite) does a fast relevance pass;
stage 2 (flash) deep-scores the survivors. A deterministic `min_required_years`
regex pre-filter drops over-senior roles *before* any LLM sees them (this is the
load-bearing, heavily-tested function — see `tests/test_min_required_years.py`).

### 3. Dashboard (`local/app.py` + `local/qt/`) + résumé engine (`local/resume_tailor/`)
PySide6/Qt app (entry point `local/app.py`): high-score triage, an SQLite-backed
application tracker (`local/seen_db.py`) with follow-up nudges, a stats tab, the
Settings/Resume Data/Apply Answers editors, and the **Tailor resume** button. The
job tables are `QTableView` + `QSortFilterProxyModel` (virtualized, smooth). Pure
data/config logic is toolkit-agnostic (`local/jobsdata.py`, `local/chrome.py`).
Heavy operations (scrape, tailor, prep-sheet, resume.md) run on Qt worker threads
(`local/qt/workers.py`) and marshal results back via signals, so the window never
freezes. Tailoring a multi-job selection fans the jobs out **concurrently** on a
`ThreadPoolExecutor` (the work is I/O- + `pdflatex`-bound, so threads genuinely
overlap); per-job failures are captured and reported in one aggregate dialog, registry
writes happen back on the UI thread (the SQLite connection is thread-affine), and a
warning precedes very large batches. Tailoring streams live per-job progress to the status bar
via a `MainWindow.tailor_progress` Qt signal (the engine's `on_status` callback, queued cross-thread
from the pool workers). See `MainWindow._tailor_work`/`_finish_tailor`. The
**Apply** button is the rightmost action and turns green only when the selected job has both its
résumé PDF and `apply.md` on disk; clicking it opens the posting in Chrome and swaps the bottom
score preview for a right-side **Apply panel** (copyable doc paths + the apply sheet; `✕` closes, and
**"I applied to this job"** confirms → records the job applied in the Tracker → closes).

A few **durability/visibility** affordances: the Tracker tab can **Export / Import** the whole
`seen.db` (`SeenRegistry.export_to` via SQLite `VACUUM INTO`; `import_from` merges — newer
`status_date` wins, earliest `applied_date` kept, seen unioned). The Stats tab shows a fresh/stale
**pipeline badge** (`jobsdata.run_staleness` + the `stale_after_hours` setting). The Resume Data tab
warns when `resume.md` has drifted behind `master_experience.yaml` (`resume_md.resume_md_stale`,
mtime compare) with a one-click Regenerate. With zero jobs loaded the High Score tab shows a
first-run get-started hint (`JobsTab.set_empty_widget`).

## The résumé engine in depth (`local/resume_tailor/`)

The whole engine obeys one rule: **select and re-phrase, never invent.** Every
bullet must be traceable to a fact ("atom") the user actually wrote in
`master_experience.yaml`.

| Module | Role |
|--------|------|
| `config.py` | Paths + model tiers (flash-lite / flash / pro) + the escalating timeout schedule, all env-overridable. |
| `llm.py` | The single Gemini transport (`call()` → `_call_gemini`). Each request gets a per-call timeout that escalates across attempts (`tailor_timeout_schedule()`, default 60→120→180s) and retries **on timeout only**, on top of the existing 429/transient backoff — so a hung call can't stall a tailor run. |
| `assets.py` | Loads/caches `master_experience.yaml` (atoms, blocks, `tailor:` config) and the LaTeX preamble. |
| `compose.py` | The LLM stages: `select` → `rephrase` → `verify` → `compress_skills`, plus the constrained `rephrase_fix`/`refit`/`shrink` fix-ups. |
| `layout.py` | The hard layout spec: per-bullet printed-line budgets and fill floors (single-line ≥75%, multi-line last line ≥50%), all calibrated to the template. |
| `render.py` | Assembles the `.tex` — header + Education + body, all generated from the yaml. |
| `compile.py` | Runs `pdflatex` and enforces one page (drop-weakest-bullet + shrink loop). |
| `latexutil.py` | Escaping, emphasis stripping, date formatting, unicode-math → LaTeX. |
| `output.py` | Where the PDF goes; candidate name from the yaml. |
| `ats.py` | Deterministic ATS keyword-coverage report. |
| `coverletter.py`, `prep.py`, `research.py`, `apply_data.py` | Optional artifacts: cover letter, interview-prep sheet, grounded company research, and the self-contained `apply.md` apply sheet. |
| `master_gaps.py` | The JD-gap suggester: find skills the JD wants that aren't in your file, screen + place them (flash-lite), write back with a reviewable diff + backup. |
| `run.py` | Orchestrates the full pipeline and exposes the CLI. Artifact generation (cover letter / ATS / prep) and tone are now config-driven, default-preserving. |
| `apply.py`, `apply_config.py` | Apply automation: resolve a tailored job's folder (by the `apply.md` meta marker), build the apply context, open the posting (never submits); `standard_answers` defaults (work auth, sponsorship, EEO, structured address). |

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

## Apply automation (`apply.py` + the `apply.md` apply sheet)
`apply_data.write` drops a single self-contained `apply.md` next to each tailored résumé. It is a
**fallback for portals that don't auto-fill the form from an uploaded résumé** — so it lists **no files
to upload**; it opens with a "when to use this sheet" note, then the fill-it-out playbook (never submit;
never log in / enter passwords, payment, SSN, or government IDs; never solve CAPTCHAs — pause and hand
off; e-sign with the candidate's name + today's date; use `XXXXX` for a blocking required field with no
answer and flag it), then candidate basics + structured address, education, **this job's tailored
résumé as markdown** (work experience / projects / leadership / technical skills), the active standard
answers, and a hidden HTML-comment meta marker carrying the job identity for lookup. The résumé sections
are rendered **deterministically** by mirroring `render.py`'s selection + grouping — fed the tailor's own
`sel` + surviving `bullets` + `skill_lines`, so the sheet reflects exactly the blocks on the PDF (only
selected blocks; each surviving bullet verbatim) with **no extra LLM call**. The dashboard's **Apply**
button (and `python -m resume_tailor.apply`) resolves the folder via that marker, opens the posting in
Chrome, and shows the Apply panel — which **renders the sheet as formatted markdown** while "Copy apply
sheet" copies the raw source; the user pastes `apply.md` into Claude-in-Chrome to fill the fields by hand
and **stop for human review — nothing auto-submits.** (The former `apply-to-job` skill is retired — its
contract now lives at the top of every `apply.md`.)

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
                                                                    └► ATS report, cover letter, prep, apply.md
```

## Where the tests live
- `tests/test_min_required_years.py` — the years pre-filter regex.
- `tests/test_tailor_config.py` — config-driven layout + yaml-sourced rendering.
- `tests/test_bullet_length.py` — fill floors + unicode-math conversion.
- `tests/test_master_gaps.py` — JD-gap detection, comment-preserving write, diff.
- `tests/test_seen_reconcile.py`, `tests/test_download_race.py` — registry + scraper edge cases.
- `tests/smoke_qt.py` — Qt dashboard smoke (run directly with `QT_QPA_PLATFORM=offscreen`, not under pytest).
