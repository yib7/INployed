# INployed user guide

Everything the dashboard and the CLIs can do, once the [README's Quick start](../README.md#quick-start)
has you running. Skim the headings; nothing here is required reading.

Every `python` in the commands below is the project venv's interpreter from Quick start
Step 2, which is never activated: from the repo root that is `venv\Scripts\python.exe`
(`venv/bin/python` on macOS or Linux), and from `local/` it is `..env\Scripts\python.exe`.

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
pointing at none of them. Fields are re-checked when you tab out of them, not only at Save. If a
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
**Apply Answers** tab: add your own, and mark each *fixed* (never changed) or *open-ended*
(adaptable per job).

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
   ledger, and on a later visit to the same portal signs in with it. The password is
   typed into a real password field only, it is compared by length only after the
   fill, and it never reaches a file, a log, the record or Jev.

**When the run stops.** The run submits an application when every required field was
filled from your answers and read back correctly and Jev picks out the submit button.
It stops only in these cases, with the window left open and the queue row saying why:

- **Submit when verified** is off (or the run has `--no-submit`): every job stops at its
  submit step as **Ready to submit**.
- A required question has no answer in your data. Answer it in the job's panel and
  **Re-queue**; the answer is kept for later applications.
- A required question the run never answers: an SSN, a birthdate, bank or card details,
  an ID number. You finish that application by hand; an optional one is left blank.
- The page cannot be passed: a payment request, a closed or dead posting, a page that
  does not move after its button, a sign-in it cannot complete, or LinkedIn signed out.
- A CAPTCHA challenge opens. The run never solves one. With the window visible it waits
  up to five minutes for you to solve it, then carries on; hidden, or unsolved, the job
  stops.

Sign-in screens are handled on the application's own site only: the address and the
password on one screen, or the address first and the password or the create-account form
on the next (iCIMS, Workday). Other boxes on a sign-up form are filled from your data.
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
