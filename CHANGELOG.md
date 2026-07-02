# Changelog

All notable changes to INployed are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims for
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.4.0] - 2026-07-01

### Added
- Technical-skills lines now print the JD's spelling for a tech skill too, not just concepts.
  The same alias idea is split into two anchored maps so a real keyword ATS sees the JD's
  exact term without dumbing the résumé down: `skill_aliases` (existing) are **printable
  spelling variants** that are matched AND swapped onto a tech line when the JD uses them
  (a posting that says "Postgres" makes the line print "Postgres" instead of "PostgreSQL");
  a new `skill_aliases_match_only` map holds **broader synonyms** that count toward ATS
  coverage and are never proposed as a gap but are never printed (so "Large Language Models"
  matches your specific "LLM APIs (Gemini, OpenAI, Claude)" token without replacing it on the
  page). The swap is deterministic (no new LLM call), only fires when the JD uses the alias
  and not the canonical (a direct hit keeps your spelling), respects the one-line width cap,
  and is anchored exactly like concepts. Toggle with `RESUME_TAILOR_TECH_ALIASES` (default on);
  "Check setup" warns on an unanchored canonical in either map.
- The scorer's `resume.md` now guarantees the concepts/methodologies pool survives generation.
  `resume.md` (what every posting is scored against) is produced from the master by a model
  call whose prompt asked for one line per skills pool, but nothing enforced the
  `concepts_and_methodologies` line, so a dropped line meant a posting screening for a concept
  the candidate owns could be under-scored. The prompt now names the line explicitly, and a
  deterministic zero-cost pass appends, verbatim and dedup-aware, any pool concept the model
  dropped. Nothing is invented; if the pool is absent or the master unparsable it is a no-op.
- Tiered, rank-based project bullet allotment. Until now every project on the tailored résumé
  got a flat bullet count (`PROJECT_BULLETS_MAX`, default 2) unless individually named in the
  `project_layout` map — spending the same space on the headline, most-JD-relevant project as on
  the weakest. A new optional `project_bullet_tiers` config (a list of `{projects, bullets}` tier
  objects, e.g. `[{projects: 2, bullets: 3}, {projects: 2, bullets: 2}, {projects: 1, bullets: 1}]`)
  sizes projects by **strength rank**: `select()` already orders projects strongest-first for the
  job, so the top tier earns more bullets and weaker ones fewer, with projects past the last tier
  falling back to the global default. Unlike the existing name-keyed `project_layout` (static),
  tiers follow whichever project ranks strongest for *this* job. Tiered projects pad UP from their
  own unused atoms (best-effort, bounded by what the project actually has — nothing is invented), so
  a strong-but-thin project simply stays at its atom count; one-page enforcement, which already drops
  weakest-first, then claws bullets back only from the weak end, reinforcing the emphasis. Precedence:
  an explicit name-keyed `project_layout` entry still wins, then tiers, then the global
  `PROJECT_BULLETS_MAX`. Opt-in and gated by the existing `resume_layout_enabled` master toggle (part
  of the same A/B test). Editable from the dashboard — Resume Data tab > "Projects on the résumé" >
  the "Bullets by strength" box, where tiers are typed as `projects:bullets` pairs ("2:3, 2:2, 1:1");
  leaving it blank keeps the flat allotment.

### Changed
- The Methods concepts line now pads to a ~7-item target (was ~6), so one more genuinely
  earned concept buzzword reaches the page. Still width-capped to one printed line and still
  drawn only from concepts the user declared (`RESUME_TAILOR_SKILL_TARGETS` overrides).
- The underfull-bullet fill rescue now only grows a bullet whose printed line is below 50%
  full, instead of anything under the 90%/75% rephrase aim. The rephrase still aims for a
  well-filled line; the rescue pass no longer pads lines that are already mostly full, so a
  little white space is left alone.

### Fixed
Hardening pass from a full-code audit (24 findings; failure paths and guards only, no
happy-path behavior change):
- An existing-but-unreadable cumulative master CSV now ABORTS the run (scraper, scorer, and
  dashboard append/dedup paths) instead of being silently treated as empty, which could
  truncate the cumulative master to the latest batch. Every master/state write is now atomic
  (write to a temp file, then `os.replace`), covering the scraper and scorer masters, the
  run-state JSON, key-usage state, and the dashboard CSV paths, so a crash mid-write can no
  longer leave a half-written file.
- The key pool now applies conservative default rate limits (`5 rpm / 100 rpd`) to a model it
  does not recognize instead of retrying unthrottled, and every Gemini client is built with an
  HTTP timeout (`SCORE_HTTP_TIMEOUT_S`, default 120s) so a hung request cannot stall a run.
  Corrupt key-usage state values no longer crash loading.
- The four technical-skills lines are anchored to the user's declared pools: a token the model
  invents is dropped and replaced from the real pool (merged tokens like
  "Gemini/OpenAI/Claude API" are checked member-by-member), closing the last path by which a
  skill not owned by the user could print.
- High Score filtering no longer crashes on a master without a `deep_score` column and no
  longer returns silently empty on a non-default index; a blank or missing `is_seen` value is
  normalized to "no" so newly scraped jobs are never invisible to the High Score tab or the
  watcher popup.
- A corrupt `last_run_job_ids.json` degrades to an empty list with a warning instead of
  crashing the scrape; `pdflatex` runs under a 180s timeout with a clear message (a first-run
  MiKTeX package prompt can no longer hang tailoring forever); the master YAML is
  shape-validated at load with a clear error; editing the master clears the alias caches so a
  Check-setup run never reads stale aliases; the file watcher knows all four run labels
  (afternoon/night runs were invisible to it) and honors the `stale_after_hours` setting; a
  second dashboard launch exits silently instead of popping a modal over the live instance;
  the missing-`resume.md` path exits with an actionable message; repository links in the
  résumé normalize full URLs; and the single-instance lock is one shared class
  (`local/locks.py`) instead of two copies.

## [1.3.0] - 2026-06-29

### Added
- Anchored `skill_aliases` layer + a rendered "Methods" concepts line, so the résumé
  surfaces the concept buzzwords an ATS screens for ("data analysis", "ETL", "A/B testing",
  "data wrangling", "stakeholder management") that the candidate genuinely demonstrates but
  the résumé might never spell. Two root causes are fixed: the ATS matcher was literal (a JD
  synonym of an owned concept read as a false MISSING), and the `concepts_and_methodologies`
  pool was rendered nowhere (so those terms could never match the page). A new optional
  top-level `skill_aliases:` map (canonical -> [JD spellings]) is **anchored** — a group is
  used only when its canonical is a real skill in the taxonomy, so an alias can never inject
  an untethered keyword. It is wired into the ATS report + gap-finder (a JD synonym of an
  owned concept now counts as covered and is no longer proposed as a gap) and into a new
  fifth technical-skills line built in two tiers: Tier 1 prints, in the JD's own spelling,
  each pool concept the JD references (deterministic, ranked by JD frequency); Tier 2 pads
  to a ~6-item target from the model's role-relevance concept ranking (folded into the
  existing selection pass — **no new LLM call**). Bullets are never touched and nothing is
  invented — the line draws only from concepts the user declared. Coverage stays honest (a
  buzzword counts covered only once it is literally on the page). Toggle with
  `RESUME_TAILOR_METHODS_LINE` (default on); "Check setup" warns on an unanchored alias.
- Project bullets now lead with the project's overview. `select()` orders a project's bullets
  purely by job-relevance, which could bury the "what is this project" bullet behind detail
  bullets (e.g. a project led with its LLM-routing and Docker-sandbox bullets and only said what
  it actually was on bullet 3). A new pass floats each project's overview/intro bullet to the
  front so a reader learns what the project is before the implementation detail. A cheap model
  call picks the lead from the project's own selected bullets — pure reordering, never inventing —
  with a deterministic file-order fallback (the master authors each project's overview atom first)
  so flow is always enforced even if the call fails. Projects only; verbatim and single-bullet
  projects are untouched. Toggle with `RESUME_TAILOR_LEAD_OVERVIEW` (default on).

### Fixed
- Résumé bullets no longer end on a dangling bare number. When the model spelled a trailing
  range as words ("took 1 to 2 weeks per cycle") and the deterministic width-trim cut the
  tail, the dangling-cleanup removed only the innermost connective ("to 2" -> "took 1") and
  stopped, leaving a meaningless "...took 1." The cleanup now recognizes a chopped trailing
  quantity and drops the whole incomplete clause back to a clean boundary, while still leaving
  unit-bearing metrics ("95%", "40,000+ users") intact.
- Skills lines now fill to their configured best-N count when a category contains a merged,
  comma-bearing token like "LLM APIs (Gemini, OpenAI, Claude)". That token was being split on
  its internal commas — both in the YAML flow list (so it parsed as three pool entries) and in
  the line splitter — so it counted as three items and a 10-target Developer Tools line stopped
  at 8 with space to spare. Tokenization is now parenthesis-aware (kept or dropped whole, never
  cut to an unclosed paren) and the master entry is quoted.
- Local "Find new jobs" runs no longer re-collect (and re-score) postings the VM already
  scraped. The scraper excludes already-collected job ids by reading its host master, but on
  a local machine that file is only a small stub of recent local runs — it had no knowledge of
  the cumulative master the VM owns on Google Drive, so a local run re-pulled (re-billing Bright
  Data) and re-scored (re-billing Gemini) jobs already collected. The dashboard now points the
  scraper at the synced Drive master via `LINKEDIN_EXTRA_MASTER`, which `load_exclude_ids()`
  unions on top of the local master and `external_exclude_ids.json`. It is set only on the scrape
  subprocess (not pushed back to the VM, whose own master already is the full set), so the
  VM's exclusion is unchanged. In one real run this would have skipped 74 of 198 duplicate
  collections.

## [1.2.0] - 2026-06-28

### Added
- Underfull-bullet fill: when a tailored bullet renders shorter than its configured line
  target and the page has room, the engine now folds one concrete detail from an unused atom
  in the SAME block into that bullet (re-phrasing the group) so it fills toward its target,
  instead of leaving the line half-empty. It is strictly grounded -- the extra detail can only
  come from a real atom in the same entry, and a bullet whose block has no spare atom is left
  exactly as-is, so it never fabricates. Runs as one extra flash call only when a bullet is
  actually underfull with spare material; one-page enforcement stays the backstop. Toggle with
  `RESUME_TAILOR_FILL_UNDERFULL` (default on).

## [1.1.2] - 2026-06-28

### Changed
- Résumé project headings now show the repository link inline next to the project name
  ("Project Name | Link", italicized) like the Work Experience header, instead of
  right-aligned across the line; the link label is "Link".

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

[Unreleased]: https://github.com/yib7/INployed/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/yib7/INployed/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/yib7/INployed/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/yib7/INployed/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/yib7/INployed/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/yib7/INployed/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/yib7/INployed/releases/tag/v1.0.0
