# INployed user guide

Everything the dashboard and the CLIs can do, once the [README's Quick start](../README.md#quick-start)
has you running. Skim the headings; nothing here is required reading.

Every `python` in the commands below is the project venv's interpreter from Quick start
Step 2, which is never activated: from the repo root that is `venv\Scripts\python.exe`
(`venv/bin/python` on macOS or Linux), and from `local/` it is `..\venv\Scripts\python.exe`.

### Tailor a résumé for one job (CLI)
The résumé-tailor CLI lives in the `resume_tailor` package, so run it from `local/`:
```bash
cd local
python -m resume_tailor.run --job-id <job_posting_id> --cover-letter
```
Output (in `~/Downloads/Generated_Resumes/<Company>/<Title>/`): a one-page PDF, its
`.tex` source, `ats_report.txt` (keyword coverage), an optional cover letter as **both a
PDF and its `.tex` source** (so you can edit a word and re-run `pdflatex` without a
second model call), and `apply.md` (a self-contained apply sheet you paste into
Claude-in-Chrome). The cover letter's plain text lives in `apply.md`'s `## Cover letter`
section, which is what you paste into an application's cover-letter box.

### Write a grounded cover letter
Add `--cover-letter` to the tailor CLI (or tick the box in the dashboard) and the engine
writes the letter as narrative prose built from the atoms the tailor selected for that job.
An optional `letter.seed` block in `master_experience.yaml` gives it your own voice: two to
four sentences on what you want from your next role; the letter draws on its ideas and its
voice, quoting at most a short fragment of it. For example:

```yaml
letter:
  seed: >-
    I want to build the systems a team runs on every day, the pipelines and
    tools that quietly decide whether the rest of the work is possible.
```

Leave the block out and the letter is written from the atoms alone. **Check setup** warns
when a seed runs past 1,200 characters or the block is shaped wrong; either way the letter
still generates, with the seed capped or skipped.

Generation draws on a background excerpt of the selected entries' own atoms (capped at
30,000 characters, enough for a whole master; a longer history loses its longest
entries' last notes first, every entry stays listed, and `tailor_report.txt` carries an
advisory) plus the seed, then a second pass rewrites the draft for rhythm: varied
sentence and paragraph lengths, and any sentence that reads like a bullet with a subject
bolted on retold as a story. Two structural checks always run, whatever the **"Strip AI
writing patterns"** toggle says: a seven-word run copied straight from a résumé bullet
("bullet echo"), and sentences or paragraphs that all sit within a narrow band of the
average length ("uniform rhythm"). Settings → Résumé's **"Strip AI writing patterns from
the cover letter"** toggle now defaults **on**; see [what it
catches](#what-strip-ai-writing-patterns-from-the-cover-letter-catches) below.

### Fine-tune the résumé layout
The **Resume Data** tab has a collapsible **Resume Layout** editor for how many bullets
each section/project gets and how long each one runs. Give a section or project a
comma-separated list of per-bullet printed-line counts. For example, `2, 2, 1` means three
bullets sized 2 / 2 / 1 lines (each 1 to 3, up to 5 bullets), and the one-page tailor
honors it. A **"Bullets by strength"** box sizes projects by how strongly each ranks for
*this* job, overriding the flat count: type tiers as `projects:bullets` pairs (e.g.
`2:3, 2:2, 1:1`) and the strongest-matching projects earn the extra bullets. A master
**"Apply custom bullet layout"** checkbox turns the whole feature on or off: unchecked,
the engine uses its built-in defaults but your saved targets are kept, so you can
**A/B test** whether your custom layout helps without throwing the configuration away.

Adding an entry or achievement (**+ Add entry** / **+ Add achievement**) flags a problem as
you type, using the same rules Save enforces, and keeps OK disabled until it clears;
**+ Add achievement** also refuses an em dash anywhere in what you typed. A write that fails
leaves what you typed in the dialog so you can fix it and try again.

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
beside it on the right, with its headings, bullet lists and paragraph breaks intact: the
scorer's markdown description renders as structured text, the same column and the same
40-character floor the résumé tailor and **Ask AI** read. The card grows to at least about
half the window so there is room to read. **Hide description** gives that height back,
unless you dragged the divider above the card while the description was open: then your
size stands. The description
stays open as you click from job to job: only the text changes, so you can read down a
list without re-opening it each time. It scrolls on its own, so the card keeps its
height, and the text can be selected and copied. Both dividers are draggable: the one
between the scoring and the description (your split is remembered until you close the
dashboard) and **the divider above the card**, which sets its height. A job with no
description at all folds the card back to one column. On the Tracker the card switches to
a tracker variant with status and follow-up pills plus a suggested next step, and stays a
single column (there is no description there). The card appears only on the job-list tabs
(**High Score / All Jobs / Tracker**) and hides itself elsewhere.

At-a-glance colors: a job whose tailored-résumé folder still exists on disk is tinted
**blue** in the High Score / All Jobs lists (delete the folder and the tint clears on the
next refresh); in the **Tracker**, an *applied* job is **blue** and a *rejected* one is **red**.

**Reposts.** High Score hides a posting that shares a title, company and location with
one you already marked seen or applied, for **Hide reposts for (days)** in Settings
(Dashboard section, default 30, 0 turns it off), and an unseen duplicate of the same
posting collapses to its newest copy the same way. Nothing is marked by this: the older
posting keeps its own status, and the repost simply stays out of the list until the
window passes. When the filter hides anything, the status bar says so ("N reposts hidden").

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
  python pipeline/score_jobs.py                           # needs Gemini API keys or a Vertex AI project (auto-loads .env locally)
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

**Finding one setting among seventy-odd.** Three things at the top of the tab, in this order:

- **The search box:** type a word and the tab filters to the rows that mention it. It
  matches the setting's name, its explanation, its config key **and the chips on the
  row**, so you can search `GEMINI_API_KEYS` after reading your `.env`, or `restart` to
  list every setting that needs one. Several
  words narrow the match: `gemini key` is the key box alone.
  Sections with no match disappear, sections with one open themselves, and **clearing the
  box puts your layout back exactly as it was**. An opening the search made for you is
  never saved. If a match exists but your configuration makes it inert, a muted line under
  the results says so and names the switch: *"3 more settings apply when Scoring provider
  is 'claude'"*.
- **Show advanced settings:** off by default, folding 16 power-user rows away on a fresh
  install (the per-stage model pickers and fallback lists, scorer concurrency and retry caps)
  and 19 once VM features are on, which adds the VM plumbing. The label counts what it is
  currently withholding *for your configuration*, so ticking it really does reveal that many
  rows. Search ignores the fold: an advanced row still turns up in
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
  it bills: your Vertex project, one API key, or **pool**, the scorer's free keys with Vertex
  as the spillover (see *Tailoring on the scorer's free keys* below). See the Claude backend
  note below.
- **Dashboard / Job discovery / Scoring / Résumé:** scores, follow-up days, search
  keywords, remote types, spend caps, artifact toggles, and more. **Drop Easy Apply jobs
  before scoring** (off by default) discards LinkedIn Easy-Apply postings before they cost
  a scoring call, for anyone who only wants postings with a real application form.
  **Repost score reuse window (days)** (Scoring, under *Show advanced settings*; default
  30, 0 turns it off) copies a still-fresh master row's score onto a new posting that
  matches on title, company, location and the first 400 characters of the description, so
  a repost does not spend a fresh Gemini call every time it resurfaces.
- **Models:** the scorer's two stages **and** the résumé tailor's are **editable
  dropdowns**: the recent Gemini 3.x ids by default, plus the Claude tier ids used when a
  provider is set to `claude`. Pick one or type a custom id. The tailor asks one question
  before the rest, **simple or per stage**, described under *One model for every step*
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
pointing at none of them. Fields are re-checked the moment you edit them, and Save stays
disabled while any problem remains. If a
number you hand-edited into a config file is outside the allowed range, the spin box shows
the clamped value **and tells you** what the file actually holds; nothing is rewritten
quietly on the next Save.

Edits are written atomically (with a `.bak`) to your git-ignored `.env`,
`local/config.json`, and `search_config.json` / `scoring_config.json`. Environment
variables still override a file, and an absent file falls back to built-in defaults, so
the VM keeps running unchanged.

> **Claude backend (optional).** The résumé tailor and the local job scorer can each run
> on your Claude Code CLI subscription. Set **Resume tailor provider** or
> **Scoring provider** to `claude` (both default to `gemini`). The Claude path drives the
> headless CLI with your subscription auth (no API key) and prompt caching; left on `tiers`
> (the default), the tailor stages map fast → `claude-haiku-4-5`, standard →
> `claude-sonnet-5`, deep → `claude-opus-5`. The cloud VM always scores with Gemini,
> regardless of this setting.

#### One model for every step, or one per stage
Settings → Engine, **Tailor models: simple or per stage** (and, on the Claude provider,
**Claude models: simple or per stage**). Tailoring runs in stages, and by default each one
gets its own model: a cheap one for the small calls (entry briefs, the overview lead, verb
swaps), a standard one that selects your atoms and runs every bullet cleanup pass, and a deep
one that writes the first draft and the cover letter. That saves money, but it means three
dropdowns and three decisions before you have a working setup.

Switch the row to **simple** and there is one: **Tailor model: one for every step**. Pick a
listed id or type your own, and every stage uses it. The three per-stage pickers (which live
under *Show advanced settings*) disappear while simple is on, and reappear with your choices
intact if you switch back; nothing you typed is lost either way. Leave it on **tiers**, the
default, and nothing about your setup changes.

Two details:

- The setting is **per provider**. Gemini and Claude each have their own pair of rows, and
  you only ever see the pair for the provider you're using, so the two can differ: one model
  everywhere on Claude, the tuned per-stage split on Gemini.
- If you switch to simple and leave the model box **blank**, tailoring quietly goes back to
  the three per-stage models. A blank is treated as "no preference".

Like nearly everything saved to `.env`, this one is tagged **`restart`**: Save writes it
immediately, but the dashboard picked up its model settings when it launched, so **close and
reopen the dashboard** before the change affects a tailoring run. Save names the rows that
need it.

#### Tailoring on the scorer's free keys (`pool`)
Settings → Engine, **Resume tailor engine**. `vertex` (the default) bills every tailor call to
your Google Cloud project and `api_key` uses the single **Gemini API key (resume tailor)**.
`pool` uses the same **Gemini API keys** the job scorer rotates through, every one of them,
held to Google's free-tier limits, and bills the project only when no key can take the call:
all of them have run dry for the day, or Google is refusing every one of them for the moment
(a 503 or an unexplained 429). The keys are the **Gemini API keys (job scorer)** row under Credentials: free-tier
keys from [Google AI Studio](https://aistudio.google.com/apikey), one per Google account,
and every extra account is another day's allowance. With no **Google Cloud project ID** set
there is nothing to spill onto, so a keys-only setup never bills; the tailor stops with a
rate-limit message once every key and fallback model is spent for the day. Google counts that free allowance per key **and per model**, so a second model is a
second daily allowance: the **fallback models** boxes (under *Show advanced settings*, one
model id per line, best first) name the models the tailor may move on to when its own runs
out. With the tailor on **tiers** there is one box per step, because the lite models allow
roughly 500 free requests a day per key and the full Flash models about 20, and the fast
selection step makes most of the calls; list other `-lite` ids for that step and keep the
Flash allowance for the writing step. With **simple** there is one shared box. The scorer has
the same pair of boxes under Scoring (**Stage-1 / Stage-2 fallback models**), and both sides
share **Per-model rate limits**, one `model requests-per-minute requests-per-day` line per
model, which you need only for a model the built-in table does not know or when Google
changes an allowance. A model Google reports as overloaded is set aside for a minute and the
next one in the list is tried; a key that has spent its allowance on one model keeps whatever
it still has on the others.

#### What "Strip AI writing patterns from the cover letter" catches
Settings → Résumé, on by default. It adds a second, stricter style pass to the **cover
letter only** (the bullets have their own pass, below), applying a letter-relevant subset of
Conor Bronsdon's MIT-licensed `avoid-ai-writing` skill (credited in `docs/CREDITS.md`):

- the overused AI vocabulary: *delve*, *pivotal*, *impactful*, *learnings*, *in order to*
- *"it's not X, it's Y"* contrast framing
- hedging, and chatbot tics such as *"I hope this helps"*
- rhetorical-question openers and *"In conclusion"* endings
- the metronomic sentence rhythm that makes writing read as machine-made

The rules ride in the writing prompt whatever this setting says; the setting decides whether
the worst offenders are also caught afterwards by a deterministic checker that buys exactly
one rewrite. Two structural checks run on every letter regardless: a résumé bullet copied
into the letter word for word (seven or more words in a row), and a letter whose sentences or
paragraphs all run the same length. The grounding gate still runs last either way, so a
restyled sentence that introduces an unsupported fact is still rejected.

#### What "Strip AI writing patterns from the résumé bullets" catches
Settings → Résumé. This is the sweep, the last of the bullet passes: **on by default**, and it
**costs one model call per résumé entry on every tailor run** (a second call for an entry
whose rewrite came back too long). Turn it off to stop paying for it; the free per-bullet
style gate keeps running either way.

The always-on style gate reads one bullet at a time, so the tells it cannot see are the ones
that live across a whole entry:

- the same sentence shape reused down the list
- every bullet the same length
- a three-part series in each line
- one noun, or a ring of near-synonyms, cycling through all of them
- a bullet with no verb in it at all

So each Experience, Projects and Leadership entry goes to the model as a whole, with its
bullets and a list of what a set of deterministic checks measured in them, and the model is
asked to fix those and leave everything else alone.

**Your layout does not move**, which is the reason this is safe to leave on. A rewrite is kept
only if it prints within the same line budget your bullets are already trimmed to, so an
entry's printed height can go down and never up, and your résumé cannot be pushed onto a
second page by it. A rewrite that comes back too long is asked once more for a shorter version
and then dropped in favour of your original text; the same happens to one that loses a number
or a name, changes the opening verb, or trips the ordinary style gate. Bullets you marked
verbatim are never sent. `tailor_report.txt` in the output folder lists what was rewritten,
what was refused and why, and the lower-priority polish the sweep reported and left alone.

One more call can follow a run that lost a bullet. When the grounding gate drops a bullet
from the first draft, the tailor re-asks once from the same atoms with the unsupported term
banned, then gates the answer again: the re-ask. It has no Settings row;
`RESUME_TAILOR_REGROUND=0` in `.env` or `"reground": false` in `local/config.json` turns it
off, and `tailor_report.txt` names each bullet it recovered.

### What leaves your machine
There is no analytics, no crash reporting, and no phone-home. The only outbound
traffic is the work you asked for, and each destination gets only what it needs:

| Destination | When | What it receives |
| --- | --- | --- |
| Bright Data | you run job discovery | your search keywords and the dataset ID |
| Google Gemini (Vertex or API key) | scoring and résumé tailoring | the job description, your `resume.md` / `master_experience.yaml` content |
| Anthropic (`claude` CLI) | only if you set a provider to `claude` | the same prompts, through your own CLI login |
| the job posting's own site | only when you paste a URL into *Add job by hand* | a plain GET for the page text |
| the employer's application site | only when you run auto-apply on a queued job | the answers from your apply sheet and answer bank, typed into the form by the auto-apply browser profile; it submits only when the gate in *Auto-apply (batch, Jev-judged)* passes |
| TypeSafe (the Jev judge) | only during an auto-apply run, in `typesafe` mode | each form page's field labels, options and visible text, the names of your facts for the field mapping; the values stay on your machine until the verification and grounding checks, which receive the values it typed and an excerpt of your apply sheet |
| your own GCP VM (`gcloud compute ssh/scp`) | only when you click a VM control in *Settings* | your search and scoring config, the ids already collected, and rows to merge; plus, only when you click **Set on VM**, the one API key you typed into that box. It runs under your own `gcloud` login |
| healthchecks.io | **opt-in, VM cron only** | a start ping and the run's exit code; no job data, no identifiers |

The healthchecks ping is a dead-man's switch: a silently failing cron run emails
you. It is off unless you set `HEALTHCHECKS_URL`
yourself (see `scripts/run_scraper.sh`); unset, `ping_hc` is a no-op.

Your credentials never cross providers: the Gemini and Bright Data secrets are
stripped from the environment before the `claude` CLI is launched, the ATS master
password lives in the Windows Credential Manager and leaves it only to be typed into
a password field on an application's site or copied to the clipboard when you ask,
and nothing is written to the repo. Secrets stay in your git-ignored
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

1. Tailor the résumé for the job (the **Tailor résumé** button on the detail card).
   Tailoring does not open File Explorer by default; flip **Settings → Open output
   folder after tailoring** on if you want that.
2. Click **Apply** on the detail card. The Apply button is **green only once the job has
   both its résumé PDF and `apply.md`**. Clicking it opens the posting in Chrome and
   swaps the bottom detail card for a right-side **Apply panel**: copyable **LinkedIn** and
   **GitHub** links from your master file's basics (each row shown only when you filled
   that field in) above the résumé / cover-letter paths, and the apply sheet **rendered as
   formatted markdown** (the **Copy apply sheet** button still copies the raw markdown
   source). An **Expand** button opens the sheet in a large, resizable window for easier
   reading. Closing the panel brings the detail card back; **"I applied to this job"**
   confirms, adds the job to your Tracker as *applied*, and closes the panel (the
   right-click → *Set status → applied* still works too).
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
**Apply Answers** tab (see *Your saved answers* below). Before a manual apply,
`python local/apply_queue.py refresh-answers <id>` rewrites that job's apply sheet from your
saved answers.

CLI equivalent (from `local/`): `python -m resume_tailor.apply --job-id <id> --open`.

### Ask AI about a job
Right-click any job (or click **Ask AI** on the Apply panel, next to **Open folder**) for
a per-job chat window. It answers only from what it can see for that job: the posting, the
apply sheet when one exists, and now the full master experience file on every turn, so a
follow-up question can draw on work the tailor left out of that one résumé. An untailored
job still gets a conversation scoped to its description alone. The same 13 AI-writing rules
that guide the cover letter ride in the chat's own system prompt, and an answer of 60 words
or longer runs through the same deterministic checks: a flash-tier repair call fires once
when one trips, and em dashes are stripped from every answer regardless.

### Your saved answers (Apply Answers tab)
The **Apply Answers** tab holds every reusable answer the apply helper can put on a form: the
built-in screening questions (work authorization, sponsorship, relocation, on-site work, years
of experience, a written work-authorization statement, the EEO questions, how you heard about
the role, and your mailing address) plus any custom question you add. Each row is typed to
match its question: a Yes/No picker, a number box, a dropdown of options, or a multi-line text
box, so a saved answer can only be read one way. A yes/no or number row also takes a short,
optional note, up to 300 characters; a longer one blocks Save until you shorten it. The line
under each row reads "Forms will get: ..." (or says why it will not) so you always see what a
run would actually type.

A built-in question cannot be deleted, and **Add answer** refuses a new question only when the
run already fills it from a built-in answer, and names that answer; a question about another
country, a city, a visa type or years of one skill is yours to add. The top line counts how many answers are not set and how many are not
confirmed, and those rows carry the warning highlight; **Confirm all** confirms every row that
already holds an answer, except work authorization, sponsorship, years of experience and the
work-authorization statement: those four keep their own tick for as long as they still hold the value they started with. A damaged
answers file shows the tab as damaged and offers **Restore backup** when a good `.bak` copy sits
next to it; restoring first saves the damaged file beside it as `apply_answers.json.damaged`, so
nothing is lost. If you fix the file by hand outside the dashboard, reopen the dashboard
afterward so this tab reads it again.

**Confirmed marks an answer ready.** The auto-apply run and Test my answers only ever fill a
field from an answer that is both set and confirmed; changing an answer's value ticks its
Confirmed box for you. An answer you typed but left unconfirmed, or left blank, reads as not
set: an optional field it would have filled stays blank, and a required one stops the job for
you (see *Where a drain stops* below).

**A field gets an answer only for its own question.** A saved yes or no matches only its own
option: on a dropdown offering "Yes" and "Yes, on a work visa", a saved Yes never becomes the
qualified one. A field that asks something narrower or the reverse of a saved answer is
answered only when that answer covers it: "authorized to work without sponsorship" is answered
from your authorization and sponsorship answers together, while your total years of experience
does not answer "years of Python experience", so a required field like that stops the job for
you. The same goes for a question naming another country, a city, a visa type, a number of
years or your current job, and for a status list under a "Work authorization" heading. A custom
yes, no or number answer you add fills only the question it was saved for, word for word. When
a job stops on a question like that, add a custom answer with the question exactly as the form
words it, then run the job again.

**Relocation and on-site questions in other words.** When a form asks about relocating or
working on-site in words the check above does not match, such as "Are you willing to work in the
office in New York?", Jev reads your relocation and on-site answers and fills the field only when
it is sure (0.85 or more, far ahead of every other choice) that anyone with those answers would
pick that option. Otherwise the field stops the job as before. No other answer is read this way:
work authorization, sponsorship and years of experience always need their own question. The note
on an answer goes to Jev with it, so "only in NYC or Austin" or "at my own expense is fine" tells
it what your Yes covers.

**Test my answers.** Click **Test my answers** to run the shipped screening questions against
your saved, confirmed answers, off the UI thread, with the judge your Auto-apply judge setting
names. The dialog lists what each question would get filled with, or "stops here" when nothing
in your answers covers it. One click with the live judge costs under a cent.

**After an upgrade.** An older answers file converts to the typed store the first time you open
this tab. Check the review banner it shows: it lists each answer the conversion touched and
what it read the old text as. An answer that read word for word (a plain "Yes", a number, "TX"
for Texas) is confirmed. Every other listed answer stays unconfirmed, including one whose old
sentence says more than Yes, No or a number (for example, "No, but I will need sponsorship after
my OPT ends"): the rest of that sentence is kept in its note. Tick Confirmed on each once you
have checked it; until then, a required question it would answer stops the job. **I've checked
these** hides the banner and saves; it confirms nothing.

### Auto-apply (batch, Jev-judged)
The **Auto-apply** tab is a live view of a batch apply queue: right-click jobs in any
table and pick **Queue for auto-apply** to add the selected tailored jobs (a job with no
résumé yet is tailored first), and the tab tracks each one through queued, in progress,
ready to submit, submitted, needs human and failed. **Start auto-apply run** works
through the queue in a new terminal window, one job at a time, in a browser profile of
its own. This is the power-user path; for one job at a time, the **Apply** flow above is
the recommended way in.

**What Jev is and what it decides.** The run is ordinary code driving a Google
Chrome window (Playwright; the bundled Chromium stands in when Chrome is not installed). Every judgment call inside it goes to Jev, TypeSafe's
"System One" model, which answers structured questions with a probability: what kind
of page this is (the posting, an application form, a login wall, a code gate, a
review page, a confirmation, a CAPTCHA), which of your facts each field asks for
(with "leave blank" and "needs a written answer" always on offer), which option of a
dropdown matches your answer, which button advances and which one submits, whether
each typed value reads back correctly, and whether every sentence of a drafted answer
is supported by your apply sheet. Jev never writes text: your facts come from the
job's `apply.md` and the **Apply Answers** bank, and a free-text answer is drafted by
the résumé engine's flash-lite tier and then checked sentence by sentence by Jev
before it is typed. A box that asks for an SSN, a birthdate, bank or card details or
an ID number is never filled, whatever Jev maps it to. The thresholds behind those
decisions live in `local/apply_judge.py`, with the live answers they were tuned on.

**Setup, once.**

1. Create a key at `console.typesafe.ai/keys` and put it in `.env` as
   `TYPESAFE_API_KEY=...`, or paste it into **Settings → Auto-apply → TypeSafe API
   key** (it is stored write-only; a rotated key needs a dashboard restart). `pip install
   playwright typesafe-sdk` if **Check setup** says they are missing; the run uses your
   installed Google Chrome, and `python -m playwright install chromium` gives it a fallback
   browser when Chrome is absent; `python local/apply_run.py doctor` prints the same
   rows from a terminal.
2. Click **Sign in to sites** (or run `python local/apply_run.py login`). It opens Chrome on the
   run's own profile (a separate directory from your everyday Chrome profile, which Chrome
   keeps closed to automation) at LinkedIn's login and your inbox in two tabs; sign in
   to both and close the window. The run reuses those sessions from then on: LinkedIn
   for the posting's external Apply button, the inbox for the emailed verification
   codes some portals send when they force an account.
3. Set the **master password** (the **Set…** button on the tab). It lives in the
   Windows Credential Manager. When a portal forces an account, the run signs up with
   your email and that password, records only the email and the method in a small
   ledger, and on a later visit to the same portal signs in with it. An application
   form with a password box of its own (an account made inside the form) gets the same
   password, and the job still stops at the submit step when submitting is off. The
   password is typed on the application's own site only, into a real password field,
   it is compared by length only after the fill, and it never reaches a file, a log,
   the record or Jev. Keep it a password you use for job applications only.

**What a drain does.** Before it claims any job, a drain reads the **Apply Answers** file once:
a damaged file stops it there, before the first job runs, and it lists which built-in answers
are not set or not confirmed, since a job asking one of those questions will stop for you. The
run fills a field only from an answer that is both set and confirmed; anything else leaves an
optional field blank and stops a required one. `python local/apply_run.py drain` (the
**Start auto-apply run** button) takes the queued jobs in queue order, one at a time, up to
the batch cap (`--cap N`,
else Settings > **Max jobs queued per batch**, 10 by default). For each job it opens the
job's apply address in the run's own Chrome profile; on LinkedIn it follows the posting's
external Apply. Then it works page by page:
it reads the page (Jev's read, weighed against what the page's own structure shows), fills
the fields from your apply sheet and answer bank, reads every value back, drafts and checks
the free-text answers, signs in or makes an account with the master password where a
portal forces one, fetches an emailed code or link from your inbox, and clicks the button
Jev picks as the way on. At the submit step it sends only when submitting is on and every
required field was filled and read back correctly. Each job ends as **submitted**, **ready
to submit**, **needs human** or **failed**, and the queue row carries the reason. The tab a
job ended on stays open, except after a confirmation page. A submitted row whose reason does
not start with "confirmation page" was sent with no confirmation seen: check that one.

A drain ends when the queue is empty or the cap is reached, and it ends early in two
cases: you close the browser window (the jobs not yet started stay queued, their attempt
counts untouched), or Jev stays down through its retries (the job it was on goes back to
the queue with the attempt not counted, unless something may already have been sent).

**Where a drain stops: the park policy.** Your rule for the run: it stops a job only at the
submit step when submitting is off, at a required question your data cannot answer, or at
a dead end it cannot pass. Every such stop leaves the job's tab open and the queue row
saying why (the reason starts with the words in brackets):

- Submitting is off (**Submit when verified** off, or `--no-submit`): every job stops at
  its submit step as **Ready to submit** ("auto_apply_submit is off").
- A required question has no answer in your data ("required field without an answer").
  Click **Answer now** to open the **Apply Answers** tab (the panel itself carries no answer
  box), save the answer there, then **Re-queue**; a saved yes/no or number answer fills a
  later application only where that job words the question the same way. A way on that stays
  disabled until a field the run left blank is answered stops the same way. An optional
  question with no answer is left blank.
- A question the run never answers: an SSN, a birthdate, bank or card details, an ID
  number ("asks for ..., which auto-apply never fills"). You finish that application by
  hand.
- A payment request ("payment requested").
- A CAPTCHA or bot check nobody solved ("captcha or bot check on the page", "a CAPTCHA
  challenge ..."). The run never solves one. With the window visible it waits up to five
  minutes for you to solve it, then carries on; hidden, or unsolved, the job stops. An
  unticked CAPTCHA checkbox on a form stops the job at the submit step for you to tick.
- A dead page, a closed posting, or a job the site or LinkedIn says you applied to already
  ("error or dead page", "closed: ...", "already applied: ...").
- A posting the run cannot apply to itself: a job board's posting with no link to the
  company's site ("aggregator posting"), an Apply that is an email address ("apply by
  email"), and an Easy Apply job on LinkedIn, which you apply to there ("Easy Apply: apply
  on LinkedIn").
- LinkedIn signed out ("LinkedIn is signed out"): run `login` again.
- An account the run cannot make: a portal whose only sign-in is another site's account
  ("sign-in only through another site"), or password rules the master password cannot meet
  ("the master password does not meet the password rules").
- A way on that stays disabled after every field was answered ("the ... button stays
  disabled after the fill").
- You closed the window or the job's tab ("the browser window was closed", "the job's tab
  was closed").
- A queue entry the run cannot work, such as a hand-edited row whose paths are not text
  ("malformed queue entry"): it ends as **failed**; fix or remove the row.
- Jev down after the submit click or the code step ("check whether the application went
  through: the run stopped after the submit click (judge unavailable: ..."), or down
  under the same job twice in one drain for an error its own request can cause ("judge
  unavailable: ... parked so the queue moves on"). A busy service or a dropped connection
  never parks a job: it goes back to the queue.

Any other stop is one the run should not have made, and it is what the shakedown below
asks you to send back. The ones you may meet: Jev unsure of a page it would act on only
when sure (a confirmation, a bot check, a payment, a dead page or an unrecognised page),
a sign-in or sign-up it could not complete ("login wall", "account signup needed", "an
account exists", "emailed verification link needed"), a submit click followed by a form
or a sign-in in place of a confirmation ("check whether the application went through"),
a send that never reached the site ("the submit did not go through": nothing was sent),
a form that needs a password while no master password is set (set one and **Re-queue**;
an optional password box is left blank), and an application that went back to LinkedIn
after the company's form. A doubtful read
of a posting, a form, a review page, a sign-in, a sign-up or a code screen is acted on;
that step's own checks still apply.

A page of form boxes read as a sign-in or sign-up is treated as the form. A LinkedIn job
page with an Apply button, no form and no submit button counts as the posting until the
application's answers have gone on a page, unless Jev is sure it shows LinkedIn signed
out, a sign-up, a closed posting, a check, a payment or a confirmation. A page of a
sign-up's own boxes and a password makes an account; filling it does not count as filling
the application, though with submitting off an Apply button after it still goes to the
submit step, in case it is the application's last page.

Sign-in screens are handled on the application's own site only: the address and the
password on one screen, or the address first and the password or the create-account form
on the next (iCIMS, Workday). Other boxes on a sign-up form are filled from your data.
Only the submit step sends an application. The sign-in and code steps act only on a screen
of account boxes (a password box or a lone email box, and no resume box) or a code box, and
they never click a button that reads as sending one ("Submit application", "Apply",
"Complete application", "Create account and apply"). A code step's button that the model
reads as the submit never counts as the send: the page after it is a confirmation only
when it says the application was received, and in park mode, once your answers are on the
site, that button stops the job for you. A sign-up screen that also asks what
only an application asks (a profile link, work authorization, a written answer) is the
form: it is filled and checked, the password included, and its send-shaped button goes to
the submit step. When submitting is on and the page after that click is a form or a
sign-in instead of a confirmation, the job stops for you to check: the click may have
sent the application or only made the account, and the run cannot tell which. With a
sign-up's own boxes alone (the
address, the password, a name, a phone, the terms), such a button stops the job for you to
sign in, since the run cannot tell there whether it starts the application or sends it. On
a screen of the address and the password alone, "Sign in to apply" and "Send code" are
sign-in buttons. The master password goes only into a box named a password (never a
passcode, a one-time code, an ID number or a security answer) whose form stays on the
application's sites; if the page tries to post it anywhere else, the run blocks the post
and the job stops with nothing sent.
Controls inside another site's frame (a CAPTCHA widget, a chat or cookie widget) are
never clicked or filled, and the run goes on without them. You finish a stopped job by
hand and **Mark applied** or **Re-queue** it. The four settings under **Settings →
Auto-apply**:

- **Submit when verified** (`auto_apply_submit`, on): off parks every job at its review
  page instead. `--no-submit` on the command line does the same for one run.
- **Draft free-text answers** (`auto_apply_generate`, on): an open-ended question
  ("why this role", "a project you are proud of") gets one flash-lite draft from your
  apply sheet, and Jev checks each sentence against the sheet; a draft with an
  unsupported sentence is dropped and the field is left for you. At most three drafts
  per job. Off leaves every such field for you.
- **Hide the browser window** (`auto_apply_headless`, off): on runs Chrome with no
  window. Leave it off to watch the run and step in when it parks.
- **Auto-apply judge** (`auto_apply_jev_mode`, `typesafe`): `fake` is a test-only judge
  that answers from word overlap; the drain refuses it (so does `replay`, the test
  harness's cached judge). A dry run is `drain --no-submit` with the real judge: it
  fills every queued job and parks it at its review page.

**The record.** Every job the run touches gets an `apply_record.md` beside its
`apply.md` (the **Open application record** button): the pages it saw with the judged page kind
and its confidence, every field it filled with the value, the uploads, each
verification result, the buttons it clicked, the answers it drafted and whether they
passed, the reason it parked, the Jev requests and cost, and the final page's text.
Read it before you trust a submitted row.

**Costs.** Jev bills $0.042 per million input tokens, about $0.005 to $0.015 per
application at the page sizes seen so far (a long multi-page form sits at the top of
that range). Each drafted free-text answer adds one flash-lite call on your Gemini
lane. There is no per-request minimum and nothing is billed while the queue is empty.

CLI equivalents (from the repo root): `python local/apply_run.py drain` (the Start
button; `--cap N`, `--no-submit`, `--headless`), `one <job_id>` for one
queued job, `login` and `doctor` as above. Exit code 0 means drained or nothing queued,
2 means not configured (no key, or the job id is not queued), 1 an unexpected error.

**The drain's summary.** When a drain ends it prints one line and one table, and writes
both to `apply_drain-<YYYYMMDD-HHMMSS>.md` beside the job folders (the queue file's folder
when the jobs' folders do not share one parent):

    drained 2: submitted 0, ready_to_submit 1, needs_human 1, failed 0; Jev 23 requests, 214806 tokens, $0.0090

| # | job | end | pages | reason | trace |
|--:|-----|-----|------:|--------|-------|
| 1 | 4012345678 | ready_to_submit | 3 | auto_apply_submit is off; ... | [job folder/attempt-1](...) |
| 2 | 4012345679 | needs_human | 2 | required field without an answer: Years of SQL | [job folder/attempt-1](...) |

`end` is the job's status, `pages` the pages it read, `reason` the queue row's reason
(cut to 200 characters) and `trace` a link to that attempt's trace folder. A "re-queued"
count in the line means Jev went down and the job went back to the queue. Read each row's
reason against the park policy above: a reason on the list is a designed stop, and any
other reason is one to look at.

**Reading a trace.** Every attempt at a job leaves a folder,
`<job folder>/apply_trace/attempt-<n>/`, and the table links it. Read it in this order:

1. `run.json`: the job's status and reason, `url_chain` (every address the job's tabs
   loaded, redirects included, in order) and `setup_events` (what happened before the first
   page was read: the start, a LinkedIn hop, an Apply click).
2. `page-<k>.jpg` beside `page-<k>.json`, one pair per page read, in order. The picture is
   the page as it was judged. In the JSON, `state` and `confidence` are the read the run
   acted on; `answers.page_state` is that read with its evidence, and
   `answers.page_state_judged` is Jev's own pick with its probabilities, so a page where the
   two differ shows the page's structure outweighing Jev. `digest` lists the fields and
   buttons the run saw; the other `answers` are Jev's per question (`field_<n>_source` for
   the fact a field asks for, `field_<n>_pick` for a dropdown's option, `button_<n>_role`
   for a button), each with its confidence.
3. The page's `events`, in order: `plan` (what each field would get), `fill` and `verify`
   (which boxes were filled and whether each read back), `click` and its result,
   `decision` (a choice the run made, with `what` and `why`), and `park` (the stop and
   its evidence).
4. `end.jpg`: the page the job ended on. `job.log`: the job's log lines.

The trace never holds a field's value, the master password or an emailed code, and every
password and code box is masked in the pictures. The pictures do show the other answers
the page showed (your name, email, phone), and `apply_record.md` lists every value
filled: look through both before you pass them on.

**The probe.** `python local/apply_run.py probe <url>` reads one page the way the run
would and prints it: the fields and buttons, the page text's head, what the LinkedIn
handler and the fieldless-posting fallback would click, and whether it reads as an
account screen. It types, uploads, ticks and submits nothing, in a fresh temporary
browser profile unless you pass `--profile`. Add `--judge` to ask Jev about the page
(a few requests, a fraction of a cent): the page read with Jev's own pick beside it, each
button's role, and the step the loop would take (`--no-submit` describes that step in park
mode). `--follow-apply` clicks the page's plain Apply entry only (never on a page with
form fields) and prints the page it opens. `--headed` shows the window. Use it on a
portal before you queue its jobs, or on a job that stopped for a reason you doubt, and
send the output back with the trace.

**The shakedown: `drain --cap 2 --no-submit`.** Before a first real drain, and after an
update, run two queued jobs in park mode with the window visible:

    python local/apply_run.py drain --cap 2 --no-submit

It fills both applications and stops each at its submit step, sending nothing. It costs
a few cents of Jev at most. Queue two jobs on the portals you apply through most (a
Workday job is the most useful second one: its wizard shows every step at one address).
Leave the window alone while it runs, unless a CAPTCHA wait asks for you. Then check:

- Each row of the table ends **ready_to_submit** with "auto_apply_submit is off". Any other
  end: note its reason and whether it is on the park policy's list.
- The stop is the application's last step. For each ready_to_submit row, open its
  `end.jpg`: the page must be the one whose button sends the application (a single-page
  form, or the wizard's review or last step). A stop on a middle page means the run took
  a step button ("Save and continue", "Next") for the send, and with submitting on it
  would have clicked it as the send: send that trace.
- The email-first case. Some portals open on a page that asks only for your email, with
  an **Apply** or **Continue** beside it, and show the rest of the form after it. The run
  must go past that page. A ready_to_submit row whose `end.jpg` shows only the email box
  means the run took that first button for the send: send that trace.
- On each open tab, every field holds the right answer and no required field is empty.
- The page reads: in each `page-<k>.json`, `state` names the page you see in
  `page-<k>.jpg` (the posting, the form, a sign-in, the review page).
- The loading waits. The wait for a loading placeholder (a spinner, grey skeleton bars) is
  kept per step: a wizard that shows each step at the same address gets its own wait on
  each step. Look in each page's `events` for a `decision` whose `what` is
  `reread_after_settle` and whose `why` starts "a loading placeholder". One that ends
  "read again" is the wait working. One that says "a loading placeholder stayed up past
  ... ms", or a page whose `digest` has no fields while its picture shows a skeleton, means
  a step was read before it loaded: send that trace.

What to send back: the drain report (`apply_drain-<stamp>.md`); for each job that did not
end ready to submit, or that filled a field wrong, its `apply_trace/attempt-<n>` folder
(zipped) and a line on what the page should have got; and the probe output for any page
you doubt.

**Known limits.** These cases are rare, and each one ends in a stop you can read in the
drain report; none of them sends an application twice:

- The words that mark a button as sending, a step or a required note are English. On a
  page in another language the run leans on Jev's read alone and parks more often.
- In a lookup box (a list that appears as you type), a lone option that holds your answer
  is picked even when it reads the other way, such as "Not Hispanic or Latino" for
  "Latino". Jev's check of the filled page is what catches it. Two options that both hold
  your answer with different conditions ("Yes, I am authorized" beside "Yes, but I will
  require sponsorship") tie, and a required one parks.
- The link page from a verification email is opened outside the browser. A link page
  behind a bot check parks, and so does a button the run does not know beside the "Sign
  in with Google" style buttons on a sign-in wall.
- A form that fades in on a script timer can have a single field read as hidden: a
  required one parks, an optional one stays blank. An application's own fixed footer that
  sits outside the form can be closed as if it were an overlay.
- A thank-you page that a script loads, with a query in its address, ends
  **submitted (unconfirmed)**: check that job's email.
- A required question naming another country, a city, a visa type, a number of years of a
  particular skill (including an "at least N years" form of it), or your current job, and a
  status list under a "Work authorization" heading, stops the run: none of it is covered by a
  built-in answer. Add a custom answer with the question exactly as the form words it; it fills
  only that exact wording elsewhere.
