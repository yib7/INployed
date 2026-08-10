# Changelog

All notable changes to INployed are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims for
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.8.0] - 2026-08-09

A per-job chat, a plainer cover letter that ships its own source, two new toggles, a detail
card that opens the full job description beside the scoring, and a Settings tab you can
find things in. Then a release pass on top of that: the seven headless scripts move into
`pipeline/`, the dashboard's theme and narrow-window behaviour change how it looks, and a
batch of pipeline, config and grounding bugs is fixed. Existing saved configs keep working:
the AI-writing pass defaults off, the Apply browser-open toggle defaults on, and every
removed setting keeps its saved value working at its consumer, so nothing changes until you
change it. The one thing to update by hand is a shell script or crontab of your own that
calls `scraper.py` or `score_jobs.py` at the repo root.

### Added
- **Ask AI about this job:** a non-modal chat scoped to one posting, from the jobs
  right-click menu or the Apply panel. It answers from that job's `apply.md` and its job
  description (fenced as untrusted data, like the tailor prompts) and falls back to a bounded
  master-experience extract for a job you have not tailored yet. It answers only from that
  context and says so plainly when the sheet does not cover something. Every arm of the
  context is length-capped, because each turn re-sends the whole thing.
- **The cover letter ships its `.tex`.** The source is copied next to the PDF the way
  `resume.tex` already was, so fixing a word means editing and re-running `pdflatex` instead
  of regenerating the letter.
- **Optional avoid-AI-writing pass on the cover letter** (Settings → Resume, off by default).
  Extends the existing two-arm style gate (prompt rules plus a deterministic ban list) with
  a letter-relevant subset of Conor Bronsdon's MIT-licensed `avoid-ai-writing` skill, credited
  in `docs/CREDITS.md`. The grounding gate still runs last, so a restyled sentence that
  introduces an unsupported fact is still caught.
- **Toggle for whether Apply opens the posting** (Settings → Dashboard, on by default). Off
  keeps you in the dashboard; the posting URL is still on the apply sheet.
- **Search box on the Settings tab:** type a word and the whole tab filters to the rows that
  mention it, matching the setting's name, its explanation, its config key **and the
  chips on the row**, so `GEMINI_API_KEYS` finds the box for someone reading a `.env` or
  a GitHub issue, and `restart` finds every setting that needs one. Several
  words narrow rather than widen. Sections with no match fold away, sections with one open
  themselves, and clearing the box puts every section back exactly as you had it: an
  opening the search made for you is never saved as your layout.
- **"Show advanced settings" checkbox**, off by default, which folds 18 power-user rows
  (the ten per-stage model pickers, scorer concurrency and retry caps, VM plumbing) out of
  sight. The label counts what it is withholding for *your* configuration, so it does not
  promise rows a tick cannot deliver. Search ignores the fold, and an advanced row still turns
  up in results, tagged `(advanced)`, because hiding a setting is only defensible while it
  stays findable. Three knobs stay in plain sight on purpose: **Country code** (a non-US
  user must change it, and a mismatch mis-searches silently), **pdflatex path** (the fix for
  "no PDF came out", so hiding it behind a disclosure would be backwards), and **Max scored
  per run** (the only ceiling on an LLM bill).
- **Settings that only apply to your configuration now hide themselves.** The Gemini model
  pickers are absent while the tailor runs on Claude and vice versa, the Gemini API-key box
  appears only when the tailor bills by key, and so on. Values are never touched by this:
  switch provider, save three times, switch back, and the custom model id you typed is
  still there.
- **Unsaved-change markers:** a dot beside each edited field, "· 2 changed" on the section
  header (which stays on screen when the section is folded, which is the point), and a Save
  button that reads "Save 3 changes". A per-field **↺** appears on any row sitting off its
  default and puts that one row back. Credentials get no ↺: their default is blank, so the
  button would be offering to wipe a live key.
- **"restart" chip on the 16 settings a running dashboard cannot pick up**, and a line after
  Save naming the ones that Save changed. The dashboard reads `.env` once, at launch, so
  editing your API keys, model ids, output folder or `pdflatex` path used to save
  successfully and then quietly go on using the old value. Three of the sixteen said so in
  prose; now all of them say it the same way, in the same place.

### Changed
- **The cover-letter PDF lost its letterhead.** It now opens at the date in Times, matching
  the résumé's font, with no name banner, contact line, or company line. The résumé
  travelling with it already carries all three.
- **The cover letter's plain text moved into `apply.md`** as a `## Cover letter` section,
  replacing the separate `_Cover_Letter.txt`. That is what the auto-apply skill pastes into
  cover-letter boxes; a `.tex` would be useless there. Folders tailored before this change are
  migrated in place the next time their letter is regenerated.
- **The auto-apply skill now attaches the résumé and cover letter** instead of deferring every
  file to the human, when the artifact folder has been granted for the run. It never clicks a
  file control (that opens a native picker it cannot see), verifies the filename appears after
  each upload, and falls back to the previous "Attach before submitting" record when the grant
  is missing or an upload cannot be verified. Parking at review without submitting is
  unchanged, as are all five safety invariants; Workday still uses Apply Manually, since
  parsed autofill would write values that no longer trace to `apply.md`.
- **The detail card's description panel shows the whole posting.** It used to print a
  700-character slice of the `job_summary` column, and LinkedIn's summary is itself often a
  truncated stub, so you were reading a fragment of a fragment and had to open the posting
  to judge the job. It now takes the real description when there is one (formatted HTML
  first, then the plain field, with the summary left as the fallback) and does not truncate
  it. The markup is rendered as paragraphs and bullets, the panel scrolls instead of
  stretching the card, and the text can be selected and copied.
- **The description opens beside the scoring instead of under it.** Showing it splits the
  detail card into two columns (reasoning, strengths, and gaps on the left, the posting on
  the right) and grows the card to at least about half the window, because a full posting
  stacked under the scoring in a ~300px pane meant scrolling a column to read anything. Hiding it
  restores the height the card had; if you drag the divider above the card while the
  description is open, that drag wins and hiding leaves your size alone. The split itself is
  draggable too, and your split comes back when you re-open the description.
- **The description stays open as you click through jobs.** It used to re-collapse on every
  selection, which made comparing several postings a click-per-job chore. Now only the text
  swaps (scrolled back to the top). It folds back to a single column for a job with no
  description and when the selection is cleared, and re-opens on the next job that has one.
  The Tracker card is unchanged.
- **Whole numbers in Settings are spin boxes, and a bad value is flagged where it is.**
  Save no longer pops a modal listing every rejected field and pointing at none of them: the
  offending box outlines in red, a note appears under it, the form scrolls to the first one
  you can act on, and the status line counts them ("2 settings need fixing"). Fields are
  re-checked when you tab out of them, not only at Save. The one surviving modal is a Save
  that could not write the file. A spin box also **says so when it had to clamp a
  hand-edited value**: `max_scored_per_run: 99999` shows 5000 with a note naming the file
  and the real number, rather than silently rewriting your config on the next Save.
- **Settings snapshots are one dropdown instead of four knobs.** See *Removed*.
- **The seven headless scripts live in `pipeline/`.** `scraper.py`, `score_jobs.py`,
  `run_labels.py`, `keypool.py`, `claude_cli.py`, `merge_incoming.py` and `prune_master.py`
  moved out of the repo root, which drops to 10 files. Each one resolves its data root as
  "the repo root when I am inside `pipeline/`, otherwise my own directory", so `.env`, the
  master CSV and the run-label output folders land exactly where they did before, on a repo
  checkout and on the VM's flat home alike. The VM needs no coordinated change, and the
  dashboard, `scripts/setup.ps1` and the docs all name the new paths. **If a shell script or
  crontab of yours calls one of those scripts, add the `pipeline/` prefix.** In the same move
  `SECURITY.md` went to `.github/` (GitHub still surfaces it there), `requirements-vm.txt` to
  `scripts/` beside `run_scraper.sh`, and `pytest.ini` + `ruff.toml` merged into
  `pyproject.toml`.
- **The auto-apply inbox map no longer ships a school domain as a default row.** The map now
  seeds consumer providers only, and the help text tells you to add your own work or school
  domain. A map you have already saved is untouched.
- **Dependency pins refreshed:** pypdf 6.14.2 → 6.15.0 clears CVE-2026-71852 and
  CVE-2026-71870, two crafted-PDF resource-exhaustion bugs in the text-extraction path the
  résumé engine runs over your own PDF. google-genai 2.14.0 → 2.17.0 in both requirements
  files. ruff 0.15.17 → 0.16.2, with the lint selection now written down in `pyproject.toml`
  rather than inherited: ruff 0.16 widened its built-in default and took the same tree from 1
  finding to 1278 with no code change. numpy stays at 2.4.6 on the VM, which runs Python 3.11.

### Removed
- **`mtime_stable_seconds` is gone from the Settings tab.** It is the file-watcher's
  settle delay, in seconds, and the dashboard never read it. Three things follow, and all
  three are deliberate: **(a)** a saved value keeps working at whatever you set it to, because
  `local/watcher.py` starts from its own defaults and merges the file over them, so both an
  absent key and an existing one behave exactly as before; **(b)** the watcher's built-in
  default is 30, which is what a fresh install has always used; **(c)** restoring a settings
  snapshot taken before this change no longer replays the key. Snapshots copy whole *files*,
  but a restore replays only *schema* fields, so a snapshot holding `mtime_stable_seconds: 77`
  restores your other settings and leaves the live settle value alone, the same as every
  other non-schema key in `config.json` (`resume_layout`, `ui_scale_pct`, `removed_jobs`),
  none of which were ever restorable either.
- **`auto_apply_inbox_url` is gone from the Settings tab, but is still honoured.** It was the
  single-URL fallback that `auto_apply_inbox_map` replaced. `apply_queue.build_context()`
  reads `config.json` directly and still consults a saved value, so nobody's apply run
  changes; it simply has no editor any more. If you customised it, the map is now where you
  set an inbox per email domain.
- **A vestigial `apply` settings target:** no field targeted it and no `apply_config.json`
  exists at the repo root, so the only effect is that a legacy root-level `apply_config.json`
  is no longer copied into settings snapshots, matching `apply_answers.json`, the live
  answer store, which never was.
- **Age-based snapshot retention:** the four snapshot keys (`archive_enabled`,
  `archive_prune_mode`, `archive_prune_keep`, `archive_prune_days`) are replaced by one
  **Settings snapshots** dropdown: *Off* / *Keep everything* (the default) / *Keep newest 20*
  / *Keep newest 100*. An existing config is migrated on read under one rule, **never prune
  more aggressively than the old policy**, so a saved `Keep newest 10` reads as **Keep
  newest 20**, rounding *up*; a days-based policy becomes *Keep everything*. **Nothing on
  disk is deleted by this change**, and the four old keys are left in `config.json`
  untouched, so checking out an older commit restores the old behaviour intact. The accepted
  trade-off: a snapshot contains a copy of your `.env`, so rounding a keep-count up is a
  marginal increase in copies of your keys on disk. Rounding down would have deleted
  snapshots you still had, which the invariant above forbids.

### Fixed
- **LinkedIn's "Show more" / "Show less" no longer lands at the bottom of the description.**
  The HTML-to-text pass stripped tags but kept the text inside them, so the posting's own
  page chrome came through as prose on 2,675 of the 2,678 postings in the master file.
  Non-content elements (`script`, `style`, `button`, `icon`, `svg`, `nav`, `header`,
  `footer`, `noscript`, `form`, `select`) are now dropped together with their contents. The
  rule is structural (nothing is matched against the posting's words), so EEO statements,
  agency notices, and scam warnings still show.
- **Bullet lists in the description read as lists again.** Each `<li>` was ending up with a
  blank line after it, so a 20-bullet posting scrolled like an endless column; bullets in one
  list are now consecutive lines, with the blank line kept between a list and the prose
  around it. A bullet whose text was wrapped in a block element is reunited with its marker,
  empty markers are dropped, and the source HTML's indentation no longer leaks through as a
  gutter down the left of some lines.
- **A white 1px rule across the top of the window, on all 8 tabs.** The Fusion bevel palette
  roles were never set, so Qt drew the tab-bar base line in `#ffffff` at every window size and
  interface scale. The same pass removed the dark slab painted behind every Settings section
  title, put white-on-accent and white-on-green button text back over 4.5:1 (3.20:1 and
  2.54:1 before, on the two loudest buttons in the window), lifted the faint subtitle and
  empty-state text to 4.64:1, and named the three credential boxes so a screen reader
  announces them on focus instead of skipping the cell.
- **The window holds its shape at 1100px wide and at 150% scale.** The action-bar hint was
  sliced to a bare "Ct" and the auto-apply counts caption cut inside a word: both elide
  properly now and keep the full string in the tooltip. The auto-apply status chips no longer
  clip at 1280px. Table column widths ride the interface scale, so at 150% they stop opening a
  third too narrow and "Don't consider" is no longer elided to "Don't consi..." on the one
  kind of row that states its meaning in words rather than in colour. The Title column has a
  floor: below it the table scrolls instead of collapsing Title to an ellipsis.
- **A job title containing markup is rendered as text.** The detail card and the apply-queue
  panel handed the raw CSV string to a `QLabel` on its AutoText default, so a posting titled
  `Senior <b>Engineer</b>` would have been drawn as markup. Those labels are PlainText now.
- **A broken `master_experience.yaml` says which line, not which traceback.** The file is
  git-ignored personal data you are told to hand-edit, so a bad edit is the likeliest first
  failure in the whole résumé engine, and it used to surface as a raw `yaml.ParserError`. A
  fresh clone with no master at all surfaced as a raw `FileNotFoundError`. Both are one error
  naming the file, the line, the column and the fix, which the Resume Data tab shows as a
  message instead of crashing on.
- **Retention had silently stopped pruning.** `prune_master` read two columns in a way that
  returns a bare `None` for an absent column, so a master written before the scorer had ever
  run raised, and `run_scraper.sh` swallowed the exit code. It also emitted a traceback where
  one line naming the exception was enough.
- **A parse failure no longer sleeps 17 minutes.** The retry logic decided "rate limited" by
  substring-matching the exception text, and that text can carry up to 500 characters of model
  output, so a job description about sales quotas turned a deterministic parse failure into
  30+60+120+240+300+300 seconds of backoff per call. Both provider lanes now classify on a
  structural error kind before any message is read.
- **A settings save and a job delete no longer revert each other.** Three writers to
  `local/config.json` (the dashboard's background queue, the Settings tab on the UI thread,
  and the watcher in another process entirely) did a lock-free read-modify-write, so whichever
  read first lost its keys: a deleted job reappeared, or a page of settings did. They share a
  file lock now, and the settings writer no longer strands a `config.json.<pid>.tmp` behind a
  failed write.
- **Four scoring and grounding defects:** the grounding gate gave an abbreviation a free slot
  ("Built ingestion for the U.S. MIT lab" split mid-sentence and handed MIT the slot reserved
  for the generated verb). The tracker's status timestamps were naive local wall-clock, which
  a cross-machine tie-break compares as text, so a 09:00 change in one zone beat a later 10:00
  change in another and DST inverted an hour a year; they are UTC now. The scraper's
  `limit_per_input` reached the billed Bright Data trigger URL uncoerced and unquoted, so a
  config holding `100&limit=5000` rewrote the request. And merged incoming rows were read with
  inferred dtypes and appended to a string master, writing a score of 5 back as `5.0`.
- **Text at the subprocess boundaries is read as UTF-8.** Two shipped captures decoded with
  the OS default (cp1252 on Windows): the `pdflatex` log echo, which quotes your résumé back
  at you, so an accented company name came out mangled or raised on a stricter locale, and
  `gcloud`'s error bodies in `vm_sync`. Two user-facing notices in `local/chrome.py` were
  `print()`ed from a module the windowless dashboard imports, where stdout goes nowhere; they
  are log warnings now.

### Docs
- **README leads with the architecture.** The engineering claim is near the top with the
  measured funnel behind it, a Screenshots grid of four distinct screens (triage, tracker,
  résumé data, settings) sits after the intro, and the demo GIF moved under "What it does".
  Every screenshot and the GIF were re-shot against the current UI from synthetic fixtures:
  13 scenes, 25 seconds, with row selections and a live search filter.
- **Setup Step 2 calls `venv\Scripts\python.exe` by path** instead of `.\venv\Scripts\activate`,
  which the default PowerShell 5.1 execution policy on a clean Windows box refuses. The
  readme-setup CI job mirrors the new wording. Git is named in "You need", and Step 7 says what
  actually breaks without MiKTeX and gcloud.
- **License disclosure:** README and `docs/CREDITS.md` now state that every pin is MIT, BSD,
  Apache-2.0 or PSF except PySide6/Qt, which is LGPLv3 or GPL or a commercial Qt license, and
  why a source-only distribution that `pip install`s Qt satisfies the LGPL relink condition.

## [1.7.1] - 2026-07-28

Maintainer tooling for the project's own media. No runtime or pipeline change.

### Added
- `scripts/build_social_preview.py` composes `docs/social-preview.png` at GitHub's 1280x640
  social shape. Uploading the hero screenshot as-is letterboxes it, and its 15 px interface text
  is unreadable once a chat client shrinks the link unfurl, so the card sets the wordmark and the
  one-line description over a blurred crop of the screenshot instead.
- `scripts/build_walkthrough.py` drives the real dashboard offscreen through a 13-scene tour and
  encodes a captioned MP4. Like the screenshots, it runs against synthetic fixtures, so it costs
  nothing and shows no real data. The video is not committed: an inline player works only from
  GitHub's user-content CDN. Encoding needs `imageio-ffmpeg`, which stays out of
  `requirements.txt`.

## [1.7.0] - 2026-07-27

Three cycles of work in one release: an Easy Apply filter that stops the scorer spending on
postings you cannot apply to from here, a deterministic grounding gate for generated résumé
text, a 34-finding code-audit remediation, and a security and performance pass. Existing saved
configs keep working; the one new option (`drop_easy_apply`) defaults to off.

### Added
- **Drop Easy Apply before scoring.** A Settings toggle skips LinkedIn Easy Apply postings
  before Stage 1, so no API credits go to jobs the apply flow cannot open on an external board.
  Skips are counted in `run_stats` and named in the scraper log.
- **Grounding backstop for generated text** (`local/resume_tailor/verify.py`). Every résumé bullet
  and every cover-letter claim is checked back against an atom in `master_experience.yaml`
  after the model returns, deterministically and without a second API call. The job description
  is fenced as untrusted data in the prompts that read it.
- **Push config to the VM from Settings.** Changing a setting the scraper VM reads now offers
  to push the new config, instead of leaving the VM silently running the old one.

### Fixed
- **Security.** The prep-sheet prompt fenced its job description like the others; the Claude CLI
  child process no longer inherits other providers' API keys; two paths that could reach the
  résumé writer around the grounding gate are closed; `manual_add.fetch_url_text` is
  SSRF-hardened (no redirects to private address space).
- **A 1.8-3.5 s dashboard freeze** when sorting a large job table: sorting happens in pandas
  now rather than in the Qt proxy model.
- **Whole-master reads are streamed in bounded chunks** everywhere they were not already (the
  watcher probe, reconcile, outbox row lookup, and append/drop), so a ~35 MB master no longer
  loads end-to-end for a single row.
- Scoring robustness: separate retry budgets per key in the pool, debounced and merging state
  saves, a wider input-error catch, correct Stage-2 error counting, byte-stable master dtypes,
  and a quoted dataset id.
- Local master writes are serialized, and a scrape and a manual add can no longer run at once.
- `master_experience.yaml` writes are atomic; the résumé CLI's `--apply` is gated.
- A shared retrying atomic `os.replace` now backs every CSV, `apply.md`, and outbox writer.
- VM schedule pushes preserve crontab lines the project does not manage, and biweekly runs use
  epoch-week parity so local and VM agree on which week it is.
- `scripts/setup.ps1` survives being launched with `powershell -File`, which is how the README
  tells you to run it; on a fresh clone it previously failed.
- Table headers align with their cells, the Title column stretches, and form inputs are
  labelled.

### Changed
- Dependency pins refreshed to current stable; `google-genai` unified on the 2.x line that the
  VM has run in production since June; model ids refreshed against the current catalogs.
- `tools/` retired into the packages that own its code, the app icon is wired up, and stale
  `.gitignore` entries are gone.
- The test suite is hermetic: it no longer reads the real `.env`, and `os.environ` is restored
  per test.

### Docs
- README restructured: a seven-step Quick start, a Limitations section, regenerated screenshot
  and demo GIF, and the platform claim narrowed to what is actually tested (Windows for the
  dashboard, Linux for the pipeline scripts).
- The long feature manual moved to `docs/USER_GUIDE.md`.
- `SECURITY.md` now lists every outbound destination and what it receives.

## [1.6.2] - 2026-07-16

A portfolio ship-checklist pass: first-run experience, accessibility, and setup-accuracy
fixes. No pipeline, scoring, or résumé-engine behavior changes for an existing saved config.

### Fixed
- First launch with no data now opens the get-started dashboard instead of an empty table,
  and the `.cmd` launcher finds a project-local venv before falling back to the system Python.
- The dashboard's `master_row` lookup streams the master CSV in bounded chunks and stops at
  the first id hit, instead of reading the whole file on the UI thread.
- The scale bar's `-`/`+` buttons rendered as blank squares (default button padding consumed
  the entire fixed width); a compact stylesheet tier restores the glyphs.
- Settings and Apply Answers form inputs now carry explicit accessible names, so screen
  readers announce them (no visual change).
- `apply_driver` reads/writes its `seq.txt` counter with explicit UTF-8 encoding.
- Explicit exception chaining (`from exc` / `from None`) at the seven wrap-and-raise sites,
  so logs keep the root cause and user-facing errors stay clean.
- Removed the dead `vm_sync.changed_vm_files` helper and its tests.

### Changed
- Send2Trash 1.8.3 → 2.1.0; `requests` and the optional extras are now declared in
  `requirements.txt`; the Gemini pro-preview model pin is documented next to the setting.
- Demo GIF and dashboard screenshot regenerated to the current Settings layout.

### Docs
- README setup accuracy: gcloud marked optional, PowerShell execution-policy note, the
  Google Drive desktop-app requirement stated on the hands-off path, and Settings copy
  matched to the current layout (masked credentials, dataset ID under Connection & paths).

## [1.6.1] - 2026-07-15

A settings-audit and hardening pass, plus a scoring-doc correction. No pipeline, scoring, or
résumé-engine behavior changes for an existing saved config.

### Fixed
- `auto_apply_inbox_map`'s built-in default was missing three webmail-domain rows
  (`googlemail.com`, `live.com`, `msn.com`) that `apply_queue.DEFAULT_INBOX_MAP` already
  carried; the two are now pinned in sync by a test.
- Dropped the dead `tailor_cover_letter` setting (no code path ever read it — every tailor
  call site prompts live instead) and relocated `tailor_open_folder` to the Resume section
  and `BRIGHT_DATA_DATASET_ID` to Connection & paths, where they actually belong.
- Removed the dead slider-warning UI machinery (`warn_above`/`warn_text`) and an unreachable
  `float` Field-type branch from the settings schema and form.
- Corrected several stale Settings-tab help strings and module docstrings (Engine section,
  Resume tagline, `GOOGLE_CLOUD_LOCATION` region-fallback drift, provider push-to-VM
  behavior) to match current behavior.

### Docs
- `score_jobs.py`: clarified that the VM's Gemini-only fallback fires because `claude_cli.py`
  isn't shipped there, not because the pushed `provider` setting is ignored.

## [1.6.0] - 2026-07-12

A visual release: the dashboard gets a full token-driven restyle. No pipeline, scoring, or
résumé-engine behavior changes; the `.env` keys and config schema are untouched.

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

### Docs
- README screenshot and demo GIF regenerated for the restyled UI, and the feature
  descriptions (job detail card, status chips, identity strip) updated to match.

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

[1.8.0]: https://github.com/yib7/INployed/compare/v1.7.1...v1.8.0
[1.7.1]: https://github.com/yib7/INployed/compare/v1.7.0...v1.7.1
[1.7.0]: https://github.com/yib7/INployed/compare/v1.6.2...v1.7.0
[1.6.2]: https://github.com/yib7/INployed/compare/v1.6.1...v1.6.2
[1.6.1]: https://github.com/yib7/INployed/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/yib7/INployed/compare/v1.5.1...v1.6.0
[1.5.1]: https://github.com/yib7/INployed/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/yib7/INployed/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/yib7/INployed/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/yib7/INployed/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/yib7/INployed/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/yib7/INployed/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/yib7/INployed/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/yib7/INployed/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/yib7/INployed/releases/tag/v1.0.0
