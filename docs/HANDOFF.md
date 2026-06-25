# INployed (LinkedIn Job Scraper + Scorer) — Handoff Doc

_Last updated: 2026-06-12_

This is the operator's guide for the end-to-end pipeline: a GCP VM scrapes LinkedIn
jobs (Bright Data), scores them against a resume (Gemini via Vertex AI), uploads
results to Google Drive, and a Windows watcher pops a dashboard of the best unseen
matches.

> ## ⚠️ About credentials in this doc
> This file intentionally contains **NO private keys, API tokens, or passwords**.
> Putting secrets in a handoff doc is a security risk (docs get shared, emailed,
> synced to Drive, committed to git — one leak = full compromise).
>
> You don't need any pasted secrets to get in: **`gcloud compute ssh` generates and
> manages the SSH key for you** and pushes the public half to the VM. The login flow
> below is all you need. Where each real secret lives (so you can retrieve it
> yourself, securely) is listed in **§7 Secrets**.

---

## 1. System at a glance

```
                ┌─────────────────────── GCP VM: scraper-vm ───────────────────────┐
  cron 10:00 ET │  run_scraper.sh  →  scraper.py  →  score_jobs.py  →  rclone upload │
  cron 19:00 ET │  (Bright Data)      (Gemini/Vertex)      (to Google Drive)         │
                └───────────────────────────────┬──────────────────────────────────┘
                                                 │  gdrive:LinkedInJobs/
                                                 ▼
                                  Google Drive  (morning/ evening/ linkedin_jobs_master.csv.gz)
                                                 │  Drive desktop client syncs to  E:\My Drive\LinkedInJobs
                                                 ▼
                ┌───────────────────────── Windows PC ─────────────────────────────┐
                │  Task "LinkedInJobsWatcher"  →  watcher.py  →  app.py (dashboard)  │
                │  (logon / unlock / wake / 6×daily)   reconciles is_seen, pops UI   │
                └───────────────────────────────────────────────────────────────────┘
```

| Thing | Value |
|---|---|
| GCP project | `YOUR_GCP_PROJECT` (linked to $300 trial billing) |
| VM instance | `scraper-vm`, machine type **e2-micro** (free tier) |
| Zone | **`us-east1-c`** (NOT us-east1-b) |
| Linux user | **`clouduser`** ← always use this (the real account: venv, rclone, crontab, all data live in `/home/clouduser/`). Empty orphan accounts gcloud auto-creates when you SSH as the wrong user must never be used. |
| Google account | `you@example.com` |
| VM timezone | `America/New_York` |
| Scrape source | Bright Data LinkedIn dataset (`YOUR_DATASET_ID`) |
| Scoring | Vertex AI Gemini (`gemini-2.5-flash-lite` stage 1 + `gemini-2.5-flash` stage 2; env-overridable via `SCORE_STAGE1_MODEL` / `SCORE_STAGE2_MODEL`) |
| Transport | rclone remote `gdrive:` → Drive folder `LinkedInJobs` |

---

## 2. Get into the VM with zero friction

### One-time setup on a new machine
1. **Install the Google Cloud SDK** (winget: `winget install Google.CloudSDK`).
   On this PC it lives at:
   `C:\Users\you\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd`
   > PATH only picks up `gcloud` in **newly opened** terminals after install.
2. **Authenticate** as the right Google account:
   ```
   gcloud auth login
   ```
   (Opens a browser; sign in as `you@example.com`.)
3. **Point at the project:**
   ```
   gcloud config set project YOUR_GCP_PROJECT
   ```

### Every time — SSH in
```
gcloud compute ssh clouduser@scraper-vm --zone=us-east1-c
```
- First connection auto-creates the SSH keypair (`~/.ssh/google_compute_engine`
  and `.pub`) and registers the public key in the instance/project metadata.
  **No manual key handling required.**
- If it ever says the instance is starting/unreachable, wait ~30s and retry.

### Verify access (run these yourself if you want to confirm state)
```
gcloud auth list                 # shows the active account
gcloud config list               # shows project/account
gcloud compute instances list    # shows scraper-vm + status + zone
```

### ⚠️ Login gotchas (these have bitten us)
- **Always** `clouduser@scraper-vm`. A bare `scraper-vm:` or omitting the user
  defaults to an empty orphan account (no home dir — none of the code/data).
- **scp uses a bare colon, no `~/`:**
  `gcloud compute scp file.py clouduser@scraper-vm: --zone=us-east1-c`
  (pscp rejects `~/`; the bare colon means "home dir".)
  For pulling down, use an absolute remote path:
  `gcloud compute scp clouduser@scraper-vm:/home/clouduser/file.gz local.gz --zone=us-east1-c`
- **PowerShell 5.1 mangles inner double-quotes** inside `--command='...'`. Keep the
  remote command **single-quoted with NO inner double quotes**. (For multi-word
  `grep` patterns, match a single word instead, e.g. `grep -c ADJACENT file`.)

---

## 3. VM file layout (`/home/clouduser/`)

| Path | Role |
|---|---|
| `~/scraper.py` | Bright Data scrape → per-run CSV + appends to master |
| `~/score_jobs.py` | Two-stage Gemini scoring; folds scores into master; retries failed rows |
| `~/resume.md` | The resume the scorer matches against |
| `~/run_scraper.sh` | Cron entrypoint: scrape → score → upload (flock-guarded, healthcheck-pinged) |
| `~/linkedin_jobs_master.csv` | Cumulative scored record (source of truth, ~840 rows) |
| `~/morning/`, `~/evening/` | Per-run scored `*.csv.gz` (just that run's NEW jobs) |
| `~/last_run_job_ids.json` | Fallback exclusion list (normally exclusions come from the master: all ids scraped in the last 14 days, since Bright Data bills at collection) |
| `~/run_stats.csv` | One metrics row per scoring run (rows in/filtered/scored, errors, rescore counts, token spend). Uploaded to Drive; rendered in the dashboard's **Stats** tab |
| `~/company_blocklist.txt` | UI-managed company blocklist — pulled from Drive at the start of every run and merged with `COMPANY_BLOCKLIST` in `scraper.py` |
| `~/scraper.log` | Combined scrape+score stdout/stderr |
| `~/venv/` | Python virtualenv — pinned in `requirements-vm.txt` (pandas, google-genai, aiohttp, markdownify) |

### Cron (`crontab -l`)
```
CRON_TZ=America/New_York
0 10 * * * ~/run_scraper.sh >> ~/scraper.log 2>&1
0 19 * * * ~/run_scraper.sh >> ~/scraper.log 2>&1
```

### Manage the VM from the dashboard (no manual SSH)
The dashboard drives the VM over `gcloud compute ssh/scp` using your existing
`gcloud auth login` — it stores only the non-secret connection details
(instance / zone / project / Linux user) in your git-ignored `.env`, never a
password or key. There's **no separate VM tab**: open **Settings**, turn on
**Enable VM features** (off by default), and fill the VM (cloud scraper) section;
the controls appear at the bottom of Settings. With the toggle off, the VM area is
hidden and no push prompts fire. Then:

- **Schedule:** pick the run times from the numbered **Run 1–6** hour dropdowns (up to
  6/day, >=2 h apart) + daily/weekly/biweekly; each picked time is its own line in the
  live `crontab` preview, and *Apply schedule to VM* installs it. A run is labelled by the
  hour it starts: **morning / afternoon / evening / night** (`run_labels.py`, shared
  by `scraper.py`, `score_jobs.py`, and the dashboard; legacy morning/evening data
  still reads). `run_scraper.sh` now `mkdir`s and rclone-syncs all four label dirs.
- **Pause:** `~/pause_until` may hold a date (`YYYY-MM-DD`) **or** date+time
  (`YYYY-MM-DD HH:MM`); `run_scraper.sh` skips runs until then and self-clears. Set
  it from the VM controls (*Pause VM* / *Resume now*) or by hand:
  `echo "2026-07-01 09:00" > ~/pause_until`.
- **Push config:** copy `search_config.json` / `scoring_config.json` up with one
  click; saving a setting that *actually changes* a VM-read file also offers to push
  it (the diff is value-semantic, so re-saving identical values never prompts).
- **Push `resume.md`:** the **Resume Data** tab can regenerate the scorer's `resume.md`
  from `master_experience.yaml` via Gemini (model-selectable; faithful select-and-rephrase;
  preview-then-write with a `resume.md.bak` backup) and push it to `~/resume.md` so the
  cloud scorer matches against the same résumé. The *Push resume.md to VM* button is
  greyed out unless VM features are on and a VM is configured.

`.sh` files are pinned to LF via `.gitattributes` so a Windows checkout's CRLF
can't break the shebang when scp'd to the VM.

---

## 4. How scoring works

**Mechanical pre-filter (free, in `score_jobs.py`):**
- `JUNK_TITLE_PATTERNS` — drops senior/lead/manager/principal/staff/II–IV titles.
- Experience-years filter — `min_required_years()` + `MIN_FILTER_YEARS = 1`.
  **Only a 0-year floor survives:** "0-2 years" stays; "1+", "1-2", "2 years",
  "3+" are scrapped. A range or "N+ years" counts as a requirement on sight (lower
  bound); a bare single number needs a requirement cue nearby; marketing/tenure
  phrases ("20+ years of excellence") are ignored. **This is deliberate — leave it.**
- **Company blocklist** (`COMPANY_BLOCKLIST` in `scraper.py`) — drops spam
  aggregators (currently `"jobright"`) from fresh runs AND purges them from the
  master every run. Extended at runtime by `~/company_blocklist.txt`, which
  run_scraper.sh pulls from Drive — the dashboard's right-click → **Block
  company** appends to that file, so no scp is needed to block someone new.

**LLM scoring (Vertex AI):**
- Stage 1 (`gemini-2.5-flash-lite`, override `SCORE_STAGE1_MODEL`): every survivor scored 1–5 + reason.
- Stage 2 (`gemini-2.5-flash`, override `SCORE_STAGE2_MODEL`): jobs scoring ≥4 get deep_score 1–10, strengths,
  gaps, and a recommendation (`apply` / `consider` / `skip`).
- Prompts are tuned so **analyst/BI/business roles are in-domain** and the model
  does NOT penalize for "career trajectory" / lacking a business degree.
- **Rescore pass:** after the fresh batch, master rows whose scoring previously
  failed (score empty + not filtered, or reason/recommendation `ERROR:…`) are
  retried, newest-first, capped at 200/run (`SCORE_RESCORE_CAP`). So a transient
  429/timeout no longer permanently hides a job.
- **Spend guard:** at most 800 jobs are scored per run (`SCORE_MAX_PER_RUN`);
  overflow stays unscored and is picked up by the rescore pass next run.

**Master columns added by scoring:** `score, reason, deep_score, strengths, gaps,
recommendation, filter_junk_title, filter_too_many_years, filtered_out, is_seen`.

**Run metrics:** every `score_jobs.py` invocation appends one row to
`~/run_stats.csv` (input rows, filtered, scored, errors, stage-2 count, rescore
counts, LLM calls + prompt/output tokens). Cost or volume drift shows up there
— check the Stats tab (or the CSV on Drive) when something looks off.

**Tests:** the experience-years filter has a pinned regression suite —
`python -m pytest tests/ -v` locally before touching `min_required_years()` or
its regexes. `QT_QPA_PLATFORM=offscreen python tests/smoke_qt.py` smoke-tests the dashboard end to end.

---

## 5. Local Windows side

| Path (`<repo>\local\`) | Role |
|---|---|
| `watcher.py` | Detects synced files, reconciles `is_seen`, launches dashboard |
| `app.py` + `qt/` | PySide6/Qt dashboard (modern dark): High Score / All Jobs / **Tracker** / **Stats** / **Resume Data** / **Apply Answers** / **Settings** tabs, score-preview pane, right-click menu, **Apply** button |
| `jobsdata.py`, `chrome.py` | Toolkit-agnostic data/config logic + Chrome launcher (no Tk/Qt) |
| `settings.py` | One editable-options schema powering the **Settings** tab; atomic writes (with `.bak`) to the config files below |
| `seen_db.py` | SQLite registry: seen ids + `app_status` (application tracker) + `resume_paths` (tailored-resume folders) |
| `csv_io.py` | gz read/write + is_seen reconciliation |
| `config.json` | `gdrive_root = E:\My Drive\LinkedInJobs`, `min_score = 4`, `followup_days = 5` |
| `resume_tailor/` | Gemini resume tailor (select→rephrase→verify→PDF) + `ats.py` (keyword coverage), `research.py` (grounded company blurb), `prep.py` (interview sheets), `apply_data.py` (form-prefill profile), `apply.py` + `apply_config.py` (apply launcher + standard answers) |

**Config files (git-ignored; edited via the Settings tab, env still overrides, absent = built-in defaults):**

| Path (repo root unless noted) | Read by | Holds |
|---|---|---|
| `search_config.json` | `scraper.py` | keywords, remote types, postings-per-search, exclusion window, location/country/time-range/job-type/experience |
| `scoring_config.json` | `score_jobs.py` | stage-1/2 models, concurrency, stage-2 threshold, per-run spend caps, seniority-years cutoff |
| `apply_config.json` | `apply_data.py` | `standard_answers` (work auth, sponsorship, relocation, EEO self-id, "how did you hear") |
| `local/config.json` | dashboard | gdrive_root, min_score, followup_days, backend, résumé artifact toggles + tone |
| `setup_tasks.ps1` + `task.xml` | Registers the scheduled task |

- **Scheduled task:** `LinkedInJobsWatcher` (triggers: logon, unlock, wake, +6
  daily at 10:10/20/30 & 19:10/20/30). Pops the dashboard **only** when there are
  new unseen ≥4 jobs. **Boot/restart coverage:** the logon trigger fires after
  every boot, and `StartWhenAvailable=true` replays any daily run missed while
  the PC was off on the next boot — so the folder is always re-checked after a
  shutdown/restart. `task.xml` also carries an explicit system-startup
  `<BootTrigger>`, but registering that needs an **elevated** PowerShell; a
  non-admin `setup_tasks.ps1` run falls back to the trigger set above (which
  already covers boot) and prints a note. To add the explicit boot trigger, run
  `setup_tasks.ps1` from an Administrator PowerShell.
- **Desktop shortcut — "LinkedIn Jobs Dashboard":** opens the dashboard on
  demand (target: `pythonw open_dashboard.pyw`). `open_dashboard.pyw` reuses the
  watcher's config + Drive auto-detection to find the master (or the latest run
  files), then launches `app.py` in-process. Safe to double-click any time:
  app.py takes a single-instance lock, so a second launch just exits.
  `setup_tasks.ps1` (re)creates the shortcut on the current user's desktop
  (`-NoShortcut`/`-NoTask` skip either half).
- **is_seen survives VM overwrites:** the VM resets `is_seen=no`; the local SQLite
  registry re-applies "yes" on every sync, so triaged jobs stay hidden.
- **High-Score ordering:** score desc, then **fewest applicants first**
  (`job_num_applicants`) — the freshest apply window floats to the top.
- **Details pane:** selecting any row shows the model's stage-2 analysis
  (reason / strengths / gaps), salary range, applicant count, and a JD snippet.
- **Application tracker:** right-click a job → **Set status → applied** records
  the job in `seen.db` and marks it seen. The Tracker tab manages status
  (applied → interviewing / rejected / offer), shows **follow-up DUE** when an
  application is ≥ `followup_days` old with no follow-up, links each row to its
  tailored-resume folder, and has the **Interview prep** button (one flash call
  → `interview_prep.md`). `seen.db` lives only in `%LOCALAPPDATA%\linkedin_watcher\`,
  so the Tracker tab's **Export tracker… / Import tracker…** buttons back it up and
  restore it on another machine (import **merges** — newer status wins, nothing is
  deleted). The Stats tab shows the applied-vs-recommendation
  calibration readout (how many applications you have labeled, broken down by the
  model's recommendation). The Stats tab also flags the pipeline as
  **stale** when the newest run is older than `stale_after_hours` (default 36).
- **Tailor artifacts:** each run writes the PDF + `resume.tex` +
  `ats_report.txt` (JD keyword coverage % and missing terms) +
  `apply.md` (a self-contained apply sheet — a **fallback for portals that don't
  auto-fill from your résumé upload**, so it lists no files to upload: candidate
  basics/education/standard answers + structured address, plus this job's tailored
  résumé translated into markdown (work experience/projects/leadership/skills,
  mirrored from the run's own output — no extra AI call), with a "when to use this
  sheet" note + the fill-it-out instructions at the top).
  With "+ cover letter", the body is grounded by a
  Google-Search research blurb (`research.py`, small per-query Vertex cost; falls
  back to JD-only).
- **Browser-assisted apply:** the dashboard's **Apply** button (green only once a
  job has its résumé PDF + `apply.md`) opens the posting in Chrome and pops a
  right-side Apply panel with copyable doc paths + the apply sheet **rendered as
  markdown** (Copy apply sheet still copies the raw source). Paste the apply sheet
  into Claude-in-Chrome to fill the fields by hand. When done, **"I applied to
  this job"** (confirm) records it in the Tracker and closes the panel. **Always
  review before submitting** — nothing auto-submits.
- **Python deps:** pinned in `requirements.txt` (local) / `requirements-vm.txt`
  (VM). **MiKTeX is required locally** (pdflatex on PATH, or set
  `PDFLATEX_PATH`) for resume/cover-letter compilation:
  `winget install MiKTeX.MiKTeX`.
- **Manual open:**
  `pythonw "<repo>\local\app.py" "E:\My Drive\LinkedInJobs\linkedin_jobs_master.csv.gz"`
- Windows Python: `C:\Python314\` (pythonw at `C:\Python314\pythonw.exe`).

---

## 6. Common operations

> All run from any terminal with `gcloud` authenticated. `$g` = full gcloud path.
> Remote commands are single-quoted, no inner double quotes (see §2 gotcha).

**Run the whole pipeline now (scrape + score + upload):**
```
gcloud compute ssh clouduser@scraper-vm --zone=us-east1-c --command='~/run_scraper.sh'
```

**Re-score the existing master (after editing prompts/resume):**
```
gcloud compute ssh clouduser@scraper-vm --zone=us-east1-c --command='cd ~ && source ~/venv/bin/activate && export GOOGLE_CLOUD_PROJECT=YOUR_GCP_PROJECT && export GOOGLE_CLOUD_LOCATION=global && python score_jobs.py ~/linkedin_jobs_master.csv'
```

**Deploy a changed file to the VM:**
```
gcloud compute scp score_jobs.py clouduser@scraper-vm: --zone=us-east1-c
```

**Re-upload the master to Drive (so the dashboard sees it):**
```
gcloud compute ssh clouduser@scraper-vm --zone=us-east1-c --command='gzip -c ~/linkedin_jobs_master.csv > /tmp/m.gz && rclone copyto /tmp/m.gz gdrive:LinkedInJobs/linkedin_jobs_master.csv.gz --update && rm /tmp/m.gz'
```

**Block another spam company:** right-click the row in the dashboard → **Block
company** (appends to `company_blocklist.txt` in the Drive folder; the dashboard
hides it immediately and the VM purges it next run). Hardcoding in
`COMPANY_BLOCKLIST` in `scraper.py` + scp still works for permanent built-ins.

**Run the tests (after touching filters or the UI):**
```
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -v   # full suite incl. headless Qt UI
QT_QPA_PLATFORM=offscreen python tests/smoke_qt.py     # Qt dashboard smoke test
```

**Check recent activity / errors:**
```
gcloud compute ssh clouduser@scraper-vm --zone=us-east1-c --command='tail -40 ~/scraper.log'
```

**Watcher/UI logs (local):** `%LOCALAPPDATA%\linkedin_watcher\watcher.log`,
`ui_error.log`, plus `seen.db` and `state.json`.

---

## 7. Secrets — where they live (retrieve yourself, securely)

**None of these values are in this doc on purpose.** To move them to another
machine, copy them directly over a secure channel — don't paste them into shared docs.

| Secret | Location | Notes |
|---|---|---|
| **GCP / VM SSH access** | Managed by `gcloud`. Keypair at `~/.ssh/google_compute_engine{,.pub}` on each machine. | You normally never touch it — `gcloud auth login` + `gcloud compute ssh` regenerate/register it automatically. To reuse the SAME key elsewhere, copy both files yourself. |
| **Google account login** | `you@example.com` password / 2FA | Held by you. Needed for `gcloud auth login`. |
| **Bright Data API token** | Supplied via env: `BRIGHT_DATA_API_TOKEN` (+ `BRIGHT_DATA_DATASET_ID`), loaded from a local `.env` or exported on the VM. Never committed (see `.env.example`). | Treat as sensitive. **Rotate it** in the Bright Data dashboard if it was ever exposed, then update your `.env` / VM exports. |
| **rclone → Google Drive** | VM file `~/.config/rclone/rclone.conf` (OAuth token) | Already configured on the VM; no re-auth needed. To replicate elsewhere: `rclone config` and re-authorize, or copy that file. |
| **Vertex AI (Gemini) auth** | **No key file.** Uses the VM's attached service account via Application Default Credentials. | Service account `YOUR_SA@developer.gserviceaccount.com` has role `roles/aiplatform.user`; VM scope is `cloud-platform`. |

### IAM / scope facts (for rebuilding auth from scratch)
If Vertex calls start failing with permission/region errors, the working config is:
- API enabled: `aiplatform.googleapis.com`
- Default compute SA granted `roles/aiplatform.user`
- VM access scope widened to `cloud-platform` (stop VM → `set-service-account
  --scopes=cloud-platform` → start)
- `GOOGLE_CLOUD_LOCATION=global` (these Gemini models are global-only, not in
  us-east1/us-central1) — set in `run_scraper.sh`
> Note: IAM/scope changes must be run by a human with the right permissions
> (an automated agent is blocked from making high-severity grants).

---

## 8. Troubleshooting quick hits

- **Dashboard didn't pop:** no new unseen ≥4 jobs (by design). Force-open via the
  manual command in §5. Check `watcher.log`.
- **Dashboard shows stale data:** Drive hasn't synced yet. The dashboard
  auto-reloads itself — instantly when the OS emits a file event, and within ~15s
  via an mtime poll otherwise (e.g. Drive streaming mode), so no manual refresh is
  needed. New master from the VM lands at `E:\My Drive\LinkedInJobs\…`.
- **Vertex 403 (ACCESS_TOKEN_SCOPE_INSUFFICIENT):** VM scope — see §7.
- **Vertex 404 (model not found):** region — set `GOOGLE_CLOUD_LOCATION=global`.
- **Cron didn't fire:** confirm `CRON_TZ` + `timedatectl` shows America/New_York.
- **Free-tier cost:** scraping cost is on Bright Data (billed at collection, before
  any local filtering). VM→Drive egress ≈ <100 MB/month (well under 1 GB free tier).
- **Failure alerting:** `run_scraper.sh` pings a healthchecks.io URL on success and
  `…/fail` on any error — fill in `HEALTHCHECK_URL` at the top of the script (create
  a free check, schedule twice daily, ~2 h grace). Empty URL = pings are skipped.
  Locally, `watcher.log` warns when the synced master is > 36 h old.
- **Zero-result scrape:** scraper writes nothing and exits 0; score_jobs skips fresh
  scoring (it never re-picks an old input — anything with an existing `_scored.csv.gz`
  is skipped), runs only its rescore pass, and the master still uploads.
