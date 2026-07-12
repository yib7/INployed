# Changelog

All notable changes to INployed are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims for
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Dashboard restyle: a token-driven dark theme (`local/qt/theme.py`) with named
  surfaces/borders/semantic colors, a type-role scale (multipliers of the live base size,
  so the Interface size slider keeps working), and a custom table delegate that paints
  category row tints, selection lines, score badges, deep-score mini-bars, status pills,
  and "Open ↗" links. New identity strip (job/unseen/tracked counts + freshness), job
  detail card with strengths/gaps columns and a collapsed description toggle, tracker
  pipeline chips, a restyled auto-apply panel, and card-style Settings sections with
  secrets masked by default.
- UI copy pass: vendor-neutral wording throughout (job "discovery" instead of
  scraper/vendor names — settings labels, help text, and dialogs; the underlying `.env`
  keys and config schema are unchanged), "Found"/"Link" column headers (were
  "Scraped"/"URL"), clearer High Score legend labels including a neutral "Don't consider"
  swatch, and a shorter search placeholder.

### Removed
- The old plain-text `ScorePreview` pane (replaced by the job detail card).

## [1.5.1] - 2026-07-12

A hardening release. No new features, just bug fixes and safety guards from a code audit.
The main one wires the VM's retention prune into the cron run so the master's
stored descriptions stop growing without bound. The VM-side fixes take effect only after
redeploying the scripts (and `prune_master.py`) to the VM.

### Changed
- The scraper caps its "already collected, don't re-fetch" exclude-id set to a recency
  window (`EXCLUDE_WINDOW_DAYS`, default 90). The search only looks back 24 hours, so an
  older id is dead weight in the trigger payload; capping the set keeps the Bright Data
  trigger request from eventually overflowing its size limit. Windowing fails toward a
  superset (undated rows are kept) and degrades to keep-all on any error, so it never
  drops an id it should have excluded.
- Both scorer system prompts (Stage 1 and Stage 2) now state that the job description is
  untrusted data and that any instructions inside it are to be ignored, so a posting can't
  steer the model.
- `resume_tailor.run --csv` no longer defaults to a hardcoded drive path. It resolves the
  master from your synced Drive folder or the repo root, and asks for an explicit `--csv`
  with a clear message when it can't find one, so the CLI isn't tied to one machine.
- Smaller cleanups: the company blocklist is read once per master rewrite instead of once
  per chunk; a dead branch was removed from the key-pool selector; unused engine-label
  maps were deleted; and the apply-flow test fixtures use a synthetic identity, so no
  personal data ships in the public tree.

### Fixed
- The VM retention prune is now actually run. `prune_master.py` was written and tested but
  nothing ever invoked it, so the master's stored HTML descriptions grew without bound.
  `run_scraper.sh` now runs it after scoring, best-effort so a prune problem never fails
  the run.
- `update_master_scores` raises a clear, actionable error when the existing master is
  unreadable, matching the scraper and merge paths, instead of a raw pandas error thrown
  after the scored file was already written. It also reads the `1.0` and trailing-space
  spellings of `filtered_out` as filtered, so those rows stop being re-scored on every run.
- The key pool rolls its per-day usage counters over at Pacific midnight. A run that
  crossed midnight kept counting against the previous day, so it under-used the free-tier
  quota and spilled to paid Vertex; it now reloads the day's state and attributes usage to
  the right date.
- The apply queue orders blank-`queued_at` entries fairly. A hand-edited or pre-schema
  entry now sorts last instead of jumping ahead of genuinely older queued jobs.
- The watcher validates the shape of `state.json` before using it, so a bad hand-edit
  can't brick scheduled runs, and it writes one config key at a time to avoid a
  lost-update race with the dashboard.
- Every mid-run pipeline write goes through a temp file and an atomic replace, so a crash
  partway through can't leave a half-written master or state file. The incoming-merge step
  also no longer aborts the day's run when it can't delete an already-merged incoming file.

## [1.5.0] - 2026-07-12

The biggest release since 1.0: an optional **Claude subscription backend** for résumé
tailoring and local scoring, a **batch auto-apply queue** subsystem, a **unified master**
so local scrapes feed the cloud pipeline, **bounded-memory** VM master I/O with retention,
and a large dashboard + cover-letter pass. All prior work since 1.4.0 is folded in here.

### Added
- **Claude subscription backend (optional).** The résumé tailor and the *local* job scorer
  can each run on your Claude Code CLI subscription instead of Gemini, selected by Settings
  provider dropdowns (`tailor_provider` / `provider`, both default `gemini`). It drives the
  headless CLI with subscription auth (no API key) through a new stdlib-only `claude_cli.py`
  transport with a KeyPool-shaped `ClaudePool`, prompt caching (stable content on the cache
  breakpoint, per-item data on stdin) and a per-(model, system) warm-up gate so a batch pays
  the cache write once. Tier map: fast → `claude-haiku-4-5`, standard → `claude-sonnet-5`,
  deep → `claude-opus-4-8`. The cloud VM always scores with Gemini and falls back safely if a
  Claude config ever reaches it.
- **Auto-apply batch queue.** A new **Auto-apply** tab mirrors a batch apply queue
  (`Queue auto-apply` adds tailored jobs; the tab tracks queued / in progress / ready to
  submit / needs human), backed by a lock-guarded, atomic queue store and an ATS-accounts
  ledger that keeps passwords in the OS credential manager only. Draining runs the same
  parks-at-review, never-auto-submits flow one job at a time (advanced/optional path).
- **Unified master.** Local scrapes and hand-added jobs now feed the cloud master through a
  durable local outbox that pushes to the VM's `incoming/`, which `merge_incoming.py` drains
  into the master (master-wins, chunked) — so a job discovered locally is never stranded.
- **VM master retention + bounded memory.** `run_scraper.sh` merges `incoming/` before
  scraping; master append / rescore / merge are chunked for bounded memory, and
  `prune_master.py` keeps a rolling 3-day description-retention window.
- **healthchecks.io dead-man's switch** for the VM scraper so a missed cron run is noticed.
- **Cover letters** got a right-click "Generate cover letter" on an already-tailored job, a
  reworked left-aligned header, a copy-pasteable `.txt` export, a second cohesion pass, and
  graduation/tense-aware context.
- Dashboard: three-state Easy Apply filter, "Add job by hand", and a local watcher task that
  can auto-sync to the VM schedule.

### Changed
- Delete / mark-seen / set-status are now **optimistic**: the in-memory view updates instantly
  and the ~27 MB gzipped CSVs are rewritten on a single-flight background queue, so the UI no
  longer freezes on those actions.
- **Delete** moves a job's `Generated_Resumes` folder to the Recycle Bin and clears its
  registry row.
- `apply.md` is now pure data (full `https://` contact links, no project dates, an Awards
  sub-bullet) with the form-filler playbook moved out of the sheet.
- Résumé-tailor and scorer prompts render byte-identically on the Gemini path; the Claude path
  splits the scorer prompt at the résumé/job boundary for cache reuse.

### Fixed
- `apply_playwright.run()` now writes a terminal `report.json` on **every** exit — a
  fill/upload-phase crash records `failed:`, and a post-submit crash records
  `submitted (unconfirmed)` — so a crashed run can't leave the queue stuck or cause a
  double-apply.
- `seen.db` self-heals a malformed database (quarantine-and-recreate + atomic `app_status`
  backup/restore); the pytest suite is fully hermetic (redirected app-data, no real DB/logs).
- Local "Find new jobs" survives the dashboard closing, streams live progress, and recovers
  orphaned tailor/scrape runs at launch.
- `make_pool` tolerates a garbage timeout env value; Check setup honors the same env>file
  provider precedence the runtime uses.
- Observability + robustness batches from the code audit: logged swallowed LLM failures,
  retry jitter, guarded reads, and closed local→VM sync gaps.

### Docs
- README documents the Claude backend and the Auto-apply tab, clarifies which prerequisites
  are optional (only Python 3.14 is needed to open the dashboard), and ships a refreshed,
  higher-frame demo GIF and screenshot. Removed internal planning/spike docs from the tree.
- Dependency version-health audit (all pins on stable GA releases; model ids current).

## [1.4.0] - 2026-07-02

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
  `project_layout` map - spending the same space on the headline, most-JD-relevant project as on
  the weakest. A new optional `project_bullet_tiers` config (a list of `{projects, bullets}` tier
  objects, e.g. `[{projects: 2, bullets: 3}, {projects: 2, bullets: 2}, {projects: 1, bullets: 1}]`)
  sizes projects by **strength rank**: `select()` already orders projects strongest-first for the
  job, so the top tier earns more bullets and weaker ones fewer, with projects past the last tier
  falling back to the global default. Unlike the existing name-keyed `project_layout` (static),
  tiers follow whichever project ranks strongest for *this* job. Tiered projects pad UP from their
  own unused atoms (best-effort, bounded by what the project actually has - nothing is invented), so
  a strong-but-thin project simply stays at its atom count; one-page enforcement, which already drops
  weakest-first, then claws bullets back only from the weak end, reinforcing the emphasis. Precedence:
  an explicit name-keyed `project_layout` entry still wins, then tiers, then the global
  `PROJECT_BULLETS_MAX`. Opt-in and gated by the existing `resume_layout_enabled` master toggle (part
  of the same A/B test). Editable from the dashboard - Resume Data tab > "Projects on the résumé" >
  the "Bullets by strength" box, where tiers are typed as `projects:bullets` pairs ("2:3, 2:2, 1:1");
  leaving it blank keeps the flat allotment.

### Changed
- Dependency refresh (2026-07-01): markdownify 1.2.2 -> 1.2.3 (both requirement sets),
  CI `actions/checkout` v5 -> v7. google-genai stays 1.x locally on purpose (2.x is a
  major bump on the live LLM path); the VM's numpy stays 2.4.x (numpy 2.5+ needs
  Python >=3.12, the VM runs 3.11).
- `docs/ARCHITECTURE.md` caught up with the tree: the watcher + shared `local/locks.py`
  single-instance lock, the anchored alias maps and Methods concepts line, the
  `resume.md` concepts-pool guarantee, and module-table rows for `measure.py` /
  `master_edit.py` / `master_validate.py` / `apply_answers.py`.
- The Methods concepts line now pads to a ~7-item target (was ~6), so one more genuinely
  earned concept buzzword reaches the page. Still width-capped to one printed line and still
  drawn only from concepts the user declared (`RESUME_TAILOR_SKILL_TARGETS` overrides).
- The underfull-bullet fill rescue now only grows a bullet whose printed line is below 50%
  full, instead of anything under the 90%/75% rephrase aim. The rephrase still aims for a
  well-filled line; the rescue pass no longer pads lines that are already mostly full, so a
  little white space is left alone.

### Fixed
- The CI badge is trustworthy again. The workflow's test step ran pytest and the
  dashboard smoke test in one multi-line PowerShell block, where only the last
  command's exit code decides the step result - so a pytest failure followed by a
  passing smoke test reported green. This actually happened: a non-hermetic test
  (`test_check_setup_reports_ok` silently depended on the developer's `.env`
  supplying a Google Cloud project id) hung on CI's bare checkout, was killed by
  the test timeout, and CI still passed - never running the test files after it.
  The test now pins its inputs and stubs the error dialog, pytest and the smoke
  test are separate CI steps, and the per-test timeout lives in `pytest.ini` so a
  hung test fails fast in every environment, not only where the flag is passed.
- `master_experience.example.yaml` now buckets its skills under the keys the tailor
  actually renders (`languages` / `frameworks` / `developer_tools` / `libraries`, plus
  `concepts_and_methodologies` for the Methods line). The example previously used
  free-form keys (`ml_ai`, `data`, `cloud_devops`, `tools`), so on a fresh clone three
  of the four skill pools were empty: a first-time user's tailored résumé printed only
  the Languages line, and the fresh-clone test suite failed
  `test_compress_skills_returns_four_labeled_lines` (the same 20 example skills are
  kept, only regrouped). The taxonomy comment in the example now states the bucket
  contract instead of "group however reads best".
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
  top-level `skill_aliases:` map (canonical -> [JD spellings]) is **anchored** - a group is
  used only when its canonical is a real skill in the taxonomy, so an alias can never inject
  an untethered keyword. It is wired into the ATS report + gap-finder (a JD synonym of an
  owned concept now counts as covered and is no longer proposed as a gap) and into a new
  fifth technical-skills line built in two tiers: Tier 1 prints, in the JD's own spelling,
  each pool concept the JD references (deterministic, ranked by JD frequency); Tier 2 pads
  to a ~6-item target from the model's role-relevance concept ranking (folded into the
  existing selection pass - **no new LLM call**). Bullets are never touched and nothing is
  invented - the line draws only from concepts the user declared. Coverage stays honest (a
  buzzword counts covered only once it is literally on the page). Toggle with
  `RESUME_TAILOR_METHODS_LINE` (default on); "Check setup" warns on an unanchored alias.
- Project bullets now lead with the project's overview. `select()` orders a project's bullets
  purely by job-relevance, which could bury the "what is this project" bullet behind detail
  bullets (e.g. a project led with its LLM-routing and Docker-sandbox bullets and only said what
  it actually was on bullet 3). A new pass floats each project's overview/intro bullet to the
  front so a reader learns what the project is before the implementation detail. A cheap model
  call picks the lead from the project's own selected bullets (pure reordering, never inventing),
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
  its internal commas - both in the YAML flow list (so it parsed as three pool entries) and in
  the line splitter - so it counted as three items and a 10-target Developer Tools line stopped
  at 8 with space to spare. Tokenization is now parenthesis-aware (kept or dropped whole, never
  cut to an unclosed paren) and the master entry is quoted.
- Local "Find new jobs" runs no longer re-collect (and re-score) postings the VM already
  scraped. The scraper excludes already-collected job ids by reading its host master, but on
  a local machine that file is only a small stub of recent local runs - it had no knowledge of
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

[Unreleased]: https://github.com/yib7/INployed/compare/v1.5.1...HEAD
[1.5.1]: https://github.com/yib7/INployed/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/yib7/INployed/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/yib7/INployed/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/yib7/INployed/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/yib7/INployed/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/yib7/INployed/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/yib7/INployed/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/yib7/INployed/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/yib7/INployed/releases/tag/v1.0.0
