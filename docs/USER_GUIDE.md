# INployed user guide

Everything the dashboard and the CLIs can do, once the [README's Quick start](../README.md#quick-start)
has you running. Skim the headings; nothing here is required reading.

### Tailor a résumé for one job (CLI)
The résumé-tailor CLI lives in the `resume_tailor` package, so run it from `local/`:
```bash
cd local
python -m resume_tailor.run --job-id <job_posting_id> --cover-letter
```
Output (in `~/Downloads/Generated_Resumes/<Company>/<Title>/`): a one-page PDF, its
`.tex` source, `ats_report.txt` (keyword coverage), an optional cover letter as **both a
PDF and its `.tex` source** (so you can edit a word and re-run `pdflatex` instead of
regenerating), and `apply.md` (a self-contained apply sheet you paste into
Claude-in-Chrome). The cover letter's plain text lives in `apply.md`'s `## Cover letter`
section, which is what you paste into an application's cover-letter box.

### Fine-tune the résumé layout
The **Resume Data** tab has a collapsible **Resume Layout** editor for how many bullets
each section/project gets and how long each one runs. Give a section or project a
comma-separated list of per-bullet printed-line counts. For example, `2, 2, 1` means three
bullets sized 2 / 2 / 1 lines (each 1 to 3, up to 5 bullets), and the one-page tailor
honors it. A **"Bullets by strength"** box sizes projects by how strongly each ranks for
*this* job instead of a flat count: type tiers as `projects:bullets` pairs (e.g.
`2:3, 2:2, 1:1`) and the strongest-matching projects earn the extra bullets. A master
**"Apply custom bullet layout"** checkbox turns the whole feature on or off: unchecked,
the engine uses its built-in defaults but your saved targets are kept, so you can
**A/B test** whether your custom layout helps without throwing the configuration away.

### Find skills you forgot to list
The JD-gap helper surfaces skills a posting wants that aren't yet in your master
file, screens them to non-identifying skills, and (only on your
confirmation) folds them into the right bucket with a reviewable diff + backup.
Run it from `local/`:
```bash
cd local
python -m resume_tailor.master_gaps --jd-file job.txt          # preview
python -m resume_tailor.master_gaps --jd-file job.txt --apply  # write (.bak made)
```

### Run the dashboard
Launch it the way Step 4 describes: double-click `Open INployed Dashboard.cmd`.
The window opens maximized and gives you high-score triage, an application tracker with
follow-up nudges, and run stats. A few behaviors to know:
- **Tailor résumé** runs in the background, so the UI stays responsive.
- Select several jobs and it tailors them all at once, in parallel. A single failure is
  reported without sinking the rest, and a quick warning appears before very large batches.
- Tailoring streams live progress in the status bar (`Tailoring (2/3 done): … rephrasing
  bullets`), so a multi-minute run is never a silent freeze.
- The Step 4 get-started panel lists its three next actions (Open Settings · Find new jobs ·
  Set up Resume Data) and is replaced by the job table as soon as you have scored jobs.

Each job tab keeps a tidy filter bar: a search box plus a **Filters** button that holds
min-score / day / time / recommendation / Easy-Apply (on the Tracker, also *Follow-up due
only*), and shows how many are active. The Tracker adds a one-click **status chip bar**
(All / Applied / Interviewing / Offer / Rejected / Follow-up due, each with a live count).
Each tab keys its rows to what matters there:
**High Score** tints by recommendation + tailored-résumé (green apply · blue résumé ready ·
red tailor failed · yellow consider · plain "don't consider"), the **Tracker** tints by application status +
follow-up (blue applied · orange follow-up due · pink follow-up sent · yellow interviewing ·
green offer · red rejected), and **All Jobs** stays an untinted plain list. A small
**color legend** under each table (except All Jobs) spells the meanings out.

The actions, the interface-size control, and a **Restart** button all share **one bottom
bar**. You can size the whole interface to your display from the **Interface size** control:
a slider with `-` / `+` buttons (10% steps, 75-150%), or **Ctrl +** / **Ctrl -** (and
**Ctrl 0** to reset to 100%); the change applies **immediately** and your choice is
remembered. **Restart** closes and reopens the dashboard.

Selecting a job opens a **detail card** at the bottom: the job's title and meta line,
score / deep-score / applicants chips, the model's reasoning, strengths, and gaps, a
**Show description** button (on jobs that came with a description), and the per-job
actions (**Open posting**, **Tailor résumé**, **Apply**). Click **Show description** and
the card splits into two columns: the scoring stays on the left, the whole posting opens
beside it on the right. The card grows to at least about half the window so there is
room to read. **Hide description** gives that height back, unless you dragged the divider
above the card while the description was open: then your size stands. The description
stays open as you click from job to job: only the text changes, so you can read down a
list without re-opening it each time. It scrolls on its own instead of stretching the
card, and the text can be selected and copied. Both dividers are draggable: the one
between the scoring and the description (your split is remembered until you close the
dashboard) and **the divider above the card**, which sets its height. A job with no
description at all folds the card back to one column. On the Tracker the card switches to
a tracker variant with status and follow-up pills plus a suggested next step, and stays a
single column (there is no description there). The card appears only on the job-list tabs
(**High Score / All Jobs / Tracker**) and hides itself elsewhere.

At-a-glance colors: a job whose tailored-résumé folder still exists on disk is tinted
**blue** in the High Score / All Jobs lists (delete the folder and the tint clears on the
next refresh); in the **Tracker**, an *applied* job is **blue** and a *rejected* one is **red**.

Right-click any job to work with it: **Set status →** marks it applied / interviewing /
rejected / offer from any tab, and the menu also offers **Delete job** (any row) and
**Edit job…** (for jobs you added by hand). An **Add job by hand** button (High Score /
All Jobs toolbar) takes a pasted posting URL or job description and runs it through the same
scoring + tailoring pipeline as a scraped job. A **Find new jobs** button (bottom action bar)
kicks off a fresh discovery + score on demand; it asks first (a *small test run* or a
*full run*) because finding jobs costs real money / API credits.

The **Tracker** tab has **Export tracker… / Import tracker…** buttons. Your whole
application history (seen-state, statuses, and tailored-résumé links) lives in a local
SQLite file, so export a backup and import it on another machine. Import **merges** (a
more recent status wins; nothing is deleted). The **Stats** tab shows a **freshness
badge** (mirrored in the window's header strip): green when the latest pipeline run is recent, amber *"the cloud job search may
have failed"* once it's older than the **Flag data as stale after (hours)** setting
(default 36), so a broken cron run doesn't go unnoticed.

### Get fresh jobs
- **From the dashboard:** click **Find new jobs** and choose a *small test run* or a
  *full run*. It runs `scraper.py` then `score_jobs.py` in the background and
  refreshes the view when done.
- **On-demand (local CLI):** run your own pipeline, then open the dashboard:
  ```bash
  python pipeline/scraper.py                              # full run (needs Bright Data keys in .env)
  python pipeline/scraper.py --max-keywords 2 --limit 8   # small, cheap bounded run
  python pipeline/score_jobs.py                           # needs Vertex AI / ADC (auto-loads .env locally)
  ```
  `--max-keywords N` / `--limit N` cap a run's cost: the job-data provider bills per
  collected posting, so the full keyword list (the VM default) can collect
  thousands. Use the caps for a quick check.
- **Hands-off (recommended for daily use):** run that pair on a small GCP VM via
  cron and sync results to Google Drive, then drive the schedule, pauses, and config
  pushes from the dashboard's **Settings → VM (cloud job discovery)** section (below).
  (This path needs the **Google Drive desktop app** on your PC so the VM's output
  folder syncs down; the local CLI path above doesn't.)

### Configure everything from the Settings tab (no file editing)
Open the dashboard (double-click `Open INployed Dashboard.cmd`) and click the
**Settings** tab: one
schema-driven form that edits every tunable the project has, grouped and explained,
so a non-technical user can set things up without touching a file. Each section has a
**collapsible header** with a one-line tagline, so you can fold away the parts you're
not editing (the tagline still tells you what each collapsed section is for) and tackle
one group at a time.

**Finding one setting among sixty-odd.** Three things at the top of the tab, in this order:

- **The search box:** type a word and the tab filters to the rows that mention it. It
  matches the setting's name, its explanation, its config key **and the chips on the
  row**, so you can search `GEMINI_API_KEYS` after reading your `.env`, or `restart` to
  list every setting that needs one. Several
  words narrow rather than widen (`gemini key` is the key box, not everything Gemini).
  Sections with no match disappear, sections with one open themselves, and **clearing the
  box puts your layout back exactly as it was**. An opening the search made for you is
  never saved. If a match exists but your configuration makes it inert, a muted line under
  the results says so and names the switch: *"3 more settings apply when Scoring provider
  is 'claude'"*.
- **Show advanced settings:** off by default, folding 18 power-user rows away (the
  per-stage model pickers, scorer concurrency and retry caps, VM plumbing). The label counts
  what it is currently withholding *for your configuration*, so ticking it really does
  reveal that many rows. Search ignores the fold: an advanced row still turns up in
  results, tagged `(advanced)`.
- **Unsaved-change markers:** an accent dot appears beside every field you have edited, the
  section header picks up "· 2 changed" (visible even when the section is folded, which is
  the point), and the Save button reads "Save 3 changes". A **↺** button appears on any row
  sitting off its default and puts that one row back; credentials do not get one, because
  their default is blank and the click would wipe a live key. **Discard changes** still
  undoes everything back to how the form opened.

Rows whose value only matters to some configurations hide themselves. The Gemini model
pickers are absent while the tailor runs on Claude, and vice versa. Nothing is lost by
this: switch provider, save, switch back, and the custom model id you typed is still
there. A row tagged **`restart`** is one the dashboard reads only at startup, so saving it
writes the file immediately but the running app keeps using the old value; Save says so
again and names them.

The sections:

- **Credentials:** the job-data (Bright Data) API token, the Gemini API-key pool,
  and the résumé-tailor API key. Each box holds the saved value (read straight from
  your local `.env`), masked by default. Untick *Hide* to reveal one, edit it to
  change it, or clear the box to remove the key.
- **Connection & paths:** the job-postings dataset ID, Google Cloud project +
  location, your name (for résumé filenames), the résumé output folder and
  `pdflatex` path (with **Browse…** buttons), and which Chrome profile to open
  links in.
- **Engine:** the tailor's **provider** (Gemini or Claude) and, on Gemini, which backend
  it bills (Vertex project vs API key). See the Claude backend note below.
- **Dashboard / Job discovery / Scoring / Résumé:** scores, follow-up days, search
  keywords, remote types, spend caps, artifact toggles, and more. **Drop Easy Apply jobs
  before scoring** (off by default) discards LinkedIn Easy-Apply postings before they cost
  a scoring call, for anyone who only wants postings with a real application form.
- **Models:** the scorer's two stages **and** the résumé tailor's are **editable
  dropdowns**: the recent Gemini 3.x ids by default, plus the Claude tier ids used when a
  provider is set to `claude`. Pick one or type a custom id. The tailor asks one question
  before the rest — **simple or per stage** — described under *One model for every step*
  below.
- **Auto-apply / Settings history:** the batch-apply queue cap and which webmail
  inbox the apply agent opens for verification emails; plus a snapshot of your
  settings on every Save, restorable from **Restore from archive…**. **Settings
  snapshots** is one dropdown: *Off*, *Keep everything* (the default; nothing is ever
  deleted), *Keep newest 20*, or *Keep newest 100*. Each snapshot holds a copy of your
  `.env`, so more snapshots means more copies of your keys on this PC.
- **VM (cloud job discovery):** an **Enable VM features** master toggle (off by default)
  plus the non-secret connection details for your GCP job-discovery VM (instance, zone,
  project, Linux user). Off hides the whole VM area and silences VM prompts; turn
  it on to reveal the controls (see *Manage the VM* below).

Guard rails keep it hard to break: fixed-choice fields are **dropdowns** (no
typos), bounded numbers are **sliders** or **spin boxes**, multi-select fields are
**checkboxes**, every field has a one-line explanation **and a muted tag naming the file
its value is saved to** (e.g. `(.env)`, `(search_config.json)`) so you can find it
yourself, there's a **Discard changes** button (undo your edits back to how the form
opened) alongside **Restore defaults**, and **Save tells you exactly which fields changed**
(secrets shown as *updated* / *cleared*, never the value).

**When something is wrong, it is flagged where it is.** A rejected value outlines the box
in red with a note underneath, the form scrolls to the first one you can act on, and the
status line counts them ("2 settings need fixing"). There is no modal listing every problem and
pointing at none of them. Fields are re-checked when you tab out of them, not only at Save. If a
number you hand-edited into a config file is outside the allowed range, the spin box shows
the clamped value **and tells you** what the file actually holds, rather than quietly
rewriting it on the next Save.

Edits are written atomically (with a `.bak`) to your git-ignored `.env`,
`local/config.json`, and `search_config.json` / `scoring_config.json`. Environment
variables still override a file, and an absent file falls back to built-in defaults, so
the VM keeps running unchanged.

> **Claude backend (optional).** The résumé tailor and the local job scorer can each run
> on your Claude Code CLI subscription instead of Gemini. Set **Resume tailor provider** or
> **Scoring provider** to `claude` (both default to `gemini`). The Claude path drives the
> headless CLI with your subscription auth (no API key) and prompt caching; left on `tiers`
> (the default), the tailor stages map fast → `claude-haiku-4-5`, standard →
> `claude-sonnet-5`, deep → `claude-opus-5`. The cloud VM always scores with Gemini,
> regardless of this setting.

#### One model for every step, or one per stage
Settings → Engine, **Tailor models — simple or per stage** (and, on the Claude provider,
**Claude models — simple or per stage**). Tailoring runs in stages, and by default each one
gets its own model: a cheap model to pick which of your experiences to use, a stronger one to
write the bullets. That saves money, but it means three dropdowns and three decisions before
you have a working setup.

Switch the row to **simple** and there is one: **Tailor model — one for every step** — pick a
listed id or type your own, and every stage uses it. The three per-stage pickers (which live
under *Show advanced settings*) disappear while simple is on, and reappear with your choices
intact if you switch back — nothing you typed is lost either way. Leave it on **tiers**, the
default, and nothing about your setup changes.

Two details worth knowing:

- The setting is **per provider**. Gemini and Claude each have their own pair of rows, and
  you only ever see the pair for the provider you're using, so the two can differ: one model
  everywhere on Claude, the tuned per-stage split on Gemini.
- If you switch to simple and leave the model box **blank**, tailoring quietly goes back to
  the three per-stage models rather than failing. A blank is treated as "no preference", not
  as an instruction.

Like nearly everything saved to `.env`, this one is tagged **`restart`**: Save writes it
immediately, but the dashboard picked up its model settings when it launched, so **close and
reopen the dashboard** before the change affects a tailoring run. Save names the rows that
need it.

#### What "Strip AI writing patterns from the cover letter" catches
Settings → Résumé, off by default. It adds a second, stricter style pass to the **cover
letter only** (résumé bullets are unaffected), applying a letter-relevant subset of Conor
Bronsdon's MIT-licensed `avoid-ai-writing` skill (credited in `docs/CREDITS.md`):

- the overused AI vocabulary: *delve*, *pivotal*, *impactful*, *learnings*, *in order to*
- *"it's not X, it's Y"* contrast framing
- hedging, and chatbot tics such as *"I hope this helps"*
- rhetorical-question openers and *"In conclusion"* endings
- the metronomic sentence rhythm that makes writing read as machine-made

The rules ride in the writing prompt, and the worst offenders are also caught afterwards by
a deterministic checker that buys exactly one rewrite. It is off by default because it is a
taste call, and turning it off leaves the letter exactly as it was before the setting
existed. The grounding gate still runs last either way, so a restyled sentence that
introduces an unsupported fact is still rejected.

### What leaves your machine
There is no analytics, no crash reporting, and no phone-home. The only outbound
traffic is the work you asked for, and each destination gets only what it needs:

| Destination | When | What it receives |
| --- | --- | --- |
| Bright Data | you run job discovery | your search keywords and the dataset ID |
| Google Gemini (Vertex or API key) | scoring and résumé tailoring | the job description, your `resume.md` / `master_experience.yaml` content |
| Anthropic (`claude` CLI) | only if you set a provider to `claude` | the same prompts, through your own CLI login |
| the job posting's own site | only when you paste a URL into *Add job by hand* | a plain GET for the page text |
| the employer's application site | only when you run auto-apply on a queued job | the answers you approved, in your own Chrome; it never submits |
| your own GCP VM (`gcloud compute ssh/scp`) | only when you click a VM control in *Settings* | your search and scoring config, the ids already collected, and rows to merge; plus, only when you click **Set on VM**, the one API key you typed into that box. It runs under your own `gcloud` login |
| healthchecks.io | **opt-in, VM cron only** | a start ping and the run's exit code; no job data, no identifiers |

The healthchecks ping is a dead-man's switch so a silently failing cron run emails
you instead of rotting in the log. It is off unless you set `HEALTHCHECKS_URL`
yourself (see `scripts/run_scraper.sh`); unset, `ping_hc` is a no-op.

Your credentials never cross providers: the Gemini and Bright Data secrets are
stripped from the environment before the `claude` CLI is launched, the ATS master
password lives in the Windows Credential Manager and only ever exits to the
clipboard, and nothing is written to the repo. Secrets stay in your git-ignored
`.env`. The one credential that leaves this PC is the one you hand to **Set on VM**,
and it goes to your own VM so its cron runs can authenticate.

### Manage the VM from the dashboard
If you run discovery + scoring on a GCP VM, the dashboard drives it without
SSH-by-hand; there's **no separate VM tab**. In
**Settings**, turn on **Enable VM features** (off by default) and fill the VM
section (instance, zone, project, Linux user); these non-secret identifiers are
saved to your git-ignored `.env`. Authentication is your existing
`gcloud auth login`; **no SSH password or key is ever stored.** The VM controls
then appear at the bottom of Settings, letting you:

- **Schedule:** pick the run times from the **Run 1-6** hour dropdowns (up to 6/day, at
  least 2 h apart) and a frequency (daily / weekly / biweekly). Each picked time becomes
  its **own** `crontab` line in a live preview, and on **Apply schedule to VM** it's
  installed over `gcloud compute ssh`.
  Each run is labelled by time of day: **morning / afternoon / evening / night**.
- **Pause:** set an *until* date (optionally a time) and **Pause VM**: discovery
  skips every run until then, then resumes on its own (no API spend while paused).
  **Resume now** clears it.
- **Push config to VM:** copy your current `search_config.json` / `scoring_config.json`
  up with one click. And whenever you save a setting that **actually changes** a file
  the VM reads, the dashboard asks if you'd like to push the changed file(s) right
  then; re-saving the same values (or any non-VM setting) never prompts.
- **Credentials:** rotate the VM's own API keys without an ssh session. Pick **Bright
  Data token** or **Gemini API keys**, paste the new value into the masked box, and click
  **Set on VM**. The key is written to a `chmod 600 ~/scraper_secrets.env`,
  `run_scraper.sh` is pointed at that file, and any older inline `export` of the same
  variable is commented out so the dead value cannot stay in force. The script is backed
  up first and restored if `bash -n` rejects the result. The value is sent as a file over
  `scp`, never on a command line, because `gcloud` writes every remote command verbatim
  into its own plaintext debug log. Nothing is stored on this PC. Only those two names are
  accepted, and a value has to be letters, digits and `. _ - : , / + =`, because the
  secrets file is sourced by bash.

Every VM action asks for confirmation first and runs through `gcloud`; nothing
happens automatically. With **Enable VM features** off, none of these prompts ever
appear.

### Keep the scorer's résumé in sync (`resume.md`)
The scorer matches every job against `resume.md`. When you edit your **Resume Data**
(the master experience file), regenerate `resume.md` so the two stay in step. The
**Resume Data** tab shows an **amber warning banner** whenever `resume.md` is older than
your data (so the scorer isn't quietly matching against a stale résumé), with a one-click
**Regenerate resume.md**. To regenerate: on the
**Resume Data** tab, pick a model (`gemini-3.5-flash` by default; the dropdown lists every
3.x flash and pro id the Settings tab offers, and you can type your own) and click
**Generate from my data**. It uses Gemini to rebuild `resume.md`
**faithfully, selecting and rephrasing your data, never inventing.** You **review (and
can edit) the result before it's saved**; saving backs up the old file to `resume.md.bak`.
If VM features are on, it then offers to push the new `resume.md` to the VM, and a
**Push resume.md to VM** button does the same anytime (greyed out when VM features are
off). *(Generating makes a Gemini API call; the push runs `gcloud`, both only on your
click, each after a confirm.)*

### Apply to a job (semi-automated, in Chrome)
Every tailored résumé folder gets a self-contained **`apply.md`** apply sheet. It's a
**fallback for application portals that don't auto-fill the form from your uploaded
résumé**: when a portal parses your résumé upload into its own fields you don't need it;
use it to fill the fields **by hand** when that doesn't work.

The sheet opens with a "when to use this sheet" note and the fill-it-out instructions,
then your candidate basics + structured address, education, **this job's tailored résumé
translated into markdown** (the work experience, projects, leadership, and skills that
actually landed on the PDF: company names, titles, dates, and every bullet, so Claude can
fill the structured employment fields), and the active standard answers. It lists **no
files to upload**; it's built from the tailoring run's own output, so it mirrors the PDF
exactly with no extra AI call. To apply:

1. Tailor the résumé for the job (the **Tailor résumé** button on the detail card). Tailoring no longer pops
   open File Explorer by default; flip **Settings → Open output folder after tailoring**
   on if you want that.
2. Click **Apply** on the detail card. The Apply button is **green only once the job has
   both its résumé PDF and `apply.md`**. Clicking it opens the posting in Chrome and
   swaps the bottom detail card for a right-side **Apply panel** with the copyable
   résumé / cover-letter paths and the apply sheet **rendered as formatted markdown** (the
   **Copy apply sheet** button still copies the raw markdown source). An **Expand** button
   opens the sheet in a large, resizable window for easier reading. Closing the panel brings
   the detail card back; **"I applied to this job"** confirms, adds the
   job to your Tracker as *applied*, and closes the panel (the right-click → *Set status →
   applied* still works too).
3. **In Claude** (the Claude desktop app or this CLI) **with the Claude-in-Chrome
   extension connected**, paste the apply sheet into the chat and let Claude fill the
   Greenhouse / Lever / Ashby / Workday / generic form **page by page until the final
   Submit screen, then it stops for you to review and send.**

**What it will and won't do (safety):** the sheet's instructions tell the form-filler to
fill every field it can and flag the rest; it **never logs in, never creates accounts,
never enters passwords / payment / SSN / government IDs, never solves CAPTCHAs, and never
clicks the final submit.** At a login / account / verification / CAPTCHA wall it pauses and
asks you to do that one step, then resumes. Where the form asks for an electronic signature
it types your name + today's date; a required field with no answer gets a `XXXXX`
placeholder it flags for you. Manage your reusable answers (including address) in the
**Apply Answers** tab: add your own, and mark each *fixed* (never changed) or *open-ended*
(adaptable per job).

CLI equivalent (from `local/`): `python -m resume_tailor.apply --job-id <id> --open`.

**Batch queue (advanced).** The **Auto-apply** tab is a live view of a batch apply
queue: **Queue auto-apply** adds the selected tailored jobs, and the tab tracks each one
(queued, in progress, ready to submit, needs human). Draining the queue runs the same
semi-automated, **parks-at-review, never-auto-submits** flow one job at a time as an agent
session, so it's an optional power-user path. For everyday use, the per-job **Apply** flow
above is the recommended way in.
