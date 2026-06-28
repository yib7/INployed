# Changelog

All notable changes to INployed are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims for
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.1] - 2026-06-28

Bug fix: per-project résumé bullet counts are honored.

### Fixed
- A project's configured per-project bullet count (set in Resume Layout) was treated as a ceiling
  rather than a target: a project the selector under-filled stayed short even when the page had room,
  because only experience and leadership blocks were padded up to their configured counts. A project
  with a configured layout is now padded up to its exact count (as well as trimmed down to it) from the
  project's own unused atoms, and the selection prompt names each project's target count. One-page
  enforcement still trims a padded bullet back on overflow; unconfigured projects keep their cap-only
  behavior.

## [1.1.0] - 2026-06-28

Post-1.0 résumé-tailoring quality work: distinct leading verbs, width-aware layout, best-N skills.

### Added
- Categorized action-verb palette sourced from `resume_tailor_files/active_words.md` (558 verbs
  across 9 categories), with a built-in fallback when the file is absent.
- Best-N skills selection: skills lines are chosen for job-description relevance rather than by a
  fixed order.

### Changed
- Every tailored bullet now opens with a distinct leading verb. The model self-dedupes on the
  first pass and the code guarantees zero reuse across the résumé (cheap re-roll, then a
  deterministic in-category swap as the backstop).
- Bullet and skills-line trimming now measure real glyph widths against the template column
  instead of a character-count cap, so lines fill the page more tightly without overflowing.
- Education section header renamed from "Honors" to "Awards & Honors".
- Tightened the résumé template's vertical spacing (bullets and section subheadings).
- Manual scrapes now sync their seen job IDs to the VM so a local run is not re-collected.

## [1.0.0] - 2026-06-28

First public release: an end-to-end job-discovery and résumé-tailoring pipeline.

### Added
- Job discovery (`scraper.py`): an async Bright Data client that runs keyword/remote-type
  searches, dedupes against a cumulative master CSV, and drops blocklisted companies, with
  cost-aware exclusion of already-collected postings and a snapshot-recovery path.
- Two-stage Gemini scorer (`score_jobs.py`): a cheap flash-lite relevance pass feeds a deeper
  flash deep-score, behind a deterministic `min_required_years` pre-filter.
- PySide6/Qt dashboard (`local/app.py` + `local/qt/`): virtualized job tables, an SQLite
  application tracker with follow-up nudges, run statistics, a stale-pipeline badge, and a
  schema-driven Settings tab that edits every option (including masked secrets) from one form.
- Résumé-tailoring engine (`local/resume_tailor/`): a select / rephrase / verify / layout /
  compile pipeline that produces a one-page LaTeX résumé, cover letter, ATS keyword report, and
  interview-prep sheet, built on the rule "select and re-phrase, never invent".
- Resume Data and Apply Answers editors, a self-contained `apply.md` apply sheet, and an
  optional GCP VM scheduler driven from the dashboard over the user's own `gcloud` login.
- Cross-platform dashboard + engine (Windows / macOS / Linux); the setup scripts and VM
  automation are Windows-first.

[Unreleased]: https://github.com/yib7/INployed/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/yib7/INployed/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/yib7/INployed/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/yib7/INployed/releases/tag/v1.0.0
